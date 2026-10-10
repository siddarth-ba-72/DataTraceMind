"""DS-02: schema drift in the source (hard failure)."""

from __future__ import annotations

from datatracemind.scenarios.base import FaultScenario, InjectionContext


class SchemaDrift(FaultScenario):
    scenario_id = "DS-02"
    name = "Schema/column drift"
    description = (
        "A source migration renames orders.total_amount to order_total. "
        "The ETL fails on the missing column and analytics goes stale."
    )
    failure_type = "Hard failure"
    priority = "P0"

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
            "expected_failed_checks": ["pipeline_run_succeeded", "source_schema_contract"],
            "expected_repair": {"source.orders.amount_column": "order_total"},
        }
