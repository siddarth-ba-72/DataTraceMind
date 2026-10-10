"""Test double for the model provider.

`ScriptedProvider` stands in for Codex so that the workflow's control flow and
guardrails can be tested without spending model usage. Its tool calls are
real: it runs the same tool functions against the test database and returns
their results, so evidence in these tests is genuine. Only the role outputs
are scripted, by the test that needs them.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from datatracemind import db, store
from datatracemind.config import TRANSFORM_FILE
from datatracemind.llm.base import Availability, LLMError, RoleCall, RoleResult
from datatracemind.models import (
    Hypothesis,
    InvestigationReport,
    Observation,
    RepairPlan,
    RootCauseReport,
    TokenUsage,
    ToolCallRecord,
)
from datatracemind.tools.registry import TOOLS
from etl import transform_store

# Arguments each tool is called with when a role's plan requires it.
DEFAULT_ARGUMENTS: dict[str, dict[str, Any]] = {
    "compare_source_target_metrics": {"metric_name": "net_revenue", "time_range": "all"},
    "run_readonly_sql": {"query_template_id": "source_refunds_by_status"},
    "get_etl_code": {"version_or_path": "active"},
    "get_etl_run_logs": {"run_id_or_recent_window": "last_5"},
    "get_source_schema": {"table_name": "orders"},
    "find_missing_source_ids": {"source_table": "orders", "target_table": "fact_orders"},
    "find_duplicate_business_keys": {"table_name": "fact_payments", "key_columns": ["payment_id"]},
    "get_checkpoint": {"pipeline_name": "order_revenue"},
}

Step = Callable[[RoleCall], BaseModel]


def call_tool(tool: str, arguments: dict[str, Any] | None = None) -> ToolCallRecord:
    arguments = DEFAULT_ARGUMENTS[tool] if arguments is None else arguments
    try:
        result = TOOLS[tool].fn(**arguments)
    except Exception as exc:
        return ToolCallRecord(tool=tool, arguments=arguments, status="failed", error=str(exc))
    return ToolCallRecord(tool=tool, arguments=arguments, result=result, status="completed")


class ScriptedProvider:
    """Runs each role's required tools for real, then returns the scripted output.

    `script` maps a role to a list of steps consumed one per turn. A step is a
    function of the RoleCall that returns the role's output or raises LLMError.
    A role with no steps left uses its default output.
    """

    name = "scripted"
    model = "test-model"

    def __init__(self, script: dict[str, list[Step]] | None = None, *, available: bool = True) -> None:
        self.script = {role: list(steps) for role, steps in (script or {}).items()}
        self.available = available
        self.calls: list[RoleCall] = []

    def availability(self) -> Availability:
        if self.available:
            return Availability(True, "api_key", "scripted provider")
        return Availability(False, "none", "Install the Codex CLI and run `codex login`, or set CODEX_API_KEY.")

    def estimate_cost(self, usage: TokenUsage) -> float:
        return usage.input_tokens / 1_000_000

    def run_turn(self, call: RoleCall) -> RoleResult:
        self.calls.append(call)
        usage = TokenUsage(input_tokens=1000, cached_input_tokens=400, output_tokens=100)
        tool_calls = [call_tool(tool) for tool in call.required_tools]
        if call.role == "targeted_investigator":
            tool_calls.append(call_tool("get_checkpoint"))
        steps = self.script.get(call.role) or []
        step = steps.pop(0) if steps else DEFAULT_STEPS[call.role]
        try:
            output = step(call)
        except LLMError as exc:
            exc.tool_calls, exc.usage = tool_calls, usage
            raise
        return RoleResult(
            output=output, tool_calls=tool_calls, usage=usage, duration_s=0.01, model=self.model,
            reasoning_effort="none", est_cost_usd=self.estimate_cost(usage),
        )  # fmt: skip

    def turns(self, role: str) -> int:
        return sum(1 for call in self.calls if call.role == role)


# --- Scripted outputs --------------------------------------------------------


def report(_: RoleCall) -> InvestigationReport:
    return InvestigationReport(
        observations=[Observation(statement="see the recorded tool results", tool="(scripted)")], open_questions=[]
    )


def evidence_ids() -> list[str]:
    """IDs of all evidence filed for the (single) incident under test."""
    return [row["evidence_id"] for row in db.fetch_all("SELECT evidence_id FROM dtm.evidence ORDER BY evidence_id")]


def diagnosis(
    category: str = "refund_logic",
    *,
    confirmed: bool = True,
    confidence: float = 0.9,
    cite: Callable[[], list[str]] = evidence_ids,
    needs_more_evidence: bool = False,
    ambiguity: str = "",
) -> Step:
    def step(_: RoleCall) -> RootCauseReport:
        cited = cite()
        return RootCauseReport(
            hypotheses=[
                Hypothesis(
                    hypothesis_id="H1", category=category, statement="scripted leading hypothesis",
                    confidence=confidence, supporting_evidence=cited, contradicting_evidence=[], reasoning="scripted",
                ),
                Hypothesis(
                    hypothesis_id="H2", category="unknown", statement="scripted alternative", confidence=0.1,
                    supporting_evidence=[], contradicting_evidence=cited[:1], reasoning="scripted",
                ),
            ],
            selected_hypothesis_id="H1" if confirmed else "",
            confirmed=confirmed,
            root_cause_summary="scripted root cause",
            implicated_component=f"{TRANSFORM_FILE}: revenue.refund_statuses",
            needs_more_evidence=needs_more_evidence,
            requested_diagnostics=["read the pipeline checkpoint"] if needs_more_evidence else [],
            business_rule_ambiguity=ambiguity,
        )  # fmt: skip

    return step


def repair(*replacements: tuple[str, str], target_file: str = TRANSFORM_FILE, can_repair: bool = True) -> Step:
    """A repair that edits the deployed transformation by plain replacement."""

    def step(_: RoleCall) -> RepairPlan:
        content = transform_store.get_active().content
        for old, new in replacements:
            assert content.count(old) == 1, f"{old!r} is not in the deployed transformation exactly once"
            content = content.replace(old, new)
        return RepairPlan(
            can_repair=can_repair,
            escalation_reason="" if can_repair else "the source stopped producing data",
            target_file=target_file,
            new_content=content,
            rationale="scripted rationale",
            risk="low",
            rollback_plan="redeploy the previous version",
            affected_metrics=["daily_revenue.net_revenue"],
            regression_cases=["partial refund", "pending refund"],
        )

    return step


def raises(error: LLMError) -> Step:
    def step(_: RoleCall) -> BaseModel:
        raise error

    return step


FIX = {
    "DS-01": ("refund_statuses: [COMPLETED]", "refund_statuses: [SUCCEEDED]"),
    "DS-02": ("amount_column: total_amount", "amount_column: order_total"),
    "DS-03": ("payment_join: inner", "payment_join: left"),
    "DS-04": ("mode: append", "mode: replace_by_key"),
    "DS-07": ("timezone: UTC", "timezone: Asia/Kolkata"),
    "DS-16": ("refund_amount_basis: order_total", "refund_amount_basis: refund_amount"),
    "DS-17": ("payment_statuses: [CAPTURED, FAILED]", "payment_statuses: [CAPTURED]"),
}
# Root-cause category a correct diagnosis of each repairable scenario names.
CATEGORY = {
    "DS-01": "refund_logic",
    "DS-02": "schema_drift",
    "DS-03": "join_loss",
    "DS-04": "duplicate_load",
    "DS-07": "timezone",
    "DS-16": "refund_logic",
    "DS-17": "payment_logic",
}
# A change that parses but breaks the contract: pending refunds get subtracted.
WRONG_FIX = ("refund_statuses: [COMPLETED]", "refund_statuses: [SUCCEEDED, PENDING]")

DEFAULT_STEPS: dict[str, Step] = {
    "source_investigator": report,
    "pipeline_investigator": report,
    "targeted_investigator": report,
    "root_cause_analyst": diagnosis(),
    "repair_agent": repair(FIX["DS-01"]),
}


def incident(incident_id: str) -> dict:
    return store.get_incident(incident_id)


def statuses(incident_id: str) -> list[str]:
    """Lifecycle statuses the incident has passed through, in order."""
    return [e["status"] for e in incident(incident_id)["events"] if e["event_type"] == "lifecycle"]


def active_transform() -> str:
    return transform_store.get_active().content
