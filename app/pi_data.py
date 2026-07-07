"""
Pulls the source tables from the Pi's PostgreSQL into transient DataFrames
for downstream processing.

The Pi is the system of record; these frames are a working copy that the
watchdog refreshes once per day and downstream jobs consume. Nothing here
is the permanent store.
"""

from __future__ import annotations

import pandas as pd

from app.db import ping, read_sql

# The 9 source tables on the Pi.
DIM_TABLES = [
    "dim_users",
    "dim_users_s",
    "dim_entities",
    "dim_accounts",
    "dim_mutual_funds",
]
FACT_TABLES = [
    "fact_mutual_fund_transactions",
    "fact_stock_transactions",
    "fact_aliases",
    "fact_account_broker_mappings",
]
PI_TABLES = DIM_TABLES + FACT_TABLES


def is_pi_awake() -> bool:
    """End-to-end liveness: can we open a Postgres connection through the relay?
    This is stronger than pinging the host — it proves relay + Pi + Postgres
    are all up."""
    return ping()


def fetch_all() -> dict[str, pd.DataFrame]:
    """Load all source tables into DataFrames keyed by table name."""
    frames: dict[str, pd.DataFrame] = {}
    for table in PI_TABLES:
        frames[table] = read_sql(f'SELECT * FROM "{table}"')
    return frames
