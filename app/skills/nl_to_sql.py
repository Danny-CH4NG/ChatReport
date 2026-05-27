"""
Dialect-aware system prompt helpers.

Detects the active database type from the schema cache (populated by
schema_discovery.py), and returns a Vertica SQL hint block to be appended
to the agent's system prompt when DB_TYPE=vertica.
"""


_VERTICA_SQL_HINT_TEMPLATE = """\
[DATABASE_DIALECT: Vertica]
請注意以下 Vertica SQL 語法規則（與 PostgreSQL 不同）：
1. 型別轉換：使用 CAST(欄位 AS 型別) 或 欄位::型別（Vertica 支援 :: 語法）
2. 字串函數：SUBSTR 取代 SUBSTRING，TRIM 用法相同
3. 日期函數：DATE_TRUNC('hour', ts) 與 PostgreSQL 相同；NOW() 相同
4. 不支援 ARRAY 型別：affected_directions 等欄位在 Vertica schema 中已改為 VARCHAR（逗號分隔），\
請用 LIKE '%N%' 過濾方向值
5. LIMIT 語法相同：SELECT ... LIMIT 100
6. schema 前綴：查詢時無需手動加 schema 前綴（已透過 search_path 設定），\
但複雜 JOIN 時建議明確標示 {schema_name}.table_name
7. IDENTITY 欄位：等同 PostgreSQL SERIAL，不需在 INSERT 中指定
8. 視窗函數：語法與 PostgreSQL 相同（ROW_NUMBER, RANK, LAG, LEAD 皆支援）"""


def get_db_dialect(schema_cache: dict) -> str:
    """Return 'vertica' or 'postgres' from any table entry in the cache."""
    for info in schema_cache.values():
        db_type = info.get("db_type", "")
        if db_type:
            return db_type
    return "postgres"


def build_dialect_section(schema_cache: dict) -> str:
    """
    Return a system-prompt section describing SQL dialect rules, or '' for Postgres.

    Inserts the actual schema name so the agent can qualify table references.
    """
    if get_db_dialect(schema_cache) != "vertica":
        return ""

    schema_name = next(
        (info.get("schema_name", "public") for info in schema_cache.values()),
        "public",
    )
    return "\n\n## SQL 方言注意事項\n" + _VERTICA_SQL_HINT_TEMPLATE.format(
        schema_name=schema_name
    )
