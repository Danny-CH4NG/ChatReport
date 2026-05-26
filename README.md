# ChatReport — Chat-to-SQL for Traffic Management

![示意圖](./demo.png)

A proof-of-concept that lets operators ask natural-language questions about traffic data and get instant answers, with one-click Excel export.

```
User question
    │
    ▼
Chainlit UI (8000)
    │  HTTP
    ▼
Agent / Claude Tool Use (app/)
    │  MCP over SSE
    ▼
MCP Server (8080)  ──psycopg2──►  PostgreSQL (5432)
                                        │
                               schema.sql + seed data
```

---

## Quick Start

### 1. Prerequisites

| Tool | Minimum version |
|------|----------------|
| Docker Desktop | 24+ |
| Python (host, for seeding) | 3.10+ |
| `psycopg2-binary` | any |

```bash
pip install psycopg2-binary
```

### 2. Set up environment

```bash
cp .env.example .env
```

Open `.env` and fill in your Anthropic API key:

```env
ANTHROPIC_API_KEY=sk-ant-...
DB_PASSWORD=changeme          # change in prod
CHAINLIT_AUTH_SECRET=...      # any 32-char random string
```

### 3. Start all services

```bash
docker compose up --build
```

Three containers start in order: `postgres` → `mcp_server` → `app`.  
Wait until you see `Chainlit running on http://0.0.0.0:8000`.

### 4. Seed the database

Open a second terminal (containers must be running):

```bash
# Small dataset — ~26,000 rows, takes ~10 s (default)
python database/seed_data.py

# Medium — ~200,000 rows
python database/seed_data.py --scale medium

# Large — ~1,000,000 rows
python database/seed_data.py --scale large
```

If you changed `DB_PASSWORD` in `.env`, pass it explicitly:

```bash
python database/seed_data.py --password <your_password>
```

### 5. Open the chat UI

[http://localhost:8000](http://localhost:8000)

Try asking:

> 昨天中山區所有路口的平均車流量是多少？

> 列出本週發生過事故且尚未處理的路口

> 最近 24 小時有哪些 CMS 顯示板正在顯示警告訊息？

After a query returns results, click the **Export Excel** button to download a formatted report.

---

## Service Endpoints

| Service | URL | Notes |
|---------|-----|-------|
| Chat UI | http://localhost:8000 | Chainlit |
| MCP Server (SSE) | http://localhost:8080 | FastAPI |
| PostgreSQL | localhost:5432 | DB: `traffic_db`, User: `traffic_user` |

---

## Seeding Options

```
python database/seed_data.py [OPTIONS]

Options:
  --scale   small | medium | large     Row count preset (default: small)
  --host    HOST                        Postgres host (default: localhost)
  --port    PORT                        Postgres port (default: 5432)
  --dbname  DBNAME                      Database name (default: traffic_db)
  --user    USER                        DB user (default: traffic_user)
  --password PASSWORD                   DB password (default: changeme)
```

Scale reference:

| Scale | Rows (approx.) | Seed time |
|-------|---------------|-----------|
| small | 26,000 | ~10 s |
| medium | 200,000 | ~60 s |
| large | 1,000,000 | ~5 min |

---

## Reset & Re-seed

```bash
# Stop containers and delete the postgres volume
docker compose down -v

# Restart (schema is auto-applied on first boot)
docker compose up -d postgres

# Wait ~10 s for the healthcheck, then seed
python database/seed_data.py
```

---

## Excel Export

Each export produces an `.xlsx` file with three sheets:

| Sheet | Content |
|-------|---------|
| **Raw Data** | Full query results, frozen header, auto-width columns |
| **Statistics** | COUNT / SUM / AVG / MAX / MIN for every numeric column |
| **Audit Log** | SQL statement, timestamp, row count, last 3 conversation turns |

Conditional formatting highlights: `jam` → red, `critical` → orange, `fault` → yellow, unresolved incidents → bold.

---

## Project Structure

```
ChatReport/
├── app/
│   ├── chainlit_app.py          # Chainlit entry point
│   ├── agent.py                 # Claude Tool Use loop (ReAct)
│   ├── schema_context.yaml      # Business vocabulary → SQL mapping (edit to adapt)
│   └── skills/
│       ├── schema_discovery.py  # Discovers live schema on session start
│       ├── clarification_checker.py
│       └── excel_exporter.py    # openpyxl report builder
├── mcp_server/
│   ├── server.py                # FastAPI / SSE MCP server
│   ├── db_client.py             # psycopg2 wrapper
│   └── hooks/
│       └── pre_query.py         # SELECT-only guard, table whitelist, LIMIT injection
├── database/
│   ├── schema.sql               # Three-tier inheritance schema
│   └── seed_data.py             # Taipei intersection mock data generator
├── tests/
│   ├── unit/                    # pre_query hook, clarification checker
│   └── integration/             # DB edge-cases, live query scenarios
├── docker-compose.yml
└── .env.example
```

---

## Running Tests

```bash
pip install -r tests/requirements-test.txt
pytest
```

Integration tests require a running PostgreSQL instance (use `docker compose up -d postgres`).

---

## Adapting to Another Domain

The only file you need to edit is [`app/schema_context.yaml`](app/schema_context.yaml).  
It maps business vocabulary to table/column names and provides `query_examples` for the agent prompt. No code changes required.

---

## Security Notes

- The MCP server enforces **SELECT-only** queries; `INSERT`, `UPDATE`, `DELETE`, DDL, and `pg_sleep`/`dblink` are blocked.
- All queries are automatically capped at `LIMIT 10000` with a 30-second timeout.
- `ANTHROPIC_API_KEY` and `DB_PASSWORD` are never logged.
