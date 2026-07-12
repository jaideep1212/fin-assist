"""
Job B loader: load a day's parquet snapshot into the ephemeral `staging` schema.

Reads only the blob/parquet backup -- never the Pi. Idempotent so a failed Job B
can re-run start to finish:
  * schema/tables created with CREATE ... IF NOT EXISTS (from staging_ddl.sql)
  * each table is TRUNCATEd then loaded, so a retry produces exactly one clean
    copy regardless of what a prior half-run left behind (no double-loading)
  * any table without explicit DDL (e.g. dim_entities) is auto-created from the
    parquet as a fallback, with a warning -- add real DDL for it when you can.

bytea columns are loaded opaquely (bytes in -> bytea out); decryption is a
downstream concern with the Fernet key. This loader never decrypts anything.

Env:
  STAGING_DATABASE_URL   postgresql://user:pass@<server-fqdn>:5432/<db>
  STAGING_SOURCE         directory (or mounted blob path) holding dt=<date>/*.parquet
  STAGING_RUN_DATE       YYYY-MM-DD (defaults to latest dt= partition found)
  STAGING_DDL            path to staging_ddl.sql (default: alongside this file)
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, inspect, text

log = logging.getLogger("staging.load")

_DDL_DEFAULT = Path(__file__).with_name("staging_ddl.sql")
_SCHEMA = "staging"


def _run_ddl(engine, ddl_path: Path) -> None:
    sql = ddl_path.read_text()
    with engine.begin() as conn:
        conn.execute(text(sql))
    log.info("Applied DDL from %s", ddl_path)


def _pick_partition(source: Path, run_date: str | None) -> Path:
    if run_date:
        p = source / f"dt={run_date}"
        if not p.is_dir():
            raise FileNotFoundError(f"No partition {p}")
        return p
    parts = sorted(source.glob("dt=*"))
    if not parts:
        raise FileNotFoundError(f"No dt=* partitions under {source}")
    return parts[-1]  # latest


def _table_exists(engine, table: str) -> bool:
    return inspect(engine).has_table(table, schema=_SCHEMA)


def load_partition(engine, part_dir: Path) -> dict[str, int]:
    """Load every <table>.parquet in part_dir into staging.<table>. Returns
    {table: rows}. Truncate-then-load keeps this idempotent across retries."""
    results: dict[str, int] = {}
    for pq in sorted(part_dir.glob("*.parquet")):
        table = pq.stem
        df = pd.read_parquet(pq)

        if _table_exists(engine, table):
            with engine.begin() as conn:
                conn.execute(text(f'TRUNCATE TABLE {_SCHEMA}."{table}"'))
            df.to_sql(table, engine, schema=_SCHEMA, if_exists="append", index=False)
        else:
            # No explicit DDL (e.g. dim_entities) -> auto-create from parquet.
            log.warning("No DDL for %s.%s; auto-creating from parquet (types "
                        "are pandas-inferred). Add real DDL when possible.",
                        _SCHEMA, table)
            df.to_sql(table, engine, schema=_SCHEMA, if_exists="replace", index=False)

        results[table] = len(df)
        log.info("Loaded %s.%s (%d rows)", _SCHEMA, table, len(df))
    return results


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    url = os.environ["STAGING_DATABASE_URL"]
    source = Path(os.environ.get("STAGING_SOURCE", "./_staging"))
    run_date = os.getenv("STAGING_RUN_DATE")
    ddl_path = Path(os.getenv("STAGING_DDL", str(_DDL_DEFAULT)))

    try:
        engine = create_engine(url, future=True)
    except ModuleNotFoundError as exc:
        if exc.name == "psycopg2":
            raise SystemExit(
                "FATAL: missing Python package 'psycopg2-binary' (module: psycopg2). "
                "Install runtime deps with `python -m pip install -r requirements.txt`."
            ) from exc
        raise
    _run_ddl(engine, ddl_path)
    part = _pick_partition(source, run_date)
    log.info("Loading partition %s", part)
    results = load_partition(engine, part)
    total = sum(results.values())
    log.info("Done: %d tables, %d rows total.", len(results), total)


if __name__ == "__main__":
    main()
