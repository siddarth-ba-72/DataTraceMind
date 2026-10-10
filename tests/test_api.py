"""HTTP surface: demo controls, read models, and access control."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from datatracemind.api.app import app
from datatracemind.config import get_settings


@pytest.fixture
def client(baseline) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def test_healthy_baseline_is_visible(client):
    assert client.get("/api/health").json()["status"] == "ok"
    summary = client.get("/api/metrics/summary").json()
    assert summary["trust"] == "trustworthy"
    assert summary["pipeline"]["last_run"]["status"] == "SUCCESS"
    assert summary["pipeline"]["transform"]["version"] == 1
    assert len(client.get("/api/transactions/live?limit=7").json()["events"]) == 7
    runs = client.get("/api/pipeline/runs").json()
    assert runs[0]["status"] == "SUCCESS" and runs[0]["checks"]
    assert client.get("/api/incidents").json() == []


def test_inject_opens_an_incident_and_an_alert(client):
    assert [s["scenario_id"] for s in client.get("/api/scenarios").json()["scenarios"]] == [
        "DS-01", "DS-02", "DS-03",
    ]  # fmt: skip
    report = client.post("/api/scenarios/DS-01/inject").json()
    incident_id = report["incident_id"]
    assert client.get("/api/scenarios").json()["active"]["scenario_id"] == "DS-01"
    assert client.get("/api/metrics/summary").json()["trust"] == "under_investigation"

    detail = client.get(f"/api/incidents/{incident_id}").json()
    assert detail["status"] == "detected"
    assert detail["events"][0]["status"] == "detected"
    assert "scenario_id" not in detail
    assert client.get(f"/api/incidents/{incident_id}/events").json() == detail["events"]

    alert = client.get("/api/alerts").json()[0]
    assert alert["incident_id"] == incident_id and alert["severity"] == "critical"
    actions = [entry["action"] for entry in client.get("/api/audit").json()]
    assert {"demo.reset", "scenario.inject", "incident.open"} <= set(actions)


def test_errors_map_to_http_status_codes(client):
    assert client.post("/api/scenarios/DS-99/inject").status_code == 404
    assert client.get("/api/incidents/INC-9999").status_code == 404
    assert client.post("/api/scenarios/DS-03/inject").status_code == 200
    assert client.post("/api/scenarios/DS-01/inject").status_code == 409
    assert client.post("/api/demo/reset").status_code == 200
    assert client.get("/api/incidents").json() == []


def test_traffic_can_be_started_and_stopped(client):
    assert client.post("/api/demo/traffic/start").json()["started"] is True
    assert client.get("/api/health").json()["traffic_running"] is True
    assert client.post("/api/demo/traffic/stop").json()["stopped"] is True
    assert client.get("/api/health").json()["traffic_running"] is False


def test_demo_access_code_guards_mutating_routes(client, monkeypatch):
    monkeypatch.setenv("DTM_DEMO_ACCESS_CODE", "let-me-in")
    get_settings.cache_clear()
    try:
        assert client.post("/api/scenarios/DS-01/inject").status_code == 401
        assert client.post("/api/demo/reset", headers={"X-Demo-Access-Code": "wrong"}).status_code == 401
        assert client.get("/api/metrics/summary").status_code == 200
        allowed = client.post("/api/scenarios/DS-01/inject", headers={"X-Demo-Access-Code": "let-me-in"})
        assert allowed.status_code == 200
    finally:
        monkeypatch.delenv("DTM_DEMO_ACCESS_CODE")
        get_settings.cache_clear()
