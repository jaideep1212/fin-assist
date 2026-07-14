"""
Durable "done" markers for the daily watchdog.

A marker is a tiny blob whose *presence* means "this watchdog day completed".
The watchdog writes one after a successful run and checks it every tick, so:

  * DONE survives container restarts/redeploys (no accidental re-pull), and
  * deleting or renaming the marker forces a re-run on the next tick.

This is Option A: the run decision keys off blob *existence*, not content. The
small JSON body is informational only (handy when you inspect it).

Marker path:  <container>/_state/done-<YYYY-MM-DD>.json
  e.g.        landing-zone/_state/done-2026-07-10.json

Only used when SINK=blob. In local mode `build_marker()` returns None and the
watchdog falls back to in-memory state (fine for local dev).

Manual override (force a re-run of a day):
  # delete the marker
  az storage blob delete --account-name <acct> --container-name <container> \
      --name "_state/done-2026-07-10.json" --auth-mode login
  # or "rename" (copy to an archive name, then delete) to keep an audit trail
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from typing import Optional

from app.obs_logging import get_logger

log = get_logger("watchdog.markers")

_STATE_PREFIX = "_state"


class BlobMarkerStore:
    """Presence-of-a-blob marker store. Fail-open on read errors."""

    def __init__(self, conn_str: str, container: str, prefix: str = _STATE_PREFIX):
        # Lazy import so nothing here is required unless blob mode is actually
        # used (mirrors the lazy-import pattern in sinks.py).
        from azure.storage.blob import BlobServiceClient

        self._svc = BlobServiceClient.from_connection_string(conn_str)
        self._container = container
        self._prefix = prefix.strip("/")

    def _blob_name(self, logical_day: date) -> str:
        return f"{self._prefix}/done-{logical_day.isoformat()}.json"

    def is_done(self, logical_day: date) -> bool:
        """True iff the marker blob exists. Fail-open: on any error return False
        (treat as not-done -> the watchdog runs; a re-run just overwrites)."""
        name = self._blob_name(logical_day)
        try:
            client = self._svc.get_blob_client(self._container, name)
            return client.exists()
        except Exception:
            log.warning(
                f"marker read failed for {name}; failing open (will run)",
                extra={"fields": {"event": "marker_read", "status": "error"}},
                exc_info=True,
            )
            return False

    def mark_done(self, logical_day: date, meta: Optional[dict] = None) -> None:
        """Write the done-marker for the day (overwrites any prior marker)."""
        name = self._blob_name(logical_day)
        body = {
            "logical_day": logical_day.isoformat(),
            "status": "done",
            "run_at": datetime.now(timezone.utc).isoformat(),
        }
        if meta:
            body.update(meta)
        try:
            client = self._svc.get_blob_client(self._container, name)
            client.upload_blob(json.dumps(body, indent=2).encode(), overwrite=True)
            log.info(
                f"wrote done-marker {name}",
                extra={
                    "fields": {
                        "event": "marker_written",
                        "status": "ok",
                        "run_date": logical_day.isoformat(),
                    }
                },
            )
        except Exception:
            # Non-fatal: the run itself succeeded. Worst case a later restart
            # re-runs the day (harmless overwrite of the same partition).
            log.warning(
                f"failed to write done-marker {name} (non-fatal)",
                extra={
                    "fields": {
                        "event": "marker_write",
                        "status": "error",
                        "run_date": logical_day.isoformat(),
                    }
                },
                exc_info=True,
            )


def build_marker() -> Optional[BlobMarkerStore]:
    """Return a BlobMarkerStore when SINK=blob, else None (in-memory fallback).

    Reuses the same env the blob sink uses: BLOB_CONN_STR and BLOB_CONTAINER.
    """
    if os.getenv("SINK", "local").lower() != "blob":
        log.info(
            "SINK is not 'blob'; watchdog will use in-memory done-state",
            extra={"fields": {"event": "markers_disabled", "status": "ok"}},
        )
        return None
    conn = os.environ["BLOB_CONN_STR"]  # required in blob mode
    container = os.getenv("BLOB_CONTAINER", "staging")
    log.info(
        f"done-markers enabled at {container}/{_STATE_PREFIX}/done-<day>.json",
        extra={"fields": {"event": "markers_enabled", "status": "ok"}},
    )
    return BlobMarkerStore(conn, container)
