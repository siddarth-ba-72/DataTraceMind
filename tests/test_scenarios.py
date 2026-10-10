"""Scenario lab and detection: each fault reproduces its documented symptom."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from datatracemind import db, demo, store
from datatracemind.monitoring import monitor
from datatracemind.scenarios import SCENARIOS
from etl import pipeline


def failed_checks(report: dict) -> set[str]:
    return set(report["run"]["failed_checks"])


def incident(incident_id: str) -> dict:
    return store.get_incident(incident_id)


@pytest.mark.parametrize("scenario_id", sorted(SCENARIOS))
def test_each_scenario_fails_exactly_the_checks_its_answer_key_expects(baseline, scenario_id):
    report = demo.inject(scenario_id)
    expected = set(SCENARIOS[scenario_id].ground_truth()["expected_failed_checks"])
    assert expected <= failed_checks(report)
    assert report["incident_id"] == "INC-0001"


def test_ds01_is_silent_and_overstates_revenue_by_the_succeeded_refunds(baseline):
    report = demo.inject("DS-01")
    assert report["run"]["status"] == "SUCCESS"
    refunds = db.fetch_all(
        "SELECT o.currency, sum(r.refund_amount) AS total FROM oltp.refunds r JOIN oltp.orders o USING (order_id) "
        "WHERE r.refund_status = 'SUCCEEDED' GROUP BY 1"
    )
    after = report["post_injection_metrics"]["by_currency"]
    for row in refunds:
        assert after[row["currency"]]["net_difference"] == pytest.approx(float(row["total"]))
        assert after[row["currency"]]["analytics"]["gross"] == after[row["currency"]]["source"]["gross"]
    found = incident(report["incident_id"])
    assert found["incident_type"] == "revenue_mismatch"
    assert found["severity"] == "critical"
    assert found["status"] == "detected"
    assert "reported SUCCESS" in found["summary"]
    assert "INR higher" in found["summary"] and "USD higher" in found["summary"]


def test_ds02_fails_the_run_and_leaves_analytics_untouched(baseline):
    report = demo.inject("DS-02")
    assert report["run"]["status"] == "FAILED"
    assert "total_amount" in report["run"]["error_summary"]
    assert report["post_injection_metrics"]["by_currency"] == report["baseline_metrics"]["by_currency"]
    found = incident(report["incident_id"])
    assert found["incident_type"] == "pipeline_failure"
    schema_check = next(c for c in found["trigger"]["failed_checks"] if c["check_name"] == "source_schema_contract")
    assert schema_check["observed_value"]["missing_columns"] == ["orders.total_amount"]
    assert schema_check["observed_value"]["unexpected_columns"] == ["orders.order_total"]


def test_ds03_drops_only_orders_without_a_payment_and_keeps_revenue_right(baseline):
    report = demo.inject("DS-03")
    assert report["run"]["status"] == "SUCCESS"
    assert "net_revenue_reconciliation" not in failed_checks(report)
    unpaid = db.fetch_one(
        "SELECT count(*) AS n FROM oltp.orders o WHERE NOT EXISTS "
        "(SELECT 1 FROM oltp.payments p WHERE p.order_id = o.order_id)"
    )["n"]
    found = incident(report["incident_id"])
    completeness = next(
        c for c in found["trigger"]["failed_checks"] if c["check_name"] == "fact_orders_completeness"
    )
    assert found["incident_type"] == "record_loss"
    assert completeness["observed_value"]["missing_in_analytics"] == unpaid > 0


def test_scenarios_reset_and_replay_identically(baseline):
    first = demo.inject("DS-01")
    with pytest.raises(demo.LabError):
        demo.inject("DS-03")
    demo.reset()
    assert db.fetch_all("SELECT * FROM dtm.incidents") == []
    second = demo.inject("DS-01")
    assert second["post_injection_metrics"] == first["post_injection_metrics"]
    assert second["incident_id"] == "INC-0001"


def test_repeated_failures_do_not_open_a_second_incident(baseline):
    demo.inject("DS-01")
    again = pipeline.run_once(trigger="test")
    assert again.failed_checks
    assert monitor.evaluate_run(again) is None
    assert len(db.fetch_all("SELECT incident_id FROM dtm.incidents")) == 1
    assert len(db.fetch_all("SELECT alert_id FROM dtm.alerts")) == 1


def test_healthy_run_opens_no_incident(baseline):
    result = pipeline.run_once(trigger="test")
    assert monitor.evaluate_run(result) is None
    assert monitor.evaluate_freshness() is None
    assert db.fetch_all("SELECT * FROM dtm.incidents") == []


def test_incident_lifecycle_refuses_illegal_transitions(baseline):
    incident_id = demo.inject("DS-01")["incident_id"]
    with db.connect() as conn, pytest.raises(store.IllegalTransition):
        store.set_status(conn, incident_id, "resolved", actor="test", note="skip the workflow")
    with db.connect() as conn, pytest.raises(store.IllegalTransition):
        store.set_status(conn, incident_id, "applying", actor="test", note="skip approval")
    assert incident(incident_id)["status"] == "detected"


def test_ds04_passes_the_deploy_run_then_duplicates_the_replayed_batch(baseline):
    report = demo.inject("DS-04")
    deploy, retry = report["runs"]
    assert deploy["status"] == retry["status"] == "SUCCESS"
    assert deploy["full_refresh"] is True and deploy["failed_checks"] == []
    assert retry["full_refresh"] is False and retry["loaded"] > 0
    duplicated = db.fetch_one(
        "SELECT count(*) AS n FROM (SELECT order_id FROM analytics.fact_orders GROUP BY 1 HAVING count(*) > 1) d"
    )["n"]
    assert duplicated >= 40
    # Nothing in the source was touched: the source side of the comparison is the baseline's.
    for currency, sides in report["post_injection_metrics"]["by_currency"].items():
        assert sides["source"] == report["baseline_metrics"]["by_currency"][currency]["source"]
        assert sides["net_difference"] > 0
    found = incident(report["incident_id"])
    assert found["incident_type"] == "duplicate_records" and found["severity"] == "critical"
    assert "duplicate fact rows" in found["summary"] and "INR higher" in found["summary"]
    assert found["trigger"]["etl_run_id"] == retry["run_id"]


def test_ds06_rejects_only_the_rows_with_the_unknown_status_and_keeps_revenue_right(baseline):
    report = demo.inject("DS-06")
    assert report["run"]["status"] == "SUCCESS" and report["run"]["rejected"] == 6
    assert "net_revenue_reconciliation" not in failed_checks(report)
    assert report["run"]["transform_version"] == 1
    found = incident(report["incident_id"])
    completeness = next(
        c for c in found["trigger"]["failed_checks"] if c["check_name"] == "fact_refunds_completeness"
    )
    assert found["incident_type"] == "record_loss"
    assert completeness["observed_value"]["missing_in_analytics"] == 6
    missing = completeness["observed_value"]["missing_sample"]
    statuses = db.fetch_all("SELECT DISTINCT refund_status FROM oltp.refunds WHERE refund_id = ANY(%s)", (missing,))
    assert [row["refund_status"] for row in statuses] == ["CHARGEBACK"]


def test_ds07_moves_revenue_between_days_without_changing_any_total(baseline):
    report = demo.inject("DS-07")
    assert report["run"]["status"] == "SUCCESS"
    assert failed_checks(report) == {"net_revenue_reconciliation", "declared_status_consistent"}
    after = report["post_injection_metrics"]
    assert all(sides["within_tolerance"] for sides in after["by_currency"].values())
    assert after["mismatched_day_count"] > 0 and after["reconciled"] is False
    found = incident(report["incident_id"])
    assert found["incident_type"] == "revenue_mismatch" and found["severity"] == "high"
    assert "wrong day" in found["summary"] and "totals reconcile" in found["summary"]


def test_ds09_rejects_orders_without_an_amount_and_understates_revenue_by_their_payments(baseline):
    report = demo.inject("DS-09")
    assert report["run"]["status"] == "SUCCESS" and report["run"]["transform_version"] == 1
    nulled = [row["order_id"] for row in db.fetch_all("SELECT order_id FROM oltp.orders WHERE total_amount IS NULL")]
    assert len(nulled) == 8
    captured = db.fetch_all(
        "SELECT o.currency, sum(p.amount) AS total FROM oltp.payments p JOIN oltp.orders o USING (order_id) "
        "WHERE p.payment_status = 'CAPTURED' AND o.order_id = ANY(%s) GROUP BY 1",
        (nulled,),
    )
    after = report["post_injection_metrics"]["by_currency"]
    for row in captured:
        assert after[row["currency"]]["net_difference"] == pytest.approx(-float(row["total"]))
    assert db.fetch_all("SELECT 1 FROM analytics.fact_orders WHERE order_id = ANY(%s)", (nulled,)) == []
    found = incident(report["incident_id"])
    assert found["incident_type"] == "revenue_mismatch" and "INR lower" in found["summary"]


def test_ds16_over_subtracts_only_partial_refunds(baseline):
    report = demo.inject("DS-16")
    assert report["run"]["status"] == "SUCCESS"
    excess = db.fetch_all(
        "SELECT o.currency, sum(o.total_amount - r.refund_amount) AS total "
        "FROM oltp.refunds r JOIN oltp.orders o USING (order_id) WHERE r.refund_status = 'SUCCEEDED' GROUP BY 1"
    )
    after = report["post_injection_metrics"]["by_currency"]
    for row in excess:
        assert row["total"] > 0
        assert after[row["currency"]]["net_difference"] == pytest.approx(-float(row["total"]))
        assert after[row["currency"]]["analytics"]["gross"] == after[row["currency"]]["source"]["gross"]
    assert incident(report["incident_id"])["incident_type"] == "revenue_mismatch"


def test_ds17_overstates_revenue_by_exactly_the_failed_payments(baseline):
    report = demo.inject("DS-17")
    assert report["run"]["status"] == "SUCCESS"
    failed = db.fetch_all(
        "SELECT o.currency, sum(p.amount) AS total FROM oltp.payments p JOIN oltp.orders o USING (order_id) "
        "WHERE p.payment_status = 'FAILED' GROUP BY 1"
    )
    after = report["post_injection_metrics"]["by_currency"]
    for row in failed:
        assert after[row["currency"]]["net_difference"] == pytest.approx(float(row["total"]))
        assert after[row["currency"]]["analytics"]["refunds"] == after[row["currency"]]["source"]["refunds"]
    assert incident(report["incident_id"])["severity"] == "critical"


def test_a_source_side_fault_is_met_by_a_scheduled_run_not_a_deploy(baseline):
    demo.inject("DS-06")
    triggers = [row["trigger"] for row in db.fetch_all("SELECT trigger FROM analytics.etl_run_log ORDER BY run_id")]
    assert triggers == ["reset", "schedule"]
    demo.reset()
    demo.inject("DS-04")
    triggers = [row["trigger"] for row in db.fetch_all("SELECT trigger FROM analytics.etl_run_log ORDER BY run_id")]
    assert triggers == ["reset", "post_deploy", "retry"]


def test_lab_status_keeps_the_report_of_the_injection(baseline):
    assert demo.lab_status()["active"]["scenario_id"] is None
    report = demo.inject("DS-04")
    active = demo.lab_status()["active"]
    assert active["scenario_id"] == "DS-04"
    assert active["injection_report"] == {"runs": report["runs"], "incident_id": report["incident_id"]}
    assert demo.lab_status()["environment"]["seed_orders"] == 320


def test_public_scenario_view_explains_the_fault_without_the_scoring_key(baseline):
    report = demo.inject("DS-01")
    lab = demo.lab_status()
    for view in lab["scenarios"]:
        truth = SCENARIOS[view["scenario_id"]].ground_truth()
        assert view["etl_reports"] in ("SUCCESS", "FAILED")
        assert view["resolution"] == ("escalation" if truth["expected_outcome"] == "escalated" else "repair")
        assert all(view[field] for field in ("injection", "impact", "symptoms", "resolution_note"))
        assert view["expected_failed_checks"] == truth["expected_failed_checks"]
        # What the evaluator scores a diagnosis against stays out of the API.
        assert not {"root_cause_category", "implicated_field", "key_evidence_tools", "expected_repair"} & set(view)
    assert "scenario_id" not in incident(report["incident_id"])


def _imported_modules(path: Path) -> set[str]:
    modules = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


def test_nothing_an_agent_runs_imports_the_scenario_lab():
    """The briefings and answer keys live in the lab; no code on the agent's side may load them."""
    root = Path(demo.__file__).parent
    off_limits = ("datatracemind.scenarios", "datatracemind.demo", "datatracemind.evaluation")
    offenders = [
        str(path.relative_to(root))
        for package in ("tools", "agents", "llm", "orchestration", "repair", "monitoring")
        for path in sorted((root / package).rglob("*.py"))
        if any(module.startswith(off_limits) for module in _imported_modules(path))
    ]
    assert offenders == []
