"""
run_staging_pipeline.py  -  orchestrator for the Azure staging cycle.

One body, two triggers:
  * MANUAL  (now):   python run_staging_pipeline.py --destroy yes
  * JOB B   (later): the same script, packaged in a container, started by an
    Event Grid subscription on the watchdog's `_state/done-<day>.json` marker.
Only the trigger + credentials differ (your `az login` on the laptop vs. a
managed identity in Azure); this body is identical.

No lanes: production runs in a SEPARATE TENANT with its own login, state, key,
and .env. So there is nothing to switch here -- whichever tenant this runs in IS
the environment.

Sequence
--------
  1. terraform apply                  (stand up the ephemeral staging Postgres)
  2. terraform output -raw url         (connection string; carries pw + sslmode)
  3. add this machine's public IP as a firewall rule (the 0.0.0.0 rule is
     Azure-only; the client needs its own rule to connect)
  4. download the day's parquet from blob -> local dir  (load_staging reads a
     LOCAL folder, not blob directly)
  5. load_staging.py                   (MANDATORY: apply DDL + truncate-load)
  6. <post-load command>               (PLUGGABLE: default = verify script; any
     script that reads the contract env vars can be dropped in)
  7. terraform destroy                 (GUARDED: always runs on exit unless
     --destroy no; runs even if 5/6 failed, so nothing is left billing)

Destroy semantics
-----------------
  --destroy yes  -> destroy (also the default when the flag is omitted)
  --destroy no   -> keep the server; print a loud reminder + the destroy command.

Pluggable post-load contract (env vars set before the command runs)
-------------------------------------------------------------------
  STAGING_DATABASE_URL   live staging DB URL (load_staging.py reads this)
  PG_URL_AZURE           same (verify reads this first, so it reuses the URL
                         instead of re-running terraform output)
  STAGING_SCHEMA / AZURE_SCHEMA   'staging'
  STAGING_RUN_DATE       the dt= day being processed
  STAGING_SOURCE         local dir holding dt=<date>/*.parquet
  (plus everything already in the environment, e.g. ENCRYPTION_KEY from .env)

Prereqs (manual run): terraform + az CLI installed and `az login` done;
TF_VAR_admin_password set (terraform apply/destroy need it).

Env / overrides
---------------
  FIN_RESOURCE_GROUP   default fin-assist-rg
  FIN_STORAGE_ACCOUNT  default finassistdata
  FIN_BLOB_CONTAINER   default landing-zone
  FIN_BLOB_PREFIX      default staging       (blobs at <prefix>/dt=<date>/)
  TF_STAGING_DIR       default infra/staging-db
  POST_LOAD_CMD        default post-load command (CLI --post-load overrides).
                       Lets the container change the pluggable step via job env
                       without rebuilding the image.
  LOAD_CMD             default mandatory load command.

The client firewall rule is added only when running on the laptop. Inside an
Azure Container App (detected via CONTAINER_APP_NAME) it is skipped, since the
allow-Azure firewall rule already permits the connection.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlparse


RG = os.getenv("FIN_RESOURCE_GROUP", "fin-assist-rg")
ACCOUNT = os.getenv("FIN_STORAGE_ACCOUNT", "finassistdata")
CONTAINER = os.getenv("FIN_BLOB_CONTAINER", "landing-zone")
BLOB_PREFIX = os.getenv("FIN_BLOB_PREFIX", "staging")
TF_DIR = os.getenv("TF_STAGING_DIR", "infra/staging-db")

# The pluggable post-load step. Empty by default: the orchestrator's job is
# apply -> load -> (optional downstream) -> destroy. Verification is now a
# MANUAL, laptop-run activity (python app/verify_decrypt_export.py --profile
# Azure), not part of the automated pipeline, because its decrypted CSVs are
# only useful on the laptop.
#   * POST_LOAD_CMD set  -> run it and WAIT for it (a small script in this image,
#                           or a command that starts a separate downstream job
#                           and blocks until it finishes -- the orchestrator's
#                           finally still owns teardown either way).
#   * POST_LOAD_CMD unset -> warn and continue (data is left loaded; nothing to
#                           run). With --destroy no you can then verify manually.
DEFAULT_POST_LOAD = os.getenv("POST_LOAD_CMD", "")
DEFAULT_LOAD_CMD = os.getenv("LOAD_CMD", "python app/load_staging.py")


# Path to the local parquet scratch dir, registered for guaranteed cleanup.
# It holds a disk copy of the (encrypted but sensitive) data, so it must not be
# left behind -- not on success, failure, or interrupt.
_SCRATCH_DIR: Path | None = None


def _purge_scratch(reason: str = "") -> None:
    """Delete the scratch dir if it exists. Idempotent and never raises, so it
    is safe to call from the normal finally, from atexit, and from signal
    handlers. After deletion, verify it is really gone."""
    global _SCRATCH_DIR
    d = _SCRATCH_DIR
    if d is None:
        return
    try:
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
        # Confirm removal; if something is still there, say so loudly.
        if d.exists():
            sys.stderr.write(
                f"WARNING: could not fully remove scratch dir {d} "
                f"({reason}); it may hold data on disk -- delete it manually.\n"
            )
        else:
            _SCRATCH_DIR = None  # nothing left to clean
    except Exception as e:
        sys.stderr.write(f"WARNING: scratch cleanup error for {d}: {e}\n")


def _install_scratch_guards() -> None:
    """Ensure the scratch dir is purged even on interrupt/termination, not just
    on the normal finally path (which a hard signal would bypass)."""
    import atexit
    import signal

    atexit.register(lambda: _purge_scratch("atexit"))

    def _handler(signum, _frame):
        _purge_scratch(f"signal {signum}")
        # Re-raise default behaviour so the process still exits.
        raise SystemExit(128 + signum)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handler)
        except Exception:
            pass  # some environments disallow setting handlers; atexit still covers us


def _running_in_azure() -> bool:
    """True when running inside an Azure Container App or Job.

    Container Apps inject platform metadata env vars, but they DIFFER by type:
    an *app* gets CONTAINER_APP_NAME, while a *job* (what this runs as) gets
    CONTAINER_APP_JOB_NAME / CONTAINER_APP_JOB_EXECUTION_NAME and NOT
    CONTAINER_APP_NAME. Checking only CONTAINER_APP_NAME therefore returns False
    inside a job -- which silently disabled the container-only auth path. Check
    all three so this is true whether we run as an app or a job.

    Inside Azure the staging server is already reachable via the Terraform
    0.0.0.0 "allow Azure services" rule, and the container's egress IP is not a
    useful firewall entry -- so the client firewall step is skipped there.
    """
    return bool(
        os.getenv("CONTAINER_APP_NAME")
        or os.getenv("CONTAINER_APP_JOB_NAME")
        or os.getenv("CONTAINER_APP_JOB_EXECUTION_NAME")
    )


def _resolve_terraform_executable() -> str:
    """Find terraform.exe even when PATH is not configured in this shell.

    Resolution order:
      1) TERRAFORM_BIN environment override
      2) PATH lookup
      3) common Windows install locations (including WinGet package dir)
    """
    override = os.getenv("TERRAFORM_BIN")
    if override:
        if Path(override).is_file():
            return override
        raise SystemExit(
            f"FATAL: TERRAFORM_BIN is set but does not point to a file: {override}"
        )

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

    raise SystemExit(
        "FATAL: terraform executable not found. Install Terraform or add it to PATH. "
        "You can also set TERRAFORM_BIN to the full terraform.exe path."
    )


def _resolve_az_executable() -> str:
    """Find Azure CLI executable when PATH isn't configured in this shell."""
    override = os.getenv("AZ_BIN")
    if override:
        if Path(override).is_file():
            return override
        raise SystemExit(
            f"FATAL: AZ_BIN is set but does not point to a file: {override}"
        )

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
        Path(os.getenv("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps" / "az.exe",
    ]

    for cand in candidates:
        if cand.is_file():
            return str(cand)

    raise SystemExit(
        "FATAL: Azure CLI executable not found. Install Azure CLI or add it to PATH. "
        "You can also set AZ_BIN to the full az path."
    )


def _load_dotenv():
    """Load a .env found in cwd or its parents into os.environ (not overriding
    existing vars). Tolerates a BOM and surrounding quotes. This makes .env
    values available to child processes too (terraform, load, verify)."""
    cwd = Path.cwd()
    env_path = None
    for d in (cwd, *cwd.parents):
        cand = d / ".env"
        if cand.is_file():
            env_path = cand
            break
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


def _ensure_tf_password():
    """Terraform needs TF_VAR_admin_password (case-sensitive: variable is
    'admin_password'). Accept it from the environment, or from an uppercase
    TF_VAR_ADMIN_PASSWORD in .env, and normalise to the name terraform reads."""
    if os.getenv("TF_VAR_admin_password"):
        return
    alt = os.getenv("TF_VAR_ADMIN_PASSWORD")
    if alt:
        # Windows environment variables are case-insensitive. If the uppercase
        # key already exists, setting the lowercase key may keep the original
        # casing in the environment block. Remove then re-add to force the exact
        # key terraform expects for var name matching.
        os.environ.pop("TF_VAR_ADMIN_PASSWORD", None)
        os.environ["TF_VAR_admin_password"] = alt
        return
    raise SystemExit(
        "FATAL: no admin password for terraform. Set TF_VAR_admin_password in "
        "the shell, or TF_VAR_ADMIN_PASSWORD in .env. It becomes the staging DB "
        "admin password and is embedded in the output URL."
    )


def _ensure_python_runtime_deps():
    """Fail fast before provisioning infra if loader deps are not installed."""
    required = {
        "pandas": "pandas",
        "sqlalchemy": "sqlalchemy",
        "psycopg2": "psycopg2-binary",
        "pyarrow": "pyarrow",
    }
    missing = [
        pkg for mod, pkg in required.items() if importlib.util.find_spec(mod) is None
    ]
    if missing:
        missing_list = ", ".join(sorted(missing))
        raise SystemExit(
            "FATAL: missing Python runtime dependencies required by app/load_staging.py: "
            f"{missing_list}. Install with `python -m pip install -r requirements.txt` "
            "for local runs, and ensure your Container App Job image is rebuilt from this repo's Dockerfile."
        )


def run(cmd, *, capture=False, env=None, check=True):
    shell = isinstance(cmd, str)
    print(f"\n$ {cmd if shell else ' '.join(cmd)}")
    proc = subprocess.run(cmd, shell=shell, env=env, capture_output=capture, text=True)
    if check and proc.returncode != 0:
        if capture:
            sys.stderr.write(proc.stdout or "")
            sys.stderr.write(proc.stderr or "")
        raise SystemExit(f"FATAL: command failed ({proc.returncode}).")
    return proc.stdout.strip() if capture else None


# --- azure CLI session isolation -----------------------------------------
# In the container the az CLI logs in with the managed identity, which presents
# as a SERVICE PRINCIPAL. Terraform's azurerm auth probes the az CLI whenever it
# finds a session and rejects a service-principal one ("only supported as a
# User"). We can't disable that probe on Terraform 1.9.8 (it has no use_cli
# backend argument, and the backend ignores the ARM_USE_CLI env var). So instead
# we keep the two apart: the az CLI gets its OWN config dir, and Terraform runs
# pointed at a SEPARATE EMPTY one -- Terraform then sees no CLI session and uses
# MSI (via -backend-config=use_msi + ARM_USE_MSI), while az storage keeps its
# login. Applied only in the container; on the laptop both share your real
# ~/.azure so your interactive user login still serves az AND terraform.
_AZ_CLI_CONFIG_DIR = "/tmp/az-cli-config"  # az login + az storage read/write here
_TF_AZ_CONFIG_DIR = (
    "/tmp/tf-empty-azure"  # terraform reads this (empty) -> no CLI session
)

# Populated in the container by _ensure_state_access_key(); consumed by tf() as
# the ARM_ACCESS_KEY env var so the backend authenticates with a storage key.
_STATE_ACCESS_KEY = None


# --- terraform ------------------------------------------------------------


def tf(*args, capture=False):
    terraform_exe = _resolve_terraform_executable()
    tf_env = os.environ.copy()
    tf_pwd = None
    for k, v in tf_env.items():
        if k.lower() == "tf_var_admin_password" and v:
            tf_pwd = v
            break
    if tf_pwd:
        for k in [
            key for key in tf_env.keys() if key.lower() == "tf_var_admin_password"
        ]:
            del tf_env[k]
        tf_env["TF_VAR_admin_password"] = tf_pwd
    if _running_in_azure():
        # BACKEND auth: the state storage access key (fetched with the MI), passed
        # as an env var so it never lands in the logs. The backend uses ONLY this
        # -- no CLI, no MSI -- so the provider settings below don't affect it.
        if _STATE_ACCESS_KEY:
            tf_env["ARM_ACCESS_KEY"] = _STATE_ACCESS_KEY
        # PROVIDER auth (apply/destroy): use the az CLI session that
        # `az login --identity` established, NOT the provider's own MSI. The
        # provider's MSI hits the VM IMDS api-version (2018-02-01) that the
        # Container Apps token endpoint rejects ("UnsupportedApiVersion"), whereas
        # the az CLI already speaks that endpoint correctly. So point terraform at
        # the CLI's config dir (which holds the login) and switch it from MSI to
        # CLI auth. NOTE: the earlier "only supported as a User" rejection was
        # BACKEND-only (Terraform core's older auth lib); the azurerm v4 provider's
        # newer auth accepts a managed-identity CLI login.
        os.makedirs(_AZ_CLI_CONFIG_DIR, exist_ok=True)
        tf_env["AZURE_CONFIG_DIR"] = _AZ_CLI_CONFIG_DIR
        tf_env["ARM_USE_MSI"] = "false"
        tf_env["ARM_USE_CLI"] = "true"
    return run([terraform_exe, f"-chdir={TF_DIR}", *args], capture=capture, env=tf_env)


def tf_init():
    # Required before apply in a fresh checkout/container (no .terraform/ yet).
    # -reconfigure: adopt the current backend config cleanly (the container has
    #   no prior local backend state to migrate).
    # -input=false: never block on interactive prompts inside a container.
    init_args = ["init", "-reconfigure", "-input=false"]

    state_key = f"{Path(TF_DIR).name}.tfstate"
    init_args.append(f"-backend-config=key={state_key}")

    # Backend authentication to the state storage account differs by location:
    #   * Laptop:    your interactive `az login` (a USER account) provides an
    #                Entra ID token; use_azuread_auth uses it against the blob.
    #   * Container: Terraform 1.9.8's backend cannot authenticate to the state
    #                account with the managed identity -- it resolves the
    #                subscription tenant via the az CLI, which fails whether the
    #                CLI holds a managed-identity (SP) session ("only supported as
    #                a User") or none ("please run az login"). So we bypass all of
    #                that with a STORAGE ACCESS KEY, fetched at runtime with the
    #                MI and passed via the ARM_ACCESS_KEY env var in tf() (never on
    #                the command line, so it isn't logged). No auth backend-config
    #                is added here in that case.
    if not _running_in_azure():
        init_args.append("-backend-config=use_azuread_auth=true")

    tf(*init_args)


def tf_apply():
    _ensure_tf_password()
    tf_init()
    tf("apply", "-auto-approve", "-input=false")


def tf_output_url() -> str:
    url = tf("output", "-raw", "staging_database_url", capture=True)
    if not url:
        raise SystemExit("FATAL: terraform output staging_database_url is empty.")
    return url


def tf_destroy():
    tf_init()
    tf("destroy", "-auto-approve", "-input=false")


# --- azure: firewall + blob ----------------------------------------------


def az(*args, capture=False):
    az_exe = _resolve_az_executable()
    az_env = os.environ.copy()
    if _running_in_azure():
        # Keep the CLI's managed-identity session in its OWN dir, separate from
        # the empty one terraform reads (see the isolation note above).
        os.makedirs(_AZ_CLI_CONFIG_DIR, exist_ok=True)
        az_env["AZURE_CONFIG_DIR"] = _AZ_CLI_CONFIG_DIR
    return run([az_exe, *args], capture=capture, env=az_env)


def _ensure_azure_cli_login():
    """Sign the az CLI in when running in the container.

    The blob steps below (latest_partition_date + download_partition from the
    finassistdata account) use `az storage blob ... --auth-mode login`, which
    needs an authenticated az CLI session. On the laptop that's your manual
    `az login`. Inside the container there is none, so sign in with the job's
    USER-ASSIGNED managed identity. (Terraform doesn't need this -- its provider
    and backend use MSI via IMDS directly -- but the az CLI does.)

    Current az CLI uses --client-id for a user-assigned identity; if the image
    ships an older az, that flag was --username.
    """
    if not _running_in_azure():
        return
    client_id = os.getenv("ARM_CLIENT_ID")
    if not client_id:
        raise SystemExit(
            "FATAL: running in Azure but ARM_CLIENT_ID is not set; cannot sign "
            "the Azure CLI in with the managed identity."
        )
    az("login", "--identity", "--client-id", client_id, "--output", "none")
    sub = os.getenv("ARM_SUBSCRIPTION_ID")
    if sub:
        az("account", "set", "--subscription", sub, "--output", "none")

    # Terraform runs pointed at a SEPARATE empty AZURE_CONFIG_DIR (see tf()), so
    # it won't see this managed-identity CLI session and will use MSI. ARM_USE_CLI
    # is also set false as harmless belt-and-suspenders for the provider.
    os.environ["ARM_USE_CLI"] = "false"


def _ensure_state_access_key():
    """Fetch the state storage account key with the managed identity.

    Terraform 1.9.8's azurerm BACKEND can't authenticate to the state account
    with a managed identity (it resolves the subscription via the az CLI, which a
    managed-identity session can't satisfy). A storage access key authenticates
    straight to the blob data plane and sidesteps that entirely. The MI has
    Contributor on the state RG, so it can list keys. The key is fetched fresh
    each run, handed to terraform via ARM_ACCESS_KEY (env, never logged), and
    never persisted. No-op on the laptop, where your az login handles the backend.

    Requires shared-key access to be enabled on the account
    (allowSharedKeyAccess != false).
    """
    global _STATE_ACCESS_KEY
    if not _running_in_azure():
        return
    account = os.getenv("TF_STATE_STORAGE_ACCOUNT", "finassisttfstate")
    rg = os.getenv("TF_STATE_RESOURCE_GROUP", "fin-assist-rg")
    _STATE_ACCESS_KEY = az(
        "storage",
        "account",
        "keys",
        "list",
        "--account-name",
        account,
        "-g",
        rg,
        "--query",
        "[0].value",
        "-o",
        "tsv",
        capture=True,
    )
    if not _STATE_ACCESS_KEY:
        raise SystemExit(
            f"FATAL: could not fetch an access key for storage account "
            f"'{account}' in resource group '{rg}'. Check that the account name "
            f"is right (override with TF_STATE_STORAGE_ACCOUNT / "
            f"TF_STATE_RESOURCE_GROUP), that the managed identity can listKeys "
            f"(e.g. Contributor), and that shared-key access is enabled."
        )


def public_ip(override: str | None) -> str:
    if override:
        return override
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
    raise SystemExit("FATAL: could not determine public IP; pass --client-ip.")


def server_name_from_url(url: str) -> str:
    host = urlparse(url).hostname or ""
    return host.split(".")[0]  # <server>.postgres.database.azure.com


def add_firewall_rule(server: str, ip: str):
    az(
        "postgres",
        "flexible-server",
        "firewall-rule",
        "create",
        "--resource-group",
        RG,
        "--server-name",
        server,
        "--name",
        "orchestrator-client",
        "--start-ip-address",
        ip,
        "--end-ip-address",
        ip,
    )


def latest_partition_date() -> str:
    out = az(
        "storage",
        "blob",
        "list",
        "--account-name",
        ACCOUNT,
        "--container-name",
        CONTAINER,
        "--prefix",
        f"{BLOB_PREFIX}/dt=",
        "--auth-mode",
        "login",
        "--query",
        "[].name",
        "-o",
        "tsv",
        capture=True,
    )
    dates = sorted(set(re.findall(r"dt=(\d{4}-\d{2}-\d{2})", out or "")))
    if not dates:
        raise SystemExit(
            f"FATAL: no dt= partitions under {CONTAINER}/{BLOB_PREFIX}/. "
            "Has the watchdog written parquet to blob yet?"
        )
    return dates[-1]


def download_partition(run_date: str, workdir: Path) -> Path:
    """Download <prefix>/dt=<date>/*.parquet -> workdir/<prefix>/dt=<date>/.
    Returns the STAGING_SOURCE dir (the one that directly contains dt=...)."""
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    az(
        "storage",
        "blob",
        "download-batch",
        "--account-name",
        ACCOUNT,
        "--source",
        CONTAINER,
        "--destination",
        str(workdir),
        "--pattern",
        f"{BLOB_PREFIX}/dt={run_date}/*",
        "--auth-mode",
        "login",
    )
    source = workdir / BLOB_PREFIX  # contains dt=<date>/
    part = source / f"dt={run_date}"
    if not part.is_dir() or not any(part.glob("*.parquet")):
        raise SystemExit(
            f"FATAL: no parquet downloaded for dt={run_date} (looked under {part})."
        )
    return source


def contract_env(url: str, run_date: str, source: Path) -> dict:
    env = os.environ.copy()
    env["STAGING_DATABASE_URL"] = url  # load_staging.py reads this
    env["PG_URL_AZURE"] = url  # verify reads this first (skips re-fetch)
    env["STAGING_SCHEMA"] = "staging"
    env["AZURE_SCHEMA"] = "staging"
    env["STAGING_RUN_DATE"] = run_date
    env["STAGING_SOURCE"] = str(source)
    return env


def main() -> int:
    ap = argparse.ArgumentParser(description="Azure staging pipeline orchestrator.")
    ap.add_argument(
        "--destroy",
        choices=["yes", "no"],
        default="yes",
        help="destroy the staging server at the end. Default yes "
        "(also destroys if the flag is omitted). 'no' keeps it.",
    )
    ap.add_argument(
        "--run-date",
        default=None,
        help="dt= partition (YYYY-MM-DD). Default: latest in blob.",
    )
    ap.add_argument(
        "--post-load",
        default=None,
        help="command to run after load (pluggable). Default: verify "
        "script. Pass 'none' to skip.",
    )
    ap.add_argument(
        "--client-ip",
        default=None,
        help="public IP to allow through the firewall (default auto).",
    )
    ap.add_argument(
        "--workdir",
        default="./_staging",
        help="local dir to download parquet into (default ./_staging; "
        "add it to .gitignore and .dockerignore). It is always "
        "removed after the run -- it holds a disk copy of the data.",
    )
    args = ap.parse_args()

    # Scratch dir for the downloaded parquet (load_staging reads local files, so
    # the parquet must land on disk). It is deleted in the finally below,
    # regardless of how the run ends -- it holds a copy of the (encrypted but
    # sensitive) data, so no copy is left behind.
    workdir = Path(args.workdir)
    # Register the scratch dir for guaranteed cleanup (finally + atexit + signals).
    global _SCRATCH_DIR
    _SCRATCH_DIR = workdir
    _install_scratch_guards()

    destroy_enabled = args.destroy == "yes"
    post_load = args.post_load if args.post_load is not None else DEFAULT_POST_LOAD

    print(f"=== staging pipeline | destroy={args.destroy} ===")

    # Load .env (so terraform/load/verify subprocesses inherit it) and make sure
    # the terraform admin password is available, BEFORE we start creating things.
    _load_dotenv()
    _ensure_tf_password()
    _ensure_python_runtime_deps()
    _ensure_azure_cli_login()  # in the container, sign az in with the managed identity (no-op on laptop)
    _ensure_state_access_key()  # fetch the state storage key for the backend (no-op on laptop)

    server_created = False
    try:
        tf_apply()
        server_created = True
        url = tf_output_url()
        server = server_name_from_url(url)
        print(f"staging server: {server}")

        if _running_in_azure():
            print(
                "running in Azure (CONTAINER_APP_NAME set) - skipping client "
                "firewall rule; the allow-Azure rule already permits this."
            )
        else:
            ip = public_ip(args.client_ip)
            print(f"allowing client IP {ip} through the firewall")
            add_firewall_rule(server, ip)

        run_date = args.run_date or latest_partition_date()
        print(f"run date (dt=): {run_date}")
        source = download_partition(run_date, workdir)

        env = contract_env(url, run_date, source)

        run(DEFAULT_LOAD_CMD, env=env)  # MANDATORY

        # The parquet on disk has done its job the moment the load succeeds
        # (the data is now in the staging DB). Purge it IMMEDIATELY to minimise
        # how long a copy of the sensitive data sits on disk -- don't wait for
        # the finally.
        _purge_scratch("post-load")

        # PLUGGABLE post-load. Runs as a subprocess -> the orchestrator WAITS
        # for it to finish before proceeding to the finally/destroy, so the
        # staging DB stays alive for the whole downstream step (even if that
        # step is a separate container/job the command starts and blocks on).
        if not post_load or post_load.strip().lower() in ("none", ""):
            print(
                "WARNING: no post-load script configured (POST_LOAD_CMD unset) "
                "- nothing to run against the loaded data. The tables remain "
                "loaded in the staging DB; run verification manually if needed "
                "(python app/verify_decrypt_export.py --profile Azure) while "
                "the server is up."
            )
        else:
            print(f"running post-load step (waiting for completion): {post_load}")
            run(post_load, env=env)

        print("\n=== pipeline succeeded ===")
        return 0

    finally:
        if not server_created:
            # apply never ran/failed -> nothing to tear down or warn about.
            pass
        elif destroy_enabled:
            print("\n--- tearing down staging server ---")
            try:
                tf_destroy()
            except SystemExit:
                sys.stderr.write(
                    "WARNING: terraform destroy failed. A staging server may "
                    "still be running and BILLING. Run manually:\n"
                    f"    terraform -chdir={TF_DIR} destroy -auto-approve\n"
                )
        else:
            print("\n" + "=" * 60)
            print("SERVER LEFT RUNNING (--destroy no). It is BILLING until you")
            print("tear it down. When finished, run:")
            print(f"    terraform -chdir={TF_DIR} destroy -auto-approve")
            print("=" * 60)

        # Backstop: always ensure the scratch dir is gone (covers the paths
        # where load failed before the early purge). Idempotent.
        _purge_scratch("finally")


if __name__ == "__main__":
    import time
    import traceback

    _rc = 0
    try:
        _rc = main()
    except SystemExit as _e:
        # run() raises SystemExit on any failed subprocess; normalise the code.
        _rc = _e.code if isinstance(_e.code, int) else 1
    except BaseException:
        # Surface Python crashes instead of exiting silently (which is what made
        # the failures invisible in the container logs).
        traceback.print_exc()
        _rc = 1
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        # Optional keep-alive so a short/failed run stays attachable for log
        # capture. Set DEBUG_HOLD_SECONDS on the job (e.g. 180) to enable; unset
        # or 0 = no hold (normal production behaviour).
        try:
            _hold = int(os.getenv("DEBUG_HOLD_SECONDS", "0") or "0")
        except ValueError:
            _hold = 0
        if _hold > 0:
            print(f"[debug] holding {_hold}s for log capture (exit={_rc})", flush=True)
            time.sleep(_hold)

    sys.exit(_rc)
