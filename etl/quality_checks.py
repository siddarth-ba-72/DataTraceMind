"""Deterministic data-quality checks.

These decide whether the pipeline's output is trustworthy, and they decide
whether a repair passes. No model is involved. Every check is derived from an
approved contract or a structural invariant, never from the transformation
under test, and compares the target with the source snapshot the run read.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import psycopg
from psycopg import sql

from etl.contract import RevenueContract, load_source_schema_contract
from etl.metrics import RevenueMetrics, reconcile, target_revenue

if TYPE_CHECKING:
    from etl.pipeline import SourceSnapshot

SAMPLE_SIZE = 10

# fact table -> (business key, snapshot attribute holding the source IDs)
FACT_KEYS = {
    "fact_orders": ("order_id", "order_ids"),
    "fact_payments": ("payment_id", "payment_ids"),
    "fact_refunds": ("refund_id", "refund_ids"),
}


@dataclass
class CheckResult:
    name: str
    passed: bool
    severity: str  # critical | high | medium
    observed: dict
    expected: str

    def as_dict(self) -> dict:
        return {
            "check_name": self.name,
            "passed": self.passed,
            "severity": self.severity,
            "observed_value": self.observed,
            "expected_condition": self.expected,
        }


def check_source_schema(conn: psycopg.Connection) -> CheckResult:
    """Source columns and types still match the schema contract."""
    expected = load_source_schema_contract()
    rows = conn.execute(
        "SELECT table_name, column_name, data_type FROM information_schema.columns WHERE table_schema = 'oltp'"
    ).fetchall()
    actual = {(r["table_name"], r["column_name"]): r["data_type"] for r in rows}
    missing, mismatched = [], []
    for table, columns in expected.items():
        for column, type_prefix in columns.items():
            found = actual.get((table, column))
            if found is None:
                missing.append(f"{table}.{column}")
            elif not found.startswith(type_prefix):
                mismatched.append({"column": f"{table}.{column}", "expected": type_prefix, "actual": found})
    known = {(t, c) for t, cols in expected.items() for c in cols}
    unexpected = sorted(f"{t}.{c}" for (t, c) in actual if t in expected and (t, c) not in known)
    return CheckResult(
        name="source_schema_contract",
        passed=not missing and not mismatched,
        severity="high",
        observed={"missing_columns": missing, "type_mismatches": mismatched, "unexpected_columns": unexpected},
        expected="every column in contracts/schemas/oltp.yaml exists in the source with the contracted type",
    )


def check_run_status(status: str, error_summary: str | None) -> CheckResult:
    return CheckResult(
        name="pipeline_run_succeeded",
        passed=status == "SUCCESS",
        severity="high",
        observed={"status": status, "error": error_summary},
        expected="the ETL run finishes with status SUCCESS",
    )


def _check_reconciliation(
    source: RevenueMetrics, target: RevenueMetrics, contract: RevenueContract
) -> CheckResult:
    report = reconcile(source, target, contract.tolerance)
    return CheckResult(
        name="net_revenue_reconciliation",
        passed=report.pop("reconciled"),
        severity="critical",
        observed=report,
        expected=(
            "net revenue derived from the source by the net_revenue contract equals "
            f"analytics daily_revenue.net_revenue per currency and per day within {contract.tolerance}"
        ),
    )


def _check_order_counts(source: RevenueMetrics, target: RevenueMetrics) -> CheckResult:
    currencies = sorted(set(source.by_currency) | set(target.by_currency))
    counts = {
        c: {
            "source_orders": int(source.by_currency.get(c, {}).get("order_count", 0)),
            "analytics_order_count": int(target.by_currency.get(c, {}).get("order_count", 0)),
        }
        for c in currencies
    }
    return CheckResult(
        name="order_count_reconciliation",
        passed=all(v["source_orders"] == v["analytics_order_count"] for v in counts.values()),
        severity="high",
        observed=counts,
        expected="daily_revenue.order_count totals equal the number of source orders per currency",
    )


def _check_completeness(
    conn: psycopg.Connection, schema: str, table: str, key: str, source_ids: set[int]
) -> CheckResult:
    query = sql.SQL("SELECT DISTINCT {} AS id FROM {}.{}").format(
        sql.Identifier(key), sql.Identifier(schema), sql.Identifier(table)
    )
    target_ids = {row["id"] for row in conn.execute(query)}
    missing = sorted(source_ids - target_ids)
    orphaned = sorted(target_ids - source_ids)
    return CheckResult(
        name=f"{table}_completeness",
        passed=not missing and not orphaned,
        severity="high",
        observed={
            "source_ids": len(source_ids),
            "analytics_ids": len(target_ids),
            "missing_in_analytics": len(missing),
            "missing_sample": missing[:SAMPLE_SIZE],
            "not_in_source": len(orphaned),
            "not_in_source_sample": orphaned[:SAMPLE_SIZE],
        },
        expected=f"every source {key} has a row in {table}, and every {table} row references a source {key}",
    )


def _check_unique_keys(conn: psycopg.Connection, schema: str) -> CheckResult:
    duplicates = {}
    for table, (key, _) in FACT_KEYS.items():
        query = sql.SQL(
            "SELECT {k} AS id, count(*) AS copies FROM {s}.{t} GROUP BY {k} HAVING count(*) > 1 ORDER BY {k}"
        ).format(k=sql.Identifier(key), s=sql.Identifier(schema), t=sql.Identifier(table))
        rows = conn.execute(query).fetchall()
        duplicates[table] = {
            "duplicated_keys": len(rows),
            "extra_rows": sum(r["copies"] - 1 for r in rows),
            "sample": [r["id"] for r in rows[:SAMPLE_SIZE]],
        }
    return CheckResult(
        name="unique_business_keys",
        passed=all(v["duplicated_keys"] == 0 for v in duplicates.values()),
        severity="high",
        observed=duplicates,
        expected="order_id, payment_id, and refund_id are each unique in their fact table",
    )


def _check_eligibility(conn: psycopg.Connection, schema: str, contract: RevenueContract) -> list[CheckResult]:
    """Fact rows are flagged as the contract says they should be."""
    payments = conn.execute(
        sql.SQL(
            """
            SELECT payment_status AS status, is_revenue AS counted, count(*) AS rows, sum(amount) AS amount
            FROM {}.fact_payments
            WHERE is_revenue <> (payment_status = ANY(%s))
            GROUP BY 1, 2 ORDER BY 1
            """
        ).format(sql.Identifier(schema)),
        (list(contract.payment_statuses),),
    ).fetchall()
    refunds = conn.execute(
        sql.SQL(
            """
            SELECT refund_status AS status, is_subtracted AS subtracted, count(*) AS rows,
                   sum(refund_amount) AS refund_amount
            FROM {}.fact_refunds
            WHERE is_subtracted <> (refund_status = ANY(%s))
            GROUP BY 1, 2 ORDER BY 1
            """
        ).format(sql.Identifier(schema)),
        (list(contract.refund_statuses),),
    ).fetchall()
    wrong_amount = conn.execute(
        sql.SQL(
            """
            SELECT count(*) AS rows, coalesce(sum(subtracted_amount - refund_amount), 0) AS over_subtracted
            FROM {}.fact_refunds
            WHERE is_subtracted AND subtracted_amount <> refund_amount
            """
        ).format(sql.Identifier(schema))
    ).fetchone()
    return [
        CheckResult(
            name="payment_revenue_eligibility",
            passed=not payments,
            severity="high",
            observed={"misclassified": payments},
            expected=f"a payment counts as revenue exactly when its status is in {list(contract.payment_statuses)}",
        ),
        CheckResult(
            name="refund_subtraction_eligibility",
            passed=not refunds and wrong_amount["rows"] == 0,
            severity="high",
            observed={
                "misclassified": refunds,
                "subtracted_amount_differs_from_refund_amount": wrong_amount,
            },
            expected=(
                f"a refund is subtracted exactly when its status is in {list(contract.refund_statuses)}, "
                "and the amount subtracted is refund_amount"
            ),
        ),
    ]


def _check_refund_ceiling(conn: psycopg.Connection, schema: str) -> CheckResult:
    rows = conn.execute(
        sql.SQL(
            """
            SELECT o.order_id, o.captured_amount, sum(r.subtracted_amount) AS subtracted
            FROM {s}.fact_orders o JOIN {s}.fact_refunds r ON r.order_id = o.order_id
            WHERE r.is_subtracted
            GROUP BY o.order_id, o.captured_amount
            HAVING sum(r.subtracted_amount) > o.captured_amount
            ORDER BY o.order_id
            """
        ).format(s=sql.Identifier(schema))
    ).fetchall()
    return CheckResult(
        name="refund_within_captured_amount",
        passed=not rows,
        severity="medium",
        observed={"orders_over_refunded": len(rows), "sample": rows[:SAMPLE_SIZE]},
        expected="refunds subtracted for an order never exceed the amount captured for it",
    )


def run_target_checks(
    conn: psycopg.Connection, schema: str, snapshot: SourceSnapshot, contract: RevenueContract
) -> list[CheckResult]:
    """All post-load checks for a successful run."""
    target = target_revenue(conn, schema)
    checks = [
        _check_reconciliation(snapshot.metrics, target, contract),
        _check_order_counts(snapshot.metrics, target),
    ]
    for table, (key, attr) in FACT_KEYS.items():
        checks.append(_check_completeness(conn, schema, table, key, getattr(snapshot, attr)))
    checks.append(_check_unique_keys(conn, schema))
    checks.extend(_check_eligibility(conn, schema, contract))
    checks.append(_check_refund_ceiling(conn, schema))
    return checks


def check_declared_status(status: str, checks: list[CheckResult]) -> CheckResult:
    """A run that reports SUCCESS while its output fails checks is a silent failure."""
    failed = [c.name for c in checks if not c.passed and c.severity in ("critical", "high")]
    return CheckResult(
        name="declared_status_consistent",
        passed=not (status == "SUCCESS" and failed),
        severity="critical",
        observed={"declared_status": status, "failed_checks": failed},
        expected="a run that declares SUCCESS has no failed critical or high checks",
    )
