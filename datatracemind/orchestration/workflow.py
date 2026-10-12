"""The investigation as a graph, run by Google ADK's workflow engine.

    contract_lookup
      ├─ source_investigator ──┐
      └─ pipeline_investigator ┴─ evidence ─ weigh_evidence ─ root_cause_analyst
                                                                │        ▲
                                                 more_evidence ─┘        │
                                                 targeted_investigator ──┘
    root_cause_analyst ─ diagnosed ─ repair_agent ─ staging_validation ─ passed ─ awaiting_approval
                                          ▲                  │
                                          └───── revise ─────┘
    any node ─ escalate ─ escalated

ADK schedules the nodes, runs the two investigators side by side, and follows
the route each node returns. It decides nothing: a node is plain code that
calls one role or one deterministic check and names the next edge. Model
usage happens inside the roles, through whichever provider the run was given.

The graph ends at a human decision point. `awaiting_approval` leaves a
validated repair for a person to approve through `datatracemind.repair
.approval`; `escalated` hands over an incident the workflow could not settle
within its bounds.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from google.adk.events.event import Event
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.workflow import START, FunctionNode, JoinNode, Workflow
from google.genai import types

from datatracemind import store
from datatracemind.agents import roles
from datatracemind.db import connect
from datatracemind.llm.base import LLMError
from datatracemind.models import RepairPlan
from datatracemind.orchestration import routing
from datatracemind.orchestration.state import RunContext
from datatracemind.repair import patch_store, validation
from datatracemind.repair.patch_store import PatchRejected

logger = logging.getLogger(__name__)

APP_NAME = "datatracemind"
ESCALATE = "escalate"
INVESTIGATORS = {
    "source_investigator": roles.investigate_source,
    "pipeline_investigator": roles.investigate_pipeline,
}


class Escalation(Exception):
    """The workflow cannot proceed on its own; a human has to decide."""


def escalate(ctx: RunContext, reason: str) -> None:
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


class Investigation:
    """One run of the graph: its nodes, and what they hand to each other."""

    def __init__(self, ctx: RunContext, plan: routing.DiagnosticPlan) -> None:
        self.ctx = ctx
        self.plan = plan
        self.outcome: str | None = None
        self.escalation = ""
        self.failures: dict[str, LLMError] = {}
        self.evidence_rounds = 0
        self.requested_diagnostics: list[str] = []
        self.root_cause: dict = {}
        self.repair_attempts = 0
        self.repair_plan: RepairPlan | None = None
        # The repair that was turned down, for the next attempt to revise.
        self.rejected_repair: dict | None = None

    # --- Graph ---------------------------------------------------------------

    def graph(self) -> Workflow:
        contract_lookup = self._node("contract_lookup", self.contract_lookup)
        investigators = tuple(self._node(role, lambda role=role: self.investigate(role)) for role in INVESTIGATORS)
        evidence = JoinNode(name="evidence")
        weigh_evidence = self._node("weigh_evidence", self.weigh_evidence)
        root_cause_analyst = self._node("root_cause_analyst", self.root_cause_analyst)
        targeted_investigator = self._node("targeted_investigator", self.targeted_investigator)
        repair_agent = self._node("repair_agent", self.repair_agent)
        staging_validation = self._node("staging_validation", self.staging_validation)
        awaiting_approval = self._node("awaiting_approval", lambda: self._end("awaiting_approval"))
        escalated = self._node("escalated", self.escalated)
        return Workflow(
            name="investigation",
            edges=[
                (START, contract_lookup),
                (contract_lookup, {"found": investigators, ESCALATE: escalated}),
                *((investigator, evidence) for investigator in investigators),
                (evidence, weigh_evidence),
                (weigh_evidence, {"analyse": root_cause_analyst, ESCALATE: escalated}),
                (
                    root_cause_analyst,
                    {"diagnosed": repair_agent, "more_evidence": targeted_investigator, ESCALATE: escalated},
                ),
                (targeted_investigator, {"analyse": root_cause_analyst, ESCALATE: escalated}),
                (repair_agent, {"proposed": staging_validation, ESCALATE: escalated}),
                (staging_validation, {"passed": awaiting_approval, "revise": repair_agent, ESCALATE: escalated}),
            ],
        )

    def _node(self, name: str, step: Callable[[], str | None]) -> FunctionNode:
        """A node that runs `step` and follows the route it returns.

        The step runs on a worker thread: roles wait on the model and the
        database, and the two investigators must not wait on each other. A
        step that cannot go on raises, which takes the `escalate` edge.
        """

        async def run() -> Event | None:
            try:
                route = await asyncio.to_thread(step)
            except Escalation as exc:
                route, self.escalation = ESCALATE, str(exc)
            except LLMError as exc:
                route, self.escalation = ESCALATE, f"{type(exc).__name__}: {exc}"
            return Event(route=route) if route else None

        return FunctionNode(func=run, name=name)

    # --- Nodes ---------------------------------------------------------------

    def contract_lookup(self) -> str:
        # Contract retrieval is deterministic and takes milliseconds, so it goes
        # first: a missing definition escalates before any model usage is spent.
        missing = roles.lookup_contracts(self.ctx, self.plan)
        if missing:
            raise Escalation(
                f"No approved definition exists for {', '.join(missing)}. "
                "A human must supply it; the workflow will not assume a business rule."
            )
        return "found"

    def investigate(self, role: str) -> None:
        # A failure here is weighed once both investigators are done, so it is
        # kept instead of raised.
        try:
            self.ctx.notes[role] = INVESTIGATORS[role](self.ctx, self.plan).model_dump()
        except LLMError as exc:
            self.failures[role] = exc

    def weigh_evidence(self) -> str:
        failures = [(role, self.failures[role]) for role in INVESTIGATORS if role in self.failures]
        fatal = next((exc for _, exc in failures if not exc.retryable), None)
        if fatal is not None:
            raise fatal
        if len(failures) == len(INVESTIGATORS):
            raise Escalation("Both investigations failed: " + "; ".join(str(exc) for _, exc in failures))
        for role, exc in failures:
            # One investigator's evidence may still be enough; the analyst decides.
            self.ctx.notes[role] = {"failed": str(exc)}
        return "analyse"

    def root_cause_analyst(self) -> str:
        """Analyse the evidence. Asks for at most one targeted follow-up round."""
        self.evidence_rounds += 1
        ctx = self.ctx
        report = roles.analyse_root_cause(ctx)
        decision = routing.after_analysis(report, round_number=self.evidence_rounds)
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
                    "round": self.evidence_rounds,
                    "decision": decision.action,
                    "needs_more_evidence": report.needs_more_evidence,
                    "requested_diagnostics": report.requested_diagnostics,
                    "business_rule_ambiguity": report.business_rule_ambiguity,
                },
            )
        if decision.action == "escalate":
            raise Escalation(decision.reason)
        if decision.action == "more_evidence":
            self.requested_diagnostics = report.requested_diagnostics
            return "more_evidence"
        with connect() as conn:
            store.set_status(
                conn, ctx.incident_id, "diagnosed", actor="supervisor", note=f"Root cause: {decision.reason}",
                payload={"implicated_component": report.implicated_component,
                         "evidence": root_cause.get("supporting_evidence", [])},
            )  # fmt: skip
        self.root_cause = root_cause
        return "diagnosed"

    def targeted_investigator(self) -> str:
        report = roles.investigate_targeted(self.ctx, self.requested_diagnostics)
        self.ctx.notes[f"follow_up_round_{self.evidence_rounds}"] = report.model_dump()
        return "analyse"

    def repair_agent(self) -> str:
        self.repair_attempts += 1
        plan = roles.propose_repair(self.ctx, self.root_cause, self.rejected_repair)
        if not plan.can_repair:
            raise Escalation(f"The repair agent proposes no automatic fix: {plan.escalation_reason}")
        self.repair_plan = plan
        return "proposed"

    def staging_validation(self) -> str:
        """File the proposed repair and validate it in staging. A rejected one is revised once."""
        ctx, plan = self.ctx, self.repair_plan
        try:
            proposal = patch_store.create_proposal(
                ctx.incident_id, plan, cited_evidence=self.root_cause.get("supporting_evidence", [])
            )
        except PatchRejected as exc:
            with connect() as conn:
                store.add_event(
                    conn, ctx.incident_id, agent="validator", event_type="guardrail", status="failed",
                    summary=f"Proposed change rejected before staging: {exc}",
                )  # fmt: skip
            return self._revise({"diff": exc.diff, "failures": [str(exc)]})
        with connect() as conn:
            store.set_status(
                conn, ctx.incident_id, "repair_proposed", actor="repair_agent",
                note=f"Proposed {proposal['proposal_id']}: {plan.rationale}",
                payload={"proposal_id": proposal["proposal_id"], "patch_hash": proposal["patch_hash"]},
            )  # fmt: skip
        result = validation.validate_proposal(ctx.incident_id)
        if result.passed:
            return "passed"
        return self._revise({"diff": proposal["diff"], "failures": result.failures})

    def _revise(self, rejected: dict[str, Any]) -> str:
        self.rejected_repair = rejected
        if self.repair_attempts >= routing.MAX_REPAIR_ATTEMPTS:
            raise Escalation(
                f"No proposed repair passed validation after {routing.MAX_REPAIR_ATTEMPTS} attempts: "
                + "; ".join(rejected["failures"])
            )
        return "revise"

    def escalated(self) -> None:
        escalate(self.ctx, self.escalation)
        self._end("escalated")

    def _end(self, outcome: str) -> None:
        self.outcome = outcome


async def _drive(graph: Workflow, incident_id: str) -> None:
    runner = Runner(
        node=graph, app_name=APP_NAME, session_service=InMemorySessionService(), auto_create_session=True
    )
    message = types.Content(role="user", parts=[types.Part(text=incident_id)])
    async for event in runner.run_async(user_id="supervisor", session_id=incident_id, new_message=message):
        logger.debug("%s: %s -> %s", incident_id, event.node_info.path, event.actions.route)


def run(ctx: RunContext, plan: routing.DiagnosticPlan) -> str:
    """Run the graph for one incident. Returns `awaiting_approval` or `escalated`."""
    investigation = Investigation(ctx, plan)
    # A loop of its own: an investigation runs on a background thread.
    asyncio.run(_drive(investigation.graph(), ctx.incident_id))
    if investigation.outcome is None:
        raise RuntimeError("the investigation graph ended without reaching a decision point")
    return investigation.outcome
