"""The repair lifecycle: validation, approval bound to a patch hash, apply, verify."""

from __future__ import annotations

import pytest

from datatracemind import db, demo
from datatracemind.config import TRANSFORM_FILE
from datatracemind.models import RepairPlan
from datatracemind.monitoring.summary import metrics_summary
from datatracemind.orchestration import supervisor
from datatracemind.orchestration.supervisor import run_investigation
from datatracemind.repair import approval, executor, patch_store, validation
from datatracemind.repair.approval import ApprovalRefused
from datatracemind.repair.executor import ApplyRefused
from datatracemind.repair.patch_store import PatchRejected
from etl import pipeline, transform_store
from tests.support import FIX, WRONG_FIX, ScriptedProvider, active_transform, diagnosis, incident, repair, statuses

OTHER_HASH = "0" * 64


@pytest.fixture
def awaiting(baseline) -> str:
    """DS-01 investigated up to a validated proposal that awaits approval."""
    incident_id = demo.inject("DS-01")["incident_id"]
    assert run_investigation(incident_id, provider=ScriptedProvider()) == "awaiting_approval"
    return incident_id


def current_hash(incident_id: str) -> str:
    return incident(incident_id)["proposal"]["patch_hash"]


def plan(content: str, target_file: str = TRANSFORM_FILE) -> RepairPlan:
    return RepairPlan(
        can_repair=True, escalation_reason="", target_file=target_file, new_content=content, rationale="r",
        risk="r", rollback_plan="r", affected_metrics=[], regression_cases=[],
    )  # fmt: skip


def test_approved_patch_is_applied_verified_and_the_incident_resolves(awaiting):
    outcome = supervisor.approve_and_apply(awaiting, current_hash(awaiting), actor="Asha Rao")
    assert outcome["passed"] is True and outcome["failed_checks"] == []
    found = incident(awaiting)
    assert found["status"] == "resolved" and found["resolved_at"] is not None
    assert statuses(awaiting)[-4:] == ["awaiting_approval", "applying", "verifying", "resolved"]
    assert found["proposal"]["status"] == "verified"
    assert found["approvals"][0]["actor"] == "Asha Rao"
    assert found["approvals"][0]["patch_hash"] == found["proposal"]["patch_hash"]
    active = transform_store.get_active()
    assert active.version == found["proposal"]["applied_version"] == 3
    assert "refund_statuses: [SUCCEEDED]" in active.content
    assert active.author == "datatracemind (approved by Asha Rao)"
    summary = metrics_summary()
    assert summary["trust"] == "trustworthy" and summary["reconciliation"]["reconciled"] is True
    assert summary["reconciliation"]["by_currency"] == db.fetch_one(
        "SELECT baseline_metrics FROM dtm.lab_state"
    )["baseline_metrics"]["by_currency"]


def test_audit_trail_records_every_step_with_actor_and_hash(awaiting):
    patch = current_hash(awaiting)
    supervisor.approve_and_apply(awaiting, patch, actor="Asha Rao")
    trail = db.fetch_all("SELECT actor, action, result, details FROM dtm.audit_log ORDER BY audit_id")
    steps = [(row["action"], row["result"]) for row in trail]
    for expected in [
        ("scenario.inject", "injected"), ("incident.open", "detected"), ("repair.propose", "proposed"),
        ("repair.validate", "passed"), ("repair.approve", "approved"), ("repair.apply", "applied"),
        ("repair.verify", "verified"), ("incident.status", "resolved"),
    ]:  # fmt: skip
        assert expected in steps
    approve = next(row for row in trail if row["action"] == "repair.approve")
    apply = next(row for row in trail if row["action"] == "repair.apply")
    assert approve["actor"] == "human:Asha Rao"
    assert approve["details"]["patch_hash"] == apply["details"]["patch_hash"] == patch
    assert apply["details"]["approved_by"] == "Asha Rao"


def test_unapproved_patch_cannot_be_applied(awaiting):
    with pytest.raises(ApplyRefused, match="not approved"):
        executor.apply_approved_patch(awaiting, current_hash(awaiting))
    assert incident(awaiting)["status"] == "awaiting_approval"
    assert "refund_statuses: [COMPLETED]" in active_transform()
    refused = db.fetch_one("SELECT result, details FROM dtm.audit_log WHERE action = 'repair.apply'")
    assert refused["result"] == "refused"


def test_approval_must_name_the_current_patch_hash(awaiting):
    with pytest.raises(ApprovalRefused, match="does not match the current proposal"):
        approval.approve(awaiting, OTHER_HASH, actor="Asha Rao")
    assert incident(awaiting)["approvals"] == []
    assert incident(awaiting)["proposal"]["status"] == "validated"


def test_workflow_components_cannot_approve(awaiting):
    for actor in ("repair_agent", "supervisor", "Codex", "  ", "validator"):
        with pytest.raises(ApprovalRefused, match="named person"):
            approval.approve(awaiting, current_hash(awaiting), actor=actor)
    assert incident(awaiting)["approvals"] == []


def test_changing_the_patch_after_approval_invalidates_the_approval(awaiting):
    patch = current_hash(awaiting)
    approval.approve(awaiting, patch, actor="Asha Rao")
    # Tamper with the approved proposal's content.
    db.execute(
        "UPDATE dtm.repair_proposals SET new_content = replace(new_content, 'payment_join: left', "
        "'payment_join: inner') WHERE incident_id = %s",
        (awaiting,),
    )
    with pytest.raises(ApplyRefused, match="does not match the approved hash"):
        executor.apply_approved_patch(awaiting, patch)
    assert "payment_join: left" in active_transform() and "refund_statuses: [COMPLETED]" in active_transform()
    assert incident(awaiting)["status"] == "awaiting_approval"


def test_a_new_proposal_needs_its_own_validation_and_approval(awaiting):
    old_patch = current_hash(awaiting)
    approval.approve(awaiting, old_patch, actor="Asha Rao")
    # A restarted investigation supersedes the approved proposal.
    assert run_investigation(awaiting, provider=ScriptedProvider()) == "awaiting_approval"
    statuses_now = db.fetch_all("SELECT status FROM dtm.repair_proposals ORDER BY proposal_id")
    assert [row["status"] for row in statuses_now] == ["superseded", "validated"]
    new_patch = current_hash(awaiting)
    with pytest.raises(ApplyRefused, match="not approved"):
        executor.apply_approved_patch(awaiting, new_patch)
    supervisor.approve_and_apply(awaiting, new_patch, actor="Asha Rao")
    assert incident(awaiting)["status"] == "resolved"


def test_patch_validated_against_an_older_deployment_is_refused(awaiting):
    patch = current_hash(awaiting)
    approval.approve(awaiting, patch, actor="Asha Rao")
    with db.connect() as conn:
        transform_store.register_version(conn, active_transform() + "# touched\n", author="someone", message="edit")
    with pytest.raises(ApplyRefused, match="deployed transformation changed"):
        executor.apply_approved_patch(awaiting, patch)
    assert incident(awaiting)["status"] == "awaiting_approval"


def test_rejected_patch_is_never_applied(awaiting):
    patch = current_hash(awaiting)
    supervisor.reject(awaiting, patch, actor="Asha Rao", reason="want a second opinion")
    found = incident(awaiting)
    assert found["status"] == "rejected" and found["proposal"]["status"] == "rejected"
    assert found["approvals"][0]["decision"] == "rejected"
    assert found["approvals"][0]["reason"] == "want a second opinion"
    with pytest.raises(ApprovalRefused):
        approval.approve(awaiting, patch, actor="Asha Rao")
    with pytest.raises(ApplyRefused):
        executor.apply_approved_patch(awaiting, patch)
    assert "refund_statuses: [COMPLETED]" in active_transform()
    assert metrics_summary()["reconciliation"]["reconciled"] is False


def test_patch_that_failed_validation_cannot_be_approved_or_applied(baseline):
    incident_id = demo.inject("DS-01")["incident_id"]
    provider = ScriptedProvider({"repair_agent": [repair(WRONG_FIX), repair(WRONG_FIX)]})
    assert run_investigation(incident_id, provider=provider) == "escalated"
    patch = current_hash(incident_id)
    failures = incident(incident_id)["proposal"]["validation_result"]["failures"]
    assert "quality check failed in staging: net_revenue_reconciliation" in failures
    assert "regression case failed: refund pending" in failures
    with pytest.raises(ApprovalRefused):
        approval.approve(incident_id, patch, actor="Asha Rao")
    with pytest.raises(ApplyRefused):
        executor.apply_approved_patch(incident_id, patch)


def test_failed_post_fix_verification_rolls_back_and_does_not_resolve(awaiting, monkeypatch):
    real_run = pipeline.run_once
    patch = current_hash(awaiting)

    def failing_post_fix(**kwargs):
        result = real_run(**kwargs)
        if kwargs.get("trigger") == "post_fix":
            result.checks[0].passed = False
        return result

    monkeypatch.setattr(executor.pipeline, "run_once", failing_post_fix)
    outcome = supervisor.approve_and_apply(awaiting, patch, actor="Asha Rao")
    assert outcome["passed"] is False and outcome["rolled_back_to_version"] == 4
    found = incident(awaiting)
    assert found["status"] == "application_failed" and found["resolved_at"] is None
    assert found["proposal"]["status"] == "application_failed"
    assert "refund_statuses: [COMPLETED]" in active_transform()
    assert transform_store.get_active().author == "datatracemind (automatic rollback)"


def test_staging_validation_does_not_touch_the_demo_environment(baseline):
    incident_id = demo.inject("DS-01")["incident_id"]
    before = db.fetch_all("SELECT * FROM analytics.daily_revenue ORDER BY 1, 2")
    version = transform_store.get_active().version
    run_investigation(incident_id, provider=ScriptedProvider())
    assert db.fetch_all("SELECT * FROM analytics.daily_revenue ORDER BY 1, 2") == before
    assert transform_store.get_active().version == version
    staging = validation.staging_runs()
    assert staging[0]["trigger"] == "staging_validation" and staging[0]["status"] == "SUCCESS"


@pytest.mark.parametrize(
    "content, target, reason",
    [
        ("FIXED", "etl/pipeline.py", "only etl/transforms/order_revenue.yaml may be changed"),
        ("FIXED", "contracts/revenue.md", "only etl/transforms/order_revenue.yaml may be changed"),
        ("SAME", TRANSFORM_FILE, "identical to the deployed one"),
        ("FIXED\nload:\n  post_sql: DROP SCHEMA oltp CASCADE\n", TRANSFORM_FILE, "transformation file is invalid"),
        ("REWRITTEN", TRANSFORM_FILE, "a repair may change at most"),
    ],
)
def test_patch_guardrails_reject_unsafe_or_oversized_changes(baseline, content, target, reason):
    incident_id = demo.inject("DS-01")["incident_id"]
    deployed = active_transform()
    fixed = deployed.replace(*FIX["DS-01"])
    rewritten = "\n".join(line for line in fixed.splitlines() if not line.lstrip().startswith("#")) + "\n"
    content = content.replace("FIXED", fixed).replace("SAME", deployed).replace("REWRITTEN", rewritten)
    with pytest.raises(PatchRejected, match=reason):
        patch_store.create_proposal(incident_id, plan(content, target), cited_evidence=[])
    assert db.fetch_all("SELECT 1 FROM dtm.repair_proposals") == []


@pytest.mark.parametrize("scenario_id, category", [("DS-02", "schema_drift"), ("DS-03", "join_loss")])
def test_other_scenarios_recover_end_to_end(baseline, scenario_id, category):
    incident_id = demo.inject(scenario_id)["incident_id"]
    provider = ScriptedProvider({"root_cause_analyst": [diagnosis(category)], "repair_agent": [repair(FIX[scenario_id])]})
    assert run_investigation(incident_id, provider=provider) == "awaiting_approval"
    outcome = supervisor.approve_and_apply(incident_id, current_hash(incident_id), actor="Asha Rao")
    assert outcome["passed"] is True
    assert incident(incident_id)["status"] == "resolved"
    assert metrics_summary()["trust"] == "trustworthy"
