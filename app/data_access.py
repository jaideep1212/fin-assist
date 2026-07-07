"""
API data loaders — the query layer behind /instruments/{kind}.

PROVISIONAL: the table and column names here are best-guess defaults, read from
env vars, because the real Pi schema has not been confirmed yet. Run
inspect_schema.py and wire these to the actual columns (open item #1). The 8
source tables include dim_mutual_funds and fact_stock_transactions but NO
dedicated stock/bond dimension, so:
  - mutual funds come straight from dim_mutual_funds
  - stocks are derived from distinct symbols in the transaction facts
  - bonds have no source table yet and are intentionally unsupported (-> 404)
"""

from __future__ import annotations

import os

import pandas as pd

from app.db import read_sql

# Override any of these via env once the real schema is known.
MUTUAL_FUND_TABLE = os.getenv("MF_TABLE", "dim_mutual_funds")
STOCK_TXN_TABLE = os.getenv("STOCK_TXN_TABLE", "fact_stock_transactions")
STOCK_SYMBOL_COL = os.getenv("STOCK_SYMBOL_COL", "symbol")


def load_mutual_funds() -> pd.DataFrame:
    return read_sql(f'SELECT * FROM "{MUTUAL_FUND_TABLE}"')


def load_stocks() -> pd.DataFrame:
    # No dim_stocks table exists; list instruments from distinct traded symbols.
    return read_sql(
        f'SELECT DISTINCT "{STOCK_SYMBOL_COL}" AS symbol FROM "{STOCK_TXN_TABLE}" '
        f"ORDER BY symbol"
    )


# kind -> loader. Anything not here is an unknown kind (surfaced as 404).
_LOADERS = {
    "mutual_fund": load_mutual_funds,
    "stock": load_stocks,
}


def supported_kinds() -> list[str]:
    return sorted(_LOADERS)


def load_instruments(kind: str) -> pd.DataFrame:
    """Load the instrument list for a kind. Raises KeyError for unknown kinds."""
    loader = _LOADERS[kind]  # KeyError -> handled by the API as 404
    return loader()
