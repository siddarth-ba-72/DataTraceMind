"""Regression fixtures for a proposed transformation.

Small, fixed order bundles covering the cases the revenue and orders
contracts distinguish. Each case is run through the proposed transformation
and compared with what the *contract* says the answer is. The expected values
are computed here from the raw fixture rows and the contract rules, not from
any transformation, so a patch cannot pass by agreeing with itself.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from datatracemind.models import RegressionCase
from etl.contract import RevenueContract
from etl.transforms.order_revenue import Bundle, net_revenue_by_currency, transform_bundles
from etl.transforms.spec import TransformSpec

_AT = datetime(2026, 9, 15, 6, 30, tzinfo=UTC)


def _order(order_id: int, status: str, amount: str, currency: str = "INR") -> dict:
    return {
        "order_id": order_id, "customer_id": 1, "order_status": status, "amount": Decimal(amount),
        "currency": currency, "created_at": _AT, "updated_at": _AT,
    }  # fmt: skip


def _payment(payment_id: int, order_id: int, status: str, amount: str) -> dict:
    return {
        "payment_id": payment_id, "order_id": order_id, "payment_status": status, "amount": Decimal(amount),
        "paid_at": _AT, "updated_at": _AT,
    }  # fmt: skip


def _refund(refund_id: int, order_id: int, payment_id: int, status: str, amount: str) -> dict:
    return {
        "refund_id": refund_id, "payment_id": payment_id, "order_id": order_id, "refund_status": status,
        "refund_amount": Decimal(amount), "created_at": _AT, "updated_at": _AT,
    }  # fmt: skip


CASES: dict[str, list[Bundle]] = {
    "paid order, no refund": [Bundle(_order(1, "PAID", "1200.00"), [_payment(1, 1, "CAPTURED", "1200.00")])],
    "full refund succeeded": [
        Bundle(
            _order(2, "REFUNDED", "800.00"),
            [_payment(2, 2, "CAPTURED", "800.00")],
            [_refund(1, 2, 2, "SUCCEEDED", "800.00")],
        )
    ],
    "partial refund succeeded": [
        Bundle(
            _order(3, "PARTIALLY_REFUNDED", "1000.00"),
            [_payment(3, 3, "CAPTURED", "1000.00")],
            [_refund(2, 3, 3, "SUCCEEDED", "250.00")],
        )
    ],
    "two partial refunds on one order": [
        Bundle(
            _order(4, "PARTIALLY_REFUNDED", "900.00"),
            [_payment(4, 4, "CAPTURED", "900.00")],
            [_refund(3, 4, 4, "SUCCEEDED", "100.00"), _refund(4, 4, 4, "SUCCEEDED", "150.00")],
        )
    ],
    "refund pending": [
        Bundle(
            _order(5, "PAID", "500.00"), [_payment(5, 5, "CAPTURED", "500.00")], [_refund(5, 5, 5, "PENDING", "500.00")]
        )
    ],
    "refund failed": [
        Bundle(
            _order(6, "PAID", "500.00"), [_payment(6, 6, "CAPTURED", "500.00")], [_refund(6, 6, 6, "FAILED", "500.00")]
        )
    ],
    "payment failed, order cancelled": [
        Bundle(_order(7, "CANCELLED", "300.00"), [_payment(7, 7, "FAILED", "300.00")])
    ],
    "payment retried after a failure": [
        Bundle(
            _order(8, "PAID", "650.00"),
            [_payment(8, 8, "FAILED", "650.00"), _payment(9, 8, "CAPTURED", "650.00")],
        )
    ],
    "order with no payment": [Bundle(_order(9, "CREATED", "410.00"))],
    "two currencies kept apart": [
        Bundle(_order(10, "PAID", "100.00", "USD"), [_payment(10, 10, "CAPTURED", "100.00")]),
        Bundle(
            _order(11, "PARTIALLY_REFUNDED", "2000.00"),
            [_payment(11, 11, "CAPTURED", "2000.00")],
            [_refund(7, 11, 11, "SUCCEEDED", "500.00")],
        ),
    ],
}


def _expected(bundles: list[Bundle], contract: RevenueContract) -> dict:
    """What the contracts require for these rows."""
    net: dict[str, Decimal] = {}
    for bundle in bundles:
        currency = bundle.order["currency"]
        for payment in bundle.payments:
            if payment["payment_status"] in contract.payment_statuses:
                net[currency] = net.get(currency, Decimal(0)) + payment["amount"]
        for refund in bundle.refunds:
            if refund["refund_status"] in contract.refund_statuses:
                net[currency] = net.get(currency, Decimal(0)) - refund["refund_amount"]
    return {
        "net_revenue": {currency: float(amount) for currency, amount in sorted(net.items())},
        "fact_orders": len(bundles),
        "fact_payments": sum(len(b.payments) for b in bundles),
        "fact_refunds": sum(len(b.refunds) for b in bundles),
        "rejected": 0,
    }


def run_regression_suite(spec: TransformSpec, contract: RevenueContract) -> list[RegressionCase]:
    results = []
    for name, bundles in CASES.items():
        output = transform_bundles(bundles, spec)
        actual = {
            "net_revenue": {c: float(v) for c, v in sorted(net_revenue_by_currency(output).items())},
            "fact_orders": len(output.fact_orders),
            "fact_payments": len(output.fact_payments),
            "fact_refunds": len(output.fact_refunds),
            "rejected": len(output.rejects),
        }
        expected = _expected(bundles, contract)
        results.append(RegressionCase(name=name, passed=actual == expected, expected=expected, actual=actual))
    return results
