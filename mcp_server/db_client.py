"""
資料庫客戶端抽象層 — 支援 PostgreSQL（psycopg2）與 Vertica（vertica-python）
透過環境變數 DB_TYPE 選擇實作，呼叫端只需使用 create_db_client()。
"""
import logging
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ── 資料結構 ─────────────────────────────────────────────────────────────────

@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    execution_ms: int


@dataclass
class ColumnInfo:
    name: str
    data_type: str
    nullable: bool
    default: str | None = None
    comment: str | None = None


@dataclass
class TableSchema:
    table_name: str
    schema_name: str
    columns: list[ColumnInfo] = field(default_factory=list)
    foreign_keys: list[dict] = field(default_factory=list)


# ── 抽象介面 ─────────────────────────────────────────────────────────────────

class DBClient(ABC):
    """資料庫客戶端抽象介面，PostgresClient / VerticaClient 均須實作。"""

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def execute_query(self, sql: str, params: tuple = ()) -> QueryResult: ...

    @abstractmethod
    def list_tables(self) -> list[str]: ...

    @abstractmethod
    def describe_table(self, table_name: str) -> TableSchema: ...

    @property
    @abstractmethod
    def db_type(self) -> str: ...


# ── PostgreSQL 實作 ───────────────────────────────────────────────────────────

class PostgresClient(DBClient):
    """封裝 psycopg2，對應 DB_TYPE=postgres。"""

    def __init__(self) -> None:
        self._host     = os.environ.get("DB_HOST", "localhost")
        self._port     = int(os.environ.get("DB_PORT", 5432))
        self._dbname   = os.environ.get("DB_NAME", "traffic_db")
        self._user     = os.environ.get("DB_USER", "traffic_user")
        self._password = os.environ["DB_PASSWORD"]
        self._schema   = os.environ.get("DB_SCHEMA", "public")
        self._conn     = None

    @property
    def db_type(self) -> str:
        return "postgres"

    def connect(self) -> None:
        import psycopg2
        self._conn = psycopg2.connect(
            host=self._host, port=self._port,
            dbname=self._dbname, user=self._user, password=self._password,
            options=f"-c search_path={self._schema}",
        )
        self._conn.autocommit = True
        logger.info(
            "[PostgresClient] 已連線至 %s:%s/%s schema=%s",
            self._host, self._port, self._dbname, self._schema,
        )

    def close(self) -> None:
        if self._conn and not self._conn.closed:
            self._conn.close()

    def execute_query(self, sql: str, params: tuple = ()) -> QueryResult:
        import psycopg2.extras
        t0 = time.monotonic()
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SET LOCAL statement_timeout = '30000'")
            cur.execute(sql, params)
            rows_raw = cur.fetchall()
            columns = [desc[0] for desc in cur.description] if cur.description else []
            rows = [list(r.values()) for r in rows_raw]
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        return QueryResult(columns=columns, rows=rows, row_count=len(rows), execution_ms=elapsed_ms)

    def list_tables(self) -> list[str]:
        sql = (
            "SELECT table_name "
            "FROM information_schema.tables "
            "WHERE table_schema = %s AND table_type = 'BASE TABLE' "
            "ORDER BY table_name"
        )
        result = self.execute_query(sql, (self._schema,))
        return [row[0] for row in result.rows]

    def describe_table(self, table_name: str) -> TableSchema:
        import psycopg2.extras
        col_sql = """
            SELECT
                c.column_name,
                c.data_type,
                c.is_nullable,
                c.column_default,
                pgd.description AS comment
            FROM information_schema.columns c
            JOIN pg_class pc ON pc.relname = c.table_name
                AND pc.relnamespace = (
                    SELECT oid FROM pg_namespace WHERE nspname = %s
                )
            LEFT JOIN pg_attribute pa
                ON pa.attrelid = pc.oid AND pa.attname = c.column_name
            LEFT JOIN pg_description pgd
                ON pgd.objoid = pc.oid AND pgd.objsubid = pa.attnum
            WHERE c.table_schema = %s AND c.table_name = %s
            ORDER BY c.ordinal_position
        """
        col_result = self.execute_query(col_sql, (self._schema, self._schema, table_name))
        columns = [
            ColumnInfo(
                name=r[0], data_type=r[1],
                nullable=(r[2] == "YES"), default=r[3], comment=r[4],
            )
            for r in col_result.rows
        ]

        fk_sql = """
            SELECT
                kcu.column_name,
                ccu.table_name  AS foreign_table,
                ccu.column_name AS foreign_column
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
                ON tc.constraint_name = kcu.constraint_name
               AND tc.table_schema    = kcu.table_schema
            JOIN information_schema.constraint_column_usage ccu
                ON ccu.constraint_name = tc.constraint_name
               AND ccu.table_schema    = tc.table_schema
            WHERE tc.constraint_type = 'FOREIGN KEY'
              AND tc.table_schema = %s AND tc.table_name = %s
            ORDER BY kcu.column_name
        """
        fk_result = self.execute_query(fk_sql, (self._schema, table_name))
        foreign_keys = [
            {"column": r[0], "foreign_table": r[1], "foreign_column": r[2]}
            for r in fk_result.rows
        ]
        return TableSchema(
            table_name=table_name, schema_name=self._schema,
            columns=columns, foreign_keys=foreign_keys,
        )


# ── Vertica 實作 ──────────────────────────────────────────────────────────────

class VerticaClient(DBClient):
    """封裝 vertica-python，對應 DB_TYPE=vertica。含連線重試（最多 3 次）。"""

    _MAX_RETRIES = 3

    def __init__(self) -> None:
        self._host     = os.environ["DB_HOST"]
        self._port     = int(os.environ.get("DB_PORT", 5433))
        self._dbname   = os.environ["DB_NAME"]
        self._user     = os.environ["DB_USER"]
        self._password = os.environ["DB_PASSWORD"]
        self._schema   = os.environ.get("DB_SCHEMA", "public")
        self._timeout  = int(os.environ.get("DB_CONNECTION_TIMEOUT", 15))
        self._ssl_mode = os.environ.get("DB_SSL_MODE", "require")
        self._conn     = None

    @property
    def db_type(self) -> str:
        return "vertica"

    def connect(self) -> None:
        import vertica_python
        conn_info = {
            "host":               self._host,
            "port":               self._port,
            "database":           self._dbname,
            "user":               self._user,
            "password":           self._password,
            "connection_timeout": self._timeout,
            "ssl":                self._ssl_mode not in ("disable", ""),
            "search_path":        self._schema,
        }
        for attempt in range(1, self._MAX_RETRIES + 1):
            try:
                self._conn = vertica_python.connect(**conn_info)
                self._conn.autocommit = True
                # vertica-python 的 search_path 連線參數不穩定，明確設定確保生效
                with self._conn.cursor() as cur:
                    cur.execute(f"SET search_path = {self._schema}")
                logger.info(
                    "[VerticaClient] 已連線至 %s:%s/%s schema=%s",
                    self._host, self._port, self._dbname, self._schema,
                )
                return
            except Exception as exc:
                if attempt == self._MAX_RETRIES:
                    raise
                wait = 2 ** attempt
                logger.warning(
                    "[VerticaClient] 連線失敗（第 %d/%d 次），%ds 後重試：%s",
                    attempt, self._MAX_RETRIES, wait, exc,
                )
                time.sleep(wait)

    def close(self) -> None:
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass

    def execute_query(self, sql: str, params: tuple = ()) -> QueryResult:
        t0 = time.monotonic()
        cur = self._conn.cursor("dict")
        cur.execute(sql, params)
        rows_raw = cur.fetchall()
        columns = [desc[0] for desc in cur.description] if cur.description else []
        rows = [list(r.values()) for r in rows_raw]
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        return QueryResult(columns=columns, rows=rows, row_count=len(rows), execution_ms=elapsed_ms)

    def list_tables(self) -> list[str]:
        # Vertica 使用 v_catalog.tables（非 information_schema.tables）
        sql = """
            SELECT table_name
            FROM v_catalog.tables
            WHERE table_schema = :schema
              AND is_system_table = false
            ORDER BY table_name
        """
        result = self.execute_query(sql, {"schema": self._schema})
        return [row[0] for row in result.rows]

    def describe_table(self, table_name: str) -> TableSchema:
        col_sql = """
            SELECT column_name, data_type, is_nullable, column_default
            FROM v_catalog.columns
            WHERE table_schema = :schema
              AND table_name   = :table
            ORDER BY ordinal_position
        """
        col_result = self.execute_query(col_sql, {"schema": self._schema, "table": table_name})
        columns = [
            ColumnInfo(
                name=r[0], data_type=r[1],
                nullable=(r[2] in ("t", True)), default=r[3],
            )
            for r in col_result.rows
        ]

        fk_sql = """
            SELECT
                column_name,
                reference_table_name  AS foreign_table,
                reference_column_name AS foreign_column
            FROM v_catalog.foreign_keys
            WHERE table_schema = :schema
              AND table_name   = :table
        """
        try:
            fk_result = self.execute_query(fk_sql, {"schema": self._schema, "table": table_name})
            foreign_keys = [
                {"column": r[0], "foreign_table": r[1], "foreign_column": r[2]}
                for r in fk_result.rows
            ]
        except Exception as exc:
            # v_catalog.foreign_keys 在部分 Vertica 雲端版本不存在
            foreign_keys = []
            logger.warning("[VerticaClient] 無法查詢 %s 的外鍵：%s，以空陣列替代", table_name, exc)

        return TableSchema(
            table_name=table_name, schema_name=self._schema,
            columns=columns, foreign_keys=foreign_keys,
        )


# ── Factory ───────────────────────────────────────────────────────────────────

def create_db_client() -> DBClient:
    """
    從 DB_TYPE 環境變數建立對應的資料庫客戶端。
    呼叫端不感知底層驅動差異。

    用法：
        client = create_db_client()
        client.connect()
        tables = client.list_tables()
    """
    db_type = os.environ.get("DB_TYPE", "postgres").lower().strip()
    if db_type == "postgres":
        return PostgresClient()
    if db_type == "vertica":
        return VerticaClient()
    raise ValueError(
        f"不支援的 DB_TYPE='{db_type}'，請設定為 'postgres' 或 'vertica'"
    )
