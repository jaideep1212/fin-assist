"""
Tests for app/obs_logging.py -- the redaction layer.

These are the security-critical tests: they prove the ALLOWLIST drops anything
not explicitly safe (fail-closed) and the SCRUBBER masks secrets/PII even inside
allowed fields, the free-text message, and exceptions. If any of these regress,
data can leak to the log pipeline, so treat failures here as blocking.

Pure and deterministic: no network, no DB, no real clock. Formatter output is
parsed back from JSON and asserted on directly.
"""

import io
import json
import logging

from app.obs_logging import (
    ALLOWED_FIELDS,
    SafeJsonFormatter,
    audit_event,
    configure_root_logging,
    get_logger,
    scrub,
    _detect_host,
)

REDACT = "***REDACTED***"


def render(msg="msg", fields=None, exc_info=None, level=logging.INFO, name="test"):
    """Format one LogRecord through SafeJsonFormatter and return the parsed dict.

    Bypasses stream/handler plumbing so we test the formatter logic directly and
    deterministically (no capsys / no pre-bound stdout handler to fight)."""
    rec = logging.LogRecord(name, level, __file__, 1, msg, (), exc_info)
    if fields is not None:
        rec.fields = fields
    return json.loads(SafeJsonFormatter().format(rec))


# --- scrubber: every pattern must mask, and safe values must survive ---------


def test_scrub_masks_db_url_with_credentials():
    out = scrub("connect failed: postgres://svc:hunter2@db.internal:5432/staging")
    assert "hunter2" not in out
    assert "svc" not in out
    assert REDACT in out


def test_scrub_masks_azure_connection_string_and_sas():
    out = scrub(
        "DefaultEndpointsProtocol=https;AccountName=finassistdata;"
        "AccountKey=abc123def456==;"
    )
    assert "abc123def456==" not in out
    sas = scrub("https://acct.blob.core.windows.net/c/b?sig=deadbeef&se=2026")
    assert "deadbeef" not in sas


def test_scrub_masks_jwt():
    token = "eyJhbGciOi.eyJzdWIiOiIxMjM.SflKxwRJSMeKKF2QT4"
    assert token not in scrub(f"authorization: Bearer {token}")


def test_scrub_masks_fernet_key():
    key = "dGhpcy1pcy1hLTMyLWJ5dGUtZmVybmV0LWtleS0xMjM0NT0="
    assert key not in scrub(f"ENCRYPTION_KEY loaded {key}")


def test_scrub_masks_named_secret_assignments():
    for line in (
        "PGPASSWORD=supersecret",
        "TF_VAR_ADMIN_PASSWORD=hunter2",
        "ENCRYPTION_KEY: abcd",
        "password=letmein",
    ):
        out = scrub(line)
        assert "secret" not in out.lower().replace("redacted", "")
        assert REDACT in out


def test_scrub_masks_email_and_long_digit_runs():
    assert "jane.doe@example.com" not in scrub("user jane.doe@example.com logged in")
    assert "1234567890123456" not in scrub("account 1234567890123456 processed")


def test_scrub_leaves_safe_values_intact():
    # Small counts and ISO dates must survive -- the scrubber must not eat them.
    safe = "loaded staging.dim_users (5 rows) for run_date 2026-07-12"
    assert scrub(safe) == safe


def test_scrub_is_idempotent():
    s = "postgres://u:p@h/db and jane@x.com"
    assert scrub(scrub(s)) == scrub(s)


# --- allowlist: fail-closed. Unknown fields dropped; only safe keys emitted ---


def test_allowlist_drops_unknown_fields_and_reports_them():
    out = render(
        fields={
            "event": "load_partition",
            "row_count": 5,
            "raw_row": {"ssn": "123456789", "email": "a@b.com"},  # NOT allowed
            "database_url": "postgres://u:p@h/db",  # NOT allowed
        }
    )
    assert out["event"] == "load_partition"
    assert out["row_count"] == 5
    # dropped fields are named but their VALUES never appear anywhere.
    assert set(out["dropped_fields"]) == {"raw_row", "database_url"}
    blob = json.dumps(out)
    assert "123456789" not in blob
    assert "a@b.com" not in blob
    assert "hunter2" not in blob and "u:p@h" not in blob


def test_allowlist_keeps_only_declared_safe_fields():
    out = render(
        fields={
            "event": "table_loaded",
            "status": "ok",
            "table": "dim_users",
            "row_count": 14,
            "run_date": "2026-07-12",
        }
    )
    for k in ("event", "status", "table", "row_count", "run_date"):
        assert k in out
    assert "dropped_fields" not in out  # nothing was dropped


def test_allowed_field_with_nested_value_is_stringified_and_scrubbed():
    # A dict/list value on an ALLOWED key must not smuggle PII through.
    out = render(fields={"table": {"leak": "jane@x.com"}})
    assert "jane@x.com" not in json.dumps(out)
    assert REDACT in out["table"]


def test_allowlist_constant_is_small_and_intentional():
    # Guard against accidental allowlist bloat -- adding a field is a deliberate
    # act. If this fails, someone widened the allowlist; confirm it's safe.
    assert len(ALLOWED_FIELDS) <= 20
    assert (
        "message" not in ALLOWED_FIELDS
    )  # message is always scrubbed, never allowlisted-through


# --- message + exceptions are always scrubbed ---


def test_message_is_always_scrubbed():
    out = render(msg="connect failed to postgres://svc:hunter2@db/staging")
    assert "hunter2" not in out["message"]
    assert REDACT in out["message"]


def test_exception_emits_class_only_never_body():
    try:
        raise RuntimeError("boom postgres://svc:hunter2@db.internal/staging")
    except RuntimeError:
        import sys

        out = render(msg="job failed", exc_info=sys.exc_info())
    assert out["error_type"] == "RuntimeError"
    # the secret inside the exception message must not leak via any channel.
    assert "hunter2" not in json.dumps(out)


def test_base_envelope_fields_present():
    out = render(msg="hi")
    for k in ("ts", "level", "logger", "host", "message"):
        assert k in out


# --- audit_event: only safe fields, for the excluded PII tools ---


def _capture(logger_name):
    """Attach a StringIO handler to a named logger; return (read_fn, detach_fn).
    Works regardless of any pre-bound stdout handler."""
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(SafeJsonFormatter())
    lg = logging.getLogger(logger_name)
    lg.addHandler(h)
    return buf, lambda: lg.removeHandler(h)


def test_audit_event_emits_only_safe_fields():
    buf, detach = _capture("audit")
    try:
        audit_event(
            "decrypt_export",
            status="ok",
            table_count=10,
            secret_path="/tmp/decrypted/users.csv",
        )  # not allowed -> dropped
    finally:
        detach()
    line = json.loads(buf.getvalue().strip().splitlines()[-1])
    assert line["event"] == "decrypt_export"
    assert line["status"] == "ok"
    assert line["table_count"] == 10
    assert "secret_path" not in json.dumps(line)
    assert "/tmp/decrypted" not in json.dumps(line)


# --- host detection ---


def test_detect_host_prefers_explicit_env(monkeypatch):
    monkeypatch.setenv("OBS_HOST", "pi")
    assert _detect_host() == "pi"


def test_detect_host_recognises_container_apps_job(monkeypatch):
    monkeypatch.delenv("OBS_HOST", raising=False)
    monkeypatch.delenv("CONTAINER_APP_NAME", raising=False)
    monkeypatch.setenv("CONTAINER_APP_JOB_NAME", "orchestrator-job")
    assert _detect_host() == "azure"


# --- get_logger / configure_root_logging plumbing ---


def test_get_logger_is_idempotent_no_duplicate_handlers():
    a = get_logger("dup.test")
    before = len(a.handlers)
    b = get_logger("dup.test")
    assert a is b
    assert len(b.handlers) == before  # no second handler added


def test_configure_root_logging_replaces_plain_handlers():
    root = logging.getLogger()
    root.addHandler(logging.StreamHandler())  # a plain, non-safe handler
    configure_root_logging()
    safe = [h for h in root.handlers if getattr(h, "_obs_safe", False)]
    plain = [h for h in root.handlers if not getattr(h, "_obs_safe", False)]
    assert len(safe) == 1
    assert plain == []  # the plain handler was removed
