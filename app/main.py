"""
FastAPI surface for fin-assist.

  GET /health              liveness  — the process is up (no external deps)
  GET /ready               readiness — can we reach Postgres through the relay?
  GET /instruments/{kind}  the instrument list for a kind (stock, mutual_fund)

/health stays dependency-free so an orchestrator can tell "process alive" from
"backend reachable" (/ready). Unknown instrument kinds return 404.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException

from app.data_access import load_instruments, supported_kinds
from app.db import ping

app = FastAPI(title="fin-assist")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/ready")
def ready() -> dict:
    if not ping():
        raise HTTPException(status_code=503, detail="database unreachable")
    return {"status": "ready"}


@app.get("/instruments/{kind}")
def instruments(kind: str) -> list[dict]:
    try:
        df = load_instruments(kind)
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail=f"unknown instrument kind '{kind}'; supported: {supported_kinds()}",
        )
    return df.to_dict(orient="records")
