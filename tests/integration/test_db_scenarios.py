"""
Integration tests — PRD Section 13 acceptance query scenarios.

Each test executes the representative SQL for a scenario directly against
PostgreSQL and verifies the result set is structurally correct.

Requirements: docker compose up postgres  (or an equivalent running instance)
Run:          pytest -m integration
Skip:         pytest -m "not integration"

Scenarios covered:
  B1  目前哪些設備在故障？按行政區分類
  B2  CMS 現在還在顯示哪些訊息？
  B3  過去 3 個月各設備類型的維護費用總計？
  A1  上週哪 5 個路口機車流量最高（僅算工作日尖峰）？
  A2  哪些路口在早上 7-9 點 VD 壅塞且 TC 同時過飽和？
  A3  今天哪些 CCTV 偵測到事故，且對應路口的 CMS 有無同步顯示警告？
  A4  eTag 旅行時間最長的路段，對應 VD 的壅塞等級為何？
  MT  跨輪次對話測試（驗證歷史記憶 / clarification context）
"""
import pytest
import psycopg2.extras


pytestmark = pytest.mark.integration


# ── Helpers ───────────────────────────────────────────────────────────────────

def _exec(cursor, sql: str) -> tuple[list[str], list[tuple]]:
    """Execute sql, return (column_names, rows)."""
    cursor.execute(sql)
    cols = [desc[0] for desc in cursor.description or []]
    rows = cursor.fetchall()
    return cols, rows


# ── B-series: basic queries (single device type or cross-type statuses) ───────

class TestBasicScenarios:
    def test_b1_faulty_devices_by_district(self, cursor):
        """B1: 目前哪些設備在故障？按行政區分類"""
        sql = """
            SELECT i.district, d.device_type, COUNT(*) AS fault_count
            FROM devices d
            JOIN intersections i ON d.intersection_id = i.intersection_id
            WHERE d.status = 'fault'
            GROUP BY i.district, d.device_type
            ORDER BY i.district, d.device_type
        """
        cols, rows = _exec(cursor, sql)
        assert "district" in cols
        assert "device_type" in cols
        assert "fault_count" in cols
        # Seeded data always contains some faults
        assert len(rows) > 0, "Expected at least one faulty device in seeded data"
        for row in rows:
            fault_count = row[cols.index("fault_count")]
            assert fault_count > 0

    def test_b2_active_cms_messages(self, cursor):
        """B2: CMS 現在還在顯示哪些訊息？"""
        sql = """
            SELECT
                d.device_code,
                i.name         AS intersection_name,
                cm.message_content,
                cm.message_type,
                cm.displayed_at
            FROM cms_messages cm
            JOIN devices d ON cm.device_id = d.device_id
            JOIN intersections i ON d.intersection_id = i.intersection_id
            WHERE cm.removed_at IS NULL
            ORDER BY cm.displayed_at DESC
        """
        cols, rows = _exec(cursor, sql)
        assert "device_code"        in cols
        assert "intersection_name"  in cols
        assert "message_content"    in cols
        assert "message_type"       in cols
        # removed_at IS NULL = currently displayed; may legitimately be 0
        for row in rows:
            msg_type = row[cols.index("message_type")]
            assert msg_type in ("info", "warning", "alert", "event")

    def test_b3_maintenance_cost_by_device_type(self, cursor):
        """B3: 過去 3 個月各設備類型的維護費用總計？"""
        sql = """
            SELECT
                d.device_type,
                COUNT(*)          AS maintenance_count,
                SUM(mr.cost_ntd)  AS total_cost_ntd
            FROM maintenance_records mr
            JOIN devices d ON mr.device_id = d.device_id
            WHERE mr.work_date >= NOW() - INTERVAL '3 months'
            GROUP BY d.device_type
            ORDER BY total_cost_ntd DESC NULLS LAST
        """
        cols, rows = _exec(cursor, sql)
        assert "device_type"       in cols
        assert "maintenance_count" in cols
        assert "total_cost_ntd"    in cols
        assert len(rows) > 0, "Expected maintenance records in the past 3 months"
        for row in rows:
            device_type = row[cols.index("device_type")]
            assert device_type in ("VD", "CMS", "TC", "CCTV", "eTag")
            count = row[cols.index("maintenance_count")]
            assert count > 0


# ── A-series: advanced multi-table JOIN queries ───────────────────────────────

class TestAdvancedScenarios:
    def test_a1_top5_intersections_motorcycle_last_week(self, cursor):
        """A1: 上週哪 5 個路口機車流量最高（僅算工作日尖峰）？"""
        sql = """
            SELECT
                i.name          AS intersection_name,
                i.district,
                SUM(vr.motorcycle_count) AS total_motorcycles
            FROM vd_readings vr
            JOIN devices d  ON vr.device_id        = d.device_id
                            AND d.device_type       = 'VD'
            JOIN intersections i ON d.intersection_id = i.intersection_id
            WHERE vr.recorded_at >= date_trunc('week', NOW()) - INTERVAL '1 week'
              AND vr.recorded_at <  date_trunc('week', NOW())
              AND EXTRACT(DOW  FROM vr.recorded_at) BETWEEN 1 AND 5
              AND EXTRACT(HOUR FROM vr.recorded_at) IN (7, 8, 17, 18)
            GROUP BY i.intersection_id, i.name, i.district
            ORDER BY total_motorcycles DESC
            LIMIT 5
        """
        cols, rows = _exec(cursor, sql)
        assert "intersection_name" in cols
        assert "total_motorcycles" in cols
        assert len(rows) <= 5
        # Verify ordering: highest first
        if len(rows) >= 2:
            counts = [row[cols.index("total_motorcycles")] for row in rows]
            assert counts == sorted(counts, reverse=True)

    def test_a2_intersections_vd_congested_and_tc_oversaturated(self, cursor):
        """A2: 哪些路口在早上 7-9 點 VD 壅塞且 TC 同時過飽和？"""
        # Scope to last 7 days to keep the correlated subquery tractable.
        sql = """
            SELECT DISTINCT i.name AS intersection_name, i.district
            FROM vd_readings vr
            JOIN devices dv ON vr.device_id = dv.device_id
                            AND dv.device_type = 'VD'
            JOIN intersections i ON dv.intersection_id = i.intersection_id
            WHERE vr.congestion_level IN ('heavy', 'jam')
              AND EXTRACT(HOUR FROM vr.recorded_at) BETWEEN 7 AND 9
              AND vr.recorded_at >= NOW() - INTERVAL '7 days'
              AND EXISTS (
                  SELECT 1
                  FROM tc_phase_logs tc
                  JOIN devices dt ON tc.device_id = dt.device_id
                                  AND dt.device_type = 'TC'
                  WHERE dt.intersection_id = i.intersection_id
                    AND tc.is_oversaturated = TRUE
                    AND EXTRACT(HOUR FROM tc.recorded_at) BETWEEN 7 AND 9
                    AND tc.recorded_at::date = vr.recorded_at::date
              )
            ORDER BY i.name
            LIMIT 20
        """
        cols, rows = _exec(cursor, sql)
        assert "intersection_name" in cols
        assert "district"          in cols
        # Result may be empty if no correlated rows within the window; verify no error

    def test_a3_cctv_accidents_with_cms_warning_today(self, cursor):
        """A3: 今天哪些 CCTV 偵測到事故，且對應路口的 CMS 有無同步顯示警告？"""
        sql = """
            SELECT
                i.name           AS intersection_name,
                ce.event_type,
                ce.detected_at,
                CASE WHEN cm.message_id IS NOT NULL
                     THEN '有警告' ELSE '無警告'
                END              AS cms_warning_status
            FROM cctv_events ce
            JOIN devices dc ON ce.device_id = dc.device_id
                            AND dc.device_type = 'CCTV'
            JOIN intersections i ON dc.intersection_id = i.intersection_id
            LEFT JOIN devices dm ON dm.intersection_id = i.intersection_id
                                 AND dm.device_type = 'CMS'
            LEFT JOIN cms_messages cm ON cm.device_id = dm.device_id
                AND cm.message_type IN ('warning', 'alert')
                AND cm.displayed_at <= ce.detected_at
                AND (cm.removed_at IS NULL OR cm.removed_at >= ce.detected_at)
            WHERE ce.event_type = 'accident'
              AND ce.detected_at >= CURRENT_DATE
            ORDER BY ce.detected_at DESC
        """
        cols, rows = _exec(cursor, sql)
        assert "intersection_name"  in cols
        assert "event_type"         in cols
        assert "cms_warning_status" in cols
        for row in rows:
            assert row[cols.index("event_type")] == "accident"
            assert row[cols.index("cms_warning_status")] in ("有警告", "無警告")

    def test_a4_etag_longest_travel_vs_vd_congestion(self, cursor):
        """A4: eTag 旅行時間最長的路段，對應 VD 的壅塞等級為何？"""
        sql = """
            WITH top_segments AS (
                SELECT
                    de.intersection_id,
                    i.name                      AS intersection_name,
                    AVG(er.travel_time_sec)     AS avg_travel_time_sec
                FROM etag_readings er
                JOIN devices de ON er.device_id = de.device_id
                               AND de.device_type = 'eTag'
                JOIN intersections i ON de.intersection_id = i.intersection_id
                WHERE er.read_at >= NOW() - INTERVAL '24 hours'
                  AND er.travel_time_sec IS NOT NULL
                GROUP BY de.intersection_id, i.name
                ORDER BY avg_travel_time_sec DESC
                LIMIT 5
            )
            SELECT
                ts.intersection_name,
                ROUND(ts.avg_travel_time_sec::numeric, 1) AS avg_travel_time_sec,
                vr.congestion_level,
                COUNT(*) AS reading_count
            FROM top_segments ts
            JOIN devices dv ON dv.intersection_id = ts.intersection_id
                            AND dv.device_type = 'VD'
            JOIN vd_readings vr ON vr.device_id = dv.device_id
              AND vr.recorded_at >= NOW() - INTERVAL '24 hours'
            GROUP BY ts.intersection_name, ts.avg_travel_time_sec, vr.congestion_level
            ORDER BY ts.avg_travel_time_sec DESC, vr.congestion_level
        """
        cols, rows = _exec(cursor, sql)
        assert "intersection_name"   in cols
        assert "avg_travel_time_sec" in cols
        assert "congestion_level"    in cols
        for row in rows:
            level = row[cols.index("congestion_level")]
            assert level in ("free", "moderate", "heavy", "jam")


# ── Schema integrity checks ───────────────────────────────────────────────────

class TestSchemaIntegrity:
    """Verify that all expected tables and key columns exist in the live DB."""

    EXPECTED_TABLES = [
        "intersections", "devices",
        "vd_detail", "vd_readings",
        "cms_detail", "cms_messages",
        "tc_detail", "tc_phase_logs",
        "cctv_detail", "cctv_events",
        "etag_detail", "etag_readings",
        "incidents", "maintenance_records",
    ]

    def test_all_tables_exist(self, cursor):
        cursor.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        )
        live = {row[0] for row in cursor.fetchall()}
        for tbl in self.EXPECTED_TABLES:
            assert tbl in live, f"Missing table: {tbl}"

    def test_devices_status_enum(self, cursor):
        cursor.execute(
            "SELECT DISTINCT status FROM devices ORDER BY status"
        )
        statuses = {row[0] for row in cursor.fetchall()}
        assert statuses.issubset({"active", "fault", "maintenance", "offline"})

    def test_devices_type_enum(self, cursor):
        cursor.execute(
            "SELECT DISTINCT device_type FROM devices ORDER BY device_type"
        )
        types = {row[0] for row in cursor.fetchall()}
        assert types.issubset({"VD", "CMS", "TC", "CCTV", "eTag"})

    def test_vd_readings_congestion_enum(self, cursor):
        cursor.execute(
            "SELECT DISTINCT congestion_level FROM vd_readings ORDER BY congestion_level"
        )
        levels = {row[0] for row in cursor.fetchall()}
        assert levels.issubset({"free", "moderate", "heavy", "jam"})

    def test_seeded_data_row_counts(self, cursor):
        """Quick sanity-check that seed data was loaded."""
        for table in ("intersections", "devices", "vd_readings"):
            cursor.execute(f"SELECT COUNT(*) FROM {table}")  # noqa: S608
            count = cursor.fetchone()[0]
            assert count > 0, f"Table '{table}' is empty — did you run seed_data.py?"
