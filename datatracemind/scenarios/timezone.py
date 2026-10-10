"""DS-07: timezone boundary mismatch (temporal corruption)."""

from __future__ import annotations

from datatracemind.scenarios.base import FaultScenario, InjectionContext


class TimezoneBoundaryMismatch(FaultScenario):
    scenario_id = "DS-07"
    name = "Timezone boundary mismatch"
    description = (
        "A transformation change assigns rows to reporting days in UTC instead of the business timezone. "
        "Totals still reconcile; revenue lands on the wrong day while the ETL reports SUCCESS."
    )
    failure_type = "Temporal corruption"
    priority = "P1"
    layer = "Transformation"
    etl_reports = "SUCCESS"
    injection = (
        "Deploys a new transformation version in which revenue.timezone is UTC instead of Asia/Kolkata. "
        "Anything that happened between midnight and 05:30 India time moves to the previous day."
    )
    impact = (
        "Every daily figure is slightly wrong and the grand total is exactly right, so month-end checks pass "
        "while day-over-day reports and daily targets are off."
    )
    resolution_note = (
        "The agent should notice that currency totals reconcile while individual days do not, compare the "
        "configured timezone with the contract, and propose restoring timezone: Asia/Kolkata."
    )

    def inject(self, context: InjectionContext) -> None:
        context.deploy_transform(
            "timezone: Asia/Kolkata",
            "timezone: UTC",
            author="analytics-eng",
            message="Standardise reporting timestamps on UTC",
        )

    def expected_symptoms(self) -> list[str]:
        return [
            "ETL run status remains SUCCESS",
            "net revenue per currency reconciles with the source",
            "net revenue per day does not: adjacent days are off by offsetting amounts",
            "row counts and business keys are complete and unique",
        ]

    def ground_truth(self) -> dict:
        return {
            "root_cause_category": "timezone",
            "implicated_field": "revenue.timezone",
            "key_evidence_tools": [["get_etl_code"], ["compare_source_target_metrics", "run_readonly_sql"], ["get_metric_contract"]],
            "expected_failed_checks": ["net_revenue_reconciliation", "declared_status_consistent"],
            "expected_outcome": "awaiting_approval",
            "expected_repair": {"revenue.timezone": "Asia/Kolkata"},
        }
