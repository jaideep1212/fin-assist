"""
Database access: a SQLAlchemy engine plus two thin helpers, configured from
DATABASE_URL.

DATABASE_URL points at 127.0.0.1:5432 — the azbridge sidecar's local forwarder —
so this module is relay-agnostic: it just speaks to a local Postgres. The relay
tunnel is invisible here by design.
"""

from __future__ import annotations

import os
from functools import lru_cache

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine


def _database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return url


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Process-wide engine. pool_pre_ping recycles connections the relay may
    have dropped while idle, so callers don't hit stale sockets."""
    return create_engine(_database_url(), pool_pre_ping=True, future=True)


def read_sql(query: str, params: dict | None = None) -> pd.DataFrame:
    """Run a query and return the result as a DataFrame."""
    with get_engine().connect() as conn:
        return pd.read_sql(text(query), conn, params=params or {})


def ping() -> bool:
    """End-to-end liveness: a real SELECT 1 through the relay. True only if the
    whole chain (relay + Pi + Postgres) answers."""
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
