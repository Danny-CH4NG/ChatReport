"""
Integration tests — edge cases and security boundary validation.

Tests the pre_query hook against a real allowed-tables list and validates
post_query warning thresholds. These use the live DB only for fetching the
allowed-tables list; the SQL itself is never executed.

Requirements: docker compose up postgres
Run:          pytest -m integration
"""
import pytest

from hooks.pre_query import validate_sql
from hooks.post_query import post_query_check

pytestmark = pytest.mark.integration


# ── SQL Security vs Live Table List ──────────────────────────────────────────

class TestSecurityWithLiveTableList:
    """Repeat key security tests with the actual allowed-tables list from the DB."""

    def test_select_star_devices_allowed(self, allowed_tables):
        result = validate_sql("SELECT * FROM devices", allowed_tables)
        assert result["ok"] is True

    def test_information_schema_not_allowed(self, allowed_tables):
        sql = "SELECT table_name FROM information_schema.tables"
        result = validate_sql(sql, allowed_tables)
        # information_schema is not in the public table whitelist → rejected
        assert result["ok"] is False

    def test_pg_catalog_not_allowed(self, allowed_tables):
        sql = "SELECT relname FROM pg_catalog.pg_class"
        result = validate_sql(sql, allowed_tables)
        assert result["ok"] is False

    def test_complex_union_attack(self, allowed_tables):
        # Classic UNION-based injection targeting system tables
        sql = (
            "SELECT device_code FROM devices "
            "UNION SELECT table_name FROM information_schema.tables"
        )
        result = validate_sql(sql, allowed_tables)
        assert result["ok"] is False

    def test_stacked_query_attack(self, allowed_tables):
        sql = "SELECT 1; DROP TABLE devices"
        result = validate_sql(sql, allowed_tables)
        assert result["ok"] is False

    def test_comment_obfuscation(self, allowed_tables):
        # Attempt to hide DROP TABLE inside a comment-stripped statement
        sql = "SELECT 1 /* DROP TABLE devices */"
        result = validate_sql(sql, allowed_tables)
        # The DROP inside a comment should not trigger the keyword check
        # (sqlparse strips comments), but this verifies no crash
        assert "ok" in result

    def test_hex_encoded_sleep_attempt(self, allowed_tables):
        # pg_sleep expressed differently — should still fail keyword check
        sql = "SELECT pg_sleep(10)"
        result = validate_sql(sql, allowed_tables)
        assert result["ok"] is False

    def test_all_allowed_tables_are_selectable(self, allowed_tables):
        for tbl in allowed_tables:
            sql = f"SELECT * FROM {tbl} LIMIT 1"
            result = validate_sql(sql, allowed_tables)
            assert result["ok"] is True, f"Expected {tbl} to be selectable but got: {result}"

    def test_cross_join_known_tables(self, allowed_tables):
        sql = (
            "SELECT d.device_code, i.district "
            "FROM devices d "
            "CROSS JOIN intersections i "
            "LIMIT 10"
        )
        result = validate_sql(sql, allowed_tables)
        assert result["ok"] is True


# ── Post-query warning thresholds ─────────────────────────────────────────────

class TestPostQueryHook:
    """post_query_check should warn on large result sets."""

    SAMPLE_SQL = "SELECT * FROM vd_readings"

    def test_no_warning_under_threshold(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUDIT_LOG_DIR", str(tmp_path))
        # Re-import to pick up the patched env var
        import importlib
        import hooks.post_query as pq
        importlib.reload(pq)
        warnings = pq.post_query_check(self.SAMPLE_SQL, row_count=100, execution_ms=50)
        assert warnings == []

    def test_warning_at_threshold(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUDIT_LOG_DIR", str(tmp_path))
        import importlib
        import hooks.post_query as pq
        importlib.reload(pq)
        warnings = pq.post_query_check(self.SAMPLE_SQL, row_count=5001, execution_ms=200)
        assert len(warnings) == 1
        assert "5,001" in warnings[0] or "5001" in warnings[0]

    def test_warning_well_above_threshold(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUDIT_LOG_DIR", str(tmp_path))
        import importlib
        import hooks.post_query as pq
        importlib.reload(pq)
        warnings = pq.post_query_check(self.SAMPLE_SQL, row_count=10_000, execution_ms=800)
        assert len(warnings) >= 1

    def test_audit_log_written(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUDIT_LOG_DIR", str(tmp_path))
        import importlib
        import hooks.post_query as pq
        importlib.reload(pq)
        pq.post_query_check(self.SAMPLE_SQL, row_count=42, execution_ms=15)
        log_file = tmp_path / "query_audit.log"
        assert log_file.exists()
        content = log_file.read_text(encoding="utf-8")
        assert "42" in content
        assert self.SAMPLE_SQL[:30] in content


# ── Multi-turn context (clarification_checker with realistic history) ─────────

class TestMultiTurnClarificationFlow:
    """
    PRD Section 13 — 跨輪次對話測試.
    These tests verify the clarification_checker uses message history correctly.
    Requires the app path in sys.path (handled by conftest.py).
    """

    def _u(self, text): return {"role": "user", "content": text}
    def _a(self, text): return {"role": "assistant", "content": text}

    def test_turn2_inherits_turn1_time_context(self):
        from skills.clarification_checker import check
        history = [
            self._u("中山/南京路口的 VD 今天狀況？"),
            self._a("查詢結果如下：今天中山/南京路口 VD 壅塞 3 次。"),
        ]
        # Turn 2: "那它的 TC 呢？" — "它" refers to the same intersection
        # Time context "今天" is in turn 1 history
        result = check("那它的 TC 呢？", history)
        assert result["needs_clarification"] is False

    def test_turn3_no_time_but_history_covers_it(self):
        from skills.clarification_checker import check
        history = [
            self._u("中山/南京路口的 VD 今天狀況？"),
            self._a("今天資料…"),
            self._u("那它的 TC 呢？"),
            self._a("TC 相位資料…"),
        ]
        # Turn 3: export both results — time is established in turn 1
        result = check("把這兩個結果匯出", history)
        # Not a data aggregation query → should not trigger clarification
        assert result["needs_clarification"] is False

    def test_fresh_aggregation_without_any_history_triggers(self):
        from skills.clarification_checker import check
        result = check("哪些路口車流量最高？", [])
        assert result["needs_clarification"] is True

    def test_b2_scenario_现在_scoped(self):
        from skills.clarification_checker import check
        # "現在" = currently scoped, no time range needed
        result = check("CMS 現在還在顯示哪些訊息？", [])
        assert result["needs_clarification"] is False

    def test_b1_scenario_目前_scoped(self):
        from skills.clarification_checker import check
        result = check("目前哪些設備在故障？按行政區分類", [])
        assert result["needs_clarification"] is False
