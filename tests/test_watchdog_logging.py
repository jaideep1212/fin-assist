"""
Integration tests for the STRUCTURED LOGGING conversion of app/watchdog.py.

Distinct from test_watchdog.py (which tests the state machine): these assert the
converted module emits well-formed structured events and -- critically -- that a
secret surfacing in a failing job does NOT leak into the logs. Drives the real
DailyWatchdog with injected fakes; captures the "watchdog" logger's output.
"""

import io
import json
import logging
from datetime import datetime, time as dtime, timezone

import pytest

from app.obs_logging import SafeJsonFormatter
from app.watchdog import DailyWatchdog

TZ = "UTC"
NOON = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def events():
    """Capture "watchdog" logger output as a list of parsed JSON dicts.

    Attaches its own StringIO handler so it works regardless of the module's
    pre-bound stdout handler (which stays, harmlessly, writing elsewhere)."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(SafeJsonFormatter())
    lg = logging.getLogger("watchdog")
    lg.addHandler(handler)

    def read():
        text = buf.getvalue().strip()
        return [json.loads(line) for line in text.splitlines() if line]

    yield read
    lg.removeHandler(handler)


def _wd(job, *, awake=True, marker=None):
    return DailyWatchdog(
        job=job,
        is_awake=lambda: awake,
        cutoff=dtime(23, 59),
        day_start=dtime(0, 0),
        tz=TZ,
        clock=lambda: NOON,
        marker=marker,
    )


def test_success_emits_armed_then_job_done(events):
    wd = _wd(lambda run_date: None)
    wd.tick()
    kinds = [e.get("event") for e in events()]
    assert "armed" in kinds
    assert "job_done" in kinds
    done = next(e for e in events() if e.get("event") == "job_done")
    assert done["status"] == "ok"
    assert done["run_date"] == "2026-07-12"


def test_events_are_valid_json_with_envelope(events):
    _wd(lambda run_date: None).tick()
    for e in events():
        for key in ("ts", "level", "logger", "host", "message", "event"):
            assert key in e


def test_failing_job_logs_error_type_not_the_secret(events):
    """The crown-jewels test: a job that raises with a connection string in the
    message must log a scrubbed event with only the exception class name."""

    def bad(run_date):
        raise RuntimeError(
            "connect failed: postgres://svc:hunter2@db.internal:5432/staging"
        )

    _wd(bad).tick()
    failed = next(e for e in events() if e.get("event") == "job_failed")
    assert failed["status"] == "error"
    assert failed["error_type"] == "RuntimeError"
    # the secret must not appear anywhere across ALL emitted lines.
    blob = json.dumps(events())
    assert "hunter2" not in blob
    assert "svc:hunter2" not in blob
    assert (
        "db.internal" not in blob or "***" in blob
    )  # host may ride in traceback; must be scrubbed


def test_pi_unreachable_emits_retry(events):
    _wd(lambda run_date: None, awake=False).tick()
    kinds = {e.get("event") for e in events()}
    assert "pi_unreachable" in kinds
    retry = next(e for e in events() if e.get("event") == "pi_unreachable")
    assert retry["status"] == "retry"


def test_no_pii_or_secret_fields_in_any_event(events):
    """Whatever the watchdog logs, no event should carry a raw URL/secret shape."""

    def bad(run_date):
        raise ValueError("PGPASSWORD=letmein at postgres://u:p@h/db")

    _wd(bad).tick()
    blob = json.dumps(events())
    assert "letmein" not in blob
    assert "u:p@h" not in blob
