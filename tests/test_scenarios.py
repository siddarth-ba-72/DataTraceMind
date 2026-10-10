"""Scenario lab and detection: each fault reproduces its documented symptom."""

from __future__ import annotations

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


def test_public_scenario_view_and_incident_carry_no_answer_key(baseline):
    report = demo.inject("DS-01")
    lab = demo.lab_status()
    for view in lab["scenarios"]:
        assert set(view) == {"scenario_id", "name", "description", "failure_type", "priority"}
    assert "scenario_id" not in incident(report["incident_id"])
