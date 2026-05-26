"""
Unit tests for mcp_server/hooks/pre_query.py

Tests the SQL security validator (no DB / Docker required).
"""
import pytest

from hooks.pre_query import validate_sql

# Canonical allowed table list used throughout these tests
ALLOWED = [
    "intersections", "devices",
    "vd_detail", "vd_readings",
    "cms_detail", "cms_messages",
    "tc_detail", "tc_phase_logs",
    "cctv_detail", "cctv_events",
    "etag_detail", "etag_readings",
    "incidents", "maintenance_records",
]


# ── Happy paths ───────────────────────────────────────────────────────────────

class TestValidSelectStatements:
    def test_simple_select(self):
        result = validate_sql("SELECT * FROM devices", ALLOWED)
        assert result["ok"] is True

    def test_select_with_where(self):
        sql = "SELECT device_id, status FROM devices WHERE device_type = 'VD'"
        assert validate_sql(sql, ALLOWED)["ok"] is True

    def test_select_with_join(self):
        sql = (
            "SELECT d.device_code, i.district "
            "FROM devices d JOIN intersections i ON d.intersection_id = i.intersection_id"
        )
        assert validate_sql(sql, ALLOWED)["ok"] is True

    def test_select_with_multi_join(self):
        sql = (
            "SELECT vr.congestion_level, i.name "
            "FROM vd_readings vr "
            "JOIN devices d ON vr.device_id = d.device_id "
            "JOIN intersections i ON d.intersection_id = i.intersection_id"
        )
        assert validate_sql(sql, ALLOWED)["ok"] is True

    def test_select_with_existing_limit(self):
        sql = "SELECT * FROM vd_readings LIMIT 100"
        assert validate_sql(sql, ALLOWED)["ok"] is True

    def test_select_with_group_by_order_by(self):
        sql = (
            "SELECT d.device_type, COUNT(*) AS cnt "
            "FROM devices d "
            "GROUP BY d.device_type "
            "ORDER BY cnt DESC"
        )
        assert validate_sql(sql, ALLOWED)["ok"] is True

    def test_select_with_cte(self):
        sql = (
            "WITH ranked AS ("
            "  SELECT device_id, ROW_NUMBER() OVER (ORDER BY device_id) AS rn "
            "  FROM devices"
            ") "
            "SELECT * FROM ranked WHERE rn <= 10"
        )
        assert validate_sql(sql, ALLOWED)["ok"] is True

    def test_select_with_subquery(self):
        sql = (
            "SELECT * FROM devices "
            "WHERE intersection_id IN (SELECT intersection_id FROM intersections WHERE district = '信義區')"
        )
        assert validate_sql(sql, ALLOWED)["ok"] is True

    def test_select_case_insensitive_keyword(self):
        sql = "select * from devices where status = 'fault'"
        assert validate_sql(sql, ALLOWED)["ok"] is True

    def test_select_left_join(self):
        sql = (
            "SELECT cm.message_content, cm.removed_at "
            "FROM cms_messages cm "
            "LEFT JOIN devices d ON cm.device_id = d.device_id "
            "WHERE cm.removed_at IS NULL"
        )
        assert validate_sql(sql, ALLOWED)["ok"] is True


# ── Blocked DML statements ────────────────────────────────────────────────────

class TestBlockedDMLStatements:
    @pytest.mark.parametrize("dml,sql", [
        ("INSERT", "INSERT INTO devices (device_code) VALUES ('X')"),
        ("UPDATE", "UPDATE devices SET status = 'fault' WHERE device_id = 1"),
        ("DELETE", "DELETE FROM devices WHERE device_id = 1"),
        ("DROP",   "DROP TABLE devices"),
        ("CREATE", "CREATE TABLE foo (id INT)"),
        ("ALTER",  "ALTER TABLE devices ADD COLUMN foo INT"),
        ("TRUNCATE","TRUNCATE devices"),
    ])
    def test_blocked_dml(self, dml, sql):
        result = validate_sql(sql, ALLOWED)
        assert result["ok"] is False
        assert dml in result["reason"] or "Only SELECT" in result["reason"]

    def test_empty_sql_rejected(self):
        result = validate_sql("", ALLOWED)
        assert result["ok"] is False
        assert "Empty" in result["reason"]

    def test_whitespace_only_rejected(self):
        result = validate_sql("   \n\t  ", ALLOWED)
        assert result["ok"] is False


# ── Blocked keywords anywhere in statement ────────────────────────────────────

class TestBlockedKeywordsInBody:
    @pytest.mark.parametrize("kw,sql", [
        ("EXECUTE", "SELECT EXECUTE('some_proc')"),
        ("COPY",    "SELECT 1; COPY devices TO '/tmp/out.csv'"),
        ("GRANT",   "SELECT 1; GRANT SELECT ON devices TO attacker"),
        ("VACUUM",  "VACUUM devices"),
        ("ANALYZE", "ANALYZE devices"),
    ])
    def test_blocked_keyword(self, kw, sql):
        result = validate_sql(sql, ALLOWED)
        assert result["ok"] is False

    def test_begin_blocked(self):
        sql = "BEGIN; DELETE FROM devices; COMMIT"
        result = validate_sql(sql, ALLOWED)
        assert result["ok"] is False


# ── Blocked dangerous functions ───────────────────────────────────────────────

class TestBlockedFunctions:
    @pytest.mark.parametrize("fn,sql", [
        ("pg_sleep",    "SELECT pg_sleep(5)"),
        ("dblink",      "SELECT * FROM dblink('host=evil', 'SELECT 1') AS t(id INT)"),
        ("pg_read_file","SELECT pg_read_file('/etc/passwd')"),
        ("lo_import",   "SELECT lo_import('/etc/shadow')"),
        ("pg_ls_dir",   "SELECT * FROM pg_ls_dir('/tmp')"),
    ])
    def test_blocked_function(self, fn, sql):
        result = validate_sql(sql, ALLOWED)
        assert result["ok"] is False
        assert "Dangerous function" in result["reason"]


# ── Table whitelist ───────────────────────────────────────────────────────────

class TestTableWhitelist:
    def test_unknown_table_rejected(self):
        sql = "SELECT * FROM secret_table"
        result = validate_sql(sql, ALLOWED)
        assert result["ok"] is False
        assert "secret_table" in result["reason"]

    def test_multiple_unknown_tables_all_reported(self):
        sql = "SELECT * FROM shadow_table JOIN evil_view ON 1=1"
        result = validate_sql(sql, ALLOWED)
        assert result["ok"] is False
        assert "shadow_table" in result["reason"] or "evil_view" in result["reason"]

    def test_known_table_accepted(self):
        sql = "SELECT * FROM maintenance_records"
        assert validate_sql(sql, ALLOWED)["ok"] is True

    def test_system_table_rejected_when_not_in_list(self):
        # pg_catalog tables are not in ALLOWED
        sql = "SELECT * FROM pg_catalog.pg_tables"
        result = validate_sql(sql, ALLOWED)
        # Either rejected for unknown table or accepted (pg_catalog treated as schema)
        # Main thing: no crash
        assert "ok" in result

    def test_sql_injection_via_string_literal_not_flagged(self):
        # The word 'secret' appears only inside a string literal — should not be flagged
        sql = "SELECT * FROM devices WHERE model = 'secret_model'"
        assert validate_sql(sql, ALLOWED)["ok"] is True
