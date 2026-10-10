"""The agent roles of the investigation workflow.

Three are model-backed turns (investigator in two modes, root-cause analyst,
repair agent). Contract lookup is a deterministic retrieval step: it calls the
contract tools in-process and files what they return. The validator lives in
`datatracemind.repair.validation` and is deterministic code.
"""

from __future__ import annotations

from datatracemind import store
from datatracemind.agents import prompts
from datatracemind.agents.runner import file_tool_calls, run_role
from datatracemind.db import connect
from datatracemind.llm.base import InvalidOutput
from datatracemind.models import InvestigationReport, RepairPlan, RootCauseReport, ToolCallRecord
from datatracemind.observability import span
from datatracemind.orchestration.routing import (
    ALL_INVESTIGATION_TOOLS,
    MAX_TOOL_CALLS_PER_TURN,
    REPAIR_TOOLS,
    DiagnosticPlan,
)
from datatracemind.orchestration.state import RunContext
from datatracemind.tools.investigation import ToolError
from datatracemind.tools.registry import TOOLS


def investigate_source(ctx: RunContext, plan: DiagnosticPlan) -> InvestigationReport:
    report, _ = run_role(
        ctx,
        role="source_investigator",
        goal="Profile the source data and compare it with analytics",
        prompt=prompts.source_investigator(
            ctx.incident, required=plan.source_required, max_calls=MAX_TOOL_CALLS_PER_TURN
        ),
        output_model=InvestigationReport,
        allowed_tools=plan.source_tools,
        required_tools=plan.source_required,
    )
    return report


def investigate_pipeline(ctx: RunContext, plan: DiagnosticPlan) -> InvestigationReport:
    report, _ = run_role(
        ctx,
        role="pipeline_investigator",
        goal="Inspect the transformation file, its history, and the run logs",
        prompt=prompts.pipeline_investigator(
            ctx.incident, required=plan.pipeline_required, max_calls=MAX_TOOL_CALLS_PER_TURN
        ),
        output_model=InvestigationReport,
        allowed_tools=plan.pipeline_tools,
        required_tools=plan.pipeline_required,
    )
    return report


def investigate_targeted(ctx: RunContext, requests: list[str]) -> InvestigationReport:
    """Follow-up round: run the diagnostics the analyst asked for."""
    before = _evidence_ids(ctx)

    def must_add_evidence(_: InvestigationReport) -> None:
        if _evidence_ids(ctx) == before:
            raise InvalidOutput("the follow-up made no successful tool call, so it added no evidence")

    report, _ = run_role(
        ctx,
        role="targeted_investigator",
        goal="Run the diagnostics the analyst asked for",
        prompt=prompts.targeted_investigator(ctx.incident, requests=requests, max_calls=MAX_TOOL_CALLS_PER_TURN),
        output_model=InvestigationReport,
        allowed_tools=ALL_INVESTIGATION_TOOLS,
        validate=must_add_evidence,
    )
    return report


def _evidence_ids(ctx: RunContext) -> set[str]:
    with connect() as conn:
        return {item["evidence_id"] for item in store.list_evidence(conn, ctx.incident_id)}


def lookup_contracts(ctx: RunContext, plan: DiagnosticPlan) -> list[str]:
    """Retrieve the approved contracts for this incident. Returns the missing ones.

    Deterministic: no model decides which rule applies. A contract that does
    not exist is reported to the supervisor, which escalates instead of
    letting a later role fill the gap with an assumption.
    """
    role = "contract_agent"
    missing: list[str] = []
    calls: list[ToolCallRecord] = []
    with span(role, "AGENT", input=[f"{kind}:{name}" for kind, name in plan.contracts], **{"agent.name": role}):
        for kind, name in plan.contracts:
            tool = "get_metric_contract" if kind == "metric" else "get_data_contract"
            arguments = {"metric_name" if kind == "metric" else "dataset_name": name}
            with span(tool, "TOOL", input=arguments, **{"neatlogs.tool.name": tool}) as tool_span:
                try:
                    result = TOOLS[tool].fn(**arguments)
                except ToolError as exc:
                    tool_span.fail(str(exc))
                    missing.append(f"{kind} contract '{name}'")
                    calls.append(ToolCallRecord(tool=tool, arguments=arguments, status="failed", error=str(exc)))
                else:
                    tool_span.set_output({"path": result["path"]})
                    calls.append(ToolCallRecord(tool=tool, arguments=arguments, result=result, status="completed"))
    evidence_ids = file_tool_calls(ctx, role, calls)
    with connect() as conn:
        store.add_event(
            conn, ctx.incident_id, agent=role, event_type="agent_run",
            status="failed" if missing else "completed",
            summary=(
                f"No approved definition found for: {', '.join(missing)}" if missing
                else f"Retrieved {len(evidence_ids)} approved contract(s)"
            ),
            payload={"evidence_ids": evidence_ids, "missing": missing},
        )  # fmt: skip
        store.update_state(
            conn,
            ctx.incident_id,
            approved_business_rules=[
                {"contract": call.arguments, "path": call.result["path"]} for call in calls if call.status == "completed"
            ],
        )
    return missing


def analyse_root_cause(ctx: RunContext) -> RootCauseReport:
    with connect() as conn:
        evidence = store.list_evidence(conn, ctx.incident_id)
    known = {item["evidence_id"] for item in evidence}

    def cites_real_evidence(report: RootCauseReport) -> None:
        if not report.hypotheses:
            raise InvalidOutput("no hypotheses were given")
        cited = {e for h in report.hypotheses for e in (*h.supporting_evidence, *h.contradicting_evidence)}
        unknown = sorted(cited - known)
        if unknown:
            raise InvalidOutput(f"these evidence IDs do not exist: {unknown}; valid IDs are {sorted(known)}")
        if report.confirmed:
            selected = next(
                (h for h in report.hypotheses if h.hypothesis_id == report.selected_hypothesis_id), None
            )
            if selected is None:
                raise InvalidOutput("confirmed is true but selected_hypothesis_id names no listed hypothesis")
            if not selected.supporting_evidence:
                raise InvalidOutput("a confirmed root cause must cite the evidence that supports it")

    report, _ = run_role(
        ctx,
        role="root_cause_analyst",
        goal=f"Rank hypotheses against {len(evidence)} evidence items",
        prompt=prompts.root_cause_analyst(ctx.incident, evidence=evidence, notes=ctx.notes),
        output_model=RootCauseReport,
        validate=cites_real_evidence,
    )
    return report


def propose_repair(ctx: RunContext, root_cause: dict, previous: dict | None) -> RepairPlan:
    with connect() as conn:
        evidence = store.list_evidence(conn, ctx.incident_id)
    cited = set(root_cause.get("supporting_evidence", []))
    relevant = [e for e in evidence if e["evidence_id"] in cited or e["kind"] == "contract"]
    plan, _ = run_role(
        ctx,
        role="repair_agent",
        goal="Revise the rejected repair" if previous else "Propose a minimal repair for the confirmed root cause",
        prompt=prompts.repair_agent(ctx.incident, root_cause=root_cause, evidence=relevant, previous=previous),
        output_model=RepairPlan,
        allowed_tools=REPAIR_TOOLS,
        # The patch must start from the file as deployed, read in this turn.
        required_tools=("get_etl_code",),
    )
    return plan
