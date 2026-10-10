"""DS-04: duplicate ingestion on retry (silent duplication)."""

from __future__ import annotations

from datatracemind.config import PIPELINE_NAME
from datatracemind.scenarios.base import FaultScenario, InjectionContext

# Size of the batch the retry reads a second time, in orders.
REPLAYED_ORDERS = 40


class DuplicateIngestionOnRetry(FaultScenario):
    scenario_id = "DS-04"
    name = "Duplicate ingestion on retry"
    description = (
        "A transformation change makes the load append-only. The deploy itself runs clean; the next retry "
        "reads a batch that was already loaded and inserts it a second time while the ETL reports SUCCESS."
    )
    failure_type = "Silent duplication"
    priority = "P1"
    layer = "Load path"
    etl_reports = "SUCCESS"
    injection = (
        "Two steps. First it deploys a new transformation version in which load.mode is append instead of "
        "replace_by_key; the run that follows rebuilds every fact and passes every check. Then it puts the "
        f"pipeline checkpoint back to before the newest {REPLAYED_ORDERS} orders, as if the run that loaded "
        "them had died before recording it. The retry reads that batch again and appends it."
    )
    impact = (
        "Orders, payments, and refunds in the replayed batch are counted twice, so revenue and order counts "
        "are inflated. The deploy looked healthy, which is what makes this one hard to trace back."
    )
    resolution_note = (
        "The agent should find the duplicated business keys, tie them to the append-only load, and propose "
        "restoring load.mode: replace_by_key. Staging replays the load to prove the fix is idempotent."
    )
    second_run_trigger = "retry"

    def inject(self, context: InjectionContext) -> None:
        context.deploy_transform(
            "mode: replace_by_key",
            "mode: append",
            author="data-platform",
            message="Load facts with plain inserts to shorten the load step",
        )

    def after_first_run(self, context: InjectionContext) -> bool:
        cutoff = context.conn.execute(
            """
            SELECT min(updated_at) AS at
            FROM (SELECT updated_at FROM oltp.orders ORDER BY updated_at DESC, order_id DESC LIMIT %s) AS newest
            """,
            (REPLAYED_ORDERS,),
        ).fetchone()["at"]
        context.conn.execute(
            "UPDATE analytics.etl_checkpoint SET watermark = %s, updated_at = now() WHERE pipeline_name = %s",
            (cutoff, PIPELINE_NAME),
        )
        return True

    def expected_symptoms(self) -> list[str]:
        return [
            "the run after the deploy is SUCCESS with every check passing",
            "the retry is SUCCESS too, and loads rows although nothing changed in the source",
            "order_id, payment_id, and refund_id values appear twice in the fact tables",
            "analytics net revenue and order counts exceed the source-derived values",
        ]

    def ground_truth(self) -> dict:
        return {
            "root_cause_category": "duplicate_load",
            "implicated_field": "load.mode",
            "key_evidence_tools": [
                ["get_etl_code"],
                ["find_duplicate_business_keys", "run_readonly_sql"],
                ["get_etl_run_logs", "get_checkpoint"],
            ],
            "expected_failed_checks": [
                "unique_business_keys",
                "net_revenue_reconciliation",
                "order_count_reconciliation",
                "declared_status_consistent",
            ],
            "expected_outcome": "awaiting_approval",
            "expected_repair": {"load.mode": "replace_by_key"},
        }
