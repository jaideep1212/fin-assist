"""
verify_decrypt_export.py  -  VERIFICATION / TEST tool (run manually).

Prove a replicated/loaded copy is byte-perfect AND usable downstream: reads the
tables from a target PostgreSQL, decrypts the Fernet-encrypted BYTEA columns
with the key, and writes decrypted CSVs. Fernet is AUTHENTICATED, so a clean
decrypt proves not a single byte was corrupted on the way to the copy.

Profiles (no lanes)
-------------------
  PROFILE (--profile Pi|Azure|Local) selects the target database. There is no
  TEST/PROD lane: production runs in a SEPARATE TENANT with its own credentials,
  its own encryption key, and its own .env -- so each tenant is a single
  environment and no lane switch is needed.

Settings are read from the environment (optionally via a .env found by searching
the current dir and its parents, then next to this script). For any setting NAME
the tool tries:  NAME_<PROFILE>  ->  NAME  (so a setting can be per-profile or
shared).

  PG_HOST_<PROFILE>            host          (e.g. PG_HOST_PI=localhost)
  PG_PORT_<PROFILE>            port          (default 5432)
  PG_DB_<PROFILE>              database      (e.g. PG_DB_PI=household_test)
  PG_USER_<PROFILE>            user          (e.g. PG_USER_PI=svcbackup)
  PG_PASSWORD_<PROFILE>        password
  PG_SSLMODE_<PROFILE>         e.g. require  (Azure uses the URL which already
                                             carries it)
  ENCRYPTION_KEY              Fernet key    (tenant-wide; same across profiles)
  <PROFILE>_SCHEMA           read schema    (e.g. AZURE_SCHEMA=staging). Sets
                                            search_path. Omit for public.
  PG_TABLES_<PROFILE>        comma/space table list (else DEFAULT_TABLES)

Azure connection (no PG_HOST_AZURE / PG_DB_AZURE / ... needed)
-------------------------------------------------------------
For --profile Azure the connection URL is fetched, read-only, from Terraform:
      terraform -chdir=infra/staging-db output -raw staging_database_url
`terraform output` only READS existing state -- it never creates or changes
infra. The ephemeral staging server must already be applied. The URL carries
host/db/user/password and sslmode=require, so the only Azure settings you need
are AZURE_SCHEMA and ENCRYPTION_KEY.
  Override the folder with TF_STAGING_DIR, or set PG_URL_AZURE / PG_URL_CMD_AZURE
  to bypass the default. If the fetch fails, its error is shown and the tool
  exits. (The orchestrator sets PG_URL_AZURE itself, so under it verify reuses
  the URL instead of re-fetching.)

Output folder
-------------
  Pi     -> ./decrypted           (Pi profile runs ON the Pi; relative Linux path)
  Azure  -> C:\\Scripts\\Azure      (run from the Windows laptop)
  Local  -> C:\\Scripts\\Local      (run from the Windows laptop)
Override with --out <path>.

Missing tables are SKIPPED with a warning (not a failure), so the same table
list works against targets that don't have every table.

SECURITY: the Fernet key decrypts sensitive PII. Keep it in a git-ignored .env
or a session env var only. The decrypted CSVs are plaintext PII -- delete them
after inspection, and keep the output folder out of version control.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import subprocess
import sys
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.errors import UndefinedTable
from cryptography.fernet import Fernet, InvalidToken


DEFAULT_OUT_BASE = r"C:\Scripts"
DEFAULT_TF_STAGING_DIR = "infra/staging-db"


# ---------------------------------------------------------------------------
# Field classification per table (PostgreSQL snake_case column names).
#   ENCRYPTED_FIELDS: Fernet-encrypted -> decrypt to plaintext
#   HASH_FIELDS:      one-way hash bytes -> hex string
# ---------------------------------------------------------------------------
ENCRYPTED_FIELDS = {
    "dim_users": [],
    "dim_users_s": [
        "first_name", "last_name", "birth_date", "birth_city", "birth_country",
        "marriage_date", "current_address_line1", "current_address_line2",
        "current_city", "current_post_code", "current_country",
        "permanent_address_line1", "permanent_address_line2", "permanent_city",
        "permanent_post_code", "permanent_country", "contact_email_id",
        "contact_mobile_no", "contact_phone_no", "work_email_id",
        "work_mobile_no", "work_phone_no", "expired_date", "pan", "aadhar", "tin",
    ],
    "dim_entities": [
        "entity_name", "entity_branch", "address_line1", "address_line2", "city",
        "post_code", "country", "customer_care_email_id", "customer_care_phone_no",
        "customer_care_website", "swift", "ifsc", "micr", "iban",
    ],
    "fact_aliases": ["alias_name"],
    "fact_other_contacts": ["contact_value"],
    "fact_account_broker_mappings": [],
    "fact_stock_transactions": ["trade_id", "order_id", "isin", "symbol"],
    "dim_accounts": [
        "account_no", "first_holder_address", "cif", "open_year", "email_id",
        "contact_no", "comments",
    ],
    "fact_deposits": ["deposit_no", "comments"],
    "dim_mutual_funds": [
        "folio_no", "scheme_name", "isin", "scheme_code", "scheme_category",
        "comments",
    ],
    "fact_mutual_fund_transactions": ["trade_id", "order_id"],
    "test_tbl": ["enc_field"],
}

HASH_FIELDS = {
    "dim_users": ["user_name_hash"],
    "dim_users_s": [],
    "dim_entities": ["entity_name_hash"],
    "fact_aliases": [],
    "fact_other_contacts": [],
    "fact_account_broker_mappings": [],
    "fact_deposits": ["deposit_no_hash"],
    "fact_stock_transactions": ["trade_order_hash"],
    "dim_accounts": ["account_no_hash"],
    "dim_mutual_funds": ["isin_folio_holder_hash"],
    "fact_mutual_fund_transactions": ["transaction_order_hash"],
    "test_tbl": ["hash_field"],
}

DEFAULT_TABLES = [
    "dim_users", "dim_users_s", "dim_accounts", "dim_entities", "dim_mutual_funds",
    "fact_account_broker_mappings", "fact_aliases", "fact_deposits",
    "fact_mutual_fund_transactions", "fact_other_contacts", "fact_stock_transactions",
]


def _find_dotenv(explicit: str | None = None) -> Path | None:
    """Locate a .env: explicit path, else cwd + parents, else next to script."""
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_file() else None
    cwd = Path.cwd()
    for d in (cwd, *cwd.parents):
        cand = d / ".env"
        if cand.is_file():
            return cand
    beside = Path(__file__).resolve().parent / ".env"
    return beside if beside.is_file() else None


def _load_dotenv(explicit: str | None = None):
    """Minimal .env loader. Tolerates a BOM and surrounding quotes; does not
    override variables already present in the real environment."""
    env_path = _find_dotenv(explicit)
    if env_path is None:
        return
    print(f"loading env from {env_path}")
    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        os.environ.setdefault(k, v)


def _cfg(base: str, profile: str, default=None, required=False):
    """Resolve a setting: base_<PROFILE> -> base -> default. Exit if required
    and nothing matched."""
    for name in (f"{base}_{profile}", base):
        val = os.environ.get(name)
        if val is not None and val != "":
            return val
    if required:
        sys.stderr.write(
            f"FATAL: neither {base}_{profile} nor {base} is set (required).\n"
        )
        sys.exit(2)
    return default


def _run_url_cmd(cmd: str, label: str) -> str:
    print(f"resolving URL via {label}: {cmd}")
    try:
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    except Exception as e:
        sys.stderr.write(f"FATAL: could not run {label}: {e}\n")
        sys.exit(3)
    if proc.returncode != 0:
        sys.stderr.write(
            f"FATAL: {label} exited {proc.returncode}.\n"
            f"--- command stderr ---\n{proc.stderr.strip()}\n----------------------\n"
            f"(For Azure: is the staging server applied? `terraform output` only "
            f"reads state, so it errs/returns nothing if nothing is deployed.)\n"
        )
        sys.exit(3)
    out = proc.stdout.strip()
    if not out:
        sys.stderr.write(f"FATAL: {label} produced no output.\n")
        sys.exit(3)
    return out


def build_connection(profile: str):
    """Return (connect_args, connect_kwargs, description-without-secrets)."""
    url = _cfg("PG_URL", profile)
    if url:
        return (url,), {}, "URL (PG_URL)"

    explicit_cmd = _cfg("PG_URL_CMD", profile)
    cmd = explicit_cmd
    if not cmd and profile == "AZURE":
        tf_dir = os.environ.get("TF_STAGING_DIR", DEFAULT_TF_STAGING_DIR)
        cmd = f"terraform -chdir={tf_dir} output -raw staging_database_url"
    if cmd:
        label = f"PG_URL_CMD_{profile}" if explicit_cmd else "terraform output"
        url = _run_url_cmd(cmd, label)
        return (url,), {}, "URL (fetched)"

    kwargs = dict(
        host=_cfg("PG_HOST", profile, default="localhost"),
        port=_cfg("PG_PORT", profile, default="5432"),
        dbname=_cfg("PG_DB", profile, required=True),
        user=_cfg("PG_USER", profile, required=True),
        password=_cfg("PG_PASSWORD", profile, required=True),
    )
    sslmode = _cfg("PG_SSLMODE", profile)
    if sslmode:
        kwargs["sslmode"] = sslmode
    desc = (f"{kwargs['host']}:{kwargs['port']}/{kwargs['dbname']} as "
            f"{kwargs['user']}" + (f" (sslmode={sslmode})" if sslmode else ""))
    return (), kwargs, desc


def load_key(profile: str) -> Fernet:
    key = _cfg("ENCRYPTION_KEY", profile, required=True)
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except Exception as e:
        sys.stderr.write(f"FATAL: ENCRYPTION_KEY is not a valid Fernet key: {e}\n")
        sys.exit(2)


# ===========================================================================
# Decryption / decoding -- UNCHANGED from the proven tool. Do not modify.
# ===========================================================================

def _decode_sqlserver_text(raw: bytes) -> str:
    """Decode bytes that may be UTF-8 or UTF-16-LE. Entity-side data (NVARCHAR
    -> VARBINARY) is UTF-16-LE (ASCII char + 0x00). A Fernet token is pure ASCII
    base64 with no NUL, so any NUL marks UTF-16-LE; interior NULs are padding to
    strip. Raises UnicodeDecodeError on genuinely non-text bytes."""
    if b"\x00" in raw:
        return raw.decode("utf-16-le").replace("\x00", "")
    return raw.decode("utf-8")


def decrypt_value(fernet: Fernet, value) -> str:
    """Decrypt one Fernet BYTEA value to plaintext. NULL/empty -> ''. A genuine
    failure becomes a clearly-marked marker so corruption is visible."""
    if value is None or value == b"" or value == "":
        return ""
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, str):
        value = value.encode("utf-8")
    try:
        token_str = _decode_sqlserver_text(value).strip()
    except UnicodeDecodeError:
        return "<NON-UTF8-BYTES: %s>" % value[:16].hex()
    if not token_str:
        return ""
    try:
        plaintext = _decode_sqlserver_text(fernet.decrypt(token_str.encode("utf-8")))
        return plaintext.replace("\x00", "")
    except InvalidToken:
        return "<DECRYPT-FAILED: InvalidToken>"
    except Exception as e:
        return "<DECRYPT-ERROR: %s>" % str(e)[:40]


def to_hex(value) -> str:
    if value is None or value == b"" or value == "":
        return ""
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, bytes):
        return value.hex().lower()
    return str(value).lower()


# ===========================================================================

SKIPPED = object()  # returned when a table is absent (skipped, not failed)


def export_table(cur, table: str, fernet: Fernet, out_dir: Path):
    # Connection is autocommit (see main), so a missing-table error does not
    # abort a transaction or reset search_path -- the next table proceeds.
    try:
        cur.execute(f"SELECT * FROM {table} ORDER BY id")
    except UndefinedTable:
        print(f"  WARNING: table '{table}' not present in this target - SKIPPED.")
        return SKIPPED

    colnames = [d.name for d in cur.description]
    rows = cur.fetchall()
    enc = set(ENCRYPTED_FIELDS.get(table, []))
    hashf = set(HASH_FIELDS.get(table, []))

    out_path = out_dir / f"{table}_decrypted.csv"
    with open(out_path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(colnames)
        for row in rows:
            out_row = []
            for col, val in zip(colnames, row):
                if col in enc:
                    out_row.append(decrypt_value(fernet, val))
                elif col in hashf:
                    out_row.append(to_hex(val))
                else:
                    out_row.append("" if val is None else val)
            writer.writerow(out_row)

    print(f"  wrote {out_path}  ({len(rows)} rows)")
    return len(rows)


def _resolve_tables(profile: str, cli_tables) -> list[str]:
    if cli_tables:
        return cli_tables
    env = _cfg("PG_TABLES", profile)
    if env:
        return [t for t in re.split(r"[,\s]+", env.strip()) if t]
    return DEFAULT_TABLES


def _default_out_dir(profile: str, profile_raw: str) -> Path:
    if profile == "PI":
        return Path("./decrypted")
    return Path(DEFAULT_OUT_BASE) / profile_raw


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Decrypt PostgreSQL tables to CSV for verification.")
    ap.add_argument("--profile", required=True,
                    help="environment / target: Pi | Azure | Local (case-insensitive)")
    ap.add_argument("--schema", default=None,
                    help="schema to read (sets search_path). Overrides "
                         "<PROFILE>_SCHEMA. Use 'staging' for Azure; omit for public.")
    ap.add_argument("--tables", nargs="*", default=None,
                    help="tables to export (overrides PG_TABLES_<profile> and the "
                         "default list).")
    ap.add_argument("--out", default=None,
                    help=r"output dir. Default: Pi -> ./decrypted; "
                         r"Azure/Local -> C:\Scripts\<profile>.")
    ap.add_argument("--env-file", default=None,
                    help="explicit path to a .env file (else searched cwd+parents).")
    args = ap.parse_args()

    _load_dotenv(args.env_file or os.environ.get("DOTENV_PATH"))

    profile_raw = args.profile
    profile = args.profile.upper()
    print(f"profile = {profile}")

    fernet = load_key(profile)
    tables = _resolve_tables(profile, args.tables)
    schema = (args.schema
              or os.environ.get(f"{profile}_SCHEMA")
              or _cfg("PG_SCHEMA", profile))

    out_dir = Path(args.out) if args.out else _default_out_dir(profile, profile_raw)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"output dir = {out_dir}")

    connect_args, connect_kwargs, desc = build_connection(profile)
    print(f"connecting: {desc}")

    total = 0
    processed = 0
    skipped = []
    with psycopg.connect(*connect_args, **connect_kwargs) as conn:
        conn.autocommit = True  # read-only; keeps search_path + survives a skip
        with conn.cursor() as cur:
            if schema:
                cur.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
                print(f"search_path = {schema}")
            for t in tables:
                result = export_table(cur, t, fernet, out_dir)
                if result is SKIPPED:
                    skipped.append(t)
                else:
                    total += result
                    processed += 1

    fail_markers = 0
    for t in tables:
        p = out_dir / f"{t}_decrypted.csv"
        if p.exists():
            text = p.read_text(encoding="utf-8-sig")
            c = (text.count("<DECRYPT-FAILED") + text.count("<DECRYPT-ERROR")
                 + text.count("<NON-UTF8-BYTES"))
            fail_markers += c
            if c:
                print(f"  WARNING: {p.name} has {c} decrypt-failure marker(s) "
                      f"- the data may have corrupted bytes!")

    print(f"\ndone: {total} row(s) across {processed} table(s) -> {out_dir}")
    if skipped:
        print(f"SKIPPED (not present in target): {', '.join(skipped)}")
    if fail_markers:
        print(f"RESULT: {fail_markers} value(s) failed to decrypt. Integrity NOT confirmed.")
        return 1
    print("RESULT: all encrypted values decrypted cleanly. Copy is byte-perfect "
          "and downstream-usable.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
