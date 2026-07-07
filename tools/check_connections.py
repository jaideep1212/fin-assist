"""Quick connectivity check for the SQL Server -> PostgreSQL sync.

Confirms this laptop can reach BOTH databases before we build the full copy.

Before running, set the Postgres password in your terminal:
    PowerShell:        $env:PGPASSWORD = "your_pg_admin_password"
    Command Prompt:    set PGPASSWORD=your_pg_admin_password

Then run:
    python tools/test_connections.py
"""

import os
import sys
from dotenv import load_dotenv

load_dotenv()

# ---- Edit these to match your setup (none of these are secrets) ----
sqlserver = os.getenv("SQLSERVER")
sql_db = os.getenv("SQLSERVER_DB")
pg_host = os.getenv("PG_HOST")
pg_port = int(os.getenv("PG_PORT"))
pg_user = os.getenv("PG_USER")
pg_db = os.getenv("PG_DB")
# Postgres password is read from the PGPASSWORD environment variable (a secret).
# --------------------------------------------------------------------


def test_sqlserver():
    import pyodbc
    conn_str = (
        "DRIVER={ODBC Driver 18 for SQL Server};"
        f"SERVER={sqlserver};DATABASE={sql_db};"
        "Trusted_Connection=yes;TrustServerCertificate=yes;"
    )
    with pyodbc.connect(conn_str, timeout=5) as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM sys.tables")
        count = cur.fetchone()[0]
        print(f"[OK] SQL Server '{sql_db}': connected, {count} tables found.")


def test_postgres():
    import psycopg
    pg_pwd = os.getenv("PGPASSWORD")
    if not pg_pwd:
        print("[FAIL] PostgreSQL: PGPASSWORD environment variable is not set.")
        return False
    conninfo = (
        f"host={pg_host} port={pg_port} dbname={pg_db} "
        f"user={pg_user} password={pg_pwd} connect_timeout=5"
    )
    with psycopg.connect(conninfo) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = 'public'"
            )
            count = cur.fetchone()[0]
            print(f"[OK] PostgreSQL '{pg_db}' on the Pi: connected, {count} tables found.")
    return True


if __name__ == "__main__":
    ok = True

    try:
        test_sqlserver()
    except Exception as exc:
        ok = False
        print(f"[FAIL] SQL Server: {exc}")

    try:
        if test_postgres() is False:
            ok = False
    except Exception as exc:
        ok = False
        print(f"[FAIL] PostgreSQL: {exc}")

    print()
    print("Both connections OK - ready to build the copy." if ok
          else "Fix the failures above before continuing.")
    sys.exit(0 if ok else 1)
