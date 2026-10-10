"""Revenue metrics on both sides of the pipeline.

`source_revenue` derives net revenue from the operational tables using the
approved contract. `target_revenue` reads what the analytics target reports.
They share one shape so that reconciliation is a plain comparison, and the
source side never looks at the transformation it is checking.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

import psycopg
from psycopg import sql

from etl.contract import RevenueContract

ZERO = Decimal("0.00")
MEASURES = ("gross", "refunds", "net", "order_count")


def _blank() -> dict[str, Decimal]:
    return {"gross": ZERO, "refunds": ZERO, "net": ZERO, "order_count": Decimal(0)}


@dataclass
class RevenueMetrics:
    """Totals per currency, and per (business day, currency)."""

    by_currency: dict[str, dict[str, Decimal]] = field(default_factory=dict)
    daily: dict[tuple[date, str], dict[str, Decimal]] = field(default_factory=dict)

    def add(self, day: date, currency: str, measure: str, amount: Decimal) -> None:
        for cell in (
            self.by_currency.setdefault(currency, _blank()),
            self.daily.setdefault((day, currency), _blank()),
        ):
            cell[measure] += amount
            cell["net"] = cell["gross"] - cell["refunds"]

    def summary(self) -> dict:
        """JSON-friendly totals per currency."""
        return {
            currency: {
                "gross": float(cell["gross"]),
                "refunds": float(cell["refunds"]),
                "net": float(cell["net"]),
                "order_count": int(cell["order_count"]),
            }
            for currency, cell in sorted(self.by_currency.items())
        }


def source_revenue(
    conn: psycopg.Connection, contract: RevenueContract, *, as_of: datetime | None = None
) -> RevenueMetrics:
    """Net revenue derived from the source by the approved contract.

    `as_of` restricts the calculation to rows the pipeline could have seen at
    that watermark, so a comparison with the target is not skewed by
    transactions that arrived after the last load.
    """
    params = {
        "tz": contract.timezone,
        "payment_statuses": list(contract.payment_statuses),
        "refund_statuses": list(contract.refund_statuses),
        "as_of": as_of,
    }
    metrics = RevenueMetrics()
    for row in conn.execute(
        """
        SELECT o.currency, (p.paid_at AT TIME ZONE %(tz)s)::date AS day, sum(p.amount) AS amount
        FROM oltp.payments p JOIN oltp.orders o ON o.order_id = p.order_id
        WHERE p.payment_status = ANY(%(payment_statuses)s)
          AND (%(as_of)s::timestamptz IS NULL OR p.updated_at <= %(as_of)s)
        GROUP BY 1, 2
        """,
        params,
    ):
        metrics.add(row["day"], row["currency"], "gross", row["amount"])
    for row in conn.execute(
        """
        SELECT o.currency, (r.created_at AT TIME ZONE %(tz)s)::date AS day, sum(r.refund_amount) AS amount
        FROM oltp.refunds r JOIN oltp.orders o ON o.order_id = r.order_id
        WHERE r.refund_status = ANY(%(refund_statuses)s)
          AND (%(as_of)s::timestamptz IS NULL OR r.updated_at <= %(as_of)s)
        GROUP BY 1, 2
        """,
        params,
    ):
        metrics.add(row["day"], row["currency"], "refunds", row["amount"])
    for row in conn.execute(
        """
        SELECT currency, (created_at AT TIME ZONE %(tz)s)::date AS day, count(*) AS orders
        FROM oltp.orders
        WHERE (%(as_of)s::timestamptz IS NULL OR created_at <= %(as_of)s)
        GROUP BY 1, 2
        """,
        params,
    ):
        metrics.add(row["day"], row["currency"], "order_count", Decimal(row["orders"]))
    return metrics


def target_revenue(conn: psycopg.Connection, schema: str) -> RevenueMetrics:
    """Revenue as reported by `daily_revenue` in the given target schema."""
    metrics = RevenueMetrics()
    query = sql.SQL(
        "SELECT revenue_date, currency, gross_paid_amount, refund_amount, order_count FROM {}.daily_revenue"
    ).format(sql.Identifier(schema))
    for row in conn.execute(query):
        day, currency = row["revenue_date"], row["currency"]
        metrics.add(day, currency, "gross", row["gross_paid_amount"])
        metrics.add(day, currency, "refunds", row["refund_amount"])
        metrics.add(day, currency, "order_count", Decimal(row["order_count"]))
    return metrics


def reconcile(source: RevenueMetrics, target: RevenueMetrics, tolerance: Decimal) -> dict:
    """Compare the two sides. Returns a JSON-friendly report."""
    currencies = sorted(set(source.by_currency) | set(target.by_currency))
    by_currency = {}
    for currency in currencies:
        src = source.by_currency.get(currency, _blank())
        tgt = target.by_currency.get(currency, _blank())
        by_currency[currency] = {
            "source": {m: float(src[m]) for m in ("gross", "refunds", "net")},
            "analytics": {m: float(tgt[m]) for m in ("gross", "refunds", "net")},
            "net_difference": float(tgt["net"] - src["net"]),
            "within_tolerance": abs(tgt["net"] - src["net"]) <= tolerance,
        }
    mismatched_days = sorted(
        {
            day.isoformat()
            for day, currency in set(source.daily) | set(target.daily)
            if abs(
                source.daily.get((day, currency), _blank())["net"]
                - target.daily.get((day, currency), _blank())["net"]
            )
            > tolerance
        }
    )
    return {
        "by_currency": by_currency,
        "mismatched_day_count": len(mismatched_days),
        "mismatched_days_sample": mismatched_days[:5],
        "tolerance": float(tolerance),
        "reconciled": all(c["within_tolerance"] for c in by_currency.values()) and not mismatched_days,
    }
