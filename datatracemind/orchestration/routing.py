"""Routing decisions of the supervisor, as plain functions.

Which tools each investigator gets for an incident type, and what happens
after the analyst and after validation. Keeping these free of I/O makes the
workflow's branches testable without a model.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from datatracemind.models import RootCauseReport

# One turn per role, retried at most once; one extra evidence round; one
# revision of a rejected repair. An incident that needs more goes to a human.
MAX_ROLE_ATTEMPTS = 2
MAX_EVIDENCE_ROUNDS = 2
MAX_REPAIR_ATTEMPTS = 2
MAX_TOOL_CALLS_PER_TURN = 6
MIN_CONFIDENCE = 0.6

REPAIR_TOOLS = ("get_etl_code", "get_metric_contract", "get_data_contract")
ALL_INVESTIGATION_TOOLS = (
    "get_source_schema",
    "get_analytics_schema",
    "profile_column",
    "run_readonly_sql",
    "compare_source_target_metrics",
    "find_missing_source_ids",
    "find_duplicate_business_keys",
    "get_etl_code",
    "get_etl_run_logs",
    "get_checkpoint",
)

_SOURCE_TOOLS = (
    "compare_source_target_metrics",
    "run_readonly_sql",
    "profile_column",
    "get_source_schema",
    "find_missing_source_ids",
    "find_duplicate_business_keys",
)
_PIPELINE_TOOLS = ("get_etl_code", "get_etl_run_logs", "get_checkpoint", "get_analytics_schema", "run_readonly_sql")


@dataclass(frozen=True)
class DiagnosticPlan:
    """What each parallel investigation must do for one incident type."""

    source_required: tuple[str, ...]
    pipeline_required: tuple[str, ...]
    # (kind, name) of the contracts that define correct behaviour here.
    contracts: tuple[tuple[str, str], ...]
    source_tools: tuple[str, ...] = _SOURCE_TOOLS
    pipeline_tools: tuple[str, ...] = _PIPELINE_TOOLS


_PLANS = {
    "revenue_mismatch": DiagnosticPlan(
        source_required=("compare_source_target_metrics", "run_readonly_sql"),
        pipeline_required=("get_etl_code", "get_etl_run_logs"),
        contracts=(("metric", "net_revenue"),),
    ),
    "rule_violation": DiagnosticPlan(
        source_required=("run_readonly_sql",),
        pipeline_required=("get_etl_code", "get_etl_run_logs"),
        contracts=(("metric", "net_revenue"),),
    ),
    "pipeline_failure": DiagnosticPlan(
        source_required=("get_source_schema",),
        pipeline_required=("get_etl_run_logs", "get_etl_code"),
        contracts=(("data", "oltp_schema"), ("data", "orders")),
    ),
    "schema_drift": DiagnosticPlan(
        source_required=("get_source_schema",),
        pipeline_required=("get_etl_run_logs", "get_etl_code"),
        contracts=(("data", "oltp_schema"),),
    ),
    "record_loss": DiagnosticPlan(
        source_required=("find_missing_source_ids", "run_readonly_sql"),
        pipeline_required=("get_etl_code", "get_etl_run_logs"),
        contracts=(("data", "orders"), ("metric", "net_revenue")),
    ),
    "duplicate_records": DiagnosticPlan(
        source_required=("find_duplicate_business_keys",),
        pipeline_required=("get_etl_code", "get_etl_run_logs", "get_checkpoint"),
        contracts=(("data", "orders"),),
    ),
}
_DEFAULT_PLAN = DiagnosticPlan(
    source_required=("compare_source_target_metrics",),
    pipeline_required=("get_etl_run_logs", "get_etl_code"),
    contracts=(("metric", "net_revenue"), ("data", "orders")),
)


def plan_for(incident_type: str) -> DiagnosticPlan:
    return _PLANS.get(incident_type, _DEFAULT_PLAN)


@dataclass(frozen=True)
class Decision:
    action: Literal["diagnosed", "more_evidence", "escalate"]
    reason: str


def after_analysis(report: RootCauseReport, *, round_number: int) -> Decision:
    """What to do with a root-cause report. `round_number` starts at 1."""
    if report.business_rule_ambiguity.strip():
        return Decision(
            "escalate",
            f"A business rule needs a human decision: {report.business_rule_ambiguity.strip()}",
        )
    selected = next((h for h in report.hypotheses if h.hypothesis_id == report.selected_hypothesis_id), None)
    if report.confirmed and selected is not None and selected.confidence >= MIN_CONFIDENCE:
        return Decision("diagnosed", report.root_cause_summary)
    if round_number < MAX_EVIDENCE_ROUNDS and report.needs_more_evidence and report.requested_diagnostics:
        return Decision("more_evidence", "The analyst asked for targeted diagnostics.")
    return Decision(
        "escalate",
        "The evidence does not confirm a root cause"
        + (f" after {round_number} rounds of investigation." if round_number > 1 else "."),
    )
