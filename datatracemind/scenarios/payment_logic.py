"""DS-17: failed payments counted as revenue (silent corruption)."""

from __future__ import annotations

from datatracemind.scenarios.base import FaultScenario, InjectionContext


class FailedPaymentsCounted(FaultScenario):
    scenario_id = "DS-17"
    name = "Failed payments counted as revenue"
    description = (
        "A transformation change counts every payment attempt as gross revenue, including failed ones. "
        "The ETL keeps reporting SUCCESS while revenue is overstated."
    )
    failure_type = "Silent semantic corruption"
    priority = "P1"
    layer = "Transformation"
    etl_reports = "SUCCESS"
    injection = (
        "Deploys a new transformation version in which revenue.payment_statuses is [CAPTURED, FAILED] "
        "instead of [CAPTURED]. A declined card, and the failed first attempt of a retried payment, now "
        "both count as money received."
    )
    impact = (
        "Gross and net revenue include money that was never collected. Orders paid on a second attempt are "
        "counted twice."
    )
    resolution_note = (
        "The agent should show FAILED payments flagged as revenue, check the contract's included statuses, "
        "and propose restoring payment_statuses: [CAPTURED] for your approval."
    )

    def inject(self, context: InjectionContext) -> None:
        context.deploy_transform(
            "payment_statuses: [CAPTURED]",
            "payment_statuses: [CAPTURED, FAILED]",
            author="growth-analytics",
            message="Count all payment attempts in gross revenue",
        )

    def expected_symptoms(self) -> list[str]:
        return [
            "ETL run status remains SUCCESS",
            "analytics gross and net revenue exceed the source-derived values",
            "the difference equals the total of FAILED payments in the source",
            "refunds and order counts still reconcile",
        ]

    def ground_truth(self) -> dict:
        return {
            "root_cause_category": "payment_logic",
            "implicated_field": "revenue.payment_statuses",
            "key_evidence_tools": [["get_etl_code"], ["compare_source_target_metrics", "run_readonly_sql"], ["get_metric_contract"]],
            "expected_failed_checks": [
                "net_revenue_reconciliation",
                "payment_revenue_eligibility",
                "declared_status_consistent",
            ],
            "expected_outcome": "awaiting_approval",
            "expected_repair": {"revenue.payment_statuses": ["CAPTURED"]},
        }
