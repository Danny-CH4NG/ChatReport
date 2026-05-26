"""
PostgreSQL MCP Server — SSE mode
Exposes: list_tables / describe_table / get_sample_rows / execute_query
"""
import logging
import os
import time
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import psycopg2.extras
import yaml
from mcp.server.fastmcp import FastMCP

from db_client import get_db
from hooks.post_query import post_query_check
from hooks.pre_query import validate_sql

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── MCP server instance ──────────────────────────────────────────────────────
mcp = FastMCP(
    "traffic-db-mcp",
    host=os.getenv("MCP_HOST", "0.0.0.0"),
    port=int(os.getenv("MCP_PORT", "8080")),
)

# ── Load schema context (optional; injected into describe_table) ─────────────
_schema_ctx: dict = {}
_ctx_path = os.getenv("SCHEMA_CONTEXT_PATH", "/app/schema_context.yaml")
if os.path.exists(_ctx_path):
    with open(_ctx_path, encoding="utf-8") as _f:
        _schema_ctx = yaml.safe_load(_f) or {}
    logger.info("Schema context loaded from %s", _ctx_path)
else:
    logger.info("No schema context found at %s (will return raw schema)", _ctx_path)


# ── Internal helper ──────────────────────────────────────────────────────────

def _fetch_allowed_tables() -> list[str]:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
                "ORDER BY table_name"
            )
            return [row[0] for row in cur.fetchall()]


def _serialize(value: Any) -> Any:
    """Make psycopg2 return values JSON-safe."""
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (list, tuple)) and not isinstance(value, str):
        return [_serialize(v) for v in value]
    return value


def _serialize_row(row: tuple) -> list:
    return [_serialize(v) for v in row]


def _inject_limit(sql: str, max_rows: int) -> str:
    """Append LIMIT if the query doesn't already have one."""
    if "limit" not in sql.lower():
        return f"{sql.rstrip().rstrip(';')} LIMIT {max_rows}"
    return sql


# ── MCP Tools ────────────────────────────────────────────────────────────────

@mcp.tool()
def list_tables() -> list[str]:
    """List all queryable tables in the traffic_db database."""
    return _fetch_allowed_tables()


@mcp.tool()
def describe_table(table_name: str) -> dict[str, Any]:
    """
    Return column definitions (name, type, nullable, comment),
    foreign key relationships, and business context from schema_context.yaml
    for the given table.
    """
    allowed = _fetch_allowed_tables()
    if table_name not in allowed:
        return {"error": f"Table '{table_name}' not found. Available: {allowed}"}

    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:

            # Column definitions + PostgreSQL COMMENT ON COLUMN
            cur.execute(
                """
                SELECT
                    c.column_name,
                    c.data_type,
                    c.udt_name,
                    c.is_nullable,
                    c.column_default,
                    pgd.description AS comment
                FROM information_schema.columns c
                JOIN pg_class pc ON pc.relname = c.table_name
                    AND pc.relnamespace = (
                        SELECT oid FROM pg_namespace WHERE nspname = 'public'
                    )
                LEFT JOIN pg_attribute pa
                    ON pa.attrelid = pc.oid AND pa.attname = c.column_name
                LEFT JOIN pg_description pgd
                    ON pgd.objoid = pc.oid AND pgd.objsubid = pa.attnum
                WHERE c.table_schema = 'public'
                  AND c.table_name   = %s
                ORDER BY c.ordinal_position
                """,
                (table_name,),
            )
            columns = [dict(row) for row in cur.fetchall()]

            # Foreign key relationships
            cur.execute(
                """
                SELECT
                    kcu.column_name,
                    ccu.table_name  AS foreign_table,
                    ccu.column_name AS foreign_column
                FROM information_schema.table_constraints      tc
                JOIN information_schema.key_column_usage       kcu
                    ON tc.constraint_name = kcu.constraint_name
                   AND tc.table_schema    = kcu.table_schema
                JOIN information_schema.constraint_column_usage ccu
                    ON ccu.constraint_name = tc.constraint_name
                   AND ccu.table_schema    = tc.table_schema
                WHERE tc.constraint_type = 'FOREIGN KEY'
                  AND tc.table_name      = %s
                ORDER BY kcu.column_name
                """,
                (table_name,),
            )
            foreign_keys = [dict(row) for row in cur.fetchall()]

    result: dict[str, Any] = {
        "table_name": table_name,
        "columns": columns,
        "foreign_keys": foreign_keys,
    }

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

    # Also surface domain vocabulary if present
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
    allowed = _fetch_allowed_tables()
    if table_name not in allowed:
        return {"error": f"Table '{table_name}' not found."}

    limit = max(1, min(limit, 10))
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"SELECT * FROM {table_name} ORDER BY 1 LIMIT %s",  # noqa: S608
                (limit,),
            )
            rows = cur.fetchall()

    # Serialize dates/Decimals
    return [
        {k: _serialize(v) for k, v in row.items()}
        for row in rows
    ]


@mcp.tool()
def execute_query(sql: str) -> dict[str, Any]:
    """
    Execute a SELECT query against traffic_db.

    Safety:
      - Only SELECT allowed; INSERT/UPDATE/DELETE/DROP etc. are rejected
      - All referenced tables must be in the known table list
      - Dangerous functions (pg_sleep, dblink …) are blocked
      - Auto-appends LIMIT 10000 if absent
      - Hard query timeout: 30 seconds

    Returns:
      {columns, rows, row_count, execution_ms, warnings}
      or {error, sql} on failure
    """
    allowed_tables = _fetch_allowed_tables()

    validation = validate_sql(sql, allowed_tables)
    if not validation["ok"]:
        logger.warning("SQL rejected: %s | SQL: %.200s", validation["reason"], sql)
        return {"error": validation["reason"], "sql": sql}

    sql_exec = _inject_limit(sql, 10_000)
    logger.info("Executing: %.200s", sql_exec)

    start = time.monotonic()
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '30000'")
                cur.execute(sql_exec)
                columns = [desc[0] for desc in (cur.description or [])]
                rows = cur.fetchall()
    except Exception as exc:
        logger.error("Query error: %s", exc)
        return {"error": str(exc), "sql": sql_exec}

    elapsed_ms = int((time.monotonic() - start) * 1000)
    row_count = len(rows)

    warnings = post_query_check(sql_exec, row_count, elapsed_ms)

    return {
        "columns": columns,
        "rows": [_serialize_row(r) for r in rows],
        "row_count": row_count,
        "execution_ms": elapsed_ms,
        "warnings": warnings,
    }


# ── Entry point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logger.info(
        "Starting MCP server on %s:%s",
        os.getenv("MCP_HOST", "0.0.0.0"),
        os.getenv("MCP_PORT", "8080"),
    )
    mcp.run(transport="sse")
