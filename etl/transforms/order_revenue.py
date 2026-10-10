"""Order-to-revenue transformation: source rows in, fact rows out.

Pure functions with no database access, so the same code runs in the live
pipeline, in staging validation, and in regression fixtures. Behaviour is
driven entirely by the TransformSpec.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from zoneinfo import ZoneInfo

from etl.transforms.spec import TransformSpec

ORDER_STATUSES = {"CREATED", "PAID", "CANCELLED", "PARTIALLY_REFUNDED", "REFUNDED"}
PAYMENT_STATUSES = {"CAPTURED", "FAILED"}
REFUND_STATUSES = {"PENDING", "SUCCEEDED", "FAILED"}

ZERO = Decimal("0.00")


@dataclass
class Bundle:
    """One order with all of its payments and refunds."""

    order: dict
    payments: list[dict] = field(default_factory=list)
    refunds: list[dict] = field(default_factory=list)


@dataclass
class TransformResult:
    fact_orders: list[dict] = field(default_factory=list)
    fact_payments: list[dict] = field(default_factory=list)
    fact_refunds: list[dict] = field(default_factory=list)
    rejects: list[dict] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.fact_orders) + len(self.fact_payments) + len(self.fact_refunds)


def _amount_problem(value: object) -> str | None:
    if value is None:
        return "amount is null"
    if not isinstance(value, Decimal):
        return f"amount is not numeric ({type(value).__name__})"
    if value < 0:
        return "amount is negative"
    return None


def _order_problem(order: dict) -> str | None:
    if order["order_status"] not in ORDER_STATUSES:
        return f"unknown order_status {order['order_status']!r}"
    currency = order["currency"]
    if not (isinstance(currency, str) and len(currency) == 3 and currency.isupper()):
        return f"invalid currency {currency!r}"
    return _amount_problem(order["amount"])


def transform_bundles(bundles: list[Bundle], spec: TransformSpec) -> TransformResult:
    """Turn order bundles into fact rows according to `spec`."""
    tz = ZoneInfo(spec.revenue.timezone)
    payment_statuses = set(spec.revenue.payment_statuses)
    refund_statuses = set(spec.revenue.refund_statuses)
    result = TransformResult()

    def reject(table: str, key: int, reason: str) -> None:
        result.rejects.append({"table": table, "id": key, "reason": reason})

    for bundle in bundles:
        order = bundle.order
        order_id = order["order_id"]
        problem = _order_problem(order)
        if problem:
            # Children take their currency from the order, so they cannot be
            # loaded without it.
            reject("orders", order_id, problem)
            for payment in bundle.payments:
                reject("payments", payment["payment_id"], "parent order rejected")
            for refund in bundle.refunds:
                reject("refunds", refund["refund_id"], "parent order rejected")
            continue

        currency = order["currency"]
        valid_payment_ids: set[int] = set()
        captured_amount = ZERO
        for payment in bundle.payments:
            problem = _amount_problem(payment["amount"])
            if payment["payment_status"] not in PAYMENT_STATUSES:
                problem = f"unknown payment_status {payment['payment_status']!r}"
            if problem:
                reject("payments", payment["payment_id"], problem)
                continue
            valid_payment_ids.add(payment["payment_id"])
            is_revenue = payment["payment_status"] in payment_statuses
            if is_revenue:
                captured_amount += payment["amount"]
            result.fact_payments.append(
                {
                    "payment_id": payment["payment_id"],
                    "order_id": order_id,
                    "payment_status": payment["payment_status"],
                    "amount": payment["amount"],
                    "currency": currency,
                    "paid_at": payment["paid_at"],
                    "revenue_date": payment["paid_at"].astimezone(tz).date(),
                    "is_revenue": is_revenue,
                }
            )

        for refund in bundle.refunds:
            problem = _amount_problem(refund["refund_amount"])
            if refund["refund_status"] not in REFUND_STATUSES:
                problem = f"unknown refund_status {refund['refund_status']!r}"
            elif refund["payment_id"] not in valid_payment_ids:
                problem = "refund references a payment that was not loaded"
            if problem:
                reject("refunds", refund["refund_id"], problem)
                continue
            is_subtracted = refund["refund_status"] in refund_statuses
            if not is_subtracted:
                subtracted = ZERO
            elif spec.revenue.refund_amount_basis == "order_total":
                subtracted = order["amount"]
            else:
                subtracted = refund["refund_amount"]
            result.fact_refunds.append(
                {
                    "refund_id": refund["refund_id"],
                    "payment_id": refund["payment_id"],
                    "order_id": order_id,
                    "refund_status": refund["refund_status"],
                    "refund_amount": refund["refund_amount"],
                    "currency": currency,
                    "created_at": refund["created_at"],
                    "revenue_date": refund["created_at"].astimezone(tz).date(),
                    "is_subtracted": is_subtracted,
                    "subtracted_amount": subtracted,
                }
            )

        # An inner join to payments produces no order row for an order that
        # has none; a left join keeps it.
        if spec.fact_orders.payment_join == "inner" and not bundle.payments:
            continue
        result.fact_orders.append(
            {
                "order_id": order_id,
                "customer_id": order["customer_id"],
                "order_status": order["order_status"],
                "total_amount": order["amount"],
                "currency": currency,
                "order_date": order["created_at"].astimezone(tz).date(),
                "payment_count": len(bundle.payments),
                "captured_amount": captured_amount,
                "created_at": order["created_at"],
                "updated_at": order["updated_at"],
            }
        )
    return result


def net_revenue_by_currency(result: TransformResult) -> dict[str, Decimal]:
    """Net revenue implied by a set of fact rows, per currency."""
    totals: dict[str, Decimal] = {}
    for row in result.fact_payments:
        if row["is_revenue"]:
            totals[row["currency"]] = totals.get(row["currency"], ZERO) + row["amount"]
    for row in result.fact_refunds:
        if row["is_subtracted"]:
            totals[row["currency"]] = totals.get(row["currency"], ZERO) - row["subtracted_amount"]
    return totals
