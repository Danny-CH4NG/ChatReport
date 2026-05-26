-- =============================================================
-- Traffic Management System Database Schema
-- PostgreSQL 16 | traffic_db
-- =============================================================

-- =====================
-- Layer 1: Intersections
-- =====================

CREATE TABLE intersections (
    intersection_id SERIAL PRIMARY KEY,
    name            VARCHAR(100) NOT NULL,
    district        VARCHAR(50)  NOT NULL,
    road_class      VARCHAR(20)  NOT NULL CHECK (road_class IN ('national','provincial','county','city')),
    lat             DECIMAL(9,6),
    lng             DECIMAL(9,6),
    total_approaches INT         DEFAULT 4,
    created_at      TIMESTAMP   NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE intersections IS '路口基本資料';
COMMENT ON COLUMN intersections.road_class IS 'national=國道, provincial=省道, county=縣道, city=市區道路';

-- =====================
-- Layer 2: Device Base Table
-- =====================

CREATE TABLE devices (
    device_id        SERIAL PRIMARY KEY,
    device_code      VARCHAR(30)  UNIQUE NOT NULL,
    intersection_id  INT          NOT NULL REFERENCES intersections(intersection_id),
    device_type      VARCHAR(10)  NOT NULL CHECK (device_type IN ('VD','CMS','TC','CCTV','eTag')),
    model            VARCHAR(100),
    vendor           VARCHAR(100),
    install_date     DATE,
    ip_address       INET,
    status           VARCHAR(20)  NOT NULL DEFAULT 'active'
                     CHECK (status IN ('active','fault','maintenance','offline')),
    last_heartbeat   TIMESTAMP
);

COMMENT ON TABLE devices IS '所有設備共用基底表，查特定類型設備用 WHERE device_type=... 再 JOIN 對應 _detail 子表';
COMMENT ON COLUMN devices.device_code IS '設備代碼，格式如 VD-001-N';
COMMENT ON COLUMN devices.status IS 'active=正常, fault=故障, maintenance=維護中, offline=離線';

-- =====================
-- Layer 3a: VD (Vehicle Detector)
-- =====================

CREATE TABLE vd_detail (
    device_id               INT PRIMARY KEY REFERENCES devices(device_id),
    detection_method        VARCHAR(20) NOT NULL CHECK (detection_method IN ('loop','radar','video','microwave')),
    lane_count              INT         NOT NULL DEFAULT 2,
    approach_direction      VARCHAR(10) CHECK (approach_direction IN ('N','S','E','W','NE','NW','SE','SW')),
    detection_length_m      DECIMAL(5,2),
    supports_vehicle_class  BOOLEAN     NOT NULL DEFAULT TRUE
);

COMMENT ON TABLE vd_detail IS 'VD 設備規格，1:1 對應 devices';

CREATE TABLE vd_readings (
    reading_id        BIGSERIAL PRIMARY KEY,
    device_id         INT          NOT NULL REFERENCES devices(device_id),
    recorded_at       TIMESTAMP    NOT NULL,
    lane_no           INT          NOT NULL DEFAULT 1,
    vehicle_count     INT          NOT NULL DEFAULT 0,
    avg_speed_kmh     DECIMAL(5,2),
    occupancy_pct     DECIMAL(5,2),
    congestion_level  VARCHAR(10)  NOT NULL CHECK (congestion_level IN ('free','moderate','heavy','jam')),
    car_count         INT          NOT NULL DEFAULT 0,
    truck_count       INT          NOT NULL DEFAULT 0,
    motorcycle_count  INT          NOT NULL DEFAULT 0
);

COMMENT ON TABLE vd_readings IS 'VD 車流量時序資料，每 5 分鐘一筆';
COMMENT ON COLUMN vd_readings.congestion_level IS 'free=順暢, moderate=稍壅, heavy=壅塞, jam=嚴重壅塞';

-- =====================
-- Layer 3b: CMS (Changeable Message Sign)
-- =====================

CREATE TABLE cms_detail (
    device_id         INT PRIMARY KEY REFERENCES devices(device_id),
    sign_type         VARCHAR(20) NOT NULL CHECK (sign_type IN ('LED','LCD','fiber_optic')),
    display_rows      INT         NOT NULL DEFAULT 3,
    display_cols      INT         NOT NULL DEFAULT 16,
    supports_graphics BOOLEAN     NOT NULL DEFAULT FALSE,
    mounting_type     VARCHAR(20) NOT NULL CHECK (mounting_type IN ('overhead','roadside','portal'))
);

COMMENT ON TABLE cms_detail IS 'CMS 設備規格，1:1 對應 devices';

CREATE TABLE cms_messages (
    message_id      BIGSERIAL PRIMARY KEY,
    device_id       INT          NOT NULL REFERENCES devices(device_id),
    displayed_at    TIMESTAMP    NOT NULL,
    removed_at      TIMESTAMP,
    message_content TEXT         NOT NULL,
    message_type    VARCHAR(20)  NOT NULL CHECK (message_type IN ('info','warning','alert','event')),
    triggered_by    VARCHAR(50)  NOT NULL CHECK (triggered_by IN ('manual','auto_incident','schedule'))
);

COMMENT ON TABLE cms_messages IS 'CMS 訊息顯示記錄；removed_at IS NULL 表示目前仍在顯示';

-- =====================
-- Layer 3c: TC (Traffic Controller)
-- =====================

CREATE TABLE tc_detail (
    device_id            INT PRIMARY KEY REFERENCES devices(device_id),
    controller_type      VARCHAR(20) NOT NULL CHECK (controller_type IN ('fixed','actuated','adaptive')),
    phase_count          INT         NOT NULL DEFAULT 4,
    has_ats              BOOLEAN     NOT NULL DEFAULT FALSE,
    coordination_group   VARCHAR(20),
    cycle_plan_count     INT         NOT NULL DEFAULT 3
);

COMMENT ON TABLE tc_detail IS 'TC 設備規格，1:1 對應 devices';

CREATE TABLE tc_phase_logs (
    log_id              BIGSERIAL PRIMARY KEY,
    device_id           INT       NOT NULL REFERENCES devices(device_id),
    recorded_at         TIMESTAMP NOT NULL,
    phase_no            INT       NOT NULL,
    green_duration_sec  INT       NOT NULL,
    red_duration_sec    INT       NOT NULL,
    cycle_length_sec    INT       NOT NULL,
    is_oversaturated    BOOLEAN   NOT NULL DEFAULT FALSE
);

COMMENT ON TABLE tc_phase_logs IS 'TC 相位切換時序記錄';
COMMENT ON COLUMN tc_phase_logs.is_oversaturated IS '過飽和：需求超過容量，綠燈結束仍有車輛等待';

-- =====================
-- Layer 3d: CCTV
-- =====================

CREATE TABLE cctv_detail (
    device_id             INT PRIMARY KEY REFERENCES devices(device_id),
    resolution            VARCHAR(20) NOT NULL DEFAULT '1920x1080',
    fps                   INT         NOT NULL DEFAULT 30,
    has_ptz               BOOLEAN     NOT NULL DEFAULT FALSE,
    has_ai_analysis       BOOLEAN     NOT NULL DEFAULT FALSE,
    coverage_angle_deg    INT,
    storage_days          INT         NOT NULL DEFAULT 30
);

COMMENT ON TABLE cctv_detail IS 'CCTV 設備規格，1:1 對應 devices';

CREATE TABLE cctv_events (
    event_id        BIGSERIAL PRIMARY KEY,
    device_id       INT         NOT NULL REFERENCES devices(device_id),
    detected_at     TIMESTAMP   NOT NULL,
    event_type      VARCHAR(30) NOT NULL
                    CHECK (event_type IN ('wrong_way','stopped_vehicle','crowd','jaywalking','accident')),
    confidence_pct  INT         NOT NULL CHECK (confidence_pct BETWEEN 0 AND 100),
    is_confirmed    BOOLEAN     NOT NULL DEFAULT FALSE,
    snapshot_url    TEXT
);

COMMENT ON TABLE cctv_events IS 'CCTV AI 或人工偵測到的交通事件';

-- =====================
-- Layer 3e: eTag
-- =====================

CREATE TABLE etag_detail (
    device_id      INT PRIMARY KEY REFERENCES devices(device_id),
    antenna_count  INT         NOT NULL DEFAULT 2,
    read_range_m   INT         NOT NULL DEFAULT 10,
    lane_binding   VARCHAR(20) NOT NULL DEFAULT 'all'
);

COMMENT ON TABLE etag_detail IS 'eTag 讀取器規格，1:1 對應 devices';

CREATE TABLE etag_readings (
    reading_id        BIGSERIAL PRIMARY KEY,
    device_id         INT         NOT NULL REFERENCES devices(device_id),
    read_at           TIMESTAMP   NOT NULL,
    vehicle_class     VARCHAR(20) NOT NULL CHECK (vehicle_class IN ('car','truck','bus','motorcycle')),
    travel_time_sec   INT,
    origin_device_id  INT         REFERENCES devices(device_id)
);

COMMENT ON TABLE etag_readings IS 'eTag 讀取記錄，含旅行時間';

-- =====================
-- Cross-device: Incidents & Maintenance
-- =====================

CREATE TABLE incidents (
    incident_id              SERIAL PRIMARY KEY,
    intersection_id          INT          NOT NULL REFERENCES intersections(intersection_id),
    detected_by_device_id    INT          REFERENCES devices(device_id),
    incident_type            VARCHAR(30)  NOT NULL
                             CHECK (incident_type IN ('accident','congestion','road_block','vd_anomaly','equipment_fault','special_event')),
    severity                 VARCHAR(20)  NOT NULL CHECK (severity IN ('low','medium','high','critical')),
    occurred_at              TIMESTAMP    NOT NULL,
    resolved_at              TIMESTAMP,
    affected_directions      VARCHAR(20)[],
    description              TEXT
);

COMMENT ON TABLE incidents IS '跨設備交通事件彙整；resolved_at IS NULL 表示未解除';
COMMENT ON COLUMN incidents.severity IS 'low<medium<high<critical';

CREATE TABLE maintenance_records (
    record_id         SERIAL PRIMARY KEY,
    device_id         INT          NOT NULL REFERENCES devices(device_id),
    work_date         DATE         NOT NULL,
    work_type         VARCHAR(30)  NOT NULL
                      CHECK (work_type IN ('inspection','repair','firmware_update','replacement','calibration')),
    technician        VARCHAR(100),
    cost_ntd          DECIMAL(10,2),
    downtime_minutes  INT          NOT NULL DEFAULT 0,
    notes             TEXT
);

COMMENT ON TABLE maintenance_records IS '設備維護記錄';

-- =====================
-- Indexes
-- =====================

-- vd_readings: 最常見的查詢模式
CREATE INDEX idx_vd_readings_device_time    ON vd_readings    (device_id, recorded_at DESC);
CREATE INDEX idx_vd_readings_time           ON vd_readings    (recorded_at DESC);
CREATE INDEX idx_vd_readings_congestion     ON vd_readings    (congestion_level, recorded_at DESC);

-- cms_messages
CREATE INDEX idx_cms_messages_device_time   ON cms_messages   (device_id, displayed_at DESC);
CREATE INDEX idx_cms_messages_active        ON cms_messages   (removed_at) WHERE removed_at IS NULL;

-- tc_phase_logs
CREATE INDEX idx_tc_phase_device_time       ON tc_phase_logs  (device_id, recorded_at DESC);
CREATE INDEX idx_tc_phase_oversaturated     ON tc_phase_logs  (is_oversaturated, recorded_at DESC);

-- cctv_events
CREATE INDEX idx_cctv_events_device_time    ON cctv_events    (device_id, detected_at DESC);
CREATE INDEX idx_cctv_events_type           ON cctv_events    (event_type, detected_at DESC);

-- etag_readings
CREATE INDEX idx_etag_readings_device_time  ON etag_readings  (device_id, read_at DESC);

-- incidents
CREATE INDEX idx_incidents_intersection     ON incidents      (intersection_id, occurred_at DESC);
CREATE INDEX idx_incidents_unresolved       ON incidents      (resolved_at) WHERE resolved_at IS NULL;

-- devices
CREATE INDEX idx_devices_intersection       ON devices        (intersection_id);
CREATE INDEX idx_devices_type_status        ON devices        (device_type, status);

-- maintenance_records
CREATE INDEX idx_maintenance_device         ON maintenance_records (device_id, work_date DESC);
