"""DS-03: join drops valid orders (silent record loss)."""

from __future__ import annotations

from datatracemind.scenarios.base import FaultScenario, InjectionContext


class JoinDropsOrders(FaultScenario):
    scenario_id = "DS-03"
    name = "Join drops valid orders"
    description = (
        "A transformation change joins orders to payments with an inner join. "
        "Orders that have no payment disappear from analytics while the ETL reports SUCCESS."
    )
    failure_type = "Silent record loss"
    priority = "P0"

    def inject(self, context: InjectionContext) -> None:
        context.deploy_transform(
            "payment_join: left",
            "payment_join: inner",
            author="analytics-eng",
            message="Simplify the fact_orders build to a single join on payments",
        )

    def expected_symptoms(self) -> list[str]:
        return [
            "ETL run status remains SUCCESS",
            "fact_orders has fewer distinct order_ids than oltp.orders",
            "every missing order has no payment row",
            "net revenue still reconciles",
        ]

    def ground_truth(self) -> dict:
        return {
            "root_cause_category": "join_loss",
            "implicated_field": "fact_orders.payment_join",
            # One tool from each group should be behind the cited evidence.
            "key_evidence_tools": [["get_etl_code"], ["find_missing_source_ids", "run_readonly_sql"], ["get_data_contract"]],
            "expected_failed_checks": [
                "fact_orders_completeness",
                "order_count_reconciliation",
                "declared_status_consistent",
            ],
            "expected_repair": {"fact_orders.payment_join": "left"},
        }
