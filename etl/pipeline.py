"""The order-to-revenue ETL.

One run:

1. Extract. A single repeatable-read snapshot of the source finds the orders
   changed since the checkpoint and reads each one with all of its payments
   and refunds. The same snapshot yields the contract-derived source metrics
   the run is later checked against, so both describe the same instant.
2. Transform. `etl.transforms.order_revenue` turns the bundles into fact rows
   as the active transformation file dictates; invalid rows are rejected.
3. Load. In one transaction the affected orders' fact rows are replaced,
   `daily_revenue` is rebuilt from the facts, and the checkpoint advances.
4. Check. Deterministic quality checks compare the target with the snapshot.

Watermark and retries: the checkpoint stores the snapshot time of the last
successful load. The next run re-reads everything changed since five seconds
before it, which covers transactions that were still in flight; replace-by-key
loading makes that overlap, and any rerun, harmless. A failed run changes
nothing in the target and does not advance the checkpoint. When the active
transformation version differs from the one that produced the current facts,
the run rebuilds every fact from the source.

Run status describes the job, not the data: a run can be SUCCESS with failed
quality checks. That gap is the silent failure DataTraceMind exists to catch.
"""

from __future__ import annotations

import argparse
import logging
import threading
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import psycopg
from psycopg import IsolationLevel, sql

from datatracemind.db import ANALYTICS_SCHEMA, connect
from etl import run_log, transform_store
from etl.contract import RevenueContract, load_revenue_contract
from etl.metrics import RevenueMetrics, source_revenue
from etl.quality_checks import (
    CheckResult,
    check_declared_status,
    check_run_status,
    check_source_schema,
    run_target_checks,
)
from etl.transforms.order_revenue import Bundle, TransformResult, transform_bundles
from etl.transforms.spec import TransformSpec, parse_spec

logger = logging.getLogger(__name__)

_THREAD_LOCK = threading.RLock()
_ADVISORY_LOCK_KEY = 0x44544D01
_lock_holder: ExitStack | None = None
_lock_depth = 0


@contextmanager
def pipeline_lock() -> Iterator[None]:
    """Serialise everything that reads or rewrites pipeline state.

    Scheduled runs, staging validation, fault injection, repair, and reset all
    take it. It is re-entrant, and it holds across processes as well as
    threads (a PostgreSQL advisory lock on its own connection), so the API's
    worker and a script such as the evaluator can share one database.
    """
    global _lock_holder, _lock_depth
    with _THREAD_LOCK:
        if _lock_depth == 0:
            holder = ExitStack()
            conn = holder.enter_context(connect(autocommit=True))
            conn.execute("SELECT pg_advisory_lock(%s)", (_ADVISORY_LOCK_KEY,))
            _lock_holder = holder
        _lock_depth += 1
        try:
            yield
        finally:
            _lock_depth -= 1
            if _lock_depth == 0 and _lock_holder is not None:
                # Closing the session releases the advisory lock.
                _lock_holder.close()
                _lock_holder = None

WATERMARK_OVERLAP = timedelta(seconds=5)
REJECT_SAMPLE = 20

_FACT_COLUMNS = {
    "fact_orders": (
        "order_id", "customer_id", "order_status", "total_amount", "currency", "order_date",
        "payment_count", "captured_amount", "created_at", "updated_at",
    ),
    "fact_payments": (
        "payment_id", "order_id", "payment_status", "amount", "currency", "paid_at",
        "revenue_date", "is_revenue",
    ),
    "fact_refunds": (
        "refund_id", "payment_id", "order_id", "refund_status", "refund_amount", "currency",
        "created_at", "revenue_date", "is_subtracted", "subtracted_amount",
    ),
}  # fmt: skip


@dataclass
class SourceSnapshot:
    """What one run read from the source, all from the same instant."""

    taken_at: datetime
    changed_order_ids: list[int]
    bundles: list[Bundle]
    metrics: RevenueMetrics
    order_ids: set[int]
    payment_ids: set[int]
    refund_ids: set[int]

    @property
    def extracted_count(self) -> int:
        return sum(1 + len(b.payments) + len(b.refunds) for b in self.bundles)


@dataclass
class RunResult:
    run_id: int
    target_schema: str
    status: str
    transform_version: int | None
    full_refresh: bool = False
    extracted: int = 0
    transformed: int = 0
    rejected: int = 0
    loaded: int = 0
    watermark: datetime | None = None
    error_summary: str | None = None
    checks: list[CheckResult] = field(default_factory=list)
    source_summary: dict | None = None

    @property
    def failed_checks(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.passed]


def _ids(conn: psycopg.Connection, table: str, key: str) -> set[int]:
    query = sql.SQL("SELECT {} AS id FROM oltp.{}").format(sql.Identifier(key), sql.Identifier(table))
    return {row["id"] for row in conn.execute(query)}


def extract(spec: TransformSpec, contract: RevenueContract, *, since: datetime | None) -> SourceSnapshot:
    """Read changed orders (all orders when `since` is None) from one snapshot."""
    with connect() as conn:
        conn.isolation_level = IsolationLevel.REPEATABLE_READ
        conn.read_only = True
        taken_at = conn.execute("SELECT now() AS now").fetchone()["now"]
        if since is None:
            changed = sorted(_ids(conn, "orders", "order_id"))
        else:
            rows = conn.execute(
                """
                SELECT order_id FROM oltp.orders WHERE updated_at > %(since)s
                UNION SELECT order_id FROM oltp.payments WHERE updated_at > %(since)s
                UNION SELECT order_id FROM oltp.refunds WHERE updated_at > %(since)s
                """,
                {"since": since - WATERMARK_OVERLAP},
            ).fetchall()
            changed = sorted(row["order_id"] for row in rows)

        # The extraction queries run on every pass, also when nothing changed,
        # so a source column the pipeline depends on cannot vanish unnoticed.
        orders = conn.execute(
            sql.SQL(
                "SELECT order_id, customer_id, order_status, {} AS amount, currency, created_at, updated_at "
                "FROM oltp.orders WHERE order_id = ANY(%s) ORDER BY order_id"
            ).format(sql.Identifier(spec.source.orders.amount_column)),
            (changed,),
        ).fetchall()
        bundles: dict[int, Bundle] = {order["order_id"]: Bundle(order=order) for order in orders}
        for payment in conn.execute(
            "SELECT payment_id, order_id, payment_status, amount, paid_at, updated_at "
            "FROM oltp.payments WHERE order_id = ANY(%s) ORDER BY payment_id",
            (changed,),
        ):
            bundles[payment["order_id"]].payments.append(payment)
        for refund in conn.execute(
            "SELECT refund_id, payment_id, order_id, refund_status, refund_amount, created_at, updated_at "
            "FROM oltp.refunds WHERE order_id = ANY(%s) ORDER BY refund_id",
            (changed,),
        ):
            bundles[refund["order_id"]].refunds.append(refund)

        return SourceSnapshot(
            taken_at=taken_at,
            changed_order_ids=changed,
            bundles=list(bundles.values()),
            metrics=source_revenue(conn, contract),
            order_ids=_ids(conn, "orders", "order_id"),
            payment_ids=_ids(conn, "payments", "payment_id"),
            refund_ids=_ids(conn, "refunds", "refund_id"),
        )


def _insert_facts(
    conn: psycopg.Connection, schema: str, table: str, rows: list[dict], run_id: int, version: int
) -> None:
    if not rows:
        return
    columns = _FACT_COLUMNS[table]
    query = sql.SQL("INSERT INTO {}.{} ({}, etl_run_id, transform_version) VALUES ({}, %s, %s)").format(
        sql.Identifier(schema),
        sql.Identifier(table),
        sql.SQL(", ").join(map(sql.Identifier, columns)),
        sql.SQL(", ").join(sql.Placeholder() * len(columns)),
    )
    conn.cursor().executemany(query, [(*(row[c] for c in columns), run_id, version) for row in rows])


def rebuild_daily_revenue(conn: psycopg.Connection, schema: str, version: int) -> None:
    """Recompute the daily aggregate, each measure at its own grain."""
    conn.execute(sql.SQL("DELETE FROM {}.daily_revenue").format(sql.Identifier(schema)))
    conn.execute(
        sql.SQL(
            """
            INSERT INTO {s}.daily_revenue
                (revenue_date, currency, gross_paid_amount, refund_amount, net_revenue, order_count,
                 refreshed_at, transform_version)
            SELECT day, currency, sum(gross), sum(refunds), sum(gross) - sum(refunds), sum(orders),
                   clock_timestamp(), %s
            FROM (
                SELECT revenue_date AS day, currency, amount AS gross, 0 AS refunds, 0 AS orders
                FROM {s}.fact_payments WHERE is_revenue
                UNION ALL
                SELECT revenue_date, currency, 0, subtracted_amount, 0
                FROM {s}.fact_refunds WHERE is_subtracted
                UNION ALL
                SELECT order_date, currency, 0, 0, 1 FROM {s}.fact_orders
            ) AS contributions
            GROUP BY day, currency
            """
        ).format(s=sql.Identifier(schema)),
        (version,),
    )


def load(
    conn: psycopg.Connection,
    schema: str,
    *,
    run_id: int,
    version: int,
    spec: TransformSpec,
    snapshot: SourceSnapshot,
    result: TransformResult,
    full_refresh: bool,
) -> None:
    """Write fact rows, rebuild the aggregate, and advance the checkpoint."""
    for table in _FACT_COLUMNS:
        target = sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(table))
        if full_refresh:
            conn.execute(sql.SQL("TRUNCATE {}").format(target))
        elif spec.load.mode == "replace_by_key" and snapshot.changed_order_ids:
            conn.execute(
                sql.SQL("DELETE FROM {} WHERE order_id = ANY(%s)").format(target),
                (snapshot.changed_order_ids,),
            )
        _insert_facts(conn, schema, table, getattr(result, table), run_id, version)
    rebuild_daily_revenue(conn, schema, version)
    run_log.set_checkpoint(
        conn, schema, watermark=snapshot.taken_at, transform_version=version, run_id=run_id
    )


def _describe(exc: BaseException) -> str:
    lines = str(exc).strip().splitlines()
    return f"{type(exc).__name__}: {lines[0] if lines else ''}".strip()


def run_once(
    *,
    target_schema: str = ANALYTICS_SCHEMA,
    spec_text: str | None = None,
    transform_version: int | None = None,
    full_refresh: bool = False,
    trigger: str = "schedule",
) -> RunResult:
    """Run the pipeline once and record the run, whatever its outcome.

    By default it uses the active transformation version. Staging validation
    passes `spec_text` (the patched file) and a staging `target_schema`.
    """
    with pipeline_lock():
        contract = load_revenue_contract()
        if spec_text is None:
            active = transform_store.get_active()
            spec_text, transform_version = active.content, active.version
        elif transform_version is None:
            raise ValueError("transform_version is required with an explicit spec_text")

        run_id = run_log.start_run(
            target_schema, trigger=trigger, transform_version=transform_version, full_refresh=full_refresh
        )
        result = RunResult(
            run_id=run_id, target_schema=target_schema, status="RUNNING", transform_version=transform_version
        )
        step = "configure"
        snapshot: SourceSnapshot | None = None
        spec: TransformSpec | None = None
        try:
            spec = parse_spec(spec_text)
            with connect() as conn:
                checkpoint = run_log.get_checkpoint(conn, target_schema)
            result.full_refresh = (
                full_refresh or checkpoint is None or checkpoint["transform_version"] != transform_version
            )
            step = "extract"
            snapshot = extract(
                spec, contract, since=None if result.full_refresh else checkpoint["watermark"]
            )
            step = "transform"
            transformed = transform_bundles(snapshot.bundles, spec)
            step = "load"
            result.extracted = snapshot.extracted_count
            result.transformed = transformed.row_count
            result.rejected = len(transformed.rejects)
            result.loaded = transformed.row_count
            result.watermark = snapshot.taken_at
            result.source_summary = snapshot.metrics.summary()
            with connect() as conn:
                load(
                    conn,
                    target_schema,
                    run_id=run_id,
                    version=transform_version,
                    spec=spec,
                    snapshot=snapshot,
                    result=transformed,
                    full_refresh=result.full_refresh,
                )
                run_log.finish_run(
                    conn,
                    target_schema,
                    run_id,
                    status="SUCCESS",
                    full_refresh=result.full_refresh,
                    extracted=result.extracted,
                    transformed=result.transformed,
                    rejected=result.rejected,
                    loaded=result.loaded,
                    watermark=result.watermark,
                    details={
                        "changed_orders": len(snapshot.changed_order_ids),
                        "rejects_sample": transformed.rejects[:REJECT_SAMPLE],
                        "source_summary": result.source_summary,
                    },
                )
            result.status = "SUCCESS"
        except Exception as exc:  # the run is recorded as FAILED; the worker keeps going
            logger.warning("ETL run %s failed during %s: %s", run_id, step, _describe(exc))
            result.status = "FAILED"
            result.error_summary = _describe(exc)
            result.extracted = result.transformed = result.rejected = result.loaded = 0
            with connect() as conn:
                run_log.finish_run(
                    conn,
                    target_schema,
                    run_id,
                    status="FAILED",
                    full_refresh=result.full_refresh,
                    error_summary=result.error_summary,
                    details={"failed_step": step},
                )

        with connect() as conn:
            schema_check = (
                check_source_schema(conn, spec.source.orders.amount_column) if spec else check_source_schema(conn)
            )
            checks = [check_run_status(result.status, result.error_summary), schema_check]
            if result.status == "SUCCESS" and snapshot is not None:
                checks.extend(run_target_checks(conn, target_schema, snapshot, contract))
            checks.append(check_declared_status(result.status, checks))
            run_log.record_checks(conn, target_schema, run_id, checks)
        result.checks = checks
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the order-to-revenue ETL.")
    parser.add_argument("--loop", type=float, metavar="SECONDS", help="run repeatedly at this interval")
    parser.add_argument("--full-refresh", action="store_true", help="rebuild every fact from the source")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    while True:
        result = run_once(full_refresh=args.full_refresh, trigger="cli")
        failed = ", ".join(c.name for c in result.failed_checks) or "none"
        logger.info(
            "run %s %s: extracted=%s loaded=%s rejected=%s failed_checks=%s",
            result.run_id, result.status, result.extracted, result.loaded, result.rejected, failed,
        )  # fmt: skip
        if args.loop is None:
            break
        time.sleep(args.loop)


if __name__ == "__main__":
    main()
