"""DS-01: refund state omitted from revenue logic (silent corruption)."""

from __future__ import annotations

from datatracemind.scenarios.base import FaultScenario, InjectionContext


class RefundLogicOmitted(FaultScenario):
    scenario_id = "DS-01"
    name = "Refund state omitted from revenue logic"
    description = (
        "A transformation change makes the pipeline stop subtracting completed refunds. "
        "The ETL keeps reporting SUCCESS while net revenue is overstated."
    )
    failure_type = "Silent semantic corruption"
    priority = "P0"

    def inject(self, context: InjectionContext) -> None:
        # The source records a completed refund as SUCCEEDED. The deployed
        # filter names a status the source never emits, so no refund qualifies.
        context.deploy_transform(
            "refund_statuses: [SUCCEEDED]",
            "refund_statuses: [COMPLETED]",
            author="payments-platform",
            message="Use the refund status names from the payments service v2 API",
        )

    def expected_symptoms(self) -> list[str]:
        return [
            "ETL run status remains SUCCESS",
            "analytics net revenue exceeds source-derived net revenue",
            "the difference equals the total of SUCCEEDED refunds in the source",
            "gross revenue and order counts still reconcile",
        ]

    def ground_truth(self) -> dict:
        return {
            "root_cause_category": "refund_logic",
            "implicated_field": "revenue.refund_statuses",
            # One tool from each group should be behind the cited evidence.
            "key_evidence_tools": [["get_etl_code"], ["compare_source_target_metrics", "run_readonly_sql"], ["get_metric_contract"]],
            "expected_failed_checks": [
                "net_revenue_reconciliation",
                "refund_subtraction_eligibility",
                "declared_status_consistent",
            ],
            "expected_repair": {"revenue.refund_statuses": ["SUCCEEDED"]},
        }
