"""FastAPI service: demo controls, metrics, incidents, and the repair lifecycle.

Lifecycle rules are enforced by the modules the routes call, never by the
routes or the frontend. A route only translates their refusals into HTTP
status codes.
"""

from __future__ import annotations

import functools
import hmac
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from datatracemind import db, demo, store
from datatracemind.api.snapshot import SnapshotCache
from datatracemind.config import REPO_ROOT, get_settings
from datatracemind.llm import get_provider
from datatracemind.monitoring.summary import live_transactions, metrics_summary
from datatracemind.orchestration import supervisor
from datatracemind.repair import approval, executor, patch_store, validation
from datatracemind.workers import EtlWorker
from etl import run_log, transform_store
from simulator.generator import MAX_BATCH, TRAFFIC_PROFILES

logger = logging.getLogger(__name__)


def _auto_investigate(incident_id: str) -> None:
    if not get_settings().auto_investigate:
        return
    try:
        supervisor.start_investigation(incident_id)
    except supervisor.InvestigationRefused as exc:
        logger.warning("automatic investigation of %s not started: %s", incident_id, exc)


etl_worker = EtlWorker(on_incident=_auto_investigate)
# How old an answer in a page's snapshot may be when nothing was changed through the API.
SNAPSHOT_TTL_S = 2.0
snapshots = SnapshotCache(ttl_s=SNAPSHOT_TTL_S)

# A name, not free text: it is stored in the transformation history, which
# agents can read through get_etl_code, so it must not be able to carry
# instructions.
ApproverName = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._@-]{0,59}$")


class ApprovalDecision(BaseModel):
    patch_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    approver: str = ApproverName


class RejectionDecision(ApprovalDecision):
    reason: str | None = Field(default=None, max_length=500)


class ApplyRequest(BaseModel):
    patch_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class TrafficStart(BaseModel):
    profile: Literal[TRAFFIC_PROFILES] | None = None


class TrafficRate(BaseModel):
    profile: Literal[TRAFFIC_PROFILES]


class TrafficBatch(BaseModel):
    size: int = Field(default=25, ge=1, le=MAX_BATCH)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    snapshots.invalidate()
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
    if supervisor.investigation_running():
        raise HTTPException(status_code=409, detail="an investigation is running; wait for it to finish")
    return demo.reset()


@router.get("/demo/traffic")
def traffic_status() -> dict:
    return demo.traffic.status()


@guarded.post("/demo/traffic/rate")
def traffic_rate(request: TrafficRate) -> dict:
    """Choose the rate without starting or stopping the stream."""
    demo.traffic.set_profile(request.profile)
    return {"traffic": demo.traffic.status()}


@guarded.post("/demo/traffic/start")
def traffic_start(request: TrafficStart | None = None) -> dict:
    """Start the live stream, optionally at a given rate."""
    started = demo.traffic.start(request.profile if request else None)
    return {"started": started, "traffic_running": True, "traffic": demo.traffic.status()}


@guarded.post("/demo/traffic/stop")
def traffic_stop() -> dict:
    return {"stopped": demo.traffic.stop(), "traffic_running": False, "traffic": demo.traffic.status()}


@guarded.post("/demo/traffic/batch")
def traffic_batch(request: TrafficBatch | None = None) -> dict:
    """Write a fixed number of transactions to the source at once."""
    written = demo.traffic.send_batch(request.size if request else TrafficBatch().size)
    return {"written": written, "traffic": demo.traffic.status()}


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
    report = demo.inject(scenario_id)
    if report["incident_id"]:
        _auto_investigate(report["incident_id"])
    return report


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


# --- Agent and repair lifecycle ---------------------------------------------


@router.get("/agent/status")
def agent_status() -> dict:
    """Whether an investigation could start now, and why not if it could not."""
    provider = get_provider()
    availability = provider.availability()
    settings = get_settings()
    return {
        "available": availability.available,
        "method": availability.method,
        "detail": availability.detail,
        "provider": provider.name,
        "model": provider.model,
        "workflow_version": supervisor.WORKFLOW_VERSION,
        "investigation_running": supervisor.investigation_running(),
        "auto_investigate": settings.auto_investigate,
        "limits": {"per_hour": settings.investigations_per_hour, "per_day": settings.investigations_per_day},
        "access_code_required": settings.demo_access_code is not None,
    }


@guarded.post("/incidents/{incident_id}/investigate", status_code=202)
def incident_investigate(incident_id: str) -> dict:
    return supervisor.start_investigation(incident_id)


@guarded.post("/incidents/{incident_id}/validate")
def incident_validate(incident_id: str) -> dict:
    return validation.validate_proposal(incident_id).model_dump(mode="json")


@guarded.post("/incidents/{incident_id}/approve")
def incident_approve(incident_id: str, decision: ApprovalDecision) -> dict:
    outcome = supervisor.approve_and_apply(incident_id, decision.patch_hash, actor=decision.approver)
    return {"decision": "approved", "post_fix": outcome, "incident": store.get_incident(incident_id)}


@guarded.post("/incidents/{incident_id}/reject")
def incident_reject(incident_id: str, decision: RejectionDecision) -> dict:
    supervisor.reject(incident_id, decision.patch_hash, actor=decision.approver, reason=decision.reason)
    return {"decision": "rejected", "incident": store.get_incident(incident_id)}


@guarded.post("/incidents/{incident_id}/apply")
def incident_apply(incident_id: str, request: ApplyRequest) -> dict:
    outcome = executor.apply_approved_patch(incident_id, request.patch_hash)
    return {"post_fix": outcome, "incident": store.get_incident(incident_id)}


@guarded.post("/incidents/{incident_id}/verify")
def incident_verify(incident_id: str) -> dict:
    """Run post-fix verification if it is pending; otherwise return the recorded result."""
    with db.connect() as conn:
        status = store.get_incident_row(conn, incident_id)["status"]
    if status == "applying":
        return executor.run_post_fix_verification(incident_id)
    with db.connect() as conn:
        try:
            proposal = patch_store.get_proposal(conn, incident_id)
        except LookupError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    if proposal["post_fix_result"] is None:
        raise HTTPException(status_code=409, detail=f"{incident_id} is {status}; no repair has been applied")
    return proposal["post_fix_result"]


# --- Page snapshots ----------------------------------------------------------


def _incident_or_none(incident_id: str) -> dict | None:
    try:
        return store.get_incident(incident_id)
    except store.IncidentNotFound:
        return None


# What the pages show, by the name they ask for it, with the row limits they use.
# These read the database, so their answers are shared for SNAPSHOT_TTL_S.
_SNAPSHOT_PARTS: dict[str, Callable[[], object]] = {
    "metrics": metrics_summary,
    "runs": lambda: pipeline_runs(limit=8),
    "live": lambda: transactions_live(limit=12),
    "incidents": incidents,
    "audit": lambda: audit_log(limit=300),
    "scenarios": demo.lab_status,
}
# These are held in memory, so they are read for every response.
_SNAPSHOT_LIVE_PARTS: dict[str, Callable[[], object]] = {
    "agent": agent_status,
    "traffic": demo.traffic.status,
}


@router.get("/snapshot")
def snapshot(parts: str, incident: str | None = None) -> dict:
    """Everything a page shows, in one response.

    `parts` is a comma-separated list of names from the tables above, plus
    `incident` for the detail of the incident whose ID is given (null when
    there is no such incident). The pages poll this instead of one route for
    each panel.
    """
    names = list(dict.fromkeys(name for name in parts.split(",") if name))
    unknown = [n for n in names if n not in _SNAPSHOT_PARTS and n not in _SNAPSHOT_LIVE_PARTS and n != "incident"]
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown snapshot parts: {', '.join(unknown)}")
    if "incident" in names and not incident:
        raise HTTPException(status_code=422, detail="the incident part needs an incident ID")
    pending = {name: snapshots.get(name, _SNAPSHOT_PARTS[name]) for name in names if name in _SNAPSHOT_PARTS}
    if "incident" in names:
        pending["incident"] = snapshots.get(f"incident:{incident}", functools.partial(_incident_or_none, incident))
    answer = {name: future.result() for name, future in pending.items()}
    # Read last, so that they are as current as the response.
    answer.update({name: _SNAPSHOT_LIVE_PARTS[name]() for name in names if name in _SNAPSHOT_LIVE_PARTS})
    return answer


def create_app() -> FastAPI:
    app = FastAPI(title="DataTraceMind", lifespan=lifespan)
    app.include_router(router)
    app.include_router(guarded)

    @app.middleware("http")
    async def _fresh_snapshots_after_a_change(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # Emptied before the response leaves, so the page's next snapshot cannot predate its own change.
        try:
            return await call_next(request)
        finally:
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                snapshots.invalidate()

    @app.exception_handler(store.IncidentNotFound)
    def _not_found(request: Request, exc: store.IncidentNotFound) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": f"incident {exc} not found"})

    @app.exception_handler(store.IllegalTransition)
    @app.exception_handler(demo.LabError)
    @app.exception_handler(approval.ApprovalRefused)
    @app.exception_handler(executor.ApplyRefused)
    @app.exception_handler(validation.ValidationRefused)
    def _conflict(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(supervisor.InvestigationRefused)
    def _refused(request: Request, exc: supervisor.InvestigationRefused) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})

    # The built frontend is served from the same origin as the API, so one
    # service is the whole deployment. Mounted last so that /api routes take
    # precedence.
    dashboard = REPO_ROOT / "frontend" / "dist"
    if (dashboard / "index.html").is_file():
        app.mount("/", StaticFiles(directory=dashboard, html=True), name="dashboard")
    else:
        logger.warning("frontend/dist is missing, so only /api is served; run `npm run build` in frontend/")
    return app


app = create_app()
