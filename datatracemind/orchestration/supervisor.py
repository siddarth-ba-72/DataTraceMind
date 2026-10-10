"""The supervisor: an explicit state machine over the incident lifecycle.

It decides which role runs next and never produces evidence or diagnoses
itself. One investigation run is one neatlogs trace:

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
from concurrent.futures import ThreadPoolExecutor

from datatracemind import store
from datatracemind.agents import roles
from datatracemind.config import get_settings
from datatracemind.db import connect
from datatracemind.llm import get_provider
from datatracemind.llm.base import LLMError, Provider
from datatracemind.observability import in_current_trace, investigation_run, span
from datatracemind.orchestration import routing
from datatracemind.orchestration.state import RunContext
from datatracemind.repair import approval, executor, patch_store, validation
from datatracemind.repair.patch_store import PatchRejected

logger = logging.getLogger(__name__)

# Label on every trace and run record. Bump it whenever a prompt, a tool, or a
# routing rule changes, so that before/after runs can be told apart.
#   v1  first complete workflow.
#   v2  pipeline investigator reads pipeline source code only when the
#       transformation file and its history do not explain the failed checks
#       (in the v1 baseline run that turn read two source files it did not
#       need and was the most expensive: 6 tool calls, 86,868 input tokens).
WORKFLOW_VERSION = "v2"

_active: dict[str, threading.Thread] = {}
_active_lock = threading.Lock()


class InvestigationRefused(RuntimeError):
    """An investigation cannot start now. `status_code` is the HTTP status to report."""

    def __init__(self, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


class Escalation(Exception):
    """The workflow cannot proceed on its own; a human has to decide."""


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


def _escalate(ctx: RunContext, reason: str) -> None:
    """Hand the incident to a human, recording why."""
    with connect() as conn:
        incident = store.get_incident_row(conn, ctx.incident_id, for_update=True)
        errors = [*incident["state"].get("errors", []), {"stage": incident["status"], "reason": reason}]
        store.update_state(conn, ctx.incident_id, errors=errors)
        if "escalated" in store.TRANSITIONS[incident["status"]]:
            store.set_status(conn, ctx.incident_id, "escalated", actor="supervisor", note=f"Escalated to a human: {reason}")
        else:
            store.add_event(
                conn, ctx.incident_id, agent="supervisor", event_type="error", status="failed", summary=reason
            )


def _gather_evidence(ctx: RunContext, plan: routing.DiagnosticPlan) -> None:
    """Retrieve the governing contracts, then run both investigators concurrently."""
    # Contract retrieval is deterministic and takes milliseconds, so it goes
    # first: a missing definition escalates before any model usage is spent.
    missing_contracts = roles.lookup_contracts(ctx, plan)
    if missing_contracts:
        raise Escalation(
            f"No approved definition exists for {', '.join(missing_contracts)}. "
            "A human must supply it; the workflow will not assume a business rule."
        )
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="investigator") as pool:
        futures = {
            "source_investigator": pool.submit(in_current_trace(roles.investigate_source), ctx, plan),
            "pipeline_investigator": pool.submit(in_current_trace(roles.investigate_pipeline), ctx, plan),
        }
        failures: dict[str, LLMError] = {}
        for role, future in futures.items():
            try:
                ctx.notes[role] = future.result().model_dump()
            except LLMError as exc:
                failures[role] = exc
    fatal = next((exc for exc in failures.values() if not exc.retryable), None)
    if fatal is not None:
        raise fatal
    if len(failures) == len(futures):
        raise Escalation("Both investigations failed: " + "; ".join(str(exc) for exc in failures.values()))
    for role, exc in failures.items():
        # One investigator's evidence may still be enough; the analyst decides.
        ctx.notes[role] = {"failed": str(exc)}


def _diagnose(ctx: RunContext) -> dict:
    """Analyse the evidence, with at most one targeted follow-up round."""
    for round_number in range(1, routing.MAX_EVIDENCE_ROUNDS + 1):
        report = roles.analyse_root_cause(ctx)
        decision = routing.after_analysis(report, round_number=round_number)
        selected = next((h for h in report.hypotheses if h.hypothesis_id == report.selected_hypothesis_id), None)
        root_cause = {
            "summary": report.root_cause_summary,
            "implicated_component": report.implicated_component,
            "confirmed": decision.action == "diagnosed",
            **(selected.model_dump() if selected else {}),
        }
        with connect() as conn:
            store.update_state(
                conn,
                ctx.incident_id,
                hypotheses=[h.model_dump() for h in report.hypotheses],
                selected_root_cause=root_cause if decision.action == "diagnosed" else None,
                analysis={
                    "round": round_number,
                    "decision": decision.action,
                    "needs_more_evidence": report.needs_more_evidence,
                    "requested_diagnostics": report.requested_diagnostics,
                    "business_rule_ambiguity": report.business_rule_ambiguity,
                },
            )
        if decision.action == "diagnosed":
            with connect() as conn:
                store.set_status(
                    conn, ctx.incident_id, "diagnosed", actor="supervisor", note=f"Root cause: {decision.reason}",
                    payload={"implicated_component": report.implicated_component,
                             "evidence": root_cause.get("supporting_evidence", [])},
                )  # fmt: skip
            return root_cause
        if decision.action == "escalate":
            raise Escalation(decision.reason)
        ctx.notes[f"follow_up_round_{round_number}"] = roles.investigate_targeted(
            ctx, report.requested_diagnostics
        ).model_dump()
    raise Escalation("The evidence does not confirm a root cause.")


def _repair(ctx: RunContext, root_cause: dict) -> None:
    """Propose a repair and validate it in staging, revising a rejected one once."""
    previous: dict | None = None
    for _attempt in range(routing.MAX_REPAIR_ATTEMPTS):
        plan = roles.propose_repair(ctx, root_cause, previous)
        if not plan.can_repair:
            raise Escalation(f"The repair agent proposes no automatic fix: {plan.escalation_reason}")
        try:
            proposal = patch_store.create_proposal(
                ctx.incident_id, plan, cited_evidence=root_cause.get("supporting_evidence", [])
            )
        except PatchRejected as exc:
            with connect() as conn:
                store.add_event(
                    conn, ctx.incident_id, agent="validator", event_type="guardrail", status="failed",
                    summary=f"Proposed change rejected before staging: {exc}",
                )  # fmt: skip
            previous = {"diff": exc.diff, "failures": [str(exc)]}
            continue
        with connect() as conn:
            store.set_status(
                conn, ctx.incident_id, "repair_proposed", actor="repair_agent",
                note=f"Proposed {proposal['proposal_id']}: {plan.rationale}",
                payload={"proposal_id": proposal["proposal_id"], "patch_hash": proposal["patch_hash"]},
            )  # fmt: skip
        result = validation.validate_proposal(ctx.incident_id)
        if result.passed:
            return
        previous = {"diff": proposal["diff"], "failures": result.failures}
    raise Escalation(
        f"No proposed repair passed validation after {routing.MAX_REPAIR_ATTEMPTS} attempts: "
        + "; ".join(previous["failures"] if previous else [])
    )


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
        with investigation_run(
            incident_id, scenario_id=incident["scenario_id"], version=WORKFLOW_VERSION
        ) as trace_id:
            with connect() as conn:
                conn.execute("UPDATE dtm.investigation_runs SET trace_id = %s WHERE run_id = %s", (trace_id, run_id))
                store.update_state(
                    conn, incident_id,
                    trace_ref={"trace_id": trace_id, "workflow_version": WORKFLOW_VERSION, "run_id": run_id},
                )  # fmt: skip
            try:
                _gather_evidence(ctx, routing.plan_for(incident["incident_type"]))
                root_cause = _diagnose(ctx)
                _repair(ctx, root_cause)
                outcome = "awaiting_approval"
            except Escalation as exc:
                _escalate(ctx, str(exc))
                outcome = "escalated"
            except LLMError as exc:
                _escalate(ctx, f"{type(exc).__name__}: {exc}")
                outcome = "escalated"
    except Exception as exc:
        logger.exception("investigation of %s failed unexpectedly", incident_id)
        try:
            _escalate(ctx, f"The workflow stopped on an unexpected error: {type(exc).__name__}: {exc}")
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


def _scenario_of(incident_id: str) -> str | None:
    with connect() as conn:
        return store.get_incident_row(conn, incident_id)["scenario_id"]


def approve_and_apply(incident_id: str, patch_hash: str, *, actor: str) -> dict:
    """Record a human approval, then apply and verify that exact patch.

    "Auto-apply" means this: application follows approval without a second
    click. Both steps re-check their own preconditions; the trace of the
    decision and its outcome is grouped with the incident's investigation.
    """
    with investigation_run(
        incident_id, scenario_id=_scenario_of(incident_id), version=WORKFLOW_VERSION, phase="approval_and_apply"
    ):
        gate = {"incident_id": incident_id, "patch_hash": patch_hash, "decision": "approve"}
        with span("human_approval", "GUARDRAIL", input=gate):
            approval.approve(incident_id, patch_hash, actor=actor)
        return executor.apply_approved_patch(incident_id, patch_hash)


def reject(incident_id: str, patch_hash: str, *, actor: str, reason: str | None) -> dict:
    with investigation_run(
        incident_id, scenario_id=_scenario_of(incident_id), version=WORKFLOW_VERSION, phase="rejection"
    ):
        gate = {"incident_id": incident_id, "patch_hash": patch_hash, "decision": "reject"}
        with span("human_approval", "GUARDRAIL", input=gate):
            return approval.reject(incident_id, patch_hash, actor=actor, reason=reason)
