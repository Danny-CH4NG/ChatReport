"""
psycopg2 connection pool with context manager.
Thread-safe; safe for FastMCP sync tool handlers.
"""
import contextlib
import logging
import os

import psycopg2
from psycopg2.pool import ThreadedConnectionPool

logger = logging.getLogger(__name__)

_pool: ThreadedConnectionPool | None = None


def _make_pool() -> ThreadedConnectionPool:
    return ThreadedConnectionPool(
        minconn=2,
        maxconn=10,
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "5432")),
        dbname=os.getenv("DB_NAME", "traffic_db"),
        user=os.getenv("DB_USER", "traffic_user"),
        password=os.getenv("DB_PASSWORD", "changeme"),
    )


def _get_pool() -> ThreadedConnectionPool:
    global _pool
    if _pool is None or _pool.closed:
        _pool = _make_pool()
        logger.info(
            "DB pool created → %s:%s/%s",
            os.getenv("DB_HOST", "localhost"),
            os.getenv("DB_PORT", "5432"),
            os.getenv("DB_NAME", "traffic_db"),
        )
    return _pool


@contextlib.contextmanager
def get_db():
    """Yield a connection from the pool; commit on success, rollback on error."""
    pool = _get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)
