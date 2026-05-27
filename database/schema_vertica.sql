-- =============================================================
-- Traffic Management System Database Schema — Vertica Edition
-- Target: HPE Vertica | Converted from database/schema.sql (PostgreSQL 16)
-- =============================================================
--
-- Conversion notes (vs PostgreSQL schema):
--   SERIAL / BIGSERIAL   → INT / BIGINT IDENTITY(1,1)
--   INET                 → VARCHAR(45)           (no native INET type)
--   VARCHAR(20)[] ARRAY  → VARCHAR(500)           (comma-separated values)
--   TEXT                 → VARCHAR(65000)          (Vertica max VARCHAR)
--   DEFAULT TRUE/FALSE   → DEFAULT true/false      (lowercase literals)
--   Partial indexes      → Regular indexes         (WHERE clause not supported)
--   Constraints          → Defined but NOT ENFORCED (Vertica optimizer hints)
--
-- ⚠️  本檔案僅供結構參考，請勿對正式 Vertica 執行。
--    正式 Vertica 為唯讀環境，不允許任何 DDL/DML 操作。
--
-- =============================================================

-- =====================
-- Layer 1: Intersections
-- =====================

CREATE TABLE IF NOT EXISTS intersections (
    intersection_id INT           IDENTITY(1,1) PRIMARY KEY,
    name            VARCHAR(100)  NOT NULL,
    district        VARCHAR(50)   NOT NULL,
    road_class      VARCHAR(20)   NOT NULL
                    CHECK (road_class IN ('national','provincial','county','city')),
    lat             DECIMAL(9,6),
    lng             DECIMAL(9,6),
    total_approaches INT          DEFAULT 4,
    created_at      TIMESTAMP     NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE intersections IS '路口基本資料';
COMMENT ON COLUMN intersections.road_class IS 'national=國道, provincial=省道, county=縣道, city=市區道路';

-- =====================
-- Layer 2: Device Base Table
-- =====================

CREATE TABLE IF NOT EXISTS devices (
    device_id        INT           IDENTITY(1,1) PRIMARY KEY,
    device_code      VARCHAR(30)   NOT NULL UNIQUE,
    intersection_id  INT           NOT NULL REFERENCES intersections(intersection_id),
    device_type      VARCHAR(10)   NOT NULL
                     CHECK (device_type IN ('VD','CMS','TC','CCTV','eTag')),
    model            VARCHAR(100),
    vendor           VARCHAR(100),
    install_date     DATE,
    ip_address       VARCHAR(45),                          -- INET → VARCHAR(45)
    status           VARCHAR(20)   NOT NULL DEFAULT 'active'
                     CHECK (status IN ('active','fault','maintenance','offline')),
    last_heartbeat   TIMESTAMP
);

COMMENT ON TABLE  devices IS '所有設備共用基底表，查特定類型設備用 WHERE device_type=... 再 JOIN 對應 _detail 子表';
COMMENT ON COLUMN devices.device_code IS '設備代碼，格式如 VD-001-N';
COMMENT ON COLUMN devices.status IS 'active=正常, fault=故障, maintenance=維護中, offline=離線';

-- =====================
-- Layer 3a: VD (Vehicle Detector)
-- =====================

CREATE TABLE IF NOT EXISTS vd_detail (
    device_id               INT          PRIMARY KEY REFERENCES devices(device_id),
    detection_method        VARCHAR(20)  NOT NULL
                            CHECK (detection_method IN ('loop','radar','video','microwave')),
    lane_count              INT          NOT NULL DEFAULT 2,
    approach_direction      VARCHAR(10)
                            CHECK (approach_direction IN ('N','S','E','W','NE','NW','SE','SW')),
    detection_length_m      DECIMAL(5,2),
    supports_vehicle_class  BOOLEAN      NOT NULL DEFAULT true  -- FALSE→false
);

COMMENT ON TABLE vd_detail IS 'VD 設備規格，1:1 對應 devices';

CREATE TABLE IF NOT EXISTS vd_readings (
    reading_id        BIGINT        IDENTITY(1,1) PRIMARY KEY,  -- BIGSERIAL→BIGINT IDENTITY
    device_id         INT           NOT NULL REFERENCES devices(device_id),
    recorded_at       TIMESTAMP     NOT NULL,
    lane_no           INT           NOT NULL DEFAULT 1,
    vehicle_count     INT           NOT NULL DEFAULT 0,
    avg_speed_kmh     DECIMAL(5,2),
    occupancy_pct     DECIMAL(5,2),
    congestion_level  VARCHAR(10)   NOT NULL
                      CHECK (congestion_level IN ('free','moderate','heavy','jam')),
    car_count         INT           NOT NULL DEFAULT 0,
    truck_count       INT           NOT NULL DEFAULT 0,
    motorcycle_count  INT           NOT NULL DEFAULT 0
);

COMMENT ON TABLE  vd_readings IS 'VD 車流量時序資料，每 5 分鐘一筆';
COMMENT ON COLUMN vd_readings.congestion_level IS 'free=順暢, moderate=稍壅, heavy=壅塞, jam=嚴重壅塞';

-- =====================
-- Layer 3b: CMS (Changeable Message Sign)
-- =====================

CREATE TABLE IF NOT EXISTS cms_detail (
    device_id         INT          PRIMARY KEY REFERENCES devices(device_id),
    sign_type         VARCHAR(20)  NOT NULL
                      CHECK (sign_type IN ('LED','LCD','fiber_optic')),
    display_rows      INT          NOT NULL DEFAULT 3,
    display_cols      INT          NOT NULL DEFAULT 16,
    supports_graphics BOOLEAN      NOT NULL DEFAULT false,  -- FALSE→false
    mounting_type     VARCHAR(20)  NOT NULL
                      CHECK (mounting_type IN ('overhead','roadside','portal'))
);

COMMENT ON TABLE cms_detail IS 'CMS 設備規格，1:1 對應 devices';

CREATE TABLE IF NOT EXISTS cms_messages (
    message_id      BIGINT        IDENTITY(1,1) PRIMARY KEY,
    device_id       INT           NOT NULL REFERENCES devices(device_id),
    displayed_at    TIMESTAMP     NOT NULL,
    removed_at      TIMESTAMP,
    message_content VARCHAR(65000) NOT NULL,               -- TEXT → VARCHAR(65000)
    message_type    VARCHAR(20)   NOT NULL
                    CHECK (message_type IN ('info','warning','alert','event')),
    triggered_by    VARCHAR(50)   NOT NULL
                    CHECK (triggered_by IN ('manual','auto_incident','schedule'))
);

COMMENT ON TABLE cms_messages IS 'CMS 訊息顯示記錄；removed_at IS NULL 表示目前仍在顯示';

-- =====================
-- Layer 3c: TC (Traffic Controller)
-- =====================

CREATE TABLE IF NOT EXISTS tc_detail (
    device_id            INT          PRIMARY KEY REFERENCES devices(device_id),
    controller_type      VARCHAR(20)  NOT NULL
                         CHECK (controller_type IN ('fixed','actuated','adaptive')),
    phase_count          INT          NOT NULL DEFAULT 4,
    has_ats              BOOLEAN      NOT NULL DEFAULT false,
    coordination_group   VARCHAR(20),
    cycle_plan_count     INT          NOT NULL DEFAULT 3
);

COMMENT ON TABLE tc_detail IS 'TC 設備規格，1:1 對應 devices';

CREATE TABLE IF NOT EXISTS tc_phase_logs (
    log_id              BIGINT      IDENTITY(1,1) PRIMARY KEY,
    device_id           INT         NOT NULL REFERENCES devices(device_id),
    recorded_at         TIMESTAMP   NOT NULL,
    phase_no            INT         NOT NULL,
    green_duration_sec  INT         NOT NULL,
    red_duration_sec    INT         NOT NULL,
    cycle_length_sec    INT         NOT NULL,
    is_oversaturated    BOOLEAN     NOT NULL DEFAULT false
);

COMMENT ON TABLE  tc_phase_logs IS 'TC 相位切換時序記錄';
COMMENT ON COLUMN tc_phase_logs.is_oversaturated IS '過飽和：需求超過容量，綠燈結束仍有車輛等待';

-- =====================
-- Layer 3d: CCTV
-- =====================

CREATE TABLE IF NOT EXISTS cctv_detail (
    device_id             INT          PRIMARY KEY REFERENCES devices(device_id),
    resolution            VARCHAR(20)  NOT NULL DEFAULT '1920x1080',
    fps                   INT          NOT NULL DEFAULT 30,
    has_ptz               BOOLEAN      NOT NULL DEFAULT false,
    has_ai_analysis       BOOLEAN      NOT NULL DEFAULT false,
    coverage_angle_deg    INT,
    storage_days          INT          NOT NULL DEFAULT 30
);

COMMENT ON TABLE cctv_detail IS 'CCTV 設備規格，1:1 對應 devices';

CREATE TABLE IF NOT EXISTS cctv_events (
    event_id        BIGINT       IDENTITY(1,1) PRIMARY KEY,
    device_id       INT          NOT NULL REFERENCES devices(device_id),
    detected_at     TIMESTAMP    NOT NULL,
    event_type      VARCHAR(30)  NOT NULL
                    CHECK (event_type IN ('wrong_way','stopped_vehicle','crowd','jaywalking','accident')),
    confidence_pct  INT          NOT NULL CHECK (confidence_pct BETWEEN 0 AND 100),
    is_confirmed    BOOLEAN      NOT NULL DEFAULT false,
    snapshot_url    VARCHAR(65000)                             -- TEXT → VARCHAR(65000)
);

COMMENT ON TABLE cctv_events IS 'CCTV AI 或人工偵測到的交通事件';

-- =====================
-- Layer 3e: eTag
-- =====================

CREATE TABLE IF NOT EXISTS etag_detail (
    device_id      INT          PRIMARY KEY REFERENCES devices(device_id),
    antenna_count  INT          NOT NULL DEFAULT 2,
    read_range_m   INT          NOT NULL DEFAULT 10,
    lane_binding   VARCHAR(20)  NOT NULL DEFAULT 'all'
);

COMMENT ON TABLE etag_detail IS 'eTag 讀取器規格，1:1 對應 devices';

CREATE TABLE IF NOT EXISTS etag_readings (
    reading_id        BIGINT       IDENTITY(1,1) PRIMARY KEY,
    device_id         INT          NOT NULL REFERENCES devices(device_id),
    read_at           TIMESTAMP    NOT NULL,
    vehicle_class     VARCHAR(20)  NOT NULL
                      CHECK (vehicle_class IN ('car','truck','bus','motorcycle')),
    travel_time_sec   INT,
    origin_device_id  INT          REFERENCES devices(device_id)
);

COMMENT ON TABLE etag_readings IS 'eTag 讀取記錄，含旅行時間';

-- =====================
-- Cross-device: Incidents & Maintenance
-- =====================

CREATE TABLE IF NOT EXISTS incidents (
    incident_id              INT           IDENTITY(1,1) PRIMARY KEY,
    intersection_id          INT           NOT NULL REFERENCES intersections(intersection_id),
    detected_by_device_id    INT           REFERENCES devices(device_id),
    incident_type            VARCHAR(30)   NOT NULL
                             CHECK (incident_type IN
                                ('accident','congestion','road_block',
                                 'vd_anomaly','equipment_fault','special_event')),
    severity                 VARCHAR(20)   NOT NULL
                             CHECK (severity IN ('low','medium','high','critical')),
    occurred_at              TIMESTAMP     NOT NULL,
    resolved_at              TIMESTAMP,
    -- ARRAY → VARCHAR(500), comma-separated direction codes e.g. 'N,S'
    -- Query tip: use LIKE '%N%' instead of ANY(ARRAY[...])
    affected_directions      VARCHAR(500),
    description              VARCHAR(65000)                    -- TEXT → VARCHAR(65000)
);

COMMENT ON TABLE  incidents IS '跨設備交通事件彙整；resolved_at IS NULL 表示未解除';
COMMENT ON COLUMN incidents.severity IS 'low<medium<high<critical';
COMMENT ON COLUMN incidents.affected_directions IS '受影響方向，逗號分隔（原 PG ARRAY），查詢用 LIKE ''%N%''';

CREATE TABLE IF NOT EXISTS maintenance_records (
    record_id         INT           IDENTITY(1,1) PRIMARY KEY,
    device_id         INT           NOT NULL REFERENCES devices(device_id),
    work_date         DATE          NOT NULL,
    work_type         VARCHAR(30)   NOT NULL
                      CHECK (work_type IN
                          ('inspection','repair','firmware_update','replacement','calibration')),
    technician        VARCHAR(100),
    cost_ntd          DECIMAL(10,2),
    downtime_minutes  INT           NOT NULL DEFAULT 0,
    notes             VARCHAR(65000)                           -- TEXT → VARCHAR(65000)
);

COMMENT ON TABLE maintenance_records IS '設備維護記錄';

-- =====================
-- Indexes
-- Vertica is a columnar store; query performance is primarily driven by
-- projections (auto-created) rather than B-tree indexes.
-- The indexes below are included for schema parity and optimizer hints.
-- Partial indexes (WHERE clause) are not supported in Vertica — converted
-- to regular indexes covering the same columns.
-- =====================

CREATE INDEX IF NOT EXISTS idx_vd_readings_device_time   ON vd_readings    (device_id, recorded_at);
CREATE INDEX IF NOT EXISTS idx_vd_readings_time          ON vd_readings    (recorded_at);
CREATE INDEX IF NOT EXISTS idx_vd_readings_congestion    ON vd_readings    (congestion_level, recorded_at);

CREATE INDEX IF NOT EXISTS idx_cms_messages_device_time  ON cms_messages   (device_id, displayed_at);
CREATE INDEX IF NOT EXISTS idx_cms_messages_removed      ON cms_messages   (removed_at);   -- was partial WHERE removed_at IS NULL

CREATE INDEX IF NOT EXISTS idx_tc_phase_device_time      ON tc_phase_logs  (device_id, recorded_at);
CREATE INDEX IF NOT EXISTS idx_tc_phase_oversaturated    ON tc_phase_logs  (is_oversaturated, recorded_at);

CREATE INDEX IF NOT EXISTS idx_cctv_events_device_time   ON cctv_events    (device_id, detected_at);
CREATE INDEX IF NOT EXISTS idx_cctv_events_type          ON cctv_events    (event_type, detected_at);

CREATE INDEX IF NOT EXISTS idx_etag_readings_device_time ON etag_readings  (device_id, read_at);

CREATE INDEX IF NOT EXISTS idx_incidents_intersection    ON incidents      (intersection_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_incidents_resolved        ON incidents      (resolved_at);   -- was partial WHERE resolved_at IS NULL

CREATE INDEX IF NOT EXISTS idx_devices_intersection      ON devices        (intersection_id);
CREATE INDEX IF NOT EXISTS idx_devices_type_status       ON devices        (device_type, status);

CREATE INDEX IF NOT EXISTS idx_maintenance_device        ON maintenance_records (device_id, work_date);
