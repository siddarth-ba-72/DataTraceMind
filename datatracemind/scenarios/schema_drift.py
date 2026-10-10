"""Source schema changes: DS-02 (a renamed column) and DS-09 (a column made nullable)."""

from __future__ import annotations

from datatracemind.scenarios.base import FaultScenario, InjectionContext

# Paid orders that lose their amount in DS-09.
NULLED_ORDERS = 8


class SchemaDrift(FaultScenario):
    scenario_id = "DS-02"
    name = "Schema/column drift"
    description = (
        "A source migration renames orders.total_amount to order_total. "
        "The ETL fails on the missing column and analytics goes stale."
    )
    failure_type = "Hard failure"
    priority = "P0"
    layer = "Source schema"
    etl_reports = "FAILED"
    injection = (
        "Runs a migration on the source: ALTER TABLE oltp.orders RENAME COLUMN total_amount TO order_total. "
        "The transformation still reads total_amount."
    )
    impact = (
        "Every run fails, so analytics stops refreshing. The numbers are not wrong, they are old, and they "
        "get older until someone notices."
    )
    resolution_note = (
        "The agent should match the run error to the renamed column and propose pointing "
        "source.orders.amount_column at order_total. It must not alter the source schema."
    )

    def inject(self, context: InjectionContext) -> None:
        context.conn.execute("ALTER TABLE oltp.orders RENAME COLUMN total_amount TO order_total")

    def expected_symptoms(self) -> list[str]:
        return [
            "ETL runs fail with an undefined-column error",
            "the source schema no longer matches its schema contract",
            "daily_revenue stops refreshing",
        ]

    def ground_truth(self) -> dict:
        return {
            "root_cause_category": "schema_drift",
            "implicated_field": "source.orders.amount_column",
            # One tool from each group should be behind the cited evidence.
            "key_evidence_tools": [["get_etl_code", "get_etl_run_logs"], ["get_source_schema", "profile_column"]],
            "expected_failed_checks": ["pipeline_run_succeeded", "source_schema_contract"],
            "expected_outcome": "awaiting_approval",
            "expected_repair": {"source.orders.amount_column": "order_total"},
        }


class NullAmounts(FaultScenario):
    scenario_id = "DS-09"
    name = "Order amounts become nullable"
    description = (
        "A source migration drops the NOT NULL constraint on orders.total_amount and a backfill leaves some "
        "paid orders without an amount. The pipeline rejects those orders and their payments while the ETL "
        "reports SUCCESS."
    )
    failure_type = "Silent row rejection"
    priority = "P1"
    layer = "Source data"
    etl_reports = "SUCCESS"
    injection = (
        "Runs a migration on the source that makes oltp.orders.total_amount nullable, then sets the amount of "
        f"the first {NULLED_ORDERS} paid orders to NULL. The transformation is not changed."
    )
    impact = (
        "Valid captured payments vanish from analytics because their parent order is rejected. Revenue is "
        "understated and the only trace is a rejected-row count in the run log."
    )
    resolution = "escalation"
    resolution_note = (
        "No transformation change is correct here: the pipeline is right to reject an order with no amount, "
        "and no contract defines a default. The workflow should identify the defect in the source and "
        "escalate to a human instead of inventing a value."
    )

    def inject(self, context: InjectionContext) -> None:
        context.conn.execute("ALTER TABLE oltp.orders ALTER COLUMN total_amount DROP NOT NULL")
        context.conn.execute(
            """
            UPDATE oltp.orders SET total_amount = NULL, updated_at = now()
            WHERE order_id IN (
                SELECT order_id FROM oltp.orders WHERE order_status = 'PAID' ORDER BY order_id LIMIT %s
            )
            """,
            (NULLED_ORDERS,),
        )

    def expected_symptoms(self) -> list[str]:
        return [
            "ETL run status remains SUCCESS, with a non-zero rejected-row count",
            "orders with a NULL amount, and their payments, are missing from the fact tables",
            "analytics net revenue is lower than source-derived net revenue by those orders' captured payments",
            "the transformation file has not changed",
        ]

    def ground_truth(self) -> dict:
        return {
            "root_cause_category": "source_data_defect",
            "implicated_field": "orders.total_amount",
            "key_evidence_tools": [
                ["get_etl_run_logs", "find_missing_source_ids"],
                ["profile_column", "get_source_schema", "run_readonly_sql"],
            ],
            "expected_failed_checks": [
                "net_revenue_reconciliation",
                "fact_orders_completeness",
                "fact_payments_completeness",
                "order_count_reconciliation",
                "declared_status_consistent",
            ],
            "expected_outcome": "escalated",
            "expected_repair": {},
        }
