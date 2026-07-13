"""
export_decrypted_tables_to_csv.py  -  build the "verified_data" export.

Reads the two transaction tables from the staging PostgreSQL, decrypts their
Fernet-encrypted BYTEA columns, resolves the person/entity/fund IDs they
reference to (masked) names, DROPS all raw ID columns, and uploads one CSV per
table to
    finassistdata / landing-zone / verified_data/<table>.csv

Tables exported (decrypted, "basic" form -- all id / *_id columns removed):
  * fact_mutual_fund_transactions
  * fact_stock_transactions

Name enrichment (masked)
------------------------
IDs are used only to look values up; they are NOT written out. Instead a
resolved column is appended for each reference:
  * holder / nominee IDs  -> dim_users_s (joined on dim_users_s.user_id == the
    holder/nominee id; the id references the user, the name lives in _s).
  * entity / broker IDs   -> dim_entities.entity_name.
  * fund_id (MF txns)     -> dim_mutual_funds: folio_no + isin (in the clear)
    and the fund's first-holder name (masked).
Lookups fall back to fact_aliases (record_type = source table name) when a
primary name is blank. Every resolved NAME is MASKED to only its first two and
last two characters (interior letters/digits become '*'); folio_no/isin and the
other decrypted business columns are left in the clear.

Connection / key / schema resolution is shared with app/verify_decrypt_export.py
(the proven Postgres reader + Fernet decryptor) -- see --profile there. Defaults
to the Azure staging target, so under the orchestrator (which exports
PG_URL_AZURE, AZURE_SCHEMA and ENCRYPTION_KEY) it just works as a post-load step.

Usage:
    python tools/export_decrypted_tables_to_csv.py                  # Azure staging -> blob
    python tools/export_decrypted_tables_to_csv.py --profile Local  # a different target
    python tools/export_decrypted_tables_to_csv.py --no-upload --keep-local --out ./out

Blob upload uses `az storage blob upload --auth-mode login`, so an authenticated
Azure CLI session with "Storage Blob Data Contributor" on the account is
required (your `az login` on the laptop; the managed identity in the container).
"""

from __future__ import annotations

import argparse
import csv
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

import psycopg
from psycopg import sql
from psycopg.errors import UndefinedTable

# Reuse the proven connection + decryption helpers rather than re-implementing
# them (Fernet decoding of SQL-Server-origin UTF-16-LE bytes is subtle).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.verify_decrypt_export import (  # noqa: E402
    _load_dotenv,
    _cfg,
    build_connection,
    load_key,
    decrypt_value,
    to_hex,
)


# ---------------------------------------------------------------------------
# Blob destination
# ---------------------------------------------------------------------------
DEFAULT_ACCOUNT = "finassistdata"
DEFAULT_CONTAINER = "landing-zone"
DEFAULT_BLOB_DIR = "verified_data"

# Schema each profile reads from when nothing is set explicitly. The Azure
# staging DB puts every table in the `staging` schema, so default to it -- that
# way a manual run needs only ENCRYPTION_KEY in .env (the DB URL is fetched from
# terraform, the schema defaults here).
DEFAULT_PROFILE_SCHEMA = {"AZURE": "staging"}


# ---------------------------------------------------------------------------
# Per-table field classification (PostgreSQL snake_case columns).
#   ENCRYPTED_FIELDS: Fernet-encrypted BYTEA -> decrypt to plaintext
#   HASH_FIELDS:      one-way hash BYTEA      -> hex string
# ---------------------------------------------------------------------------
ENCRYPTED_FIELDS = {
    "fact_mutual_fund_transactions": ["trade_id", "order_id"],
    "fact_stock_transactions": ["trade_id", "order_id", "isin", "symbol"],
}

HASH_FIELDS = {
    "fact_mutual_fund_transactions": ["transaction_order_hash"],
    "fact_stock_transactions": ["trade_order_hash"],
}

# Name columns to append per table, as (output_column, kind, source_id_column).
#   kind "user"         -> masked full name from dim_users_s (by user_id)
#   kind "entity"       -> masked entity_name from dim_entities (by id)
#   kind "entity_clear" -> entity_name from dim_entities, NOT masked (brokers)
#   kind "registrar"    -> masked name of the entity's registrar (entities[registrar_id])
#   kind "fund"         -> a dim_mutual_funds attribute (by fund id; NOT masked;
#                          output_column names the attribute, e.g. scheme_name)
#   kind "fund_user"    -> masked holder/nominee name of the fund (by fund id);
#                          the fund id-field is output_column with _name -> _id
#                          (first_holder_name -> first_holder_id, etc.)
ENRICH_SPEC = {
    "fact_mutual_fund_transactions": [
        # fund_id -> dim_mutual_funds attributes (decrypted / hash->hex, in the
        # clear) plus the fund's holder/nominee names (masked). broker not masked.
        ("isin_folio_holder_hash", "fund", "fund_id"),
        ("folio_no", "fund", "fund_id"),
        ("scheme_name", "fund", "fund_id"),
        ("isin", "fund", "fund_id"),
        ("scheme_code", "fund", "fund_id"),
        ("scheme_category", "fund", "fund_id"),
        ("is_dividend", "fund", "fund_id"),
        ("first_holder_id", "fund", "fund_id"),
        ("first_holder_name", "fund_user", "fund_id"),
        ("joint_holder1_name", "fund_user", "fund_id"),
        ("joint_holder2_name", "fund_user", "fund_id"),
        ("nominee1_name", "fund_user", "fund_id"),
        ("nominee2_name", "fund_user", "fund_id"),
        ("broker_name", "entity_clear", "broker_id"),
    ],
    "fact_stock_transactions": [
        ("holder_name", "user", "holder_id"),
        ("nominee_name", "user", "nominee_id"),
        ("linked_entity_name", "entity", "linked_entity_id"),
        ("broker_name", "entity_clear", "broker_id"),
    ],
}

TARGET_TABLES = [
    "fact_mutual_fund_transactions",
    "fact_stock_transactions",
]

SKIPPED = object()  # returned when a target table is absent in the DB


# ---------------------------------------------------------------------------
# Masking
# ---------------------------------------------------------------------------


def mask_name(value) -> str:
    """Keep only the first two and last two characters; mask interior
    letters/digits with '*'. Strings of <=4 chars are already all first/last
    two, so they pass through unchanged."""
    if value is None:
        return ""
    s = str(value)
    if len(s) <= 4:
        return s
    interior = "".join("*" if ch.isalnum() else ch for ch in s[2:-2])
    return s[:2] + interior + s[-2:]


# ---------------------------------------------------------------------------
# Reading + lookups
# ---------------------------------------------------------------------------


def _read_table(cur, table: str):
    """Return (colnames, rows). (None, None) if the table is absent."""
    try:
        cur.execute(f"SELECT * FROM {table}")
    except UndefinedTable:
        return None, None
    colnames = [d.name for d in cur.description]
    return colnames, cur.fetchall()


def _alias_kind(record_type) -> str | None:
    """fact_aliases.record_type holds the SOURCE TABLE NAME. Classify it (case-
    and underscore-insensitively) as a 'user' alias (dim_users / dim_users_s) or
    an 'entity' alias (dim_entities); anything else is not a name we resolve."""
    rt = str(record_type or "").strip().lower().replace("_", "")
    if rt.startswith("dimusers"):
        return "user"
    if rt.startswith("dimentit"):
        return "entity"
    return None


def build_lookups(cur, fernet) -> dict:
    """Build the raw (UNMASKED) name lookups. Masking happens once at resolution
    time so the primary->alias fallback compares real presence.

      user_raw[id]      -> "First Last" from dim_users_s (joined on user_id)
      user_alias[id]    -> alias_name from fact_aliases (record_type ~ dim_users*)
      entity_raw[id]    -> entity_name from dim_entities
      entity_alias[id]  -> alias_name from fact_aliases (record_type ~ dim_entities)
      registrar_of[id]  -> the entity's registrar_id (another entity id)
    """
    lk = {
        "user_raw": {},
        "user_alias": {},
        "entity_raw": {},
        "entity_alias": {},
        "registrar_of": {},
        "fund": {},  # fund id -> {folio_no, isin, first_holder_id}
    }

    # dim_users_s: primary holder/nominee names.
    cols, rows = _read_table(cur, "dim_users_s")
    if cols is None:
        print(
            "  WARNING: dim_users_s not present - holder/nominee names rely on aliases."
        )
    else:
        i = {c: n for n, c in enumerate(cols)}
        for r in rows:
            first = (
                decrypt_value(fernet, r[i["first_name"]]) if "first_name" in i else ""
            )
            last = decrypt_value(fernet, r[i["last_name"]]) if "last_name" in i else ""
            full = f"{first} {last}".strip()
            if full:
                lk["user_raw"][r[i["user_id"]]] = full

    # dim_entities: primary entity/broker/registrar names + registrar linkage.
    cols, rows = _read_table(cur, "dim_entities")
    if cols is None:
        print(
            "  WARNING: dim_entities not present - entity/broker names rely on aliases."
        )
    else:
        i = {c: n for n, c in enumerate(cols)}
        for r in rows:
            eid = r[i["id"]]
            name = (
                decrypt_value(fernet, r[i["entity_name"]]) if "entity_name" in i else ""
            )
            if name:
                lk["entity_raw"][eid] = name
            lk["registrar_of"][eid] = (
                r[i["registrar_id"]] if "registrar_id" in i else None
            )

    # dim_mutual_funds: lets fact_mutual_fund_transactions resolve its fund_id to
    # the fund's scheme/folio attributes and first-holder name.
    cols, rows = _read_table(cur, "dim_mutual_funds")
    if cols is not None:
        i = {c: n for n, c in enumerate(cols)}

        def _dec(r, col):
            return decrypt_value(fernet, r[i[col]]) if col in i else ""

        holder_id_cols = (
            "first_holder_id",
            "joint_holder1_id",
            "joint_holder2_id",
            "nominee1_id",
            "nominee2_id",
        )
        for r in rows:
            fund = {
                # hash -> hex; scheme/folio identifiers -> decrypted plaintext.
                "isin_folio_holder_hash": to_hex(r[i["isin_folio_holder_hash"]])
                if "isin_folio_holder_hash" in i
                else "",
                "folio_no": _dec(r, "folio_no"),
                "scheme_name": _dec(r, "scheme_name"),
                "isin": _dec(r, "isin"),
                "scheme_code": _dec(r, "scheme_code"),
                "scheme_category": _dec(r, "scheme_category"),
                "is_dividend": r[i["is_dividend"]]
                if "is_dividend" in i and r[i["is_dividend"]] is not None
                else "",
            }
            # holder/nominee ids -> resolved to (masked) names at enrich time.
            for idf in holder_id_cols:
                fund[idf] = r[i[idf]] if idf in i else None
            lk["fund"][r[i["id"]]] = fund

    # fact_aliases: alternate names, used as fallback when the primary is blank.
    cols, rows = _read_table(cur, "fact_aliases")
    if cols is not None:
        i = {c: n for n, c in enumerate(cols)}
        for r in rows:
            kind = _alias_kind(r[i["record_type"]]) if "record_type" in i else None
            if kind is None:
                continue
            rid = r[i["record_id"]] if "record_id" in i else None
            if rid is None:
                continue
            name = (
                decrypt_value(fernet, r[i["alias_name"]]) if "alias_name" in i else ""
            )
            if not name:
                continue
            bucket = "user_alias" if kind == "user" else "entity_alias"
            lk[bucket].setdefault(rid, name)  # first non-empty alias wins

    return lk


def _user_name(uid, lk) -> str:
    if uid is None:
        return ""
    return lk["user_raw"].get(uid) or lk["user_alias"].get(uid) or ""


def _entity_name(eid, lk) -> str:
    if eid is None:
        return ""
    return lk["entity_raw"].get(eid) or lk["entity_alias"].get(eid) or ""


def _enrich_values(table, row, idx, lk):
    """Compute the appended values for one row, in the order declared by
    ENRICH_SPEC[table]. Name kinds resolve primary-then-alias and are masked to
    first-2 + last-2 chars; "fund" attributes (scheme/folio/isin/hash/is_dividend)
    are kept in the clear."""
    out = []
    for out_col, kind, id_col in ENRICH_SPEC.get(table, []):
        rid = row[idx[id_col]] if id_col in idx else None
        if kind == "user":
            out.append(mask_name(_user_name(rid, lk)))
        elif kind == "entity":
            out.append(mask_name(_entity_name(rid, lk)))
        elif kind == "entity_clear":
            out.append(_entity_name(rid, lk))  # brokers: unmasked
        elif kind == "registrar":
            reg_id = lk["registrar_of"].get(rid) if rid is not None else None
            out.append(mask_name(_entity_name(reg_id, lk)))
        elif kind == "fund":
            fund = lk["fund"].get(rid) if rid is not None else None
            out.append(fund.get(out_col, "") if fund else "")
        elif kind == "fund_user":
            fund = lk["fund"].get(rid) if rid is not None else None
            hid = fund.get(out_col.replace("_name", "_id")) if fund else None
            out.append(mask_name(_user_name(hid, lk)))
        else:
            out.append("")
    return out


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

# Columns kept in the output despite matching the id / *_id shape: the row's own
# primary key ("id"), the (first) holder id, and the decrypted external broker
# identifiers.
_KEEP_ID_COLS = {"id", "holder_id", "trade_id", "order_id"}


def _is_id_col(name: str) -> bool:
    """True for the raw id / *_id columns dropped from the "basic" output. The
    FK ids are used only to resolve names/attributes (holder_id, broker_id,
    fund_id, ...); id / trade_id / order_id are whitelisted as real data."""
    if name in _KEEP_ID_COLS:
        return False
    return name == "id" or name.endswith("_id")


def export_table(cur, table, fernet, lk, out_dir: Path) -> int:
    cols, rows = _read_table(cur, table)
    if cols is None:
        print(f"  WARNING: table '{table}' not present in target - SKIPPED.")
        return SKIPPED

    enc = set(ENCRYPTED_FIELDS.get(table, []))
    hashf = set(HASH_FIELDS.get(table, []))
    idx = {c: n for n, c in enumerate(cols)}
    # Enrichment reads the full row (IDs included) but the IDs themselves are not
    # written -- only the non-id base columns plus the resolved name columns.
    kept_cols = [c for c in cols if not _is_id_col(c)]
    extra_cols = [c for c, _, _ in ENRICH_SPEC.get(table, [])]

    out_path = out_dir / f"{table}.csv"
    with open(out_path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(kept_cols + extra_cols)
        for row in rows:
            out_row = []
            for col, val in zip(cols, row):
                if _is_id_col(col):
                    continue  # dropped from output; still available via idx/lookups
                if col in enc:
                    out_row.append(decrypt_value(fernet, val))
                elif col in hashf:
                    out_row.append(to_hex(val))
                else:
                    out_row.append("" if val is None else val)
            out_row.extend(_enrich_values(table, row, idx, lk))
            writer.writerow(out_row)

    print(
        f"  wrote {out_path.name}  ({len(rows)} rows, "
        f"{len(kept_cols)} cols +{len(extra_cols)} resolved, ids dropped)"
    )
    return len(rows)


# ---------------------------------------------------------------------------
# Blob upload (az CLI, --auth-mode login)
# ---------------------------------------------------------------------------


def _resolve_az() -> str:
    from_path = shutil.which("az")
    if from_path:
        return from_path
    candidates = [
        Path(os.getenv("ProgramFiles(x86)", ""))
        / "Microsoft SDKs"
        / "Azure"
        / "CLI2"
        / "wbin"
        / "az.cmd",
        Path(os.getenv("ProgramFiles", ""))
        / "Microsoft SDKs"
        / "Azure"
        / "CLI2"
        / "wbin"
        / "az.cmd",
        Path(os.getenv("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps" / "az.cmd",
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    raise SystemExit(
        "FATAL: Azure CLI ('az') not found. Install it or add it to PATH "
        "(needed to upload to blob; re-run with --no-upload to skip)."
    )


def _resolve_terraform() -> str | None:
    """Find terraform.exe even when it isn't on PATH (mirrors the orchestrator).
    Returns None if it genuinely can't be found -- the caller decides what to do,
    since terraform is only needed for the laptop URL fetch."""
    override = os.getenv("TERRAFORM_BIN")
    if override and Path(override).is_file():
        return override
    from_path = shutil.which("terraform")
    if from_path:
        return from_path
    candidates = [
        Path(os.getenv("ProgramFiles", ""))
        / "HashiCorp"
        / "Terraform"
        / "terraform.exe",
        Path(os.getenv("ProgramFiles", "")) / "Terraform" / "terraform.exe",
        Path(os.getenv("ChocolateyInstall", "")) / "bin" / "terraform.exe",
    ]
    localappdata = os.getenv("LOCALAPPDATA", "")
    if localappdata:
        winget_root = Path(localappdata) / "Microsoft" / "WinGet" / "Packages"
        if winget_root.is_dir():
            candidates.extend(winget_root.glob("Hashicorp.Terraform_*\\terraform.exe"))
    for cand in candidates:
        if cand.is_file():
            return str(cand)
    return None


def upload_blob(local_path: Path, blob_name: str, account: str, container: str) -> None:
    az = _resolve_az()
    cmd = [
        az,
        "storage",
        "blob",
        "upload",
        "--account-name",
        account,
        "--container-name",
        container,
        "--name",
        blob_name,
        "--file",
        str(local_path),
        "--auth-mode",
        "login",
        "--overwrite",
        "true",
        "--only-show-errors",
    ]
    print(f"  uploading -> {container}/{blob_name}")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout or "")
        sys.stderr.write(proc.stderr or "")
        raise SystemExit(
            f"FATAL: blob upload failed for {blob_name} (az exit {proc.returncode})."
        )


def _count_fail_markers(path: Path) -> int:
    text = path.read_text(encoding="utf-8-sig")
    return (
        text.count("<DECRYPT-FAILED")
        + text.count("<DECRYPT-ERROR")
        + text.count("<NON-UTF8-BYTES")
    )


# ---------------------------------------------------------------------------
# Client firewall (laptop only; a no-op inside Azure)
# ---------------------------------------------------------------------------


def _running_in_azure() -> bool:
    """True inside an Azure Container App or Job. There the staging server is
    already reachable via its 'allow Azure services' (0.0.0.0) firewall rule, so
    no client rule is needed -- same detection the orchestrator uses."""
    return bool(
        os.getenv("CONTAINER_APP_NAME")
        or os.getenv("CONTAINER_APP_JOB_NAME")
        or os.getenv("CONTAINER_APP_JOB_EXECUTION_NAME")
    )


def _public_ip() -> str | None:
    for svc in (
        "https://api.ipify.org",
        "https://ifconfig.me/ip",
        "https://checkip.amazonaws.com",
    ):
        try:
            with urllib.request.urlopen(svc, timeout=10) as r:
                ip = r.read().decode().strip()
                if re.match(r"^\d+\.\d+\.\d+\.\d+$", ip):
                    return ip
        except Exception:
            continue
    return None


def ensure_client_firewall(url: str, resource_group: str) -> None:
    """Allow this machine's public IP through the staging server's firewall.

    A no-op inside Azure (the allow-Azure rule already permits the connection),
    so this exists purely so a LAPTOP run doesn't time out. The server name is
    parsed from the connection URL. Best-effort: a failure is warned about, not
    fatal -- the subsequent connect will surface the real outcome."""
    if _running_in_azure():
        print(
            "running in Azure - staging firewall already allows Azure services; "
            "no client rule needed."
        )
        return
    server = (urlparse(url).hostname or "").split(".")[0]
    if not server:
        print(
            "WARNING: could not parse server name from the DB URL; "
            "skipping firewall rule."
        )
        return
    ip = _public_ip()
    if not ip:
        print(
            "WARNING: could not determine public IP; skipping firewall rule "
            "(connection may time out)."
        )
        return
    az = _resolve_az()
    print(f"allowing client IP {ip} through {server}'s firewall ...")
    proc = subprocess.run(
        [
            az,
            "postgres",
            "flexible-server",
            "firewall-rule",
            "create",
            "--resource-group",
            resource_group,
            "--server-name",
            server,
            "--name",
            "verified-export-client",
            "--start-ip-address",
            ip,
            "--end-ip-address",
            ip,
            "--only-show-errors",
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout or "")
        sys.stderr.write(proc.stderr or "")
        print(
            "WARNING: firewall-rule create failed; if the connection times "
            "out, add your IP manually or check your az permissions."
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Decrypt + name-enrich the 3 holding/transaction tables and "
        "upload them to landing-zone/verified_data/."
    )
    ap.add_argument(
        "--profile",
        default="Azure",
        help="DB target: Azure | Pi | Local (case-insensitive). "
        "Default Azure (the staging server).",
    )
    ap.add_argument(
        "--schema",
        default=None,
        help="schema to read (sets search_path). Default: "
        "<PROFILE>_SCHEMA / PG_SCHEMA (Azure staging uses 'staging').",
    )
    ap.add_argument("--account", default=DEFAULT_ACCOUNT, help="storage account.")
    ap.add_argument("--container", default=DEFAULT_CONTAINER, help="blob container.")
    ap.add_argument(
        "--blob-dir",
        default=DEFAULT_BLOB_DIR,
        help="directory (prefix) inside the container.",
    )
    ap.add_argument(
        "--out",
        default=None,
        help="local dir for the CSVs (default: a temp dir, purged after upload).",
    )
    ap.add_argument(
        "--keep-local",
        action="store_true",
        help="do not delete the local CSVs after upload (they hold "
        "decrypted PII -- handle with care).",
    )
    ap.add_argument(
        "--no-upload",
        action="store_true",
        help="write the CSVs locally only; skip the blob upload "
        "(implies --keep-local).",
    )
    ap.add_argument(
        "--env-file",
        default=None,
        help="explicit .env path (else searched cwd+parents).",
    )
    ap.add_argument(
        "--resource-group",
        default=os.getenv("FIN_RESOURCE_GROUP", "fin-assist-rg"),
        help="resource group of the staging server (for the laptop "
        "firewall rule; default fin-assist-rg).",
    )
    ap.add_argument(
        "--no-firewall",
        action="store_true",
        help="skip adding this machine's IP to the staging firewall "
        "(auto-skipped in Azure regardless).",
    )
    args = ap.parse_args()

    _load_dotenv(args.env_file or os.environ.get("DOTENV_PATH"))

    profile = args.profile.upper()
    print(f"profile = {profile}")

    fernet = load_key(profile)
    schema = (
        args.schema
        or os.environ.get(f"{profile}_SCHEMA")
        or _cfg("PG_SCHEMA", profile)
        or DEFAULT_PROFILE_SCHEMA.get(profile)
    )

    # Local output dir: an explicit --out, else a temp dir we purge afterwards.
    using_temp = args.out is None
    out_dir = (
        Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="verified_data_"))
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"output dir = {out_dir}")

    # If the Azure URL will be fetched from terraform (no PG_URL / PG_URL_CMD
    # set), make sure terraform is found even when it isn't on PATH: resolve its
    # full path and hand build_connection an explicit PG_URL_CMD_AZURE. No-op in
    # the container, where the orchestrator supplies PG_URL_AZURE directly.
    if (
        profile == "AZURE"
        and not _cfg("PG_URL", profile)
        and not _cfg("PG_URL_CMD", profile)
    ):
        tf_exe = _resolve_terraform()
        if tf_exe and shutil.which("terraform") is None:
            tf_dir = os.getenv("TF_STAGING_DIR", "infra/staging-db")
            os.environ["PG_URL_CMD_AZURE"] = (
                f'"{tf_exe}" -chdir={tf_dir} output -raw staging_database_url'
            )

    connect_args, connect_kwargs, desc = build_connection(profile)
    print(f"connecting: {desc}")

    # Open the staging firewall for this machine (laptop only; no-op in Azure).
    # Only meaningful when connecting by URL to the Azure server.
    if not args.no_firewall and profile == "AZURE" and connect_args:
        ensure_client_firewall(connect_args[0], args.resource_group)

    written = {}  # table -> local Path
    total = 0
    skipped = []
    try:
        with psycopg.connect(*connect_args, **connect_kwargs) as conn:
            conn.autocommit = True  # read-only; keeps search_path, survives a skip
            with conn.cursor() as cur:
                if schema:
                    cur.execute(
                        sql.SQL("SET search_path TO {}").format(sql.Identifier(schema))
                    )
                    print(f"search_path = {schema}")

                print(
                    "building name lookups (dim_users_s, dim_entities, fact_aliases)..."
                )
                lk = build_lookups(cur, fernet)
                print(
                    f"  {len(lk['user_raw'])} users, {len(lk['entity_raw'])} entities, "
                    f"{len(lk['user_alias'])}+{len(lk['entity_alias'])} aliases"
                )

                for table in TARGET_TABLES:
                    print(f"exporting {table} ...")
                    result = export_table(cur, table, fernet, lk, out_dir)
                    if result is SKIPPED:
                        skipped.append(table)
                    else:
                        total += result
                        written[table] = out_dir / f"{table}.csv"

        # Integrity check: surface any values that failed to decrypt.
        fail_markers = sum(_count_fail_markers(p) for p in written.values())
        if fail_markers:
            print(
                f"WARNING: {fail_markers} value(s) failed to decrypt "
                f"(see <DECRYPT-*> markers in the CSVs)."
            )

        # Upload.
        if args.no_upload:
            print("--no-upload: CSVs written locally only.")
        else:
            print(f"uploading to {args.account}/{args.container}/{args.blob_dir}/ ...")
            for table, path in written.items():
                blob_name = f"{args.blob_dir}/{table}.csv"
                upload_blob(path, blob_name, args.account, args.container)

        print(f"\ndone: {total} row(s) across {len(written)} table(s).")
        if skipped:
            print(f"SKIPPED (not present): {', '.join(skipped)}")
        return 0

    finally:
        # The local CSVs hold decrypted PII. Purge the temp dir after upload
        # unless the caller asked to keep it (or we never uploaded).
        keep = args.keep_local or args.no_upload
        if using_temp and not keep:
            shutil.rmtree(out_dir, ignore_errors=True)
            if not out_dir.exists():
                print(f"purged local CSVs at {out_dir}")
        elif keep:
            print(f"local CSVs kept at {out_dir} (contain decrypted PII).")


if __name__ == "__main__":
    sys.exit(main())
