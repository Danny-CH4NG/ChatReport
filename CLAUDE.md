# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Chat-to-SQL PoC for a traffic management system. Users ask questions in natural language → Claude Agent generates SQL → queries PostgreSQL → exports Excel. See `PRD_Chat_to_SQL_Traffic_PoC.md` for the full specification.

## Common Commands

### Start all services
```bash
cp .env.example .env   # fill in ANTHROPIC_API_KEY
docker compose up --build
```

### Seed the database (after postgres is running)
```bash
# From host (requires psycopg2: pip install psycopg2-binary)
python database/seed_data.py --scale small    # ~26,000 rows (default)
python database/seed_data.py --scale medium   # ~200,000 rows
python database/seed_data.py --scale large    # ~1,000,000 rows

# Override connection (defaults: localhost:5432, traffic_db, traffic_user, changeme)
python database/seed_data.py --host localhost --password <DB_PASSWORD>
```

### Reset and re-seed
```bash
docker compose down -v        # drops pg_data volume
docker compose up -d postgres
# wait for healthcheck, then:
python database/seed_data.py
```

### Access the app
- Chat UI: http://localhost:8000
- MCP Server (SSE): http://localhost:8080
- PostgreSQL: localhost:5432 (use any DB client)

## Architecture

```
Chainlit (8000) ──HTTP──► FastAPI/Agent (app/) ──MCP/SSE──► MCP Server (8080) ──psycopg2──► PostgreSQL (5432)
```

**Three-service Docker Compose** (`docker-compose.yml`): `postgres` → `mcp_server` → `app`. Startup order enforced via `depends_on` + healthcheck.

### Database: Three-tier inheritance (`database/schema.sql`)

```
intersections
  └── devices  (base, all 5 types share this table, filtered by device_type)
        ├── vd_detail    1:1   →  vd_readings     (time-series, 5-min intervals)
        ├── cms_detail   1:1   →  cms_messages    (time-series)
        ├── tc_detail    1:1   →  tc_phase_logs   (time-series, 2-min intervals)
        ├── cctv_detail  1:1   →  cctv_events     (time-series)
        └── etag_detail  1:1   →  etag_readings   (time-series)
              ↓
        incidents         (cross-device events, FK to intersections + devices)
        maintenance_records (FK to devices)
```

To query a specific device type: always start from `devices WHERE device_type='VD'` then JOIN the `_detail` and time-series tables. The `schema_context.yaml` in `app/` encodes this as `join_pattern`.

### app/ — Agent logic

| File | Role |
|------|------|
| `chainlit_app.py` | Entry point; manages `cl.user_session` with `message_history`, `last_query_result`, `schema_cache` |
| `agent.py` | Claude Tool Use loop (ReAct); receives full message history each turn |
| `schema_context.yaml` | Business vocabulary injected into system prompt; **volume-mounted** so editable without rebuilding the image |
| `skills/schema_discovery.py` | Called once on session start: `list_tables` + `describe_table` → enriched schema cache |
| `skills/clarification_checker.py` | Decides whether to ask the user for missing time range / location before running SQL |
| `skills/excel_exporter.py` | openpyxl: 3 sheets (raw data, stats summary, query audit), conditional formatting |

History is capped at 20 turns (keeps newest 15 + first system message) to avoid context overflow.

### mcp_server/ — PostgreSQL MCP Server (SSE mode)

Exposes four tools: `list_tables`, `describe_table`, `get_sample_rows`, `execute_query`.

Security enforced in `hooks/pre_query.py`: SELECT-only whitelist, table whitelist, blocks `pg_sleep`/`dblink`. Auto-appends `LIMIT 10000` and 30-second query timeout.

### Excel export format

Three sheets per export:
1. Raw query results (frozen header row, auto column width, blue header)
2. Auto-generated statistics (COUNT/SUM/AVG/MAX/MIN)
3. Audit log (SQL, timestamp, row count, last 3 conversation turns)

Conditional formatting: `jam` → red, `critical` → orange, `fault` → yellow, unresolved incidents → bold.

## Key Design Decisions

- **No pgAdmin** in Docker Compose — use any local DB client pointed at `localhost:5432`.
- **Excel is triggered manually** via a Chainlit Action Button after the query completes, not auto-generated.
- **`schema_context.yaml` is the only file to edit** when adapting this PoC to a different domain (e.g. HR, e-commerce). It maps business vocabulary to table/column names and provides `query_examples` for the agent.
- **LLM**: `claude-sonnet-4-5` only; no fallback to other providers.
- **Session memory** lives in `cl.user_session` — not persisted across sessions.

## Environment Variables

See `.env.example`. Required: `ANTHROPIC_API_KEY`. Optional overrides: `DB_PASSWORD`, `CHAINLIT_AUTH_SECRET`.
