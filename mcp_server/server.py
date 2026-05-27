"""
MCP Server — SSE mode（支援 PostgreSQL 與 Vertica）
Exposes: list_tables / describe_table / get_sample_rows / execute_query
"""
import logging
import os
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# 本地直接執行時（非 Docker）自動載入根目錄 .env
load_dotenv(Path(__file__).parent.parent / ".env")

from db_client import TableSchema, create_db_client
from hooks.post_query import post_query_check
from hooks.pre_query import validate_sql

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── MCP server instance ───────────────────────────────────────────────────────
mcp = FastMCP(
    "traffic-db-mcp",
    host=os.getenv("MCP_HOST", "0.0.0.0"),
    port=int(os.getenv("MCP_PORT", "8080")),
)

# ── DB client（module-level singleton）────────────────────────────────────────
_db_client = create_db_client()
_db_client.connect()
logger.info("DB client ready: db_type=%s", _db_client.db_type)

# ── Schema context（選用；inject 至 describe_table）──────────────────────────
_schema_ctx: dict = {}
_ctx_path = os.getenv("SCHEMA_CONTEXT_PATH", "/app/schema_context.yaml")
if os.path.exists(_ctx_path):
    with open(_ctx_path, encoding="utf-8") as _f:
        _schema_ctx = yaml.safe_load(_f) or {}
    logger.info("Schema context loaded from %s", _ctx_path)
else:
    logger.info("No schema context found at %s (will return raw schema)", _ctx_path)


# ── Type normalisation (Vertica → portable names) ─────────────────────────────

_VERTICA_TYPE_MAP: dict[str, str] = {
    "int":         "INTEGER",
    "int8":        "BIGINT",
    "float8":      "FLOAT",
    "numeric":     "DECIMAL",
    "varchar":     "VARCHAR",
    "char":        "CHAR",
    "bool":        "BOOLEAN",
    "timestamp":   "TIMESTAMP",
    "date":        "DATE",
    "long varchar": "TEXT",
}


def _normalize_type(raw_type: str, db_type: str) -> str:
    """Map Vertica native type names to portable names understood by the Agent."""
    if db_type == "vertica":
        base = raw_type.lower().split("(")[0].strip()
        return _VERTICA_TYPE_MAP.get(base, raw_type.upper())
    return raw_type


# ── Helpers ───────────────────────────────────────────────────────────────────

def _serialize(value: Any) -> Any:
    """讓查詢結果可 JSON 序列化。"""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (list, tuple)):
        return [_serialize(v) for v in value]
    return value


def _inject_limit(sql: str, max_rows: int) -> str:
    if "limit" not in sql.lower():
        return f"{sql.rstrip().rstrip(';')} LIMIT {max_rows}"
    return sql


def _table_schema_to_dict(schema: TableSchema) -> dict[str, Any]:
    """將 TableSchema dataclass 轉為 JSON-safe dict（給 describe_table 使用）。"""
    return {
        "table_name":  schema.table_name,
        "schema_name": schema.schema_name,
        "db_type":     _db_client.db_type,
        "columns": [
            {
                "name":     col.name,
                "type":     _normalize_type(col.data_type, _db_client.db_type),
                "nullable": col.nullable,
                "default":  col.default,
                "comment":  col.comment,
            }
            for col in schema.columns
        ],
        "foreign_keys": schema.foreign_keys,
    }


# ── MCP Tools ─────────────────────────────────────────────────────────────────

@mcp.tool()
def list_tables() -> list[str]:
    """List all queryable tables in the database."""
    return _db_client.list_tables()


@mcp.tool()
def describe_table(table_name: str) -> dict[str, Any]:
    """
    Return column definitions (name, type, nullable, comment),
    foreign key relationships, and business context from schema_context.yaml
    for the given table.
    """
    allowed = _db_client.list_tables()
    if table_name not in allowed:
        return {"error": f"Table '{table_name}' not found. Available: {allowed}"}

    schema = _db_client.describe_table(table_name)
    result = _table_schema_to_dict(schema)

    # Enrich with business semantics from schema_context.yaml
    ctx = _schema_ctx.get("tables", {}).get(table_name)
    if ctx:
        result.update({
            "alias":           ctx.get("alias"),
            "description":     ctx.get("description"),
            "join_pattern":    ctx.get("join_pattern"),
            "query_tips":      ctx.get("query_tips"),
            "common_patterns": ctx.get("common_patterns"),
        })

    vocab = _schema_ctx.get("database", {}).get("domain_vocabulary")
    if vocab:
        result["domain_vocabulary"] = vocab

    return result


@mcp.tool()
def get_sample_rows(table_name: str, limit: int = 5) -> list[dict] | dict:
    """
    Return up to `limit` sample rows (capped at 10) from the table.
    Useful for understanding data formats before writing queries.
    """
    allowed = _db_client.list_tables()
    if table_name not in allowed:
        return {"error": f"Table '{table_name}' not found."}

    limit = max(1, min(limit, 10))
    result = _db_client.execute_query(
        f"SELECT * FROM {table_name} ORDER BY 1 LIMIT {limit}"  # noqa: S608
    )
    return [
        {col: _serialize(val) for col, val in zip(result.columns, row)}
        for row in result.rows
    ]


@mcp.tool()
def execute_query(sql: str) -> dict[str, Any]:
    """
    Execute a SELECT query against the database.

    Safety:
      - Only SELECT allowed; INSERT/UPDATE/DELETE/DROP etc. are rejected
      - All referenced tables must be in the known table list
      - Dangerous functions (pg_sleep, dblink …) are blocked
      - Auto-appends LIMIT 10000 if absent
      - Hard query timeout: 30 seconds (PostgreSQL only via statement_timeout)

    Returns:
      {columns, rows, row_count, execution_ms, warnings}
      or {error, sql} on failure
    """
    allowed_tables = _db_client.list_tables()

    validation = validate_sql(sql, allowed_tables)
    if not validation["ok"]:
        logger.warning("SQL rejected: %s | SQL: %.200s", validation["reason"], sql)
        return {"error": validation["reason"], "sql": sql}

    sql_exec = _inject_limit(sql, 10_000)
    logger.info("Executing [%s]: %.200s", _db_client.db_type, sql_exec)

    try:
        query_result = _db_client.execute_query(sql_exec)
    except Exception as exc:
        logger.error("Query error: %s", exc)
        return {"error": str(exc), "sql": sql_exec}

    warnings = post_query_check(sql_exec, query_result.row_count, query_result.execution_ms)

    return {
        "columns":      query_result.columns,
        "rows":         [[_serialize(v) for v in row] for row in query_result.rows],
        "row_count":    query_result.row_count,
        "execution_ms": query_result.execution_ms,
        "warnings":     warnings,
    }


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logger.info(
        "Starting MCP server on %s:%s",
        os.getenv("MCP_HOST", "0.0.0.0"),
        os.getenv("MCP_PORT", "8080"),
    )
    mcp.run(transport="sse")
