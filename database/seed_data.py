"""
Traffic Management System - Mock Data Generator
Usage:
    python seed_data.py --scale small    # ~26,000 rows (default)
    python seed_data.py --scale medium   # ~200,000 rows
    python seed_data.py --scale large    # ~1,000,000 rows
    python seed_data.py --host localhost --port 5432 --dbname traffic_db \
                        --user traffic_user --password changeme
"""
import argparse
import random
import sys
from datetime import datetime, timedelta, date
from decimal import Decimal

import psycopg2
from psycopg2.extras import execute_values

# ── Taipei intersection data ────────────────────────────────────────────────
INTERSECTIONS = [
    ("中山北路/南京東路口",   "中山區", "city",   25.052600, 121.524800),
    ("忠孝東路/敦化南路口",   "大安區", "city",   25.041200, 121.551400),
    ("信義路/復興南路口",     "大安區", "city",   25.034900, 121.543700),
    ("民權東路/松江路口",     "中山區", "city",   25.063400, 121.533200),
    ("建國北路/八德路口",     "松山區", "city",   25.048700, 121.541600),
    ("基隆路/仁愛路口",       "大安區", "city",   25.031500, 121.558900),
    ("羅斯福路/和平東路口",   "大安區", "city",   25.021800, 121.534200),
    ("天母東路/中山北路口",   "士林區", "city",   25.106700, 121.525300),
    ("內湖路/民權大道口",     "內湖區", "city",   25.073400, 121.587200),
    ("文山路/景美路口",       "文山區", "city",   24.989600, 121.551200),
    ("板橋中山路/府中路口",   "板橋區", "county", 25.013500, 121.460200),
    ("新店中正路/北宜路口",   "新店區", "county", 24.965400, 121.537800),
    ("三重重新路/中山路口",   "三重區", "county", 25.064800, 121.487600),
    ("新莊中正路/龍安街口",   "新莊區", "county", 25.029700, 121.443500),
    ("土城金城路/學府路口",   "土城區", "county", 24.991200, 121.437600),
]

DISTRICTS = list({x[1] for x in INTERSECTIONS})

VENDORS = ["Siemens", "Kapsch", "Iteris", "Econolite", "Q-Free",
           "TransCore", "Aldridge", "SWARCO", "瑞昱科技", "中菱工程"]

TECHNICIANS = ["王大明", "李小華", "陳建志", "林美玲", "黃文豪",
               "張雅惠", "吳志遠", "劉俊賢", "許雅婷", "蔡松澤"]


# ── Scale parameters ────────────────────────────────────────────────────────
SCALE_PARAMS = {
    "small":  {"days": 3,  "vd": 20, "cms": 10, "tc": 15, "cctv": 10, "etag": 5},
    "medium": {"days": 14, "vd": 40, "cms": 20, "tc": 30, "cctv": 20, "etag": 10},
    "large":  {"days": 60, "vd": 80, "cms": 40, "tc": 60, "cctv": 40, "etag": 20},
}


# ── Helpers ──────────────────────────────────────────────────────────────────
def rand_ip():
    return f"192.168.{random.randint(1, 10)}.{random.randint(1, 254)}"


def rand_ts(start: datetime, end: datetime) -> datetime:
    delta = end - start
    return start + timedelta(seconds=random.randint(0, int(delta.total_seconds())))


def is_peak(ts: datetime) -> bool:
    """工作日 07-09 or 17-19"""
    if ts.weekday() >= 5:
        return False
    return (7 <= ts.hour < 9) or (17 <= ts.hour < 19)


def is_offpeak(ts: datetime) -> bool:
    if ts.weekday() >= 5:
        return False
    return (10 <= ts.hour < 16) or (19 <= ts.hour < 23)


def congestion_from_speed_occ(speed: float, occ: float) -> str:
    if occ > 85 or speed < 10:
        return "jam"
    if occ > 65 or speed < 25:
        return "heavy"
    if occ > 40 or speed < 45:
        return "moderate"
    return "free"


def batch_insert(cur, table: str, cols: list[str], rows: list[tuple], page: int = 2000):
    sql = f"INSERT INTO {table} ({','.join(cols)}) VALUES %s"
    for i in range(0, len(rows), page):
        execute_values(cur, sql, rows[i:i + page])


# ── Seeder class ─────────────────────────────────────────────────────────────
class Seeder:
    def __init__(self, conn, scale: str):
        self.conn = conn
        self.cur = conn.cursor()
        p = SCALE_PARAMS[scale]
        self.days = p["days"]
        self.n_vd   = p["vd"]
        self.n_cms  = p["cms"]
        self.n_tc   = p["tc"]
        self.n_cctv = p["cctv"]
        self.n_etag = p["etag"]

        self.now = datetime.now().replace(microsecond=0)
        self.start_dt = self.now - timedelta(days=self.days)

        self.intersection_ids: list[int] = []
        self.device_ids: dict[str, list[int]] = {t: [] for t in ("VD","CMS","TC","CCTV","eTag")}

    # ── intersections ────────────────────────────────────────────────────────
    def seed_intersections(self):
        print("  inserting intersections...")
        rows = [(name, dist, rc, lat, lng, random.randint(3, 4), self.now)
                for name, dist, rc, lat, lng in INTERSECTIONS]
        batch_insert(self.cur, "intersections",
                     ["name","district","road_class","lat","lng","total_approaches","created_at"],
                     rows)
        self.cur.execute("SELECT intersection_id FROM intersections ORDER BY intersection_id")
        self.intersection_ids = [r[0] for r in self.cur.fetchall()]

    # ── devices + detail tables ───────────────────────────────────────────────
    def _make_device(self, dtype: str, seq: int, iid: int) -> tuple:
        """Returns a device row tuple (without device_id)."""
        code = f"{dtype}-{seq:03d}-{random.choice('NSEW')}"
        status = random.choices(
            ["active", "fault", "maintenance", "offline"],
            weights=[78, 10, 8, 4]
        )[0]
        install = date(2020, 1, 1) + timedelta(days=random.randint(0, 1500))
        hb = self.now - timedelta(minutes=random.randint(0, 60)) if status == "active" else None
        return (code, iid, dtype,
                random.choice(["Model-A","Model-B","Model-C"]),
                random.choice(VENDORS),
                install, rand_ip(), status, hb)

    def seed_devices(self):
        print("  inserting devices + detail tables...")
        int_cycle = list(range(len(self.intersection_ids)))
        dev_cols = ["device_code","intersection_id","device_type",
                    "model","vendor","install_date","ip_address","status","last_heartbeat"]

        for dtype, count, detail_fn in [
            ("VD",   self.n_vd,   self._vd_detail),
            ("CMS",  self.n_cms,  self._cms_detail),
            ("TC",   self.n_tc,   self._tc_detail),
            ("CCTV", self.n_cctv, self._cctv_detail),
            ("eTag", self.n_etag, self._etag_detail),
        ]:
            dev_rows = []
            for i in range(count):
                iid = self.intersection_ids[int_cycle[i % len(int_cycle)]]
                dev_rows.append(self._make_device(dtype, i + 1, iid))

            execute_values(self.cur,
                f"INSERT INTO devices ({','.join(dev_cols)}) VALUES %s RETURNING device_id",
                dev_rows)
            ids = [r[0] for r in self.cur.fetchall()]
            self.device_ids[dtype] = ids
            detail_fn(ids)

    def _vd_detail(self, ids: list[int]):
        rows = [(did,
                 random.choice(["loop","radar","video","microwave"]),
                 random.randint(2, 4),
                 random.choice(["N","S","E","W"]),
                 round(random.uniform(10, 30), 2),
                 True)
                for did in ids]
        batch_insert(self.cur, "vd_detail",
                     ["device_id","detection_method","lane_count",
                      "approach_direction","detection_length_m","supports_vehicle_class"], rows)

    def _cms_detail(self, ids: list[int]):
        rows = [(did,
                 random.choice(["LED","LCD","fiber_optic"]),
                 random.randint(2, 4),
                 random.randint(12, 20),
                 random.choice([True, False]),
                 random.choice(["overhead","roadside","portal"]))
                for did in ids]
        batch_insert(self.cur, "cms_detail",
                     ["device_id","sign_type","display_rows","display_cols",
                      "supports_graphics","mounting_type"], rows)

    def _tc_detail(self, ids: list[int]):
        rows = [(did,
                 random.choice(["fixed","actuated","adaptive"]),
                 random.randint(4, 8),
                 random.choice([True, False]),
                 f"COR-{random.randint(1,5):02d}" if random.random() > 0.4 else None,
                 random.randint(2, 6))
                for did in ids]
        batch_insert(self.cur, "tc_detail",
                     ["device_id","controller_type","phase_count","has_ats",
                      "coordination_group","cycle_plan_count"], rows)

    def _cctv_detail(self, ids: list[int]):
        rows = [(did,
                 random.choice(["1920x1080","2560x1440","3840x2160"]),
                 random.choice([25, 30, 60]),
                 random.choice([True, False]),
                 random.random() > 0.4,
                 random.randint(90, 270),
                 random.choice([14, 30, 60]))
                for did in ids]
        batch_insert(self.cur, "cctv_detail",
                     ["device_id","resolution","fps","has_ptz","has_ai_analysis",
                      "coverage_angle_deg","storage_days"], rows)

    def _etag_detail(self, ids: list[int]):
        rows = [(did,
                 random.choice([1, 2, 4]),
                 random.choice([6, 8, 10, 12]),
                 random.choice(["all","lane1","lane2"]))
                for did in ids]
        batch_insert(self.cur, "etag_detail",
                     ["device_id","antenna_count","read_range_m","lane_binding"], rows)

    # ── vd_readings ──────────────────────────────────────────────────────────
    def seed_vd_readings(self):
        print("  inserting vd_readings...")
        # fetch lane_count and status per device
        self.cur.execute(
            "SELECT d.device_id, d.status, v.lane_count "
            "FROM devices d JOIN vd_detail v USING (device_id) "
            "WHERE d.device_type = 'VD'"
        )
        vd_meta = {r[0]: (r[1], r[2]) for r in self.cur.fetchall()}

        rows = []
        interval = timedelta(minutes=5)
        cursor_dt = self.start_dt.replace(second=0, microsecond=0)

        while cursor_dt <= self.now:
            peak = is_peak(cursor_dt)
            for did, (status, lanes) in vd_meta.items():
                # fault devices have data gaps
                if status == "fault" and random.random() < 0.6:
                    continue
                if status == "offline":
                    continue
                for lane in range(1, lanes + 1):
                    if peak:
                        speed = random.uniform(5, 40)
                        occ   = random.uniform(50, 95)
                        total = random.randint(30, 80)
                    elif is_offpeak(cursor_dt):
                        speed = random.uniform(35, 70)
                        occ   = random.uniform(10, 45)
                        total = random.randint(8, 30)
                    else:
                        speed = random.uniform(40, 80)
                        occ   = random.uniform(5, 25)
                        total = random.randint(2, 15)

                    # motorcycle ratio higher in city intersections
                    moto_ratio = random.uniform(0.25, 0.55)
                    truck_ratio = random.uniform(0.05, 0.15)
                    moto  = int(total * moto_ratio)
                    truck = int(total * truck_ratio)
                    car   = max(0, total - moto - truck)
                    cong  = congestion_from_speed_occ(speed, occ)

                    rows.append((did, cursor_dt, lane, total,
                                 round(speed, 2), round(occ, 2),
                                 cong, car, truck, moto))
            cursor_dt += interval

        batch_insert(self.cur, "vd_readings",
                     ["device_id","recorded_at","lane_no","vehicle_count",
                      "avg_speed_kmh","occupancy_pct","congestion_level",
                      "car_count","truck_count","motorcycle_count"], rows)
        print(f"    → {len(rows):,} rows")

    # ── cms_messages ─────────────────────────────────────────────────────────
    def seed_cms_messages(self):
        print("  inserting cms_messages...")
        rows = []
        for did in self.device_ids["CMS"]:
            n_msgs = random.randint(8, 25)
            for _ in range(n_msgs):
                disp = rand_ts(self.start_dt, self.now - timedelta(hours=1))
                # 15% still displaying
                rem = None if random.random() < 0.15 else disp + timedelta(hours=random.uniform(0.5, 4))
                mtype = random.choices(
                    ["info", "warning", "alert", "event"],
                    weights=[30, 30, 25, 15]
                )[0]
                contents = {
                    "info":    ["前方施工，請減速慢行", "道路維修，請繞道行駛", "感謝您遵守交通規則"],
                    "warning": ["前方壅塞，車速 20 km/h", "路面濕滑，請注意安全", "前方事故，請保持距離"],
                    "alert":   ["緊急！前方道路封閉", "重大事故，請改道", "超強颱風警報，非必要勿外出"],
                    "event":   ["馬拉松活動，部分道路管制", "跨年活動交通管制", "演唱會疏導中"],
                }
                triggered = random.choices(
                    ["manual", "auto_incident", "schedule"],
                    weights=[40, 40, 20]
                )[0]
                rows.append((did, disp, rem, random.choice(contents[mtype]), mtype, triggered))

        batch_insert(self.cur, "cms_messages",
                     ["device_id","displayed_at","removed_at",
                      "message_content","message_type","triggered_by"], rows)
        print(f"    → {len(rows):,} rows")

    # ── tc_phase_logs ─────────────────────────────────────────────────────────
    def seed_tc_phase_logs(self):
        print("  inserting tc_phase_logs...")
        self.cur.execute(
            "SELECT d.device_id, d.status, t.phase_count "
            "FROM devices d JOIN tc_detail t USING (device_id) "
            "WHERE d.device_type = 'TC'"
        )
        tc_meta = {r[0]: (r[1], r[2]) for r in self.cur.fetchall()}

        rows = []
        interval = timedelta(seconds=120)
        cursor_dt = self.start_dt.replace(second=0, microsecond=0)

        while cursor_dt <= self.now:
            peak = is_peak(cursor_dt)
            for did, (status, phase_count) in tc_meta.items():
                if status == "offline":
                    continue
                if status == "fault" and random.random() < 0.5:
                    continue
                for phase in range(1, phase_count + 1):
                    if peak:
                        green = random.randint(20, 45)
                        red   = random.randint(40, 90)
                        oversat = random.random() < 0.35
                    else:
                        green = random.randint(25, 60)
                        red   = random.randint(20, 50)
                        oversat = random.random() < 0.05
                    cycle = green + red + random.randint(3, 5)
                    rows.append((did, cursor_dt, phase, green, red, cycle, oversat))
            cursor_dt += interval

        batch_insert(self.cur, "tc_phase_logs",
                     ["device_id","recorded_at","phase_no",
                      "green_duration_sec","red_duration_sec",
                      "cycle_length_sec","is_oversaturated"], rows)
        print(f"    → {len(rows):,} rows")

    # ── cctv_events ───────────────────────────────────────────────────────────
    def seed_cctv_events(self):
        print("  inserting cctv_events...")
        rows = []
        for did in self.device_ids["CCTV"]:
            n_events = random.randint(15, 40)
            for _ in range(n_events):
                det = rand_ts(self.start_dt, self.now)
                etype = random.choices(
                    ["wrong_way","stopped_vehicle","crowd","jaywalking","accident"],
                    weights=[5, 35, 20, 30, 10]
                )[0]
                conf = random.randint(60, 100)
                confirmed = conf >= 80 and random.random() > 0.3
                snap = f"/snapshots/{did}/{det.strftime('%Y%m%d_%H%M%S')}.jpg"
                rows.append((did, det, etype, conf, confirmed, snap))

        batch_insert(self.cur, "cctv_events",
                     ["device_id","detected_at","event_type",
                      "confidence_pct","is_confirmed","snapshot_url"], rows)
        print(f"    → {len(rows):,} rows")

    # ── etag_readings ─────────────────────────────────────────────────────────
    def seed_etag_readings(self):
        print("  inserting etag_readings...")
        rows = []
        etag_ids = self.device_ids["eTag"]
        for did in etag_ids:
            n = int(5000 / max(len(etag_ids), 1))
            for _ in range(n):
                read_at = rand_ts(self.start_dt, self.now)
                vclass = random.choices(
                    ["car","truck","bus","motorcycle"],
                    weights=[55, 15, 10, 20]
                )[0]
                travel = random.randint(60, 900) if random.random() > 0.1 else None
                origin = random.choice(etag_ids) if len(etag_ids) > 1 and random.random() > 0.3 else None
                if origin == did:
                    origin = None
                rows.append((did, read_at, vclass, travel, origin))

        batch_insert(self.cur, "etag_readings",
                     ["device_id","read_at","vehicle_class",
                      "travel_time_sec","origin_device_id"], rows)
        print(f"    → {len(rows):,} rows")

    # ── incidents ─────────────────────────────────────────────────────────────
    def seed_incidents(self):
        print("  inserting incidents...")
        all_devices = [d for dlist in self.device_ids.values() for d in dlist]
        n = int(120 * self.days / 3)
        rows = []
        for _ in range(n):
            iid = random.choice(self.intersection_ids)
            itype = random.choices(
                ["accident","congestion","road_block","vd_anomaly","equipment_fault","special_event"],
                weights=[15, 40, 10, 15, 10, 10]
            )[0]
            sev = random.choices(
                ["low","medium","high","critical"],
                weights=[30, 40, 20, 10]
            )[0]
            occ = rand_ts(self.start_dt - timedelta(days=28), self.now)
            # 20% still unresolved
            res = None if random.random() < 0.2 else occ + timedelta(minutes=random.randint(15, 240))
            dirs = random.sample(["N","S","E","W"], k=random.randint(1, 3))
            detby = random.choice(all_devices) if random.random() > 0.2 else None
            desc_map = {
                "accident":       "交通事故，已通知警方處理",
                "congestion":     "交通壅塞，車速低於 20 km/h",
                "road_block":     "道路封閉，維修工程進行中",
                "vd_anomaly":     "VD 偵測異常，資料可能不準確",
                "equipment_fault":"設備故障，已派技師處理",
                "special_event":  "特殊活動交通管制",
            }
            rows.append((iid, detby, itype, sev, occ, res, dirs, desc_map[itype]))

        batch_insert(self.cur, "incidents",
                     ["intersection_id","detected_by_device_id","incident_type",
                      "severity","occurred_at","resolved_at",
                      "affected_directions","description"], rows)
        print(f"    → {len(rows):,} rows")

    # ── maintenance_records ───────────────────────────────────────────────────
    def seed_maintenance(self):
        print("  inserting maintenance_records...")
        all_devices = [d for dlist in self.device_ids.values() for d in dlist]
        n = int(80 * self.days / 3)
        rows = []
        for _ in range(n):
            did = random.choice(all_devices)
            wdate = (self.now - timedelta(days=random.randint(0, 90))).date()
            wtype = random.choices(
                ["inspection","repair","firmware_update","replacement","calibration"],
                weights=[35, 25, 20, 10, 10]
            )[0]
            cost_map = {
                "inspection":     (500,  3000),
                "repair":         (2000, 20000),
                "firmware_update":(300,  1500),
                "replacement":    (5000, 50000),
                "calibration":    (800,  3500),
            }
            lo, hi = cost_map[wtype]
            cost = round(random.uniform(lo, hi), 2)
            downtime = 0 if wtype == "firmware_update" else random.randint(15, 480)
            notes_map = {
                "inspection":     "例行巡查，設備正常",
                "repair":         "元件損壞，已完成更換",
                "firmware_update":"韌體升級至最新版本",
                "replacement":    "設備老化，全機更換",
                "calibration":    "感測器校正完成",
            }
            rows.append((did, wdate, wtype, random.choice(TECHNICIANS),
                         cost, downtime, notes_map[wtype]))

        batch_insert(self.cur, "maintenance_records",
                     ["device_id","work_date","work_type","technician",
                      "cost_ntd","downtime_minutes","notes"], rows)
        print(f"    → {len(rows):,} rows")

    # ── run all ───────────────────────────────────────────────────────────────
    def run(self):
        steps = [
            self.seed_intersections,
            self.seed_devices,
            self.seed_vd_readings,
            self.seed_cms_messages,
            self.seed_tc_phase_logs,
            self.seed_cctv_events,
            self.seed_etag_readings,
            self.seed_incidents,
            self.seed_maintenance,
        ]
        for step in steps:
            step()
            self.conn.commit()
        print("\nAll seed data committed.")


# ── Main ─────────────────────────────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser(description="Seed traffic_db with mock data")
    p.add_argument("--scale",    choices=["small","medium","large"], default="small")
    p.add_argument("--host",     default="localhost")
    p.add_argument("--port",     type=int, default=5432)
    p.add_argument("--dbname",   default="traffic_db")
    p.add_argument("--user",     default="traffic_user")
    p.add_argument("--password", default="changeme")
    return p.parse_args()


def main():
    args = parse_args()
    print(f"Connecting to {args.host}:{args.port}/{args.dbname} ...")
    try:
        conn = psycopg2.connect(
            host=args.host, port=args.port, dbname=args.dbname,
            user=args.user, password=args.password
        )
    except psycopg2.OperationalError as e:
        print(f"Connection failed: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Scale: {args.scale}")
    seeder = Seeder(conn, args.scale)
    try:
        seeder.run()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
