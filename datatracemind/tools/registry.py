"""The investigation tools as the agents see them.

One entry per tool: the function, the description shown to the model, and how
a call is filed as evidence. The tool server and the in-process callers both
read this table, so a tool cannot be reachable by an agent without also being
describable as evidence.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from datatracemind.tools import investigation as inv


@dataclass(frozen=True)
class ToolSpec:
    name: str
    fn: Callable[..., dict[str, Any]]
    description: str
    evidence_kind: str
    evidence_source: str
    describe: Callable[[dict], str]


_TEMPLATE_LIST = " ".join(f"[{name}] {text}" for name, (_, text) in inv.QUERY_TEMPLATES.items())

_SPECS = (
    ToolSpec(
        "get_source_schema",
        inv.get_source_schema,
        "Columns (name, type, nullable) and row count of one operational source table. "
        "table_name: customers, orders, payments, or refunds.",
        "schema",
        "postgresql:oltp",
        lambda a: f"Columns and types of source table {a.get('table_name')}",
    ),
    ToolSpec(
        "get_analytics_schema",
        inv.get_analytics_schema,
        "Columns (name, type, nullable) and row count of one analytics table. table_name: fact_orders, "
        "fact_payments, fact_refunds, daily_revenue, etl_run_log, data_quality_results, or etl_checkpoint.",
        "schema",
        "postgresql:analytics",
        lambda a: f"Columns and types of analytics table {a.get('table_name')}",
    ),
    ToolSpec(
        "profile_column",
        inv.profile_column,
        "Profile one column: rows, nulls, distinct values; the value distribution for low-cardinality text "
        "columns; min, max, and sum for numeric columns. table_name such as oltp.refunds or "
        "analytics.fact_refunds. Fails, listing the real columns, if the column does not exist.",
        "query_result",
        "postgresql",
        lambda a: f"Profile of column {a.get('table_name')}.{a.get('column_name')}",
    ),
    ToolSpec(
        "run_readonly_sql",
        inv.run_readonly_sql,
        "Run one predefined read-only query; free-form SQL is not accepted. Amounts are in the row's "
        f"currency. query_template_id is one of: {_TEMPLATE_LIST}",
        "query_result",
        "postgresql",
        lambda a: f"Diagnostic query {a.get('query_template_id')}",
    ),
    ToolSpec(
        "compare_source_target_metrics",
        inv.compare_source_target_metrics,
        "Compare a metric derived from the source by the approved contract with what analytics reports. "
        "metric_name: net_revenue or order_count. time_range: 'all' or 'last_N_days'. For net_revenue it "
        "returns gross, refunds, and net on both sides per currency, net_difference (analytics minus "
        "source), and a sample of mismatched days.",
        "metric_comparison",
        "postgresql:oltp+analytics",
        lambda a: f"Source versus analytics {a.get('metric_name')} ({a.get('time_range', 'all')})",
    ),
    ToolSpec(
        "find_missing_source_ids",
        inv.find_missing_source_ids,
        "Find source rows that have no analytics fact row, and fact rows that have no source row. Supported "
        "pairs: orders -> fact_orders, payments -> fact_payments, refunds -> fact_refunds. Returns counts "
        "and up to 20 sample IDs.",
        "query_result",
        "postgresql:oltp+analytics",
        lambda a: f"Source IDs of {a.get('source_table')} missing from {a.get('target_table')}",
    ),
    ToolSpec(
        "find_duplicate_business_keys",
        inv.find_duplicate_business_keys,
        "Find key values that occur more than once in an analytics table, for example table_name "
        "fact_payments with key_columns ['payment_id']. Returns the number of duplicated keys, extra rows, "
        "and a sample.",
        "query_result",
        "postgresql:analytics",
        lambda a: f"Duplicate {a.get('key_columns')} in {a.get('table_name')}",
    ),
    ToolSpec(
        "get_etl_code",
        inv.get_etl_code,
        "Read pipeline code. version_or_path: 'active' (the transformation file as now deployed, plus its "
        "deployment history), 'previous', 'history', a version number, or a source path: "
        "etl/transforms/order_revenue.py (how the transformation file is interpreted) or etl/pipeline.py "
        "(extract and load).",
        "code",
        "pipeline",
        lambda a: f"Pipeline code: {a.get('version_or_path', 'active')}",
    ),
    ToolSpec(
        "get_etl_run_logs",
        inv.get_etl_run_logs,
        "ETL run history. 'recent' or 'last_N' lists runs with status, row counts, error, transformation "
        "version, and failed quality checks. A run_id returns that run with its full check results.",
        "run_log",
        "pipeline",
        lambda a: f"ETL run log: {a.get('run_id_or_recent_window', 'recent')}",
    ),
    ToolSpec(
        "get_checkpoint",
        inv.get_checkpoint,
        "The pipeline checkpoint: watermark of the last successful load and the transformation version "
        "that produced the current facts. pipeline_name: order_revenue.",
        "run_log",
        "pipeline",
        lambda a: f"Checkpoint of pipeline {a.get('pipeline_name', 'order_revenue')}",
    ),
    ToolSpec(
        "get_metric_contract",
        inv.get_metric_contract,
        "The approved business definition of a metric. metric_name: net_revenue. Fails if no approved "
        "contract exists under that name.",
        "contract",
        "contracts",
        lambda a: f"Approved metric contract: {a.get('metric_name')}",
    ),
    ToolSpec(
        "get_data_contract",
        inv.get_data_contract,
        "The approved definition of a dataset. dataset_name: orders (what fact_orders must contain) or "
        "oltp_schema (the source columns the pipeline depends on).",
        "contract",
        "contracts",
        lambda a: f"Approved data contract: {a.get('dataset_name')}",
    ),
)

TOOLS: dict[str, ToolSpec] = {spec.name: spec for spec in _SPECS}
