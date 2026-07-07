"""
Run first: discover the real tables, columns, types, and row counts on the Pi.

It reuses the app's engine (app.db -> DATABASE_URL -> 127.0.0.1:5432 via the
relay), so run it from the repo root with the relay up:

    DATABASE_URL='postgresql://user:pass@127.0.0.1:5432/investments' \
        python inspect_schema.py

The output is the ground truth the loaders in app/data_access.py should be wired
to (open item #1). Paste it back so the provisional table/column guesses — and
the placeholder schema.sql — can be replaced with the real thing.
"""
from __future__ import annotations

from sqlalchemy import inspect, text

from app.db import get_engine

# The 8 source tables we expect to find (used only to flag anything missing).
EXPECTED = [
    "dim_users",
    "dim_users_s",
    "dim_entities",
    "dim_accounts",
    "dim_mutual_funds",
    "fact_mutual_fund_transactions",
    "fact_stock_transactions",
    "fact_aliases",
    "fact_account_broker_mappings",
]

_SYSTEM_SCHEMAS = {"information_schema", "pg_catalog", "pg_toast"}


def _row_count(engine, schema: str | None, table: str) -> str:
    ident = f'"{schema}"."{table}"' if schema else f'"{table}"'
    try:
        with engine.connect() as conn:
            return f"{conn.execute(text(f'SELECT count(*) FROM {ident}')).scalar_one():,}"
    except Exception as exc:  # keep going even if one table can't be counted
        return f"(count failed: {exc.__class__.__name__})"


def main() -> None:
    engine = get_engine()
    insp = inspect(engine)

    print(f"Engine : {engine.url.render_as_string(hide_password=True)}")
    print(f"Schemas: {', '.join(insp.get_schema_names())}\n")

    found: set[str] = set()
    for schema in insp.get_schema_names():
        if schema in _SYSTEM_SCHEMAS:
            continue
        tables = insp.get_table_names(schema=schema)
        if not tables:
            continue
        print(f"=== schema: {schema} ===")
        for table in sorted(tables):
            found.add(table)
            print(f"\n  {table}  ({_row_count(engine, schema, table)} rows)")
            for col in insp.get_columns(table, schema=schema):
                null = "NULL" if col.get("nullable", True) else "NOT NULL"
                print(f"    - {col['name']:<28} {str(col['type']):<20} {null}")
        print()

    missing = [t for t in EXPECTED if t not in found]
    if missing:
        print("WARNING: expected source tables not found: " + ", ".join(missing))
    else:
        print("All 9 expected source tables present.")


if __name__ == "__main__":
    main()
