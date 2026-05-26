"""
Shared pytest fixtures and markers.

Unit tests:   no Docker required — import from mcp_server/ and app/ directly.
Integration:  require PostgreSQL at localhost:5432 (docker compose up postgres).
              Run with: pytest -m integration
              Skip with: pytest -m "not integration"
"""
import os
import sys

import pytest

# ── Make mcp_server and app importable without installing them ─────────────────
_repo = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(_repo, "mcp_server"))
sys.path.insert(0, os.path.join(_repo, "app"))


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "integration: requires a running PostgreSQL database (docker compose up postgres)",
    )


def _make_conn():
    """Create a new psycopg2 connection. Skips test on failure."""
    try:
        import psycopg2
    except ImportError:
        pytest.skip("psycopg2 not installed")

    try:
        return psycopg2.connect(
            host=os.getenv("DB_HOST", "localhost"),
            port=int(os.getenv("DB_PORT", "5432")),
            dbname=os.getenv("DB_NAME", "traffic_db"),
            user=os.getenv("DB_USER", "traffic_user"),
            password=os.getenv("DB_PASSWORD", "changeme"),
            connect_timeout=5,
        )
    except Exception as exc:
        pytest.skip(f"Cannot connect to PostgreSQL: {exc}")


@pytest.fixture
def db_conn():
    """Function-scoped DB connection — fresh per test to prevent cascade failures."""
    conn = _make_conn()
    yield conn
    try:
        conn.close()
    except Exception:
        pass


@pytest.fixture
def cursor(db_conn):
    """Per-test cursor inside a transaction that rolls back on completion."""
    cur = db_conn.cursor()
    yield cur
    try:
        db_conn.rollback()
        cur.close()
    except Exception:
        pass


@pytest.fixture
def allowed_tables(db_conn):
    """Live list of public tables — needed by validate_sql."""
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        )
        return [row[0] for row in cur.fetchall()]
