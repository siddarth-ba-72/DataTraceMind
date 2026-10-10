"""DS-06: the source starts writing a status the pipeline does not know."""

from __future__ import annotations

from datatracemind.scenarios.base import FaultScenario, InjectionContext

NEW_STATUS = "CHARGEBACK"
CHARGEBACKS = 6


class NewStatusValue(FaultScenario):
    scenario_id = "DS-06"
    name = "New status value in the source"
    description = (
        f"The payments service starts recording chargebacks as refund rows with the status {NEW_STATUS}. "
        "The pipeline rejects rows with a status it does not know while the ETL reports SUCCESS."
    )
    failure_type = "Semantic incompatibility"
    priority = "P1"
    layer = "Source data"
    etl_reports = "SUCCESS"
    injection = (
        f"Inserts {CHARGEBACKS} refund rows with refund_status = {NEW_STATUS} on the oldest paid orders that "
        "have no refund. No schema changes and no transformation change: only a value nobody agreed on."
    )
    impact = (
        "Money has left the business and analytics has no record of it. Whether a chargeback should reduce "
        "net revenue is undefined, so there is no right number to report yet."
    )
    resolution = "escalation"
    resolution_note = (
        "The revenue contract lists PENDING, SUCCEEDED, and FAILED and says nothing about chargebacks. The "
        "workflow should find the unknown status, state that the business rule is missing, and escalate to a "
        "human instead of guessing how to count it."
    )

    def inject(self, context: InjectionContext) -> None:
        context.conn.execute(
            """
            INSERT INTO oltp.refunds (payment_id, order_id, refund_status, refund_amount)
            SELECT p.payment_id, o.order_id, %s, p.amount
            FROM oltp.orders o
            JOIN oltp.payments p ON p.order_id = o.order_id AND p.payment_status = 'CAPTURED'
            WHERE o.order_status = 'PAID'
              AND NOT EXISTS (SELECT 1 FROM oltp.refunds r WHERE r.order_id = o.order_id)
            ORDER BY o.order_id
            LIMIT %s
            """,
            (NEW_STATUS, CHARGEBACKS),
        )

    def expected_symptoms(self) -> list[str]:
        return [
            "ETL run status remains SUCCESS, with a non-zero rejected-row count",
            f"the {NEW_STATUS} refund rows exist in the source and are missing from fact_refunds",
            "net revenue still reconciles, because the contract does not subtract that status either",
            "the transformation file has not changed",
        ]

    def ground_truth(self) -> dict:
        return {
            "root_cause_category": "schema_drift",
            # A new value is a change at the source; calling it a defect is also defensible.
            "also_accepted_categories": ["source_data_defect"],
            "implicated_field": "refunds.refund_status",
            "key_evidence_tools": [
                ["run_readonly_sql", "profile_column", "find_missing_source_ids"],
                ["get_etl_run_logs", "get_etl_code"],
                ["get_metric_contract"],
            ],
            "expected_failed_checks": ["fact_refunds_completeness", "declared_status_consistent"],
            "expected_outcome": "escalated",
            "expected_repair": {},
        }
