"""Read-only investigation tools.

Every function here connects with the read-only database role and returns a
small JSON-friendly dict. None accepts SQL: table and column names are
checked against fixed lists or the catalog, and queries are templates with
typed parameters. This module must not import the scenario lab or anything
else that knows which fault was injected.

Source-side figures are computed "as of" the pipeline's last successful load
(the checkpoint watermark), so they are comparable with the analytics side
even while new transactions keep arriving.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from psycopg import sql

from datatracemind.config import PIPELINE_NAME, REPO_ROOT, TRANSFORM_FILE
from datatracemind.db import ANALYTICS_SCHEMA, SOURCE_SCHEMA, connect, to_jsonable
from etl import run_log
from etl.contract import ContractNotFound, load_revenue_contract, read_contract_text
from etl.metrics import reconcile, source_revenue, target_revenue

SOURCE_TABLES = ("customers", "orders", "payments", "refunds")
ANALYTICS_TABLES = (
    "fact_orders",
    "fact_payments",
    "fact_refunds",
    "daily_revenue",
    "etl_run_log",
    "data_quality_results",
    "etl_checkpoint",
)
# Pipeline source files an investigator may read, besides the transformation file.
READABLE_CODE = ("etl/transforms/order_revenue.py", "etl/pipeline.py")
SAMPLE_LIMIT = 20
MAX_DISTINCT_VALUES = 25


class ToolError(ValueError):
    """The tool was called with arguments it does not accept."""


def _resolve_table(table_name: str, schemas: tuple[str, ...]) -> tuple[str, str]:
    """Split 'schema.table' (or a bare table name) and check it is allowed."""
    name = table_name.strip().lower()
    schema, _, table = name.rpartition(".")
    allowed = {SOURCE_SCHEMA: SOURCE_TABLES, ANALYTICS_SCHEMA: ANALYTICS_TABLES}
    candidates = [schema] if schema else [s for s in schemas if table in allowed[s]]
    for candidate in candidates:
        if candidate in schemas and table in allowed[candidate]:
            return candidate, table
    options = ", ".join(f"{s}.{t}" for s in schemas for t in allowed[s])
    raise ToolError(f"unknown table {table_name!r}; available tables: {options}")


def _columns(conn: psycopg.Connection, schema: str, table: str) -> list[dict]:
    return conn.execute(
        "SELECT column_name AS name, data_type AS type, is_nullable = 'YES' AS nullable "
        "FROM information_schema.columns WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
        (schema, table),
    ).fetchall()


def _watermark(conn: psycopg.Connection) -> datetime | None:
    checkpoint = run_log.get_checkpoint(conn, ANALYTICS_SCHEMA)
    return checkpoint["watermark"] if checkpoint else None


def _describe_table(schema: str, table: str) -> dict:
    with connect(readonly=True) as conn:
        columns = _columns(conn, schema, table)
        rows = conn.execute(
            sql.SQL("SELECT count(*) AS n FROM {}.{}").format(sql.Identifier(schema), sql.Identifier(table))
        ).fetchone()["n"]
    return {"table": f"{schema}.{table}", "row_count": rows, "columns": columns}


def get_source_schema(table_name: str) -> dict[str, Any]:
    schema, table = _resolve_table(table_name, (SOURCE_SCHEMA,))
    return _describe_table(schema, table)


def get_analytics_schema(table_name: str) -> dict[str, Any]:
    schema, table = _resolve_table(table_name, (ANALYTICS_SCHEMA,))
    return _describe_table(schema, table)


def profile_column(table_name: str, column_name: str) -> dict[str, Any]:
    schema, table = _resolve_table(table_name, (SOURCE_SCHEMA, ANALYTICS_SCHEMA))
    with connect(readonly=True) as conn:
        columns = {c["name"]: c["type"] for c in _columns(conn, schema, table)}
        column = column_name.strip().lower()
        if column not in columns:
            raise ToolError(
                f"column {schema}.{table}.{column} does not exist; columns are: {', '.join(columns)}"
            )
        target = sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(table))
        col = sql.Identifier(column)
        profile = conn.execute(
            sql.SQL(
                "SELECT count(*) AS rows, count(*) FILTER (WHERE {c} IS NULL) AS nulls, "
                "count(DISTINCT {c}) AS distinct_values FROM {t}"
            ).format(c=col, t=target)
        ).fetchone()
        result: dict[str, Any] = {"table": f"{schema}.{table}", "column": column, "type": columns[column], **profile}
        kind = columns[column]
        if kind in ("numeric", "integer", "bigint"):
            result["range"] = conn.execute(
                sql.SQL("SELECT min({c}) AS min, max({c}) AS max, sum({c}) AS sum FROM {t}").format(c=col, t=target)
            ).fetchone()
        elif kind.startswith("timestamp") or kind == "date":
            result["range"] = conn.execute(
                sql.SQL("SELECT min({c}) AS min, max({c}) AS max FROM {t}").format(c=col, t=target)
            ).fetchone()
        if profile["distinct_values"] <= MAX_DISTINCT_VALUES and kind in ("text", "character", "boolean"):
            result["values"] = conn.execute(
                sql.SQL("SELECT {c}::text AS value, count(*) AS rows FROM {t} GROUP BY 1 ORDER BY 2 DESC, 1").format(
                    c=col, t=target
                )
            ).fetchall()
    return to_jsonable(result)


# --- Query templates ---------------------------------------------------------


def _int_list(parameters: dict, name: str, *, limit: int) -> list[int]:
    value = parameters.get(name)
    if not isinstance(value, list) or not value or not all(isinstance(v, int) for v in value):
        raise ToolError(f"parameter {name!r} must be a non-empty list of integers")
    if len(value) > limit:
        raise ToolError(f"parameter {name!r} accepts at most {limit} values")
    return value


def _int_param(parameters: dict, name: str, *, default: int, low: int, high: int) -> int:
    value = parameters.get(name, default)
    if not isinstance(value, int) or not low <= value <= high:
        raise ToolError(f"parameter {name!r} must be an integer between {low} and {high}")
    return value


def _status_param(parameters: dict, name: str) -> str:
    value = parameters.get(name)
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,39}", value):
        raise ToolError(f"parameter {name!r} must be a status name such as SUCCEEDED")
    return value


def _t_source_refunds_by_status(conn, parameters, as_of):
    return conn.execute(
        """
        SELECT o.currency, r.refund_status, count(*) AS refunds, sum(r.refund_amount) AS total_refund_amount
        FROM oltp.refunds r JOIN oltp.orders o ON o.order_id = r.order_id
        WHERE %(as_of)s::timestamptz IS NULL OR r.updated_at <= %(as_of)s
        GROUP BY 1, 2 ORDER BY 1, 2
        """,
        {"as_of": as_of},
    ).fetchall()


def _t_source_payments_by_status(conn, parameters, as_of):
    return conn.execute(
        """
        SELECT o.currency, p.payment_status, count(*) AS payments, sum(p.amount) AS total_amount
        FROM oltp.payments p JOIN oltp.orders o ON o.order_id = p.order_id
        WHERE %(as_of)s::timestamptz IS NULL OR p.updated_at <= %(as_of)s
        GROUP BY 1, 2 ORDER BY 1, 2
        """,
        {"as_of": as_of},
    ).fetchall()


def _t_source_orders_by_status(conn, parameters, as_of):
    return conn.execute(
        """
        SELECT o.order_status, count(*) AS orders,
               count(*) FILTER (WHERE NOT EXISTS (SELECT 1 FROM oltp.payments p WHERE p.order_id = o.order_id))
                   AS orders_without_any_payment
        FROM oltp.orders o
        WHERE %(as_of)s::timestamptz IS NULL OR o.created_at <= %(as_of)s
        GROUP BY 1 ORDER BY 1
        """,
        {"as_of": as_of},
    ).fetchall()


def _t_analytics_refunds_by_status(conn, parameters, as_of):
    return conn.execute(
        """
        SELECT currency, refund_status, is_subtracted, count(*) AS refunds,
               sum(refund_amount) AS total_refund_amount, sum(subtracted_amount) AS total_subtracted_amount
        FROM analytics.fact_refunds GROUP BY 1, 2, 3 ORDER BY 1, 2, 3
        """
    ).fetchall()


def _t_analytics_payments_by_status(conn, parameters, as_of):
    return conn.execute(
        """
        SELECT currency, payment_status, is_revenue, count(*) AS payments, sum(amount) AS total_amount
        FROM analytics.fact_payments GROUP BY 1, 2, 3 ORDER BY 1, 2, 3
        """
    ).fetchall()


def _t_analytics_orders_by_status(conn, parameters, as_of):
    return conn.execute(
        """
        SELECT order_status, count(*) AS orders, count(*) FILTER (WHERE payment_count = 0) AS orders_without_any_payment
        FROM analytics.fact_orders GROUP BY 1 ORDER BY 1
        """
    ).fetchall()


def _t_source_orders_by_ids(conn, parameters, as_of):
    return conn.execute(
        """
        SELECT o.order_id, o.order_status, o.currency, o.created_at,
               (SELECT count(*) FROM oltp.payments p WHERE p.order_id = o.order_id) AS payments,
               (SELECT count(*) FROM oltp.payments p WHERE p.order_id = o.order_id
                    AND p.payment_status = 'CAPTURED') AS captured_payments,
               (SELECT count(*) FROM oltp.refunds r WHERE r.order_id = o.order_id) AS refunds,
               EXISTS (SELECT 1 FROM analytics.fact_orders f WHERE f.order_id = o.order_id) AS in_fact_orders
        FROM oltp.orders o WHERE o.order_id = ANY(%s) ORDER BY o.order_id
        """,
        (_int_list(parameters, "order_ids", limit=25),),
    ).fetchall()


def _t_refund_samples(conn, parameters, as_of):
    return conn.execute(
        """
        SELECT r.refund_id, r.order_id, r.refund_status, r.refund_amount, o.currency, r.created_at,
               f.is_subtracted AS analytics_is_subtracted, f.subtracted_amount AS analytics_subtracted_amount
        FROM oltp.refunds r
        JOIN oltp.orders o ON o.order_id = r.order_id
        LEFT JOIN analytics.fact_refunds f ON f.refund_id = r.refund_id
        WHERE r.refund_status = %(status)s AND (%(as_of)s::timestamptz IS NULL OR r.updated_at <= %(as_of)s)
        ORDER BY r.refund_id LIMIT %(limit)s
        """,
        {
            "status": _status_param(parameters, "refund_status"),
            "limit": _int_param(parameters, "limit", default=10, low=1, high=SAMPLE_LIMIT),
            "as_of": as_of,
        },
    ).fetchall()


def _t_daily_revenue_recent(conn, parameters, as_of):
    return conn.execute(
        """
        SELECT revenue_date, currency, gross_paid_amount, refund_amount, net_revenue, order_count,
               refreshed_at, transform_version
        FROM analytics.daily_revenue
        WHERE revenue_date > (SELECT max(revenue_date) FROM analytics.daily_revenue) - %s
        ORDER BY revenue_date DESC, currency
        """,
        (_int_param(parameters, "days", default=7, low=1, high=14),),
    ).fetchall()


QUERY_TEMPLATES = {
    "source_refunds_by_status": (
        _t_source_refunds_by_status,
        "Source refunds grouped by currency and refund_status: count and total refund_amount. No parameters.",
    ),
    "source_payments_by_status": (
        _t_source_payments_by_status,
        "Source payments grouped by currency and payment_status: count and total amount. No parameters.",
    ),
    "source_orders_by_status": (
        _t_source_orders_by_status,
        "Source orders grouped by order_status: count, and how many have no payment row. No parameters.",
    ),
    "analytics_refunds_by_status": (
        _t_analytics_refunds_by_status,
        "fact_refunds grouped by currency, refund_status, is_subtracted: count, refund_amount, subtracted_amount. "
        "No parameters.",
    ),
    "analytics_payments_by_status": (
        _t_analytics_payments_by_status,
        "fact_payments grouped by currency, payment_status, is_revenue: count and total amount. No parameters.",
    ),
    "analytics_orders_by_status": (
        _t_analytics_orders_by_status,
        "fact_orders grouped by order_status: count, and how many have no payment. No parameters.",
    ),
    "source_orders_by_ids": (
        _t_source_orders_by_ids,
        "State of specific source orders and whether each is in fact_orders. "
        "Parameters: order_ids (list of up to 25 integers).",
    ),
    "refund_samples_source_vs_analytics": (
        _t_refund_samples,
        "Sample source refunds of one status with how analytics treated each. "
        "Parameters: refund_status (for example SUCCEEDED), limit (1-20, default 10).",
    ),
    "daily_revenue_recent": (
        _t_daily_revenue_recent,
        "Latest rows of analytics.daily_revenue. Parameters: days (1-14, default 7).",
    ),
}


def run_readonly_sql(query_template_id: str, parameters: dict[str, Any] | None = None) -> dict[str, Any]:
    if query_template_id not in QUERY_TEMPLATES:
        raise ToolError(
            f"unknown query_template_id {query_template_id!r}; available: {', '.join(QUERY_TEMPLATES)}"
        )
    template, _ = QUERY_TEMPLATES[query_template_id]
    with connect(readonly=True) as conn:
        as_of = _watermark(conn)
        rows = template(conn, parameters or {}, as_of)
    return to_jsonable(
        {"query_template_id": query_template_id, "source_rows_as_of": as_of, "row_count": len(rows), "rows": rows}
    )


# --- Comparisons -------------------------------------------------------------


def _date_window(time_range: str, timezone: str) -> date | None:
    """First business day included by `time_range`, or None for all days."""
    value = time_range.strip().lower()
    if value in ("", "all"):
        return None
    match = re.fullmatch(r"last_(\d{1,2})_days", value)
    if not match or not 1 <= int(match.group(1)) <= 30:
        raise ToolError("time_range must be 'all' or 'last_N_days' with N between 1 and 30")
    return datetime.now(ZoneInfo(timezone)).date() - timedelta(days=int(match.group(1)) - 1)


def compare_source_target_metrics(metric_name: str, time_range: str = "all") -> dict[str, Any]:
    if metric_name not in ("net_revenue", "order_count"):
        raise ToolError("metric_name must be 'net_revenue' or 'order_count'")
    contract = load_revenue_contract()
    first_day = _date_window(time_range, contract.timezone)
    with connect(readonly=True) as conn:
        as_of = _watermark(conn)
        source = source_revenue(conn, contract, as_of=as_of)
        target = target_revenue(conn, ANALYTICS_SCHEMA)
    if first_day is not None:
        for metrics in (source, target):
            kept = {key: cell for key, cell in metrics.daily.items() if key[0] >= first_day}
            metrics.daily, metrics.by_currency = {}, {}
            for (day, currency), cell in kept.items():
                for measure in ("gross", "refunds", "order_count"):
                    metrics.add(day, currency, measure, cell[measure])

    result: dict[str, Any] = {
        "metric": metric_name,
        "time_range": time_range,
        "source_rows_as_of": as_of,
        "definition": f"contract net_revenue v{contract.version}",
    }
    if metric_name == "net_revenue":
        report = reconcile(source, target, contract.tolerance)
        result.update(report)
        days = []
        for day, currency in sorted(set(source.daily) | set(target.daily), reverse=True):
            src = source.daily.get((day, currency))
            tgt = target.daily.get((day, currency))
            src_net = src["net"] if src else 0
            tgt_net = tgt["net"] if tgt else 0
            if abs(tgt_net - src_net) > contract.tolerance:
                days.append(
                    {
                        "date": day,
                        "currency": currency,
                        "source_net": src_net,
                        "analytics_net": tgt_net,
                        "source_refunds": src["refunds"] if src else 0,
                        "analytics_refunds": tgt["refunds"] if tgt else 0,
                    }
                )
        result["mismatched_days_sample"] = days[:5]
    else:
        currencies = sorted(set(source.by_currency) | set(target.by_currency))
        result["by_currency"] = {
            c: {
                "source_orders": int(source.by_currency.get(c, {}).get("order_count", 0)),
                "analytics_order_count": int(target.by_currency.get(c, {}).get("order_count", 0)),
            }
            for c in currencies
        }
        result["reconciled"] = all(
            v["source_orders"] == v["analytics_order_count"] for v in result["by_currency"].values()
        )
    return to_jsonable(result)


_ID_PAIRS = {
    ("orders", "fact_orders"): ("order_id", "created_at"),
    ("payments", "fact_payments"): ("payment_id", "updated_at"),
    ("refunds", "fact_refunds"): ("refund_id", "created_at"),
}


def find_missing_source_ids(source_table: str, target_table: str) -> dict[str, Any]:
    _, source = _resolve_table(source_table, (SOURCE_SCHEMA,))
    _, target = _resolve_table(target_table, (ANALYTICS_SCHEMA,))
    if (source, target) not in _ID_PAIRS:
        pairs = ", ".join(f"{s} -> {t}" for s, t in _ID_PAIRS)
        raise ToolError(f"cannot compare {source} with {target}; supported pairs: {pairs}")
    key, time_column = _ID_PAIRS[(source, target)]
    with connect(readonly=True) as conn:
        as_of = _watermark(conn)
        source_ids = {
            row["id"]
            for row in conn.execute(
                sql.SQL(
                    "SELECT {k} AS id FROM oltp.{t} WHERE %(as_of)s::timestamptz IS NULL OR {c} <= %(as_of)s"
                ).format(k=sql.Identifier(key), t=sql.Identifier(source), c=sql.Identifier(time_column)),
                {"as_of": as_of},
            )
        }
        target_ids = {
            row["id"]
            for row in conn.execute(
                sql.SQL("SELECT DISTINCT {k} AS id FROM analytics.{t}").format(
                    k=sql.Identifier(key), t=sql.Identifier(target)
                )
            )
        }
    missing = sorted(source_ids - target_ids)
    extra = sorted(target_ids - source_ids)
    return to_jsonable(
        {
            "source_table": f"oltp.{source}",
            "target_table": f"analytics.{target}",
            "key": key,
            "source_rows_as_of": as_of,
            "source_ids": len(source_ids),
            "analytics_ids": len(target_ids),
            "missing_in_analytics": len(missing),
            "missing_sample": missing[:SAMPLE_LIMIT],
            "in_analytics_but_not_in_source": len(extra),
            "extra_sample": extra[:SAMPLE_LIMIT],
        }
    )


def find_duplicate_business_keys(table_name: str, key_columns: list[str]) -> dict[str, Any]:
    schema, table = _resolve_table(table_name, (ANALYTICS_SCHEMA,))
    if not key_columns or len(key_columns) > 4:
        raise ToolError("key_columns must name between one and four columns")
    with connect(readonly=True) as conn:
        known = {c["name"] for c in _columns(conn, schema, table)}
        keys = [k.strip().lower() for k in key_columns]
        unknown = [k for k in keys if k not in known]
        if unknown:
            raise ToolError(f"unknown column(s) {unknown} in {schema}.{table}; columns are: {', '.join(sorted(known))}")
        key_sql = sql.SQL(", ").join(map(sql.Identifier, keys))
        rows = conn.execute(
            sql.SQL(
                "SELECT {k}, count(*) AS copies FROM {s}.{t} GROUP BY {k} HAVING count(*) > 1 ORDER BY {k} LIMIT 500"
            ).format(k=key_sql, s=sql.Identifier(schema), t=sql.Identifier(table))
        ).fetchall()
    return to_jsonable(
        {
            "table": f"{schema}.{table}",
            "key_columns": keys,
            "duplicated_keys": len(rows),
            "extra_rows": sum(r["copies"] - 1 for r in rows),
            "sample": rows[:SAMPLE_LIMIT],
        }
    )


# --- Pipeline ----------------------------------------------------------------


def _version_meta(row: dict) -> dict:
    return {
        "version": row["version"],
        "is_active": row["is_active"],
        "author": row["author"],
        "message": row["message"],
        "deployed_at": row["created_at"],
    }


def get_etl_code(version_or_path: str = "active") -> dict[str, Any]:
    selector = version_or_path.strip()
    if selector in READABLE_CODE:
        return {"path": selector, "kind": "pipeline source code", "content": (REPO_ROOT / selector).read_text()}
    with connect(readonly=True) as conn:
        versions = conn.execute(
            "SELECT version, content, content_hash, author, message, is_active, created_at "
            "FROM pipeline.transform_versions WHERE target_file = %s ORDER BY version",
            (TRANSFORM_FILE,),
        ).fetchall()
    history = [_version_meta(v) for v in versions]
    lowered = selector.lower()
    if lowered in ("history", "versions"):
        return to_jsonable({"path": TRANSFORM_FILE, "history": history})
    chosen = None
    if lowered in ("", "active", "current", TRANSFORM_FILE):
        chosen = next((v for v in versions if v["is_active"]), None)
    elif lowered == "previous":
        active = next((v for v in versions if v["is_active"]), None)
        earlier = [v for v in versions if active and v["version"] < active["version"]]
        chosen = earlier[-1] if earlier else None
    elif re.fullmatch(r"v?\d+", lowered):
        chosen = next((v for v in versions if v["version"] == int(lowered.lstrip("v"))), None)
    if chosen is None:
        raise ToolError(
            f"nothing matches {version_or_path!r}; use 'active', 'previous', 'history', a version number "
            f"({', '.join(str(v['version']) for v in versions)}), or one of: {', '.join(READABLE_CODE)}"
        )
    return to_jsonable(
        {
            "path": TRANSFORM_FILE,
            "kind": "transformation file (interpreted by etl/transforms/order_revenue.py)",
            **_version_meta(chosen),
            "content_sha256": chosen["content_hash"],
            "content": chosen["content"],
            "history": history,
        }
    )


def get_etl_run_logs(run_id_or_recent_window: str = "recent") -> dict[str, Any]:
    selector = run_id_or_recent_window.strip().lower()
    with connect(readonly=True) as conn:
        if re.fullmatch(r"\d+", selector):
            runs = [r for r in run_log.recent_runs(conn, ANALYTICS_SCHEMA, limit=500) if r["run_id"] == int(selector)]
            if not runs:
                raise ToolError(f"no ETL run with run_id {selector}")
            return to_jsonable({"run": runs[0]})
        match = re.fullmatch(r"(?:recent|last_(\d{1,2}))", selector)
        if not match:
            raise ToolError("use 'recent', 'last_N' (N up to 50), or a run_id")
        limit = min(int(match.group(1) or 10), 50)
        runs = run_log.recent_runs(conn, ANALYTICS_SCHEMA, limit=limit)
        last_success = conn.execute(
            "SELECT max(finished_at) AS at FROM analytics.etl_run_log WHERE status = 'SUCCESS'"
        ).fetchone()["at"]
    return to_jsonable(
        {
            "last_successful_run_finished_at": last_success,
            "runs": [
                {
                    "run_id": r["run_id"],
                    "trigger": r["trigger"],
                    "status": r["status"],
                    "started_at": r["started_at"],
                    "transform_version": r["transform_version"],
                    "full_refresh": r["full_refresh"],
                    "extracted": r["extracted_count"],
                    "transformed": r["transformed_count"],
                    "rejected": r["rejected_count"],
                    "loaded": r["loaded_count"],
                    "error_summary": r["error_summary"],
                    "failed_checks": [c["check_name"] for c in r["checks"] if not c["passed"]],
                }
                for r in runs
            ],
        }
    )


def get_checkpoint(pipeline_name: str = PIPELINE_NAME) -> dict[str, Any]:
    if pipeline_name != PIPELINE_NAME:
        raise ToolError(f"unknown pipeline {pipeline_name!r}; the only pipeline is {PIPELINE_NAME!r}")
    with connect(readonly=True) as conn:
        checkpoint = run_log.get_checkpoint(conn, ANALYTICS_SCHEMA)
    if checkpoint is None:
        return {"pipeline_name": pipeline_name, "checkpoint": None}
    return to_jsonable(checkpoint)


# --- Contracts ---------------------------------------------------------------


def _contract(name: str, kind: str) -> dict:
    try:
        path, text = read_contract_text(name, kind=kind)
    except ContractNotFound as exc:
        raise ToolError(str(exc)) from exc
    return {"name": name, "path": path, "text": text}


def get_metric_contract(metric_name: str) -> dict[str, Any]:
    return _contract(metric_name.strip().lower(), "metric")


def get_data_contract(dataset_name: str) -> dict[str, Any]:
    return _contract(dataset_name.strip().lower(), "data")
