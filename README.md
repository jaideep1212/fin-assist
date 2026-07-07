# Financial Recommendation Engine — Data Segment

Containerized FastAPI service exposing the investment data layer. Reads from a
PostgreSQL database via `DATABASE_URL`, runs on Azure Container Apps, built and
deployed by Jenkins.

## Where the database lives

The container only knows `DATABASE_URL`. You choose what it points at:

- **Option A (recommended):** Pi ingests data, then syncs into **Azure Database
  for PostgreSQL**. `DATABASE_URL` points at the Azure DB. The request path never
  touches your home network.
- **Option B:** Keep the Pi as the only DB and reach it over a **Tailscale**
  (WireGuard) mesh or **Azure Relay Hybrid Connection** — outbound from the Pi,
  nothing exposed publicly. `DATABASE_URL` points at the Pi's tailnet address.
  This needs Tailscale running inside/next to the container.

Do not port-forward 5432 to the internet.

## Endpoints

- `GET /health` — liveness, no DB (used by the container healthcheck)
- `GET /ready` — readiness, pings the DB
- `GET /instruments/{kind}` — `kind` in `stocks|bonds|funds`, `?limit=&offset=`

## Local run

```bash
pip install -r requirements.txt
cp .env.example .env          # set DATABASE_URL or PG* vars
python inspect_schema.py      # discover your real tables first
uvicorn app.main:app --reload
# then: curl localhost:8000/health
```

Point the loaders at your real tables via env (no code change):
`STOCKS_TABLE`, `BONDS_TABLE`, `FUNDS_TABLE`.

## Azure — one-time setup

```bash
az group create -n rg-financial-rec -l westeurope
az acr create -g rg-financial-rec -n yourregistry --sku Basic

az containerapp env create -g rg-financial-rec -n cae-financial -l westeurope

# create the app; DATABASE_URL is stored as a secret, not baked into the image
az containerapp create \
  -g rg-financial-rec -n financial-data-api \
  --environment cae-financial \
  --image yourregistry.azurecr.io/financial-data-api:bootstrap \
  --target-port 8000 --ingress external \
  --registry-server yourregistry.azurecr.io \
  --secrets db-url="postgresql://user:pass@host:5432/investments" \
  --env-vars DATABASE_URL=secretref:db-url
```

After that, Jenkins only swaps the image tag on each build.

## Jenkins

The `Jenkinsfile` does: test → build → push to ACR → deploy (on `main`).

Agent prerequisites: `python3`, `docker`, and `az` (Azure CLI) available on the
build agent.

Credentials: install the **Azure Credentials** plugin and add a service
principal credential with ID `azure-sp`. Grant that SP `AcrPush` on the registry
and `Contributor` on the resource group. Then edit the `environment {}` block at
the top of the `Jenkinsfile` (`ACR_NAME`, `RESOURCE_GROUP`, `CONTAINERAPP`).

Images are tagged by short git SHA, so every deploy is traceable to a commit.

## Pi ↔ Azure link (Option B, Azure Relay)

The Pi's PostgreSQL is reached over an Azure Relay Hybrid Connection via
`azbridge` — see `RELAY_SETUP.md`. The app connects to `127.0.0.1:5432`; an
`azbridge` sidecar tunnels that to the Pi. Nothing on the Pi is exposed inbound.

## Daily watchdog

`run_watchdog.py` polls every minute and, the first time the Pi is reachable
that day, fetches the 8 source tables and writes a transient per-day snapshot
(parquet) for downstream jobs. Once done (or past the cutoff) it idles until the
next day, then re-arms. State machine and behaviour live in `app/watchdog.py`.

Source tables pulled: `dim_users`, `dim_users_s`, `dim_accounts`,
`dim_mutual_funds`, `fact_mutual_fund_transactions`, `fact_stock_transactions`,
`fact_aliases`, `fact_account_broker_mappings`.

Future cadences (ad-hoc / weekly / monthly / quarterly / yearly) slot into the
marked registry block in `run_watchdog.py`.

## Files

- `app/db.py` — connection engine (DATABASE_URL-first)
- `app/data_access.py` — API loaders, table names from env
- `app/main.py` — FastAPI app
- `app/pi_data.py` — fetch the 8 source tables + awake check
- `app/watchdog.py` — daily watchdog state machine
- `app/sinks.py` — transient snapshot sinks (local parquet / Azure blob)
- `run_watchdog.py` — watchdog entrypoint (APScheduler)
- `inspect_schema.py` — run first to discover the schema
- `infra/relay/` — azbridge config, Pi systemd unit, sidecar Dockerfile, Container App YAML
- `Dockerfile`, `.dockerignore`, `Jenkinsfile`
- `tests/` — API + watchdog tests run in CI

## Next

1. Run `inspect_schema.py`, share the output → wire loaders to real tables.
2. Confirm timezone / cutoff / sink for the watchdog.
3. Feature engineering on top of the daily snapshot.
