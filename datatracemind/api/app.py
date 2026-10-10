"""FastAPI service: demo controls, metrics, incidents, and the repair lifecycle.

Lifecycle rules are enforced by the modules the routes call, never by the
routes or the frontend. A route only translates their refusals into HTTP
status codes.
"""

from __future__ import annotations

import hmac
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from datatracemind import db, demo, store
from datatracemind.config import get_settings
from datatracemind.monitoring.summary import live_transactions, metrics_summary
from datatracemind.observability import init_tracing, shutdown_tracing
from datatracemind.workers import EtlWorker
from etl import run_log, transform_store

logger = logging.getLogger(__name__)

etl_worker = EtlWorker()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    init_tracing()
    db.ensure_schema()
    try:
        transform_store.get_active()
    except transform_store.NoActiveTransform:
        logger.info("empty environment; seeding the baseline")
        demo.reset()
    if get_settings().run_background_workers:
        etl_worker.start()
    yield
    etl_worker.stop()
    demo.traffic.stop()
    shutdown_tracing()


def require_demo_access(x_demo_access_code: str | None = Header(default=None)) -> None:
    """Guard for routes that change the demo or spend model usage.

    When DTM_DEMO_ACCESS_CODE is unset (local development) the routes are open.
    """
    expected = get_settings().demo_access_code
    if expected is None:
        return
    if x_demo_access_code is None or not hmac.compare_digest(x_demo_access_code, expected):
        raise HTTPException(status_code=401, detail="a valid X-Demo-Access-Code header is required")


router = APIRouter(prefix="/api")
guarded = APIRouter(prefix="/api", dependencies=[Depends(require_demo_access)])


@router.get("/health")
def health() -> dict:
    return {"status": "ok", "etl_worker_running": etl_worker.running, "traffic_running": demo.traffic.running}


# --- Demo and data -----------------------------------------------------------


@guarded.post("/demo/reset")
def demo_reset() -> dict:
    return demo.reset()


@guarded.post("/demo/traffic/start")
def traffic_start() -> dict:
    return {"started": demo.traffic.start(), "traffic_running": True}


@guarded.post("/demo/traffic/stop")
def traffic_stop() -> dict:
    return {"stopped": demo.traffic.stop(), "traffic_running": False}


@router.get("/transactions/live")
def transactions_live(limit: int = 30) -> dict:
    return live_transactions(limit=max(1, min(limit, 200)))


@router.get("/metrics/summary")
def metrics() -> dict:
    return metrics_summary()


@router.get("/pipeline/runs")
def pipeline_runs(limit: int = 20) -> list[dict]:
    with db.connect() as conn:
        return run_log.recent_runs(conn, db.ANALYTICS_SCHEMA, limit=max(1, min(limit, 100)))


@router.get("/scenarios")
def scenarios() -> dict:
    return demo.lab_status()


@guarded.post("/scenarios/{scenario_id}/inject")
def scenario_inject(scenario_id: str) -> dict:
    if scenario_id not in demo.SCENARIOS:
        raise HTTPException(status_code=404, detail=f"unknown scenario {scenario_id}")
    return demo.inject(scenario_id)


# --- Incidents ---------------------------------------------------------------


@router.get("/incidents")
def incidents() -> list[dict]:
    with db.connect() as conn:
        return store.list_incidents(conn)


@router.get("/incidents/{incident_id}")
def incident_detail(incident_id: str) -> dict:
    return store.get_incident(incident_id)


@router.get("/incidents/{incident_id}/events")
def incident_events(incident_id: str) -> list[dict]:
    with db.connect() as conn:
        store.get_incident_row(conn, incident_id)
        return store.list_events(conn, incident_id)


@router.get("/alerts")
def alerts(limit: int = 20) -> list[dict]:
    return db.fetch_all(
        "SELECT a.alert_id, a.incident_id, a.severity, a.title, a.body, a.created_at, i.status AS incident_status "
        "FROM dtm.alerts a JOIN dtm.incidents i USING (incident_id) ORDER BY a.alert_id DESC LIMIT %s",
        (max(1, min(limit, 100)),),
    )


@router.get("/audit")
def audit_log(limit: int = 200) -> list[dict]:
    return db.fetch_all(
        "SELECT audit_id, actor, action, target, result, details, created_at "
        "FROM dtm.audit_log ORDER BY audit_id DESC LIMIT %s",
        (max(1, min(limit, 1000)),),
    )


def create_app() -> FastAPI:
    app = FastAPI(title="DataTraceMind", lifespan=lifespan)
    app.include_router(router)
    app.include_router(guarded)

    @app.exception_handler(store.IncidentNotFound)
    def _not_found(request: Request, exc: store.IncidentNotFound) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": f"incident {exc} not found"})

    @app.exception_handler(store.IllegalTransition)
    @app.exception_handler(demo.LabError)
    def _conflict(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    return app


app = create_app()
