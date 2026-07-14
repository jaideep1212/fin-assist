"""
Tests for the redaction in run_staging_pipeline.py (the orchestrator).

The orchestrator ships straight to Log Analytics with no shipper in front, so
run()'s _redact() is the ONLY thing standing between terraform/az output and the
logs. These tests prove it masks this project's own identifiers AND real secrets,
on both the command echo and streamed subprocess output. Failures here mean the
orchestrator can leak, so treat them as blocking.
"""

import importlib
import pytest

# Identifiers used across the tests (fake, structurally valid GUIDs).
SUB = "2430b6d8-388d-444f-b179-9bf8a92e9955"
CLIENT = "49313fed-5786-491d-82da-a1618a21933b"
TENANT = "b0b239d2-b9cd-4a83-b1e1-a6b28a399df8"


@pytest.fixture
def orc(monkeypatch):
    """Import the orchestrator with the ARM_* identifiers set, and reset the
    cached known-IDs so _redact() recomputes them from this test's env."""
    monkeypatch.setenv("ARM_SUBSCRIPTION_ID", SUB)
    monkeypatch.setenv("ARM_CLIENT_ID", CLIENT)
    monkeypatch.setenv("ARM_TENANT_ID", TENANT)
    module = importlib.import_module("run_staging_pipeline")
    module._KNOWN_IDS = None  # force recompute against the env above
    return module


# --- _redact: mask this project's own identifiers -----------------------------


def test_redact_masks_subscription_id(orc):
    out = orc._redact(f"--subscription {SUB} --output none")
    assert SUB not in out
    assert "***SUBSCRIPTION***" in out


def test_redact_masks_client_and_tenant_ids(orc):
    out = orc._redact(f"login --client-id {CLIENT} --tenant {TENANT}")
    assert CLIENT not in out and TENANT not in out
    assert "***CLIENT_ID***" in out and "***TENANT***" in out


def test_redact_masks_id_inside_terraform_resource_path(orc):
    # the exact shape the user saw leaking from terraform output
    line = (
        f"azurerm_postgresql_flexible_server.staging: Still destroying... "
        f"[id=/subscriptions/{SUB}/resourceGroups/fin-assist-rg/providers/"
        f"Microsoft.DBforPostgreSQL/flexibleServers/fin-assist-staging-db]"
    )
    out = orc._redact(line)
    assert SUB not in out
    # non-sensitive parts survive so the line is still useful
    assert "fin-assist-staging-db" in out
    assert "Still destroying" in out


# --- _redact: mask REAL secrets via the shared scrubber -----------------------


def test_redact_masks_db_url_with_password(orc):
    out = orc._redact(
        "connect: postgres://stagingadmin:HunterPass2@fin-assist-staging-db"
        ".postgres.database.azure.com/staging"
    )
    assert "HunterPass2" not in out
    assert "stagingadmin" not in out


def test_redact_masks_named_secret_and_key(orc):
    assert "letmein" not in orc._redact("TF_VAR_ADMIN_PASSWORD=letmein")
    key = "dGhpcy1pcy1hLTMyLWJ5dGUtZmVybmV0LWtleS0xMjM0NT0="
    assert key not in orc._redact(f"ENCRYPTION_KEY={key}")


# --- _redact: safe text is left intact ---------------------------------------


def test_redact_leaves_safe_text_alone(orc):
    safe = "Apply complete! Resources: 3 added, 0 changed, 0 destroyed."
    assert orc._redact(safe) == safe


# --- run(): streamed subprocess output is redacted before it hits stdout ------


def test_run_streams_subprocess_output_redacted(orc, capsys):
    # A child process that prints a line containing BOTH a known id and a secret.
    payload = f"creating id=/subscriptions/{SUB}/x and postgres://u:secretpw@h/db"
    orc.run(["python", "-c", f"print({payload!r})"])
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert SUB not in combined  # id masked
    assert "secretpw" not in combined  # secret masked
    assert "***SUBSCRIPTION***" in combined
    assert "creating id=" in combined  # structure/readability preserved


def test_run_failed_command_raises_systemexit(orc):
    # non-zero exit still aborts (behaviour preserved from before the conversion)
    with pytest.raises(SystemExit):
        orc.run(["python", "-c", "import sys; sys.exit(3)"])
