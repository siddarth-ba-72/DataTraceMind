"""Read models for the dashboard: metrics, pipeline health, and live activity."""

from __future__ import annotations

from datetime import UTC, datetime

from datatracemind import store
from datatracemind.config import get_settings
from datatracemind.db import ANALYTICS_SCHEMA, connect
from etl import run_log, transform_store
from etl.contract import load_revenue_contract
from etl.metrics import reconcile, source_revenue, target_revenue

DAILY_WINDOW = 14


def metrics_summary() -> dict:
    """Source versus analytics revenue, pipeline health, and whether the data can be trusted."""
    contract = load_revenue_contract()
    settings = get_settings()
    with connect() as conn:
        checkpoint = run_log.get_checkpoint(conn, ANALYTICS_SCHEMA)
        as_of = checkpoint["watermark"] if checkpoint else None
        # Compare like with like: the source as the pipeline last saw it.
        source = source_revenue(conn, contract, as_of=as_of)
        target = target_revenue(conn, ANALYTICS_SCHEMA)
        report = reconcile(source, target, contract.tolerance)
        last_run = run_log.latest_run(conn, ANALYTICS_SCHEMA)
        refreshed_at = conn.execute(
            f"SELECT max(refreshed_at) AS at FROM {ANALYTICS_SCHEMA}.daily_revenue"
        ).fetchone()["at"]
        incident = store.unresolved_incident(conn)
        try:
            active = transform_store.get_active(conn)
            transform = {"version": active.version, "author": active.author, "message": active.message}
        except transform_store.NoActiveTransform:
            transform = None

    age = (datetime.now(UTC) - refreshed_at).total_seconds() if refreshed_at else None
    fresh = age is not None and age <= settings.freshness_objective_s
    if incident is not None:
        trust = "under_investigation"
    elif not fresh:
        trust = "stale"
    elif not report["reconciled"]:
        trust = "untrusted"
    else:
        trust = "trustworthy"

    days = sorted({day for day, _ in set(source.daily) | set(target.daily)})[-DAILY_WINDOW:]
    daily = [
        {
            "date": day.isoformat(),
            "currency": currency,
            "source_net": float(source.daily.get((day, currency), {}).get("net", 0)),
            "analytics_net": float(target.daily.get((day, currency), {}).get("net", 0)),
        }
        for day in days
        for currency in sorted(report["by_currency"])
        if (day, currency) in source.daily or (day, currency) in target.daily
    ]
    return {
        "as_of": as_of,
        "trust": trust,
        "reconciliation": report,
        "order_counts": {
            currency: {
                "source": int(source.by_currency.get(currency, {}).get("order_count", 0)),
                "analytics": int(target.by_currency.get(currency, {}).get("order_count", 0)),
            }
            for currency in sorted(report["by_currency"])
        },
        "daily": daily,
        "pipeline": {
            "last_run": last_run
            and {
                "run_id": last_run["run_id"],
                "status": last_run["status"],
                "finished_at": last_run["finished_at"],
                "error_summary": last_run["error_summary"],
            },
            "refreshed_at": refreshed_at,
            "freshness_seconds": age,
            "freshness_objective_seconds": settings.freshness_objective_s,
            "fresh": fresh,
            "transform": transform,
        },
        "active_incident": incident,
    }


def live_transactions(limit: int = 30) -> dict:
    """Most recent source activity, newest first, with running totals."""
    with connect() as conn:
        # to_jsonb keeps this query working whichever name the order amount
        # column currently has (see the schema-drift scenario).
        events = conn.execute(
            """
            SELECT 'order' AS kind, o.order_id AS id, o.order_id, o.order_status AS status,
                   coalesce(to_jsonb(o)->>'total_amount', to_jsonb(o)->>'order_total')::numeric AS amount,
                   o.currency::text AS currency, o.updated_at AS at
            FROM oltp.orders o
            UNION ALL
            SELECT 'payment', p.payment_id, p.order_id, p.payment_status, p.amount, o.currency::text, p.updated_at
            FROM oltp.payments p JOIN oltp.orders o ON o.order_id = p.order_id
            UNION ALL
            SELECT 'refund', r.refund_id, r.order_id, r.refund_status, r.refund_amount, o.currency::text, r.updated_at
            FROM oltp.refunds r JOIN oltp.orders o ON o.order_id = r.order_id
            ORDER BY at DESC, kind, id DESC
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
        totals = conn.execute(
            """
            SELECT (SELECT count(*) FROM oltp.orders) AS orders,
                   (SELECT count(*) FROM oltp.payments) AS payments,
                   (SELECT count(*) FROM oltp.refunds) AS refunds
            """
        ).fetchone()
    return {"totals": totals, "events": events}
