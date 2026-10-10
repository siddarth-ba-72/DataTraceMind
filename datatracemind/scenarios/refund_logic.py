"""Refund faults in the revenue logic: DS-01 (refunds ignored) and DS-16 (over-subtracted)."""

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
    layer = "Transformation"
    etl_reports = "SUCCESS"
    injection = (
        "Deploys a new transformation version in which revenue.refund_statuses is [COMPLETED] instead of "
        "[SUCCEEDED]. The source never writes COMPLETED, so no refund qualifies for subtraction."
    )
    impact = (
        "Finance sees more revenue than the business earned. Nothing is red: the job is green and the "
        "numbers look plausible."
    )
    resolution_note = (
        "The agent should trace the gap to the refund status filter and propose restoring "
        "refund_statuses: [SUCCEEDED] for your approval."
    )

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
            "expected_outcome": "awaiting_approval",
            "expected_repair": {"revenue.refund_statuses": ["SUCCEEDED"]},
        }


class PartialRefundOverSubtracted(FaultScenario):
    scenario_id = "DS-16"
    name = "Partial refunds subtract the full order amount"
    description = (
        "A transformation change subtracts the whole order value for every refund, including partial ones. "
        "The ETL keeps reporting SUCCESS while net revenue is understated."
    )
    failure_type = "Silent semantic corruption"
    priority = "P1"
    layer = "Transformation"
    etl_reports = "SUCCESS"
    injection = (
        "Deploys a new transformation version in which revenue.refund_amount_basis is order_total instead of "
        "refund_amount. Full refunds are unaffected; every partial refund now removes the entire order."
    )
    impact = (
        "Revenue is understated, and only for orders with a partial refund, so most rows look right when "
        "someone spot-checks them."
    )
    resolution_note = (
        "The agent should show that subtracted amounts exceed refund amounts on partial refunds and propose "
        "restoring refund_amount_basis: refund_amount for your approval."
    )

    def inject(self, context: InjectionContext) -> None:
        context.deploy_transform(
            "refund_amount_basis: refund_amount",
            "refund_amount_basis: order_total",
            author="finance-data",
            message="Subtract the order value when an order is refunded",
        )

    def expected_symptoms(self) -> list[str]:
        return [
            "ETL run status remains SUCCESS",
            "analytics net revenue is lower than source-derived net revenue",
            "the difference equals order amount minus refund amount, summed over SUCCEEDED partial refunds",
            "gross revenue, refund row counts, and order counts still reconcile",
        ]

    def ground_truth(self) -> dict:
        return {
            "root_cause_category": "refund_logic",
            "implicated_field": "revenue.refund_amount_basis",
            "key_evidence_tools": [["get_etl_code"], ["compare_source_target_metrics", "run_readonly_sql"], ["get_metric_contract"]],
            "expected_failed_checks": [
                "net_revenue_reconciliation",
                "refund_subtraction_eligibility",
                "declared_status_consistent",
            ],
            "expected_outcome": "awaiting_approval",
            "expected_repair": {"revenue.refund_amount_basis": "refund_amount"},
        }
