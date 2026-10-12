"""The deterministic data path: seed, ETL, quality checks, and reset."""

from __future__ import annotations

import psycopg
import pytest

from datatracemind import db, demo
from datatracemind.config import get_settings
from datatracemind.monitoring.summary import live_transactions, metrics_summary
from etl import pipeline
from simulator.generator import Simulator


def test_baseline_reconciles_and_every_check_passes(baseline):
    assert baseline["run"]["status"] == "SUCCESS"
    assert baseline["run"]["failed_checks"] == []
    assert baseline["run"]["rejected"] == 0
    summary = metrics_summary()
    assert summary["reconciliation"]["reconciled"] is True
    assert summary["trust"] == "trustworthy"
    assert set(summary["reconciliation"]["by_currency"]) == {"INR", "USD"}


def test_reset_restores_the_same_baseline(baseline):
    first = baseline["metrics"]["by_currency"]
    orders = metrics_summary()["order_counts"]
    with db.connect() as conn:
        Simulator(1).place_order(conn)
    pipeline.run_once(trigger="test")
    assert metrics_summary()["order_counts"] != orders
    assert demo.reset()["metrics"]["by_currency"] == first
    assert metrics_summary()["order_counts"] == orders


def test_rerun_and_full_refresh_do_not_duplicate(baseline):
    before = db.fetch_one("SELECT count(*) AS n FROM analytics.fact_payments")["n"]
    incremental = pipeline.run_once(trigger="test")
    full = pipeline.run_once(trigger="test", full_refresh=True)
    assert incremental.status == full.status == "SUCCESS"
    assert incremental.full_refresh is False and incremental.extracted == 0
    assert full.full_refresh is True and full.extracted > 0
    assert not incremental.failed_checks and not full.failed_checks
    assert db.fetch_one("SELECT count(*) AS n FROM analytics.fact_payments")["n"] == before
    assert metrics_summary()["reconciliation"]["by_currency"] == baseline["metrics"]["by_currency"]


def test_incremental_run_loads_new_and_changed_transactions(baseline):
    simulator = Simulator(get_settings().demo_seed + 1)
    with db.connect() as conn:
        actions = {simulator.tick(conn) for _ in range(60)}
    # The stream must exercise updates to existing rows, not only inserts.
    assert actions == {"order_placed", "refund_requested", "refund_settled"}
    result = pipeline.run_once(trigger="test")
    assert result.status == "SUCCESS"
    assert result.full_refresh is False
    assert 0 < result.extracted < 400
    assert result.failed_checks == []
    assert metrics_summary()["reconciliation"]["by_currency"] != baseline["metrics"]["by_currency"]


def test_run_log_and_quality_results_are_recorded(baseline):
    with db.connect() as conn:
        runs = pipeline.run_log.recent_runs(conn, db.ANALYTICS_SCHEMA)
    assert runs[0]["status"] == "SUCCESS"
    assert runs[0]["watermark"] is not None
    assert runs[0]["loaded_count"] == runs[0]["extracted_count"] > 0
    names = {c["check_name"] for c in runs[0]["checks"]}
    assert {"net_revenue_reconciliation", "unique_business_keys", "declared_status_consistent"} <= names
    assert all(c["passed"] for c in runs[0]["checks"])


def test_live_transactions_lists_recent_source_activity(baseline):
    live = live_transactions(limit=5)
    assert live["totals"]["orders"] == 320
    assert len(live["events"]) == 5
    assert {e["kind"] for e in live["events"]} <= {"order", "payment", "refund"}


def test_readonly_role_cannot_write_or_read_application_state(baseline):
    assert db.fetch_one("SELECT count(*) AS n FROM oltp.orders", readonly=True)["n"] == 320
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.fetch_all("SELECT * FROM dtm.incidents", readonly=True)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.fetch_all("SELECT * FROM dtm.lab_state", readonly=True)
    with pytest.raises(psycopg.Error), db.connect(readonly=True) as conn:
        conn.execute("DELETE FROM oltp.refunds")
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.connect(readonly=True) as conn:
        # Even with the session's read-only default switched off, the role has no write grant.
        conn.execute("SET default_transaction_read_only = off")
        conn.execute("COMMIT")
        conn.execute("DELETE FROM oltp.refunds")
    with db.connect(readonly=True) as conn:
        # Both are defaults of the role, so they hold however the connection is made.
        assert conn.execute("SHOW default_transaction_read_only").fetchone()["default_transaction_read_only"] == "on"
        assert conn.execute("SHOW statement_timeout").fetchone()["statement_timeout"] == "5s"


def test_pipeline_lock_is_reentrant_and_excludes_other_sessions(baseline):
    key = pipeline._ADVISORY_LOCK_KEY
    probe = "SELECT pg_try_advisory_lock(%s) AS acquired"
    with pipeline.pipeline_lock(), pipeline.pipeline_lock():
        # Another database session (as another process would be) cannot take it.
        assert db.fetch_one(probe, (key,))["acquired"] is False
        assert pipeline.run_once(trigger="test").status == "SUCCESS"  # run_once re-enters the lock
    with db.connect() as conn:
        assert conn.execute(probe, (key,)).fetchone()["acquired"] is True
