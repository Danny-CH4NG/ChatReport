# PRD — Chat 型式調用 PostgreSQL 資料 PoC
> 場景：交通管理系統（多路口 × 多設備類型 × 時間區間複雜查詢）
> 版本：v0.3｜日期：2026-05-27
> 變更：設備類型專業化（VD/CMS/TC/CCTV/eTag）、資料表改為基底+子表+時序三層繼承架構、移除 pgAdmin、所有待討事項結案、schema_context 更新

---

## 1. 專案目標

| 目標 | 說明 |
|------|------|
| **核心功能** | 使用者以自然語言提問 → AI Agent 解析為 SQL → 查詢 PostgreSQL → 匯出 Excel |
| **PoC 範圍** | 本機可執行、Docker Compose 一鍵啟動、可替換真實資料表 |
| **驗收條件** | 能正確回答至少 5 類跨表查詢（路口流量、設備異常、時段統計等），並匯出格式正確的 .xlsx |

---

## 2. 系統架構

```
┌─────────────────────────────────────────────────────────────┐
│                      Docker Compose                         │
│                                                             │
│  ┌──────────────┐    HTTP     ┌──────────────────────────┐  │
│  │   Chainlit   │◄──────────►│    FastAPI Backend       │  │
│  │  (Chat UI)   │            │  (Agent Orchestrator)    │  │
│  │  Port: 8000  │            │  Port: 8080              │  │
│  └──────────────┘            └──────────┬───────────────┘  │
│                                         │ MCP Protocol      │
│                              ┌──────────▼───────────────┐  │
│                              │   PostgreSQL MCP Server  │  │
│                              │   (stdio / SSE)          │  │
│                              └──────────┬───────────────┘  │
│                                         │ psycopg2          │
│                              ┌──────────▼───────────────┐  │
│                              │     PostgreSQL 16        │  │
│                              │   Port: 5432             │  │
│                              │   traffic_db             │  │
│                              └──────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘

Agent Flow（每次查詢）：
User NL ──► [Schema Discovery] ──► [SQL Generation] ──► [Query Execution] ──► [Excel Export]
              (MCP: list/describe)    (Claude Tool Use)    (MCP: execute)       (openpyxl)
```

### 2.1 資料流說明

1. 使用者在 Chainlit 輸入自然語言（中文/英文皆可）
2. Chainlit 將訊息送至 FastAPI Backend
3. Backend 啟動 Claude Agent，Agent 依序呼叫 MCP Tools：
   - `list_tables` → 取得資料表清單
   - `describe_table(table_name)` → 取得欄位定義與 schema 注解
   - `execute_query(sql)` → 執行生成的 SELECT SQL
4. Agent 將結果彙整，Chainlit 顯示摘要 + 提供 Excel 下載按鈕
5. 使用者點選下載，取得 .xlsx 報表

---

## 3. 技術選型

| 層級 | 選用技術 | 理由 |
|------|----------|------|
| **Chat UI** | [Chainlit](https://chainlit.io) | Python native，3 行啟動 chat UI，原生支援 file download、streaming、Action Button |
| **Agent 框架** | Anthropic Python SDK（原生 Tool Use） | 最輕量，無 LangChain 複雜度，直接控制 tool call loop |
| **LLM** | `claude-sonnet-4-5` | 成本/能力平衡最佳，Tool Use 穩定 |
| **MCP Server** | 自製 Python MCP Server（`mcp` SDK） | 完整控制 schema 注解注入、SQL 白名單過濾 |
| **資料庫** | PostgreSQL 16（Docker） | 業界標準，pgAdmin 可視化輔助開發 |
| **Excel 匯出** | openpyxl | 輕量、不依賴 Excel 安裝、支援格式/顏色/凍結欄 |
| **容器化** | Docker Compose v2 | 一個指令啟動全部服務 |
| **配置注入** | YAML 外部 schema context 檔案 | 讓 AI 理解業務術語，且不硬編碼資料庫結構 |

---

## 4. 資料表設計（交通管理場景）

### 4.1 設計原則：設備類型繼承架構

交通設備（VD / CMS / TC / CCTV / eTag）彼此有**共通欄位**（安裝位置、廠商、狀態）也有**各自專屬欄位與量測資料**。採用「基底表 + 設備型號子表 + 各自時序資料表」三層設計：

```
                      intersections（路口）
                            │ 1:N
                         devices（設備基底）
                ┌──────┬──────┬──────┬──────┐
                │      │      │      │      │
           vd_  cms_  tc_  cctv_ etag_      ← 設備規格子表（1:1）
           detail detail detail detail detail
                │      │      │      │
           vd_  cms_  tc_  cctv_              ← 各設備時序資料表（1:N）
          readings msgs cycles events
                            │
                       incidents（跨設備事件）
                            │
                    maintenance_records（維護記錄）
```

**核心優勢**：使用者可查詢「同一路口下，VD 偵測到的壅塞時段 vs TC 對應的綠燈配時」等跨設備類型的複雜組合。

---

### 4.2 設備類型說明

| 代號 | 全名 | 中文 | 主要功能 |
|------|------|------|----------|
| **VD** | Vehicle Detector | 車輛偵測器 | 偵測車流量、速度、佔有率，每 5 分鐘回報 |
| **CMS** | Changeable Message Sign | 可變資訊標誌 | 動態顯示路況訊息、施工警告、緊急通知 |
| **TC** | Traffic Controller | 交通控制器 | 控制路口號誌相位與時制計畫 |
| **CCTV** | Closed-Circuit Television | 閉路電視 | 影像監控，可觸發事件偵測 |
| **eTag** | Electronic Tag Reader | 電子標籤讀取器 | 讀取 eTag 計算旅行時間與路徑 |

---

### 4.3 資料表定義

#### `intersections`（路口）
| 欄位 | 型別 | 說明 |
|------|------|------|
| intersection_id | SERIAL PK | 路口編號 |
| name | VARCHAR(100) | 路口名稱（如「中山北路/南京東路口」） |
| district | VARCHAR(50) | 行政區 |
| road_class | VARCHAR(20) | 道路等級：`national`/`provincial`/`county`/`city` |
| lat | DECIMAL(9,6) | 緯度 |
| lng | DECIMAL(9,6) | 經度 |
| total_approaches | INT | 進口道數（通常 3-4） |
| created_at | TIMESTAMP | 建立時間 |

---

#### `devices`（設備基底表）
共通欄位，所有設備類型共享。

| 欄位 | 型別 | 說明 |
|------|------|------|
| device_id | SERIAL PK | 設備 ID |
| device_code | VARCHAR(30) UNIQUE | 設備代碼（如 `VD-001-N`） |
| intersection_id | INT FK | 所屬路口 |
| device_type | VARCHAR(10) | `VD`/`CMS`/`TC`/`CCTV`/`eTag` |
| model | VARCHAR(100) | 設備型號 |
| vendor | VARCHAR(100) | 廠商 |
| install_date | DATE | 安裝日期 |
| ip_address | INET | 設備 IP |
| status | VARCHAR(20) | `active`/`fault`/`maintenance`/`offline` |
| last_heartbeat | TIMESTAMP | 最後心跳時間 |

---

#### `vd_detail`（VD 設備規格，1:1 對應 devices）
| 欄位 | 型別 | 說明 |
|------|------|------|
| device_id | INT PK FK | 對應 devices |
| detection_method | VARCHAR(20) | `loop`/`radar`/`video`/`microwave` |
| lane_count | INT | 監測車道數 |
| approach_direction | VARCHAR(10) | 進口方向：`N`/`S`/`E`/`W` |
| detection_length_m | DECIMAL(5,2) | 偵測區長度（公尺） |
| supports_vehicle_class | BOOLEAN | 是否支援車種分類 |

#### `vd_readings`（VD 時序資料，1:N）
| 欄位 | 型別 | 說明 |
|------|------|------|
| reading_id | BIGSERIAL PK | 記錄 ID |
| device_id | INT FK | 來源 VD |
| recorded_at | TIMESTAMP | 記錄時間（每 5 分鐘） |
| lane_no | INT | 車道編號 |
| vehicle_count | INT | 車輛數 |
| avg_speed_kmh | DECIMAL(5,2) | 平均速度 |
| occupancy_pct | DECIMAL(5,2) | 佔有率（%） |
| congestion_level | VARCHAR(10) | `free`/`moderate`/`heavy`/`jam` |
| car_count | INT | 小客車數 |
| truck_count | INT | 大型車數 |
| motorcycle_count | INT | 機車數 |

---

#### `cms_detail`（CMS 設備規格，1:1）
| 欄位 | 型別 | 說明 |
|------|------|------|
| device_id | INT PK FK | 對應 devices |
| sign_type | VARCHAR(20) | `LED`/`LCD`/`fiber_optic` |
| display_rows | INT | 顯示行數 |
| display_cols | INT | 每行字元數 |
| supports_graphics | BOOLEAN | 是否支援圖形顯示 |
| mounting_type | VARCHAR(20) | `overhead`/`roadside`/`portal` |

#### `cms_messages`（CMS 顯示紀錄，1:N）
| 欄位 | 型別 | 說明 |
|------|------|------|
| message_id | BIGSERIAL PK | 記錄 ID |
| device_id | INT FK | 來源 CMS |
| displayed_at | TIMESTAMP | 開始顯示時間 |
| removed_at | TIMESTAMP | 結束顯示時間（NULL=目前仍顯示） |
| message_content | TEXT | 顯示文字內容 |
| message_type | VARCHAR(20) | `info`/`warning`/`alert`/`event` |
| triggered_by | VARCHAR(50) | 觸發來源：`manual`/`auto_incident`/`schedule` |

---

#### `tc_detail`（TC 設備規格，1:1）
| 欄位 | 型別 | 說明 |
|------|------|------|
| device_id | INT PK FK | 對應 devices |
| controller_type | VARCHAR(20) | `fixed`/`actuated`/`adaptive` |
| phase_count | INT | 相位數 |
| has_ats | BOOLEAN | 是否接入區域交控系統 |
| coordination_group | VARCHAR(20) | 幹線協調群組代號（可為空） |
| cycle_plan_count | INT | 時制計畫數量 |

#### `tc_phase_logs`（TC 相位紀錄，1:N）
| 欄位 | 型別 | 說明 |
|------|------|------|
| log_id | BIGSERIAL PK | 記錄 ID |
| device_id | INT FK | 來源 TC |
| recorded_at | TIMESTAMP | 記錄時間 |
| phase_no | INT | 相位編號 |
| green_duration_sec | INT | 實際綠燈秒數 |
| red_duration_sec | INT | 實際紅燈秒數 |
| cycle_length_sec | INT | 本次週期長度 |
| is_oversaturated | BOOLEAN | 是否發生過飽和 |

---

#### `cctv_detail`（CCTV 設備規格，1:1）
| 欄位 | 型別 | 說明 |
|------|------|------|
| device_id | INT PK FK | 對應 devices |
| resolution | VARCHAR(20) | 解析度（如 `1920x1080`） |
| fps | INT | 畫面率 |
| has_ptz | BOOLEAN | 是否支援雲台旋轉 |
| has_ai_analysis | BOOLEAN | 是否有 AI 事件偵測 |
| coverage_angle_deg | INT | 涵蓋角度 |
| storage_days | INT | 影像保存天數 |

#### `cctv_events`（CCTV 偵測事件，1:N）
| 欄位 | 型別 | 說明 |
|------|------|------|
| event_id | BIGSERIAL PK | 事件 ID |
| device_id | INT FK | 來源 CCTV |
| detected_at | TIMESTAMP | 偵測時間 |
| event_type | VARCHAR(30) | `wrong_way`/`stopped_vehicle`/`crowd`/`jaywalking`/`accident` |
| confidence_pct | INT | AI 信心度（%） |
| is_confirmed | BOOLEAN | 是否人工確認 |
| snapshot_url | TEXT | 截圖路徑（PoC 用假路徑） |

---

#### `etag_detail`（eTag 設備規格，1:1）
| 欄位 | 型別 | 說明 |
|------|------|------|
| device_id | INT PK FK | 對應 devices |
| antenna_count | INT | 天線數量 |
| read_range_m | INT | 讀取距離（公尺） |
| lane_binding | VARCHAR(20) | 綁定車道（`all`/`lane1`/`lane2`...） |

#### `etag_readings`（eTag 讀取紀錄，1:N）
| 欄位 | 型別 | 說明 |
|------|------|------|
| reading_id | BIGSERIAL PK | 讀取 ID |
| device_id | INT FK | 來源 eTag 讀取器 |
| read_at | TIMESTAMP | 讀取時間 |
| vehicle_class | VARCHAR(20) | 車種：`car`/`truck`/`bus`/`motorcycle` |
| travel_time_sec | INT | 旅行時間（秒，與前一點比較） |
| origin_device_id | INT FK | 旅行起點設備（可為空） |

---

#### `incidents`（跨設備交通事件）
| 欄位 | 型別 | 說明 |
|------|------|------|
| incident_id | SERIAL PK | 事件 ID |
| intersection_id | INT FK | 發生路口 |
| detected_by_device_id | INT FK | 最初偵測設備（可為空） |
| incident_type | VARCHAR(30) | `accident`/`congestion`/`road_block`/`vd_anomaly`/`equipment_fault`/`special_event` |
| severity | VARCHAR(20) | `low`/`medium`/`high`/`critical` |
| occurred_at | TIMESTAMP | 發生時間 |
| resolved_at | TIMESTAMP | 解除時間（NULL=未解除） |
| affected_directions | VARCHAR(20)[] | 受影響方向（PostgreSQL 陣列） |
| description | TEXT | 事件描述 |

#### `maintenance_records`（設備維護記錄）
| 欄位 | 型別 | 說明 |
|------|------|------|
| record_id | SERIAL PK | 記錄 ID |
| device_id | INT FK | 維護設備 |
| work_date | DATE | 維護日期 |
| work_type | VARCHAR(30) | `inspection`/`repair`/`firmware_update`/`replacement`/`calibration` |
| technician | VARCHAR(100) | 技師 |
| cost_ntd | DECIMAL(10,2) | 費用（新台幣） |
| downtime_minutes | INT | 停機時間（分鐘） |
| notes | TEXT | 備註 |

---

### 4.4 典型複雜查詢場景（體現多表組合價值）

| 查詢描述 | 需 JOIN 的資料表 |
|----------|----------------|
| 某路口 VD 偵測壅塞時，CMS 是否同步顯示警告？ | `vd_readings` + `cms_messages` + `devices` × 2 + `intersections` |
| TC 過飽和時段，對應的 VD 車流與旅行時間（eTag）？ | `tc_phase_logs` + `vd_readings` + `etag_readings` + `devices` × 3 |
| 有 AI 分析功能的 CCTV 偵測事件，哪些同時有 incidents 記錄？ | `cctv_events` + `cctv_detail` + `incidents` + `devices` |
| 各設備類型本季維護次數與費用，哪型號停機最久？ | `maintenance_records` + `devices` GROUP BY `device_type` |
| 早上尖峰機車流量最高的 VD 所在路口，目前設備狀態？ | `vd_readings` + `vd_detail` + `devices` + `intersections` |

---

### 4.5 Mock 資料規模（PoC 小量版）

| 資料表 | 筆數 | 備註 |
|--------|------|------|
| intersections | 15 個路口 | 涵蓋 4 個行政區 |
| devices | ~60 筆 | VD×20, CMS×10, TC×15, CCTV×10, eTag×5 |
| vd_detail / vd_readings | 20 設備 × 3 天 × 288 = ~17,280 筆 | 含車道 × 車種分類 |
| cms_detail / cms_messages | ~200 筆訊息紀錄 | 含各 message_type |
| tc_detail / tc_phase_logs | 15 設備 × 3 天 × 每 2 分鐘 = ~3,240 筆 | |
| cctv_detail / cctv_events | ~300 筆事件 | 含各 event_type，AI 信心度分布 |
| etag_detail / etag_readings | ~5,000 筆 | |
| incidents | ~120 筆 | 近 1 個月 |
| maintenance_records | ~80 筆 | 近 3 個月 |
| **合計** | **~26,000 筆** | PoC 驗證足夠，含複雜 JOIN |

> **seed_data.py 設計重點**：
> - 故障設備（status=fault）在相應時序表有「資料中斷」或「異常值」
> - VD 尖峰時段車流量高、機車佔比因路口特性而異
> - CMS 訊息與 incidents 有時間上的因果關聯（先有事件，後有 CMS 警告）
> - TC 過飽和（is_oversaturated=true）與 VD 壅塞時段有相關性
>
> 生成腳本支援 `--scale` 參數：
> ```bash
> python seed_data.py --scale small   # PoC：~26,000 筆（預設）
> python seed_data.py --scale medium  # 驗收：~200,000 筆
> python seed_data.py --scale large   # 壓測：~1,000,000 筆
> ```

---

## 5. MCP Server 設計

### 5.1 工具清單

```python
# mcp_server.py 提供以下 tools

@tool("list_tables")
def list_tables() -> list[str]:
    """列出資料庫中所有可查詢的資料表名稱"""

@tool("describe_table")
def describe_table(table_name: str) -> dict:
    """
    回傳資料表的欄位定義、型別、注解，以及外鍵關係。
    同時從 schema_context.yaml 注入業務語義說明。
    """

@tool("get_sample_rows")
def get_sample_rows(table_name: str, limit: int = 5) -> list[dict]:
    """取得資料表範例資料，協助 Agent 理解資料格式"""

@tool("execute_query")
def execute_query(sql: str) -> dict:
    """
    執行 SQL 查詢。
    安全限制：
      - 僅允許 SELECT（白名單檢查）
      - 自動加上 LIMIT 10000 防止爆量
      - 執行時間超過 30 秒自動 timeout
    回傳：{"columns": [...], "rows": [...], "row_count": int, "execution_ms": int}
    """

@tool("export_to_excel")
def export_to_excel(query_result: dict, filename: str, sheet_config: dict) -> str:
    """
    將查詢結果輸出為 .xlsx 檔案。
    支援 sheet_config：標題列、凍結欄、欄寬自動調整、條件格式（高危事件標紅）
    回傳檔案下載路徑
    """
```

### 5.2 安全機制（Hooks）

```
Pre-query Hook（SQL 執行前）：
  ├── SQL 語法解析：拒絕非 SELECT（INSERT/UPDATE/DELETE/DROP...）
  ├── Table 白名單比對：只允許查詢已登記的資料表
  └── 危險函數過濾：pg_sleep、dblink 等

Post-query Hook（SQL 執行後）：
  ├── 列數警告：超過 5000 列自動提醒使用者
  ├── 敏感欄位遮罩（可設定）
  └── 查詢 log 記錄（audit trail）
```

---

## 6. 彈性化機制（核心設計）

### 6.1 問題

不同部署場景（交通/電商/HR）資料表結構完全不同，Agent 需要理解業務術語，但不能硬編碼。

### 6.2 解法：外部 Schema Context 注入

```yaml
# schema_context.yaml — 可隨業務場景替換，開發者手動維護版本

version: "1.0.0"   # 與 schema.sql 版本對應，異動時同步更新

database:
  description: "台北市交通管理系統資料庫（PoC 版）"
  domain_vocabulary:
    路口: "查 intersections 資料表"
    VD / 車輛偵測器: "查 devices WHERE device_type='VD'，量測資料在 vd_readings"
    CMS / 可變資訊標誌: "查 devices WHERE device_type='CMS'，訊息記錄在 cms_messages"
    TC / 交通控制器 / 號誌: "查 devices WHERE device_type='TC'，相位紀錄在 tc_phase_logs"
    CCTV / 攝影機: "查 devices WHERE device_type='CCTV'，事件在 cctv_events"
    eTag / 電子標籤: "查 devices WHERE device_type='eTag'，讀取紀錄在 etag_readings"
    事件 / 事故: "查 incidents 資料表"
    維護 / 保養: "查 maintenance_records 資料表"
    尖峰時段: "工作日 07:00-09:00 及 17:00-19:00"
    離峰時段: "工作日 10:00-16:00 及 19:00-23:00"
    壅塞: "vd_readings.congestion_level IN ('heavy', 'jam')"
    故障設備: "devices.status IN ('fault', 'offline')"
    過飽和: "tc_phase_logs.is_oversaturated = true"

tables:
  devices:
    alias: "設備基底表"
    description: "所有設備共用的基礎資訊。查詢特定類型設備時搭配 WHERE device_type=... 過濾，再 JOIN 對應的 _detail 子表取規格欄位"
    join_pattern: "devices JOIN {type}_detail USING (device_id)"

  vd_readings:
    alias: "VD 車流量紀錄"
    description: "每 5 分鐘一筆，含車道、車速、佔有率、車種分類"
    query_tips:
      - "跨路口統計：JOIN devices → intersections"
      - "車種分類查詢：car_count / truck_count / motorcycle_count 欄位"
      - "壅塞判斷用 congestion_level 欄位，或用 occupancy_pct > 80 自定義"

  cms_messages:
    alias: "CMS 訊息紀錄"
    description: "記錄每次顯示內容及起訖時間。removed_at IS NULL 代表目前仍在顯示"
    common_patterns:
      - "目前顯示中：WHERE removed_at IS NULL"
      - "與 incidents 比對：用 displayed_at 與 incidents.occurred_at 做時間 JOIN"

  tc_phase_logs:
    alias: "TC 相位紀錄"
    description: "每次相位切換的綠燈/紅燈秒數記錄"
    common_patterns:
      - "過飽和：WHERE is_oversaturated = true"
      - "平均週期長度：AVG(cycle_length_sec) GROUP BY device_id, DATE_TRUNC('hour', recorded_at)"

  cctv_events:
    alias: "CCTV 偵測事件"
    description: "AI 或人工偵測的交通事件，is_confirmed=true 表示人工核認"

  etag_readings:
    alias: "eTag 旅行時間"
    description: "旅行時間計算需與 origin_device_id 對應的路口比較"

  incidents:
    alias: "交通事件"
    description: "跨設備來源的交通事件彙整，severity=critical 為最高等級"
    common_patterns:
      - "未解除事件：WHERE resolved_at IS NULL"
      - "平均處理時間：EXTRACT(EPOCH FROM (resolved_at - occurred_at))/60 AS minutes"

query_examples:
  - nl: "目前哪些 CMS 還在顯示警告訊息？"
    sql_hint: "cms_messages JOIN devices JOIN intersections WHERE removed_at IS NULL AND message_type='warning'"
  - nl: "今天哪個路口的 VD 壅塞最嚴重？"
    sql_hint: "vd_readings JOIN devices JOIN intersections WHERE DATE(recorded_at)=CURRENT_DATE GROUP BY intersection_id, SUM/COUNT jam events"
  - nl: "哪些 TC 今天發生過飽和？"
    sql_hint: "tc_phase_logs JOIN devices JOIN intersections WHERE is_oversaturated=true AND DATE(recorded_at)=CURRENT_DATE"
  - nl: "本月維護費用最高的設備是哪種類型？"
    sql_hint: "maintenance_records JOIN devices GROUP BY device_type ORDER BY SUM(cost_ntd) DESC"
```

### 6.3 Agent 啟動時的 Schema Discovery 流程

```
1. 讀取 schema_context.yaml（業務語義層）
2. 呼叫 list_tables（取得真實資料表）
3. 呼叫 describe_table × N（取得真實欄位）
4. 合併兩者 → 建立 enriched schema cache
5. 將 enriched schema 放入 Claude system prompt
```

這樣即使換成電商場景，只需替換 `schema_context.yaml`，Agent 自動適應。

---

## 7. Agent 設計

### 7.1 System Prompt 結構

```
你是交通管理系統的資料分析助理。
[SCHEMA_CONTEXT] ← 動態注入 enriched schema（每 Session 啟動時建立）

[CONVERSATION_RULES]
  - 你能記住本次對話的所有歷史查詢與結果
  - 使用者可用「剛才那個」「同樣的路口」等指代，你應從 history 中解析
  - 若使用者的問題是基於前一個查詢的延伸，應繼承前一次的過濾條件

[CLARIFICATION_RULES]  ← ★ 補問機制
  - 問題不明確時，先列出 1-3 個具體問題再執行 SQL，不要猜測
  - 常見需補問的情境：
      • 缺少時間範圍 → 詢問「請問要查哪個時間區間？例如今天、本週、或指定日期」
      • 缺少路口名稱 → 詢問「請問是哪個路口？或要查全市嗎？」
      • 查詢目的模糊 → 詢問「您想了解的是車流量、事件數，還是設備狀態？」
  - 補問格式：用 💬 開頭，清晰列出選項，讓使用者能直接回覆

[QUERY_RULES]
  - 只能查詢，不能修改資料
  - SQL 必須使用 PostgreSQL 語法
  - 查詢完成後，顯示結果摘要並說明已備妥 Excel 匯出按鈕

[RESPONSE_FORMAT]
  - 先用繁體中文說明此次查詢的邏輯與範圍
  - 再顯示重點數據（Markdown 表格，最多 20 列，超過則說明總筆數）
  - 最後一行固定顯示：「📥 如需完整資料，請點選上方『匯出 Excel』按鈕」
```

### 7.2 對話歷史管理（Session Memory）

同一個 Chainlit Session 內，所有對話輪次的訊息歷史都會保留並傳入 Claude，讓 Agent 能處理跨輪次的指代與延伸查詢。

```python
# chainlit_app.py — Session 層級的 message history 管理

@cl.on_chat_start
async def on_start():
    # 每個新 Session 初始化：schema cache + 空的 message history
    cl.user_session.set("message_history", [])
    cl.user_session.set("last_query_result", None)   # 供「匯出」按鈕使用
    schema_cache = await build_schema_cache()
    cl.user_session.set("schema_cache", schema_cache)

@cl.on_message
async def on_message(message: cl.Message):
    history = cl.user_session.get("message_history")   # 取出歷史
    schema_cache = cl.user_session.get("schema_cache")

    # 將本輪使用者訊息加入 history
    history.append({"role": "user", "content": message.content})

    # 傳入完整 history，讓 Agent 有完整上下文
    result = await run_agent(history, schema_cache)

    # 將 Agent 回覆加入 history，存回 Session
    history.append({"role": "assistant", "content": result.text})
    cl.user_session.set("message_history", history)

    # 儲存最新查詢結果，供手動匯出按鈕使用
    if result.query_result:
        cl.user_session.set("last_query_result", result.query_result)
        await show_export_button()   # 顯示匯出按鈕（見 7.4）
```

**歷史長度控管**：超過 10 輪次時，保留最近 5 輪 + 首輪系統說明，避免 context 爆量。

### 7.3 Tool Call Loop（ReAct 模式）

```python
# agent.py — Claude Tool Use loop，接收完整 history
async def run_agent(history: list[dict], schema_cache: dict) -> AgentResult:
    messages = history.copy()   # ← 直接傳入完整對話歷史

    while True:
        response = claude.messages.create(
            model="claude-sonnet-4-5",
            system=build_system_prompt(schema_cache),
            tools=MCP_TOOLS,
            messages=messages
        )

        if response.stop_reason == "tool_use":
            tool_results = await execute_tools(response.content)
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})
        elif response.stop_reason == "end_turn":
            # 判斷是「補問」還是「查詢完成」
            final_text = response.content[0].text
            query_result = extract_query_result(messages)  # 從 tool results 提取
            return AgentResult(text=final_text, query_result=query_result)
```

### 7.4 手動 Excel 匯出機制

Excel 不自動生成，改為查詢完成後顯示按鈕，由使用者主動觸發，原因：

- 減少不必要的 Excel 產生（使用者可能只是想看摘要）
- 讓使用者確認查詢結果正確後再匯出，避免錯誤資料存檔

```python
# Chainlit Action Button 實作
async def show_export_button():
    actions = [
        cl.Action(
            name="export_excel",
            label="📥 匯出完整 Excel",
            description="將完整查詢結果匯出為 .xlsx 檔案"
        )
    ]
    await cl.Message(
        content="查詢完成。如需匯出完整資料（含統計摘要與 SQL 紀錄）：",
        actions=actions
    ).send()

@cl.action_callback("export_excel")
async def on_export(action: cl.Action):
    query_result = cl.user_session.get("last_query_result")
    if not query_result:
        await cl.Message(content="⚠️ 尚無可匯出的查詢結果").send()
        return

    async with cl.Step(name="產生 Excel 報表") as step:
        filepath = await excel_exporter.generate(query_result)
        step.output = f"已生成：{filepath}"

    # Chainlit 內建 file element 讓使用者直接下載
    elements = [cl.File(name="查詢報表.xlsx", path=filepath, display="inline")]
    await cl.Message(content="✅ Excel 報表已準備完成：", elements=elements).send()
```

### 7.5 Skills 拆分（邏輯分工）

| Skill | 職責 |
|-------|------|
| `schema_discovery` | Session 啟動時讀取並快取 enriched schema |
| `clarification_checker` | 判斷使用者問題是否需要補問，回傳補問清單或通過信號 |
| `nl_to_sql` | 使用者 NL（含歷史 context）→ SQL 生成 |
| `sql_validator` | 執行前語法與安全性驗證 |
| `result_formatter` | 查詢結果 → Markdown 表格摘要 |
| `excel_exporter` | 資料 → 格式化 .xlsx（含標題、樣式、多分頁） |

---

## 8. Excel 報表格式

### 8.1 觸發方式（★ 確認：手動按鈕觸發）

使用者查詢完成後，Agent 顯示結果摘要，並同步出現「📥 匯出完整 Excel」按鈕。
使用者確認查詢正確後，手動點選按鈕才產生 .xlsx，下載連結直接出現在對話框中。

```
[對話畫面示意]
──────────────────────────────────────
User: 本週哪些路口壅塞最嚴重？

Agent: 已查詢近 7 天壅塞記錄，以下為前 5 名：
       | 路口名稱     | 壅塞次數 | 平均持續(分) |
       |-------------|---------|------------|
       | 中山/南京    | 142     | 23.4       |
       | 忠孝/敦化    | 118     | 19.8       |
       ...（共 20 個路口）
       📥 如需完整資料，請點選下方「匯出 Excel」按鈕

[  📥 匯出完整 Excel  ]   ← Chainlit Action Button
──────────────────────────────────────
```

### 8.2 固定報表樣式（openpyxl 實作）

- **Sheet 1 — 查詢結果**：原始資料，自動欄寬，首列凍結，標題列藍底白字
- **Sheet 2 — 統計摘要**：自動生成基本統計（COUNT/SUM/AVG/MAX/MIN）
- **Sheet 3 — 查詢紀錄**：SQL 語句、查詢時間、資料列數、本次對話最後 3 輪問題（audit 用途）

### 8.3 條件格式規則

| 條件 | 格式 |
|------|------|
| congestion_level = 'jam' | 紅色背景 |
| severity = 'critical' | 橘色背景 |
| status = 'fault' | 黃色背景 |
| resolved_at IS NULL（未解除事件） | 粗體 |

---

## 9. 目錄結構

```
chat-sql-traffic-poc/
│
├── docker-compose.yml              # 一鍵啟動（postgres + mcp_server + app）
├── .env.example                    # 環境變數範本
│
├── app/                            # Chainlit + Agent 主程式
│   ├── Dockerfile
│   ├── requirements.txt            # chainlit, anthropic, mcp, pyyaml, openpyxl
│   ├── chainlit_app.py             # Chainlit 入口：@cl.on_chat_start / @cl.on_message / @cl.action_callback
│   ├── agent.py                    # Claude Tool Use loop，接收完整 message_history
│   ├── schema_context.yaml         # ← 業務語義注入，開發者手動維護，volume mount 熱替換
│   └── skills/
│       ├── schema_discovery.py     # Session 啟動：list_tables + describe_table → enriched schema cache
│       ├── clarification_checker.py# 判斷是否需要補問（缺時間/缺設備/目的不明）
│       ├── nl_to_sql.py            # NL + history + schema → SQL
│       ├── sql_validator.py        # SELECT 白名單 + 資料表白名單
│       ├── result_formatter.py     # query result → Markdown 摘要（前 20 列）
│       └── excel_exporter.py       # query result → .xlsx（3 sheets + 條件格式）
│
├── mcp_server/                     # 自製 PostgreSQL MCP Server（SSE 模式）
│   ├── Dockerfile
│   ├── requirements.txt            # mcp, psycopg2-binary, pyyaml
│   ├── server.py                   # MCP tool 定義：list_tables / describe_table / get_sample_rows / execute_query
│   ├── db_client.py                # psycopg2 連線池管理
│   └── hooks/
│       ├── pre_query.py            # SQL 安全攔截（非 SELECT 一律拒絕）
│       └── post_query.py           # 列數警告、audit log 寫入
│
├── database/
│   ├── schema.sql                  # 所有資料表 DDL（intersections + devices + 5種detail + 5種時序 + incidents + maintenance）
│   ├── indexes.sql                 # 複合索引（device_id + recorded_at）
│   └── seed_data.py                # Mock 資料產生器（Faker + 自訂交通邏輯，含 --scale 參數）
│
└── outputs/                        # Excel 匯出暫存目錄（volume mount 至 host）
```

---

## 10. Docker Compose 設計

```yaml
version: "3.9"

services:
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: traffic_db
      POSTGRES_USER: traffic_user
      POSTGRES_PASSWORD: ${DB_PASSWORD}
    volumes:
      - pg_data:/var/lib/postgresql/data
      - ./database/schema.sql:/docker-entrypoint-initdb.d/01_schema.sql
    ports:
      - "5432:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U traffic_user -d traffic_db"]
      interval: 5s

  mcp_server:
    build: ./mcp_server
    environment:
      DB_HOST: postgres
      DB_PORT: 5432
      DB_NAME: traffic_db
      DB_USER: traffic_user
      DB_PASSWORD: ${DB_PASSWORD}
    depends_on:
      postgres:
        condition: service_healthy
    ports:
      - "8080:8080"   # SSE 模式，供 app 連接

  app:
    build: ./app
    environment:
      ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY}
      MCP_SERVER_URL: http://mcp_server:8080
      CHAINLIT_AUTH_SECRET: ${CHAINLIT_AUTH_SECRET}
    volumes:
      - ./outputs:/app/outputs
      - ./app/schema_context.yaml:/app/schema_context.yaml  # 熱替換，無需重建 image
    ports:
      - "8000:8000"
    depends_on:
      - mcp_server

  # pgAdmin 已移除，請使用開發者自備的資料庫訪問工具連線 localhost:5432

volumes:
  pg_data:
```

---

## 11. 環境變數（`.env`）

```env
# LLM
ANTHROPIC_API_KEY=sk-ant-...

# Database
DB_PASSWORD=changeme_in_prod

# Chainlit
CHAINLIT_AUTH_SECRET=random_secret_32chars
CHAINLIT_HOST=0.0.0.0
CHAINLIT_PORT=8000
```

---

## 12. 開發里程碑

| 階段 | 工作項目 | 預估時程 |
|------|----------|----------|
| **P0 — 基礎建設** | Docker Compose + PostgreSQL + schema.sql + seed_data.py | Day 1 |
| **P1 — MCP Server** | list_tables / describe_table / execute_query + SQL 白名單 | Day 1-2 |
| **P2 — Agent 核心** | Claude Tool Use loop + schema_context.yaml 注入 | Day 2-3 |
| **P3 — Chainlit UI** | 接通 Agent + streaming 顯示 + Action Button | Day 3 |
| **P4 — Excel 匯出** | openpyxl 格式化報表 + Chainlit 檔案下載 | Day 4 |
| **P5 — 測試驗收** | 5 類查詢場景測試 + 邊界情況處理 | Day 4-5 |

---

## 13. 驗收查詢場景

### 基礎查詢（單設備類型）

| # | 使用者問題 | 主要資料表 | JOIN 複雜度 |
|---|-----------|-----------|------------|
| B1 | 「目前哪些設備在故障？按行政區分類」 | devices + intersections | ★☆☆ |
| B2 | 「CMS 現在還在顯示哪些訊息？」 | cms_messages + devices + intersections | ★☆☆ |
| B3 | 「過去 3 個月各設備類型的維護費用總計？」 | maintenance_records + devices | ★☆☆ |

### 進階查詢（跨設備類型 JOIN）

| # | 使用者問題 | 主要資料表 | JOIN 複雜度 |
|---|-----------|-----------|------------|
| A1 | 「上週哪 5 個路口機車流量最高（僅算工作日尖峰）？」 | vd_readings + vd_detail + devices + intersections | ★★☆ |
| A2 | 「哪些路口在早上 7-9 點 VD 壅塞且 TC 同時過飽和？」 | vd_readings + tc_phase_logs + devices × 2 + intersections | ★★☆ |
| A3 | 「今天哪些 CCTV 偵測到事故，且對應路口的 CMS 有無同步顯示警告？」 | cctv_events + cms_messages + devices × 2 + intersections | ★★★ |
| A4 | 「eTag 旅行時間最長的路段，對應 VD 的壅塞等級為何？」 | etag_readings + vd_readings + devices × 2 + intersections | ★★★ |

### 跨輪次對話測試（驗證歷史記憶）

| 輪 | 使用者輸入 | 預期行為 |
|----|-----------|---------|
| 1 | 「中山/南京路口的 VD 今天狀況？」 | 查詢該路口 VD，顯示壅塞分布 |
| 2 | 「那它的 TC 呢？」 | 從 history 推斷「它」= 中山/南京，查 TC 相位 |
| 3 | 「把這兩個結果匯出」 | 識別「這兩個」= 前兩輪查詢，整合後匯出 Excel |

---

## 14. 決策紀錄（全部結案）

| # | 問題 | 決策 | 實作說明 |
|---|------|------|---------|
| 1 | **認證機制** | ✅ 無需用戶登入 | Chainlit 不加 `@cl.password_auth_callback`，直接開放存取。PoC 單機使用，無多用戶需求 |
| 2 | **查詢歷史（Session 記憶）** | ✅ Session 內保留 | `cl.user_session` 存 message_history，Session 結束後不持久化 |
| 3 | **Excel 匯出觸發** | ✅ 手動按鈕 | 查詢完成後顯示 Chainlit Action Button，使用者確認後才產生 .xlsx |
| 4 | **模擬資料量** | ✅ 小量含生成腳本 | seed_data.py 預設 ~26,000 筆，含 `--scale` 參數可擴充 |
| 5 | **即時感測器對接** | ✅ PoC 不做 | 所有資料為靜態 mock，架構不預留 WebSocket/TimescaleDB 接口 |
| 6 | **Schema 異動維護** | ✅ 開發者手動維護 | schema_context.yaml 加入 `version` 欄位，開發者異動 schema 時同步更新 YAML |
| 7 | **多語言支援** | ✅ 僅繁體中文 | System prompt 繁中，介面文案繁中，不加 i18n |
| 8 | **LLM 備援** | ✅ 無需 fallback | 僅對接 Claude API，不加 Ollama 或 OpenAI 備援 |

---

## 15. 技術風險與對策

| 風險 | 影響 | 對策 |
|------|------|------|
| SQL Injection via LLM 產生惡意 SQL | 高 | pre_query.py：僅允許 SELECT，資料表白名單比對，拒絕 pg_sleep / dblink |
| 多表 JOIN 產生笛卡爾積爆量 | 高 | execute_query 自動加 LIMIT 10000，執行時間 > 30s 強制 timeout |
| schema_context.yaml 與真實 schema 不同步 | 中 | YAML 內 version 欄位，啟動時自動 diff 資料表清單並 console 警告；開發者手動更新 |
| 設備繼承子表多，Agent JOIN 路徑選錯 | 中 | schema_context 明確標示 join_pattern，query_examples 覆蓋常見多表場景 |
| Claude API 延遲（多 tool call 往返） | 低 | Chainlit 即時 streaming 顯示「思考中…」步驟，避免白畫面；PoC 可接受 5-15 秒 |
| 補問機制過度觸發 | 低 | clarification_checker 只在「無法確定時間範圍或設備範圍」時觸發，query_examples 覆蓋的問題直接推斷 |
| Session 歷史過長 token 超限 | 中 | 超過 20 輪保留最近 15 輪 + 首輪 system message，預估不超過 50k tokens |

---

---

## 16. 補問機制範例對話（Clarification Flow）

以下展示三種典型補問場景，說明 Agent 的行為預期：

**場景 A — 缺少時間範圍**
```
User: 哪些路口壅塞？
Agent: 💬 請問您想查哪個時間區間的壅塞狀況？
       1. 今天
       2. 本週（週一至今）
       3. 指定日期範圍（請輸入，例如：5/20 ~ 5/26）
User: 本週
Agent: [執行查詢，顯示結果]
```

**場景 B — 問題可推斷，不補問**
```
User: 上週哪 5 個路口車流最高？
Agent: [直接執行，時間範圍明確，無需補問]
```

**場景 C — 跨輪次指代**
```
User: 中山/南京路口這週的設備狀況？
Agent: [查詢設備，顯示結果]
User: 那它上個月有幾次事件？
Agent: [從 history 推斷「它」= 中山/南京，查詢 incidents，無需補問]
```

---

*文件狀態：v0.3 — 所有決策結案，可進入實作階段*
