"""
Post-query hook.
Responsibilities:
  - Warn when result set is large (> 5000 rows)
  - Append an audit log entry to /app/logs/query_audit.log
"""
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_WARN_THRESHOLD = 5000
_LOG_DIR = Path(os.getenv("AUDIT_LOG_DIR", "/app/logs"))
_LOG_FILE = _LOG_DIR / "query_audit.log"


def post_query_check(sql: str, row_count: int, execution_ms: int) -> list[str]:
    """
    Run post-query checks. Returns a list of warning strings (may be empty).
    Also appends one line to the audit log.
    """
    warnings: list[str] = []

    if row_count > _WARN_THRESHOLD:
        msg = (
            f"Large result set: {row_count:,} rows returned. "
            "Consider adding filters or a smaller time range."
        )
        warnings.append(msg)
        logger.warning(msg)

    _write_audit(sql, row_count, execution_ms, warnings)
    return warnings


def _write_audit(sql: str, row_count: int, execution_ms: int, warnings: list[str]) -> None:
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "sql": sql[:500],           # truncate very long SQL
            "row_count": row_count,
            "execution_ms": execution_ms,
            "warnings": warnings,
        }
        with _LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        # Never crash the query response due to logging failure
        logger.warning("Audit log write failed: %s", e)
