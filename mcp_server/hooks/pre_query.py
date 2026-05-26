"""
Pre-query security hook.
Rules:
  1. Only SELECT statements allowed (DML/DDL whitelist check)
  2. All referenced tables must be in the allowed list
  3. Dangerous functions (pg_sleep, dblink, etc.) are blocked
"""
import re

import sqlparse
import sqlparse.tokens as T

# Any DML/DDL keyword other than SELECT is a hard block
_BLOCKED_KEYWORDS = frozenset({
    "INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER",
    "TRUNCATE", "EXECUTE", "DO", "COPY", "GRANT", "REVOKE",
    "BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE",
    "VACUUM", "ANALYZE", "REINDEX", "CLUSTER",
})

# Functions that could leak data, cause DoS, or break isolation
_BLOCKED_FUNCTIONS = re.compile(
    r"\b("
    r"pg_sleep|dblink|dblink_exec|pg_read_file|pg_read_binary_file|"
    r"lo_import|lo_export|lo_unlink|pg_ls_dir|pg_stat_file|"
    r"copy_from|copy_to|pg_cancel_backend|pg_terminate_backend|"
    r"pg_reload_conf|pg_rotate_logfile|pg_switch_wal"
    r")\s*\(",
    re.IGNORECASE,
)

# Picks up table/view names after FROM and all JOIN variants
_TABLE_REF = re.compile(
    r"\b(?:FROM|JOIN)\s+([a-zA-Z_][a-zA-Z0-9_]*)",
    re.IGNORECASE,
)

# Extracts CTE alias names: WITH alias AS ( ... ), alias2 AS ( ... )
_CTE_ALIAS = re.compile(
    r"(?:(?:\bWITH\b|,)\s*)([a-zA-Z_][a-zA-Z0-9_]*)\s+AS\s*\(",
    re.IGNORECASE,
)


def validate_sql(sql: str, allowed_tables: list[str]) -> dict:
    """
    Return {"ok": True} or {"ok": False, "reason": "<msg>"}.
    `allowed_tables` is the live list from information_schema.
    """
    sql = sql.strip()
    if not sql:
        return {"ok": False, "reason": "Empty SQL"}

    # ── 1. Parse and check first DML token ──────────────────────────────────
    stmts = sqlparse.parse(sql)
    if not stmts:
        return {"ok": False, "reason": "Could not parse SQL"}

    stmt = stmts[0]
    first_keyword = _first_dml(stmt)
    if first_keyword != "SELECT":
        return {
            "ok": False,
            "reason": f"Only SELECT is allowed; got '{first_keyword or 'unknown'}'",
        }

    # ── 2. Block forbidden keywords anywhere in the statement ───────────────
    sql_upper = sql.upper()
    for kw in _BLOCKED_KEYWORDS:
        if re.search(r"\b" + kw + r"\b", sql_upper):
            return {"ok": False, "reason": f"Keyword '{kw}' is not permitted"}

    # ── 3. Block dangerous functions ─────────────────────────────────────────
    if _BLOCKED_FUNCTIONS.search(sql):
        return {"ok": False, "reason": "Dangerous function call detected"}

    # ── 4. Table whitelist ───────────────────────────────────────────────────
    sql_clean = _strip_literals(sql)
    used = {m.group(1).lower() for m in _TABLE_REF.finditer(sql_clean)}
    # CTE aliases (e.g. WITH top_segments AS ...) are virtual, not real tables.
    cte_aliases = {m.group(1).lower() for m in _CTE_ALIAS.finditer(sql_clean)}
    allowed_lower = {t.lower() for t in allowed_tables} | cte_aliases
    unknown = used - allowed_lower
    if unknown:
        return {
            "ok": False,
            "reason": f"Table(s) not allowed: {', '.join(sorted(unknown))}",
        }

    return {"ok": True, "reason": None}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _first_dml(stmt) -> str | None:
    """Return the uppercased first DML token, skipping whitespace/comments/CTE preamble.

    CTEs start with WITH (Token.Keyword.CTE). Once seen, we skip all non-DML
    tokens until we reach the first DML keyword (the SELECT inside the CTE body
    or the outer SELECT), which is the statement's effective verb.
    """
    in_cte = False
    for token in stmt.flatten():
        if token.ttype in (T.Whitespace, T.Newline,
                           T.Comment.Single, T.Comment.Multiline):
            continue
        if token.ttype is T.Keyword.CTE:
            in_cte = True
            continue
        if token.ttype is T.DML:
            return token.normalized.upper()
        # While inside a CTE preamble, skip non-DML tokens (name, AS, punctuation…)
        if in_cte:
            continue
        # Non-whitespace, non-DML token reached first → not a pure DML statement.
        return token.normalized.upper()
    return None


def _strip_literals(sql: str) -> str:
    """Remove string literals to avoid false table-name matches."""
    sql = re.sub(r"'(?:[^'\\]|\\.)*'", "''", sql)
    sql = re.sub(r'"(?:[^"\\]|\\.)*"', '""', sql)
    return sql
