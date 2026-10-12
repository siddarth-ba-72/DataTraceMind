"""The supervisor: starts, bounds, and records an investigation run.

It never produces evidence or diagnoses itself. Which role runs next is the
graph in `datatracemind.orchestration.workflow`, which this module runs once
per investigation:

    detected -> investigating
        contract lookup, then source investigator | pipeline investigator   (concurrently)
        root-cause analyst  (one targeted follow-up round if it asks for more)
    -> diagnosed
        repair agent -> proposal -> staging validation   (one revision if rejected)
    -> awaiting_approval

It stops there. Applying a repair needs a human decision, which arrives
through `datatracemind.repair.approval`, not through this module. Anything
the workflow cannot settle within its bounds is escalated to a human.
"""

from __future__ import annotations

import logging
import threading
import time

from datatracemind import store
from datatracemind.config import get_settings
from datatracemind.db import connect
from datatracemind.llm import get_provider
from datatracemind.llm.base import Provider
from datatracemind.orchestration import routing, workflow
from datatracemind.orchestration.state import RunContext
from datatracemind.repair import approval, executor, patch_store

logger = logging.getLogger(__name__)

# Label on every run record. Bump it whenever a prompt, a tool, or a routing
# rule changes, so that before/after runs can be told apart.
#   v1  first complete workflow.
#   v2  pipeline investigator reads pipeline source code only when the
#       transformation file and its history do not explain the failed checks
#       (in the v1 baseline run that turn read two source files it did not
#       need and was the most expensive: 6 tool calls, 86,868 input tokens).
#   v3  same roles, prompts, and routing rules, now run as a Google ADK
#       workflow graph; Gemini through ADK became the default model provider.
WORKFLOW_VERSION = "v3"

_active: dict[str, threading.Thread] = {}
_active_lock = threading.Lock()


class InvestigationRefused(RuntimeError):
    """An investigation cannot start now. `status_code` is the HTTP status to report."""

    def __init__(self, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


def investigation_running(incident_id: str | None = None) -> bool:
    with _active_lock:
        threads = [_active.get(incident_id)] if incident_id else list(_active.values())
        return any(thread is not None and thread.is_alive() for thread in threads)


def _check_caps(conn) -> None:
    settings = get_settings()
    counts = conn.execute(
        "SELECT count(*) FILTER (WHERE started_at > now() - interval '1 hour') AS hour, "
        "count(*) FILTER (WHERE started_at > now() - interval '1 day') AS day FROM dtm.investigation_runs"
    ).fetchone()
    if counts["hour"] >= settings.investigations_per_hour or counts["day"] >= settings.investigations_per_day:
        raise InvestigationRefused(
            f"investigation limit reached ({settings.investigations_per_hour} per hour, "
            f"{settings.investigations_per_day} per day); try again later",
            status_code=429,
        )


def start_investigation(incident_id: str, *, provider: Provider | None = None) -> dict:
    """Check that an investigation may start, then run it on a background thread."""
    provider = provider or get_provider()
    availability = provider.availability()
    if not availability.available:
        raise InvestigationRefused(f"The agent is unavailable. {availability.detail}", status_code=503)
    with _active_lock:
        if any(thread.is_alive() for thread in _active.values()):
            raise InvestigationRefused("an investigation is already running")
        with connect() as conn:
            incident = store.get_incident_row(conn, incident_id)
            if incident["status"] not in store.RESTARTABLE:
                raise InvestigationRefused(f"{incident_id} is {incident['status']}; it cannot be investigated now")
            _check_caps(conn)
        thread = threading.Thread(
            target=run_investigation, args=(incident_id,), kwargs={"provider": provider},
            name=f"investigation-{incident_id}", daemon=True,
        )  # fmt: skip
        _active[incident_id] = thread
        thread.start()
    return {"incident_id": incident_id, "started": True, "workflow_version": WORKFLOW_VERSION}


def _begin(incident_id: str, provider: Provider) -> tuple[dict, int]:
    """Move the incident to `investigating` and open the run record."""
    with connect() as conn:
        incident = store.get_incident_row(conn, incident_id, for_update=True)
        restart = incident["status"] != "detected"
        if restart:
            # A fresh investigation invalidates any repair still on the table.
            patch_store.supersede_open(conn, incident_id)
        store.set_status(
            conn, incident_id, "investigating", actor="supervisor",
            note="Investigation restarted" if restart else "Investigation started",
            payload={"workflow_version": WORKFLOW_VERSION, "model": provider.model},
        )  # fmt: skip
        run_id = conn.execute(
            "INSERT INTO dtm.investigation_runs (incident_id, scenario_id, workflow_version, model, status) "
            "VALUES (%s, %s, %s, %s, 'running') RETURNING run_id",
            (incident_id, incident["scenario_id"], WORKFLOW_VERSION, provider.model),
        ).fetchone()["run_id"]
    return incident, run_id


def _finish(run_id: int, ctx: RunContext, status: str, started: float) -> None:
    totals = ctx.totals
    with connect() as conn:
        conn.execute(
            """
            UPDATE dtm.investigation_runs SET status = %s, finished_at = now(), duration_s = %s, role_turns = %s,
                tool_calls = %s, failed_tool_calls = %s, retries = %s, input_tokens = %s,
                cached_input_tokens = %s, output_tokens = %s, est_cost_usd = %s
            WHERE run_id = %s
            """,
            (
                status, round(time.monotonic() - started, 2), totals.role_turns, totals.tool_calls,
                totals.failed_tool_calls, totals.retries, totals.usage.input_tokens,
                totals.usage.cached_input_tokens, totals.usage.output_tokens, round(totals.est_cost_usd, 4), run_id,
            ),
        )  # fmt: skip


def run_investigation(incident_id: str, *, provider: Provider | None = None) -> str:
    """Run the workflow for one incident to its next human decision point.

    Returns the run's outcome: `awaiting_approval`, `escalated`, or `failed`.
    Never raises for a workflow failure; those end in an escalation.
    """
    provider = provider or get_provider()
    started = time.monotonic()
    incident, run_id = _begin(incident_id, provider)
    ctx = RunContext(incident=incident, provider=provider)
    outcome = "failed"
    try:
        outcome = workflow.run(ctx, routing.plan_for(incident["incident_type"]))
    except Exception as exc:
        logger.exception("investigation of %s failed unexpectedly", incident_id)
        try:
            workflow.escalate(ctx, f"The workflow stopped on an unexpected error: {type(exc).__name__}: {exc}")
        except Exception:
            logger.exception("could not record the failure of %s", incident_id)
    finally:
        try:
            _finish(run_id, ctx, outcome, started)
        except Exception:
            logger.exception("could not close run record %s", run_id)
        with _active_lock:
            _active.pop(incident_id, None)
    logger.info(
        "investigation of %s: %s in %.0fs, %s turns, %s tool calls, est $%.3f",
        incident_id, outcome, time.monotonic() - started, ctx.totals.role_turns, ctx.totals.tool_calls,
        ctx.totals.est_cost_usd,
    )  # fmt: skip
    return outcome


def approve_and_apply(incident_id: str, patch_hash: str, *, actor: str) -> dict:
    """Record a human approval, then apply and verify that exact patch.

    "Auto-apply" means this: application follows approval without a second
    click. Both steps re-check their own preconditions.
    """
    approval.approve(incident_id, patch_hash, actor=actor)
    return executor.apply_approved_patch(incident_id, patch_hash)


def reject(incident_id: str, patch_hash: str, *, actor: str, reason: str | None) -> dict:
    return approval.reject(incident_id, patch_hash, actor=actor, reason=reason)
