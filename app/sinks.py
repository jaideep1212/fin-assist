"""
Where the daily transient snapshot lands so downstream jobs can pick it up.

The watchdog holds the fetched tables as DataFrames, then writes them here as
per-day parquet snapshots (overwritten each run — transient by design).

  local : parquet on a local/mounted path (dev, or an Azure Files mount)
  blob  : parquet in Azure Blob Storage (recommended for Option B handoff)

Swap by setting SINK. To use temp tables in an Azure staging DB later, add a
SqlStagingSink with the same .write() signature — nothing else changes.
"""

from __future__ import annotations

import io
import os
from datetime import date
from pathlib import Path
from typing import Protocol

import pandas as pd


class Sink(Protocol):
    def write(self, name: str, df: pd.DataFrame, run_date: date) -> str: ...


class LocalParquetSink:
    def __init__(self, root: str) -> None:
        self.root = Path(root)

    def write(self, name: str, df: pd.DataFrame, run_date: date) -> str:
        out_dir = self.root / f"dt={run_date.isoformat()}"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{name}.parquet"
        df.to_parquet(path, index=False)
        return str(path)


class BlobParquetSink:
    """Azure Blob snapshot sink. Needs `pip install azure-storage-blob`."""

    def __init__(self, conn_str: str, container: str) -> None:
        from azure.storage.blob import BlobServiceClient  # lazy import

        self._svc = BlobServiceClient.from_connection_string(conn_str)
        self._container = container
        try:
            self._svc.create_container(container)
        except Exception:
            pass  # already exists

    def write(self, name: str, df: pd.DataFrame, run_date: date) -> str:
        buf = io.BytesIO()
        df.to_parquet(buf, index=False)
        buf.seek(0)
        blob = f"staging/dt={run_date.isoformat()}/{name}.parquet"
        self._svc.get_blob_client(self._container, blob).upload_blob(
            buf, overwrite=True
        )
        return blob


def build_sink() -> Sink:
    kind = os.getenv("SINK", "local").lower()
    if kind == "blob":
        return BlobParquetSink(
            conn_str=os.environ["BLOB_CONN_STR"],
            container=os.getenv("BLOB_CONTAINER", "staging"),
        )
    return LocalParquetSink(os.getenv("SINK_ROOT", "./_staging"))
