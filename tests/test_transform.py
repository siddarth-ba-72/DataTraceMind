"""The transformation as a pure function, and the schema that constrains it."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from etl import transform_store
from etl.transforms.order_revenue import Bundle, net_revenue_by_currency, transform_bundles
from etl.transforms.spec import SpecError, parse_spec

BASELINE = transform_store.baseline_content()
AT = datetime(2026, 10, 1, 20, 0, tzinfo=UTC)  # 01:30 on 2 October in Asia/Kolkata


def order(order_id=1, status="PAID", amount="1000.00", currency="INR") -> dict:
    return {
        "order_id": order_id, "customer_id": 7, "order_status": status, "amount": Decimal(amount),
        "currency": currency, "created_at": AT, "updated_at": AT,
    }  # fmt: skip


def payment(payment_id=10, order_id=1, status="CAPTURED", amount="1000.00") -> dict:
    return {
        "payment_id": payment_id, "order_id": order_id, "payment_status": status,
        "amount": Decimal(amount), "paid_at": AT, "updated_at": AT,
    }  # fmt: skip


def refund(refund_id=100, status="SUCCEEDED", amount="1000.00", payment_id=10, order_id=1) -> dict:
    return {
        "refund_id": refund_id, "payment_id": payment_id, "order_id": order_id, "refund_status": status,
        "refund_amount": Decimal(amount), "created_at": AT, "updated_at": AT,
    }  # fmt: skip


def spec(**replacements: str):
    text = BASELINE
    for old, new in replacements.items():
        assert text.count(old) == 1
        text = text.replace(old, new)
    return parse_spec(text)


def net(bundles, transform_spec) -> Decimal:
    return net_revenue_by_currency(transform_bundles(bundles, transform_spec)).get("INR", Decimal(0))


def test_partial_refund_subtracts_the_refunded_amount_only():
    bundle = Bundle(order(), [payment()], [refund(amount="250.00")])
    assert net([bundle], spec()) == Decimal("750.00")


def test_pending_and_failed_refunds_are_not_subtracted():
    bundle = Bundle(order(), [payment()], [refund(100, "PENDING"), refund(101, "FAILED")])
    assert net([bundle], spec()) == Decimal("1000.00")


def test_failed_payment_is_not_revenue():
    bundle = Bundle(order(status="CANCELLED"), [payment(status="FAILED")])
    result = transform_bundles([bundle], spec())
    assert net_revenue_by_currency(result) == {}
    assert result.fact_payments[0]["is_revenue"] is False
    assert result.fact_orders[0]["captured_amount"] == Decimal("0.00")


def test_refund_filter_naming_an_unknown_status_subtracts_nothing():
    bundle = Bundle(order(), [payment()], [refund()])
    faulty = spec(**{"refund_statuses: [SUCCEEDED]": "refund_statuses: [COMPLETED]"})
    assert net([bundle], faulty) == Decimal("1000.00")
    assert net([bundle], spec()) == Decimal("0.00")


def test_order_total_basis_over_subtracts_a_partial_refund():
    bundle = Bundle(order(), [payment()], [refund(amount="250.00")])
    faulty = spec(**{"refund_amount_basis: refund_amount": "refund_amount_basis: order_total"})
    assert net([bundle], faulty) == Decimal("0.00")


def test_left_join_keeps_unpaid_orders_and_inner_join_drops_them():
    unpaid = Bundle(order(order_id=2, status="CREATED"))
    assert len(transform_bundles([unpaid], spec()).fact_orders) == 1
    inner = spec(**{"payment_join: left": "payment_join: inner"})
    assert transform_bundles([unpaid], inner).fact_orders == []


def test_rows_are_dated_in_the_business_timezone():
    result = transform_bundles([Bundle(order(), [payment()], [refund()])], spec())
    assert result.fact_payments[0]["revenue_date"].isoformat() == "2026-10-02"
    assert result.fact_orders[0]["order_date"].isoformat() == "2026-10-02"


def test_unknown_status_is_rejected_not_guessed():
    bundle = Bundle(order(), [payment()], [refund(status="PAYMENT_REVERSED")])
    result = transform_bundles([bundle], spec())
    assert result.fact_refunds == []
    assert result.rejects == [{"table": "refunds", "id": 100, "reason": "unknown refund_status 'PAYMENT_REVERSED'"}]


def test_null_amount_rejects_the_order_and_its_children():
    bad = order()
    bad["amount"] = None
    result = transform_bundles([Bundle(bad, [payment()], [refund()])], spec())
    assert result.row_count == 0
    assert [r["table"] for r in result.rejects] == ["orders", "payments", "refunds"]


@pytest.mark.parametrize(
    "old, new",
    [
        ("mode: replace_by_key", "mode: replace_by_key\n  post_load_sql: DROP TABLE oltp.orders"),
        ("payment_join: left", "payment_join: cross"),
        ("amount_column: total_amount", 'amount_column: "total_amount; DROP TABLE oltp.orders"'),
        ("refund_statuses: [SUCCEEDED]", "refund_statuses: ['SUCCEEDED'' OR 1=1']"),
        ("timezone: Asia/Kolkata", "timezone: Mars/Olympus"),
        ("pipeline: order_revenue", "pipeline: something_else"),
    ],
)
def test_spec_rejects_anything_outside_the_allowed_shape(old, new):
    assert BASELINE.count(old) == 1
    with pytest.raises(SpecError):
        parse_spec(BASELINE.replace(old, new))


def test_spec_rejects_non_mapping_and_oversized_files():
    with pytest.raises(SpecError):
        parse_spec("- just\n- a list\n")
    with pytest.raises(SpecError):
        parse_spec(BASELINE + "#" * 9000)
