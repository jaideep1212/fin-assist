"""
obs_logging.py  --  structured, allowlisted logging for fin-assist.

Purpose
-------
One place that decides HOW logs are emitted so they are safe to ship to Grafana
/ Loki. Two guarantees:

  1. ALLOWLIST (fail-closed): only an explicit set of safe fields is ever
     emitted as structured data. Anything not on the list is dropped, so a new
     field carrying a secret or PII cannot leak just because someone added it.

  2. DENYLIST BACKSTOP (defence in depth): the free-text `message` and any
     allowed string values are still run through a secret/PII scrubber, because
     a secret can end up inside an allowed field (e.g. an error message that
     embeds a connection string). Allowlist decides WHAT leaves; the scrubber
     sanitises WHAT'S INSIDE what leaves.

Who adopts this
---------------
  * watchdog path  -> watchdog.py, app/watchdog.py, run_watchdog.py,
                      app/load_staging.py, app/markers.py  (already log counts
                      and status only -- this formalises it)
  * orchestrator   -> run_staging_pipeline.py  (ships from Azure; see notes on
                      the command-echo, which must NOT go through the raw path)

Who does NOT adopt this
-----------------------
  * verify_decrypt_export.py, export_decrypted_tables_to_csv.py  -- these hold
    the Fernet key and cleartext PII. They stay interactive and are EXCLUDED
    from shipping. If you want an audit trail that they ran, call
    `audit_event()` below, which emits only a fixed, field-limited line.

Usage
-----
    from obs_logging import get_logger, audit_event

    log = get_logger("watchdog.load")
    log.info("partition loaded", extra={"fields": {
        "event": "load_partition",
        "table": table,
        "row_count": len(df),
        "run_date": run_date.isoformat(),
        "status": "ok",
        "duration_ms": elapsed_ms,
    }})

Only keys in ALLOWED_FIELDS survive; everything else is dropped and counted
under a `dropped_fields` marker so you can SEE (in the safe output) that
something was withheld, without seeing its value.
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import sys
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# 1. ALLOWLIST -- the ONLY structured fields permitted to leave a host.
#    Keep this deliberately small. Add a field here ONLY after confirming it can
#    never carry a secret or personal data. When in doubt, leave it out.
# ---------------------------------------------------------------------------
ALLOWED_FIELDS: frozenset[str] = frozenset(
    {
        # what happened
        "event",  # short machine name: "load_partition", "apply", "teardown"
        "status",  # "ok" | "error" | "skipped" | "retry"
        "stage",  # pipeline stage: "init" | "apply" | "load" | "destroy"
        # safe quantities (counts and timings -- never values)
        "row_count",
        "table_count",
        "duration_ms",
        "attempt",
        # safe identifiers (structural, not personal)
        "table",  # table NAME, e.g. "dim_users" -- not its contents
        "run_date",  # a date, e.g. "2026-07-12"
        "job",  # logical job name
        "stage_dir",  # e.g. "infra/staging-db"
        "host",  # which tier emitted it: "pi" | "laptop" | "azure"
        "execution",  # container-app execution name (already non-secret)
        # error shape (class only -- NEVER the message body unscrubbed)
        "error_type",  # exception class name, e.g. "OperationalError"
    }
)

# Values for these keys must be simple scalars; dicts/lists are stringified then
# scrubbed, to avoid nested data smuggling PII through an allowed key.
_SCALAR_ONLY = True


# ---------------------------------------------------------------------------
# 2. DENYLIST SCRUBBER -- backstop patterns. Shared in spirit with the
#    shipper-level ruleset (redaction_patterns.yaml) so the two layers agree.
#    Order matters: most specific first.
# ---------------------------------------------------------------------------
_SCRUB = "***REDACTED***"

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # DB URLs with inline credentials: postgres://user:pass@host/db, mysql://, etc.
    (re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s:/@]+:[^\s:/@]+@[^\s]+"), _SCRUB),
    # Azure Storage / Service Bus / Relay connection strings & SAS.
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
    (re.compile(r"(?i)[?&]s(?:ig|v|p|e|t|r)=[^\s&\"']+"), _SCRUB),  # SAS query params
    # Fernet keys (44-char urlsafe-b64 ending '='), and generic long b64 secrets.
    (re.compile(r"\b[A-Za-z0-9\-_]{43}=\b"), _SCRUB),
    (re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"), _SCRUB),
    # JWTs.
    (re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"), _SCRUB),
    # Named secret env values if they ever appear as KEY=value in text.
    (
        re.compile(
            r"(?i)\b(PGPASSWORD|TF_VAR_ADMIN_PASSWORD|ENCRYPTION_KEY|"
            r"BLOB_CONN_STR|ARM_ACCESS_KEY|ARM_CLIENT_SECRET|"
            r"password|passwd|secret|token|api[_-]?key)\s*[=:]\s*[^\s,;\"']+"
        ),
        r"\1=" + _SCRUB,
    ),
    # PII: emails.
    (re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"), _SCRUB),
    # PII: long digit runs (account / card / phone-ish). Tune length to your data;
    # 9+ avoids nuking row_counts and dates.
    (re.compile(r"\b\d{9,}\b"), _SCRUB),
]


def scrub(text: str) -> str:
    """Run all denylist patterns over a string. Idempotent and cheap."""
    if not text:
        return text
    for pat, repl in _PATTERNS:
        text = pat.sub(repl, text)
    return text


# ---------------------------------------------------------------------------
# 3. Host label -- which tier emitted this. Overridable via env so the same
#    code labels correctly on Pi, laptop, and Azure.
# ---------------------------------------------------------------------------
def _detect_host() -> str:
    explicit = os.getenv("OBS_HOST")
    if explicit:
        return explicit
    if os.getenv("CONTAINER_APP_JOB_NAME") or os.getenv("CONTAINER_APP_NAME"):
        return "azure"
    # crude but effective fallbacks; override with OBS_HOST on the Pi/laptop.
    host = socket.gethostname().lower()
    if "pi" in host or "raspberry" in host:
        return "pi"
    return host or "unknown"


_HOST = _detect_host()


# ---------------------------------------------------------------------------
# 4. The JSON formatter: allowlist + scrub, emit one compact JSON object/line.
# ---------------------------------------------------------------------------
class SafeJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        raw_fields = getattr(record, "fields", None) or {}

        safe: dict[str, object] = {}
        dropped: list[str] = []
        for key, value in raw_fields.items():
            if key not in ALLOWED_FIELDS:
                dropped.append(key)
                continue
            if isinstance(value, (dict, list, tuple, set)):
                value = scrub(json.dumps(value, default=str))
            elif isinstance(value, str):
                value = scrub(value)
            safe[key] = value

        entry: dict[str, object] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "host": _HOST,
            # message is free-text -> ALWAYS scrubbed.
            "message": scrub(record.getMessage()),
        }
        entry.update(safe)

        # Record (without values) that something was withheld, so redaction is
        # observable rather than silent.
        if dropped:
            entry["dropped_fields"] = sorted(set(dropped))

        # Exceptions: keep the class name only, never the traceback body here
        # (a traceback can contain data / a connection string).
        if record.exc_info:
            exc_type = record.exc_info[0]
            entry["error_type"] = getattr(exc_type, "__name__", "Exception")

        return json.dumps(entry, ensure_ascii=False, default=str)


def get_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    """Return a logger wired to emit safe JSON on stdout. Idempotent."""
    logger = logging.getLogger(name)
    logger.setLevel(level)
    if not any(getattr(h, "_obs_safe", False) for h in logger.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(SafeJsonFormatter())
        handler._obs_safe = True  # type: ignore[attr-defined]
        logger.addHandler(handler)
        logger.propagate = False  # don't double-emit through the root logger
    return logger


def configure_root_logging(level: int = logging.INFO) -> None:
    """Install the safe JSON formatter on the ROOT logger.

    Call this ONCE at process start from an entrypoint, in place of
    logging.basicConfig(). It ensures third-party logs (apscheduler, sqlalchemy,
    azure-sdk, ...) are ALSO emitted as scrubbed JSON -- otherwise those records
    would bypass the redaction entirely and could leak a connection string or
    token in a library warning/traceback.

    App loggers created via get_logger() set propagate=False and carry their own
    handler, so they will NOT double-emit through this root handler.
    """
    root = logging.getLogger()
    root.setLevel(level)
    # Drop any pre-existing (non-safe) handlers, e.g. from a prior basicConfig,
    # so nothing emits unscrubbed text alongside the safe JSON.
    for h in list(root.handlers):
        if not getattr(h, "_obs_safe", False):
            root.removeHandler(h)
    if not any(getattr(h, "_obs_safe", False) for h in root.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(SafeJsonFormatter())
        handler._obs_safe = True  # type: ignore[attr-defined]
        root.addHandler(handler)


# ---------------------------------------------------------------------------
# 5. Audit line for the EXCLUDED PII tools. They do NOT ship their stdout; if
#    you want a record that they ran, call this -- it emits only fixed, safe
#    fields and never a path, a row value, or a count of rows.
# ---------------------------------------------------------------------------
def audit_event(event: str, status: str = "ok", **safe_fields: object) -> None:
    log = get_logger("audit")
    fields = {"event": event, "status": status}
    for k, v in safe_fields.items():
        if k in ALLOWED_FIELDS:
            fields[k] = v  # unknown keys silently ignored by the allowlist anyway
    log.info(event, extra={"fields": fields})


if __name__ == "__main__":
    # Self-demo: shows allowlist dropping + scrubbing in action.
    log = get_logger("demo")
    log.info(
        "partition loaded",
        extra={
            "fields": {
                "event": "load_partition",
                "table": "dim_users",
                "row_count": 5,
                "run_date": "2026-07-12",
                "status": "ok",
                # these two are NOT allowed -> dropped, and reported as dropped_fields:
                "raw_row": {"email": "a@b.com", "ssn": "123456789"},
                "database_url": "postgres://svc:hunter2@db.internal/staging",
            }
        },
    )
    # message carrying a secret -> scrubbed, not allowlisted away:
    log.error("connect failed to postgres://svc:hunter2@db.internal/staging")
    audit_event("decrypt_export", status="ok", table_count=10)
