#!/usr/bin/env python3
"""
ship_logs.py  --  Pi log shipper (the "delivery truck").

Reads NEW journald entries for one service, redacts secrets/PII, and uploads them
to the Azure Monitor Logs Ingestion API so the Pi's relay logs land in the
PiRelay_CL custom table -- visible in Grafana alongside the Azure logs.

Self-contained: needs NO project repo. Config + the one secret come from a
.env next to this file. Position is tracked with a journald CURSOR so only NEW
entries ship and it survives restarts (no re-shipping, no gaps).

One-shot by design: it ships what's new and exits. Schedule it every minute with
a systemd timer (see setup notes) -- more robust than a long-running daemon.

Dependencies (install once on the Pi):
    pip install --user azure-identity azure-monitor-ingestion

Run:
    python3 ship_logs.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV_FILE = HERE / ".env"
CURSOR_FILE = HERE / ".journal-cursor"  # journald position; created on first run


# --- config -----------------------------------------------------------------
def _load_env(path: Path) -> None:
    """Tiny .env loader (no dependency). KEY=VALUE lines; #comments ignored.
    Real environment wins over .env (setdefault)."""
    if not path.exists():
        sys.exit(f"FATAL: config file not found: {path}")
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


_load_env(ENV_FILE)

DCE_URL = os.environ["PI_LOGS_DCE_URL"]
DCR_ID = os.environ["PI_LOGS_DCR_ID"]
STREAM = os.environ["PI_LOGS_STREAM_NAME"]
TENANT = os.environ["PI_LOGS_TENANT_ID"]
CLIENT_ID = os.environ["PI_LOGS_CLIENT_ID"]
CLIENT_SECRET = os.environ["PI_LOGS_CLIENT_SECRET"]
UNIT = os.getenv("PI_LOG_UNIT", "azbridge-pi.service")
HOST = os.getenv("PI_LOG_HOST", "pi")


# --- redaction (self-contained; keep in sync with redaction_patterns.md) -----
_SCRUB = "***REDACTED***"
_PATTERNS = [
    (re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s:/@]+:[^\s:/@]+@[^\s]+"), _SCRUB),
    (
        re.compile(
            r"(?i)\b(AccountKey|SharedAccessKey|SharedAccessSignature|sig)=[^\s;&\"']+"
        ),
        r"\1=" + _SCRUB,
    ),
    (re.compile(r"(?i)\bDefaultEndpointsProtocol=[^\s\"']+"), _SCRUB),
    (
        re.compile(r"(?i)\b(Endpoint|EntityPath|SharedAccessKeyName)=[^\s;\"']+"),
        r"\1=" + _SCRUB,
    ),
    (re.compile(r"(?i)[?&]s(?:ig|v|p|e|t|r)=[^\s&\"']+"), _SCRUB),
    (re.compile(r"\b[A-Za-z0-9\-_]{43}=\b"), _SCRUB),  # Fernet key
    (re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"), _SCRUB),  # long b64 secret
    (
        re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),
        _SCRUB,
    ),  # JWT
    (
        re.compile(
            r"(?i)\b(PGPASSWORD|TF_VAR_ADMIN_PASSWORD|ENCRYPTION_KEY|BLOB_CONN_STR|"
            r"ARM_ACCESS_KEY|ARM_CLIENT_SECRET|PI_LOGS_CLIENT_SECRET|password|passwd|"
            r"secret|token|api[_-]?key)\s*[=:]\s*[^\s,;\"']+"
        ),
        r"\1=" + _SCRUB,
    ),
    (
        re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
        _SCRUB,
    ),  # email
    (re.compile(r"\b\d{9,}\b"), _SCRUB),  # long digit runs
]


def scrub(text: str) -> str:
    if not text:
        return text
    for pat, repl in _PATTERNS:
        text = pat.sub(repl, text)
    return text


def _safe(text: str) -> str:
    """scrub() plus an exact mask of this shipper's own client secret, so it can
    never appear in an error message we print."""
    t = scrub(text or "")
    if CLIENT_SECRET:
        t = t.replace(CLIENT_SECRET, _SCRUB)
    return t


_PRIORITY = {
    "0": "emerg",
    "1": "alert",
    "2": "crit",
    "3": "err",
    "4": "warning",
    "5": "notice",
    "6": "info",
    "7": "debug",
}


# --- read journald ----------------------------------------------------------
def _seed_cursor_if_first_run() -> None:
    """On the very first run, start from the CURRENT tail -- don't ship the
    whole history of the unit, and don't miss anything either. We grab the
    latest entry's __CURSOR and write it, so the main read starts right after."""
    if CURSOR_FILE.exists():
        return
    r = subprocess.run(
        ["journalctl", "-u", UNIT, "-n", "1", "--output", "json", "--no-pager"],
        capture_output=True,
        text=True,
    )
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    if not lines:
        return  # no history yet; main read (empty cursor) will simply find nothing
    try:
        cursor = json.loads(lines[-1]).get("__CURSOR")
    except json.JSONDecodeError:
        cursor = None
    if cursor:
        CURSOR_FILE.write_text(cursor)


def read_new_entries() -> list[dict]:
    """Pull NEW journald entries for UNIT since the saved cursor, shaped for
    PiRelay_CL, with message text scrubbed. --cursor-file starts after the saved
    cursor and rewrites it to the last entry shown -> no re-ship, no gaps."""
    _seed_cursor_if_first_run()
    proc = subprocess.run(
        [
            "journalctl",
            "-u",
            UNIT,
            "--output",
            "json",
            "--no-pager",
            "--cursor-file",
            str(CURSOR_FILE),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        sys.exit(f"FATAL: journalctl failed ({proc.returncode}): {_safe(proc.stderr)}")

    records: list[dict] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            j = json.loads(line)
        except json.JSONDecodeError:
            continue

        ts_us = j.get("__REALTIME_TIMESTAMP")
        ts = (
            datetime.fromtimestamp(int(ts_us) / 1_000_000, tz=timezone.utc)
            if ts_us
            else datetime.now(timezone.utc)
        )

        msg = j.get("MESSAGE", "")
        if isinstance(msg, list):  # journald returns bytes as int arrays
            msg = bytes(msg).decode("utf-8", "replace")

        # If the relay ever emits structured JSON, lift its "event"; else default.
        event = "relay_log"
        m = msg.strip()
        if m.startswith("{") and m.endswith("}"):
            try:
                event = str(json.loads(m).get("event", event))
            except json.JSONDecodeError:
                pass

        records.append(
            {
                "TimeGenerated": ts.isoformat(),
                "host": HOST,
                "unit": j.get("_SYSTEMD_UNIT", UNIT),
                "level": _PRIORITY.get(str(j.get("PRIORITY", "6")), "info"),
                "event": event,
                "message": scrub(msg),
            }
        )
    return records


# --- ship to Azure ----------------------------------------------------------
def ship(records: list[dict]) -> None:
    if not records:
        print("no new log entries; nothing to ship.")
        return
    # Imported here so the module still parses on a box without the SDK installed.
    from azure.identity import ClientSecretCredential
    from azure.monitor.ingestion import LogsIngestionClient
    from azure.core.exceptions import HttpResponseError

    credential = ClientSecretCredential(
        tenant_id=TENANT,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
    )
    client = LogsIngestionClient(
        endpoint=DCE_URL, credential=credential, logging_enable=False
    )
    try:
        client.upload(rule_id=DCR_ID, stream_name=STREAM, logs=records)
    except HttpResponseError as e:
        sys.exit(f"FATAL: upload failed: {_safe(str(e))}")
    print(f"shipped {len(records)} log entries to {STREAM}.")


def main() -> None:
    ship(read_new_entries())


if __name__ == "__main__":
    main()
