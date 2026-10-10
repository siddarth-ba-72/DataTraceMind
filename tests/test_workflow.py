"""The investigation workflow: routing, retries, escalation, and evidence rules."""

from __future__ import annotations

import pytest

from datatracemind import db, demo
from datatracemind.llm.base import ProviderError, RequiredToolMissing, UsageLimitReached
from datatracemind.monitoring.summary import metrics_summary
from datatracemind.orchestration import routing, supervisor
from datatracemind.orchestration.supervisor import InvestigationRefused, run_investigation
from etl import contract, transform_store
from tests.support import (
    CATEGORY,
    FIX,
    WRONG_FIX,
    ScriptedProvider,
    active_transform,
    diagnosis,
    incident,
    raises,
    repair,
    statuses,
)


@pytest.fixture
def ds01(baseline) -> str:
    return demo.inject("DS-01")["incident_id"]


def test_ds01_runs_from_detection_to_awaiting_approval(ds01):
    provider = ScriptedProvider()
    assert run_investigation(ds01, provider=provider) == "awaiting_approval"
    found = incident(ds01)
    assert statuses(ds01) == [
        "detected", "investigating", "diagnosed", "repair_proposed", "validating", "awaiting_approval",
    ]  # fmt: skip
    # Three roles gathered evidence; every item came from a real tool call.
    assert {e["collected_by"] for e in found["evidence"]} == {
        "source_investigator", "pipeline_investigator", "contract_agent", "repair_agent",
    }  # fmt: skip
    assert all(e["result_summary"] for e in found["evidence"])
    assert found["state"]["selected_root_cause"]["category"] == "refund_logic"
    assert found["state"]["selected_root_cause"]["supporting_evidence"]
    assert found["proposal"]["status"] == "validated"
    assert "-  refund_statuses: [COMPLETED]" in found["proposal"]["diff"]
    assert "+  refund_statuses: [SUCCEEDED]" in found["proposal"]["diff"]
    validation = found["proposal"]["validation_result"]
    assert validation["passed"] and validation["patch_hash"] == found["proposal"]["patch_hash"]
    assert validation["before_after"]["before"]["reconciled"] is False
    assert validation["before_after"]["after"]["reconciled"] is True
    assert all(case["passed"] for case in validation["regression"])
    # Nothing was applied: the demo environment is still broken and still flagged.
    assert metrics_summary()["trust"] == "under_investigation"
    assert metrics_summary()["reconciliation"]["reconciled"] is False


def test_run_record_carries_the_numbers_for_before_after_comparison(ds01):
    run_investigation(ds01, provider=ScriptedProvider())
    run = incident(ds01)["investigation_runs"][0]
    assert run["status"] == "awaiting_approval"
    assert run["workflow_version"] == supervisor.WORKFLOW_VERSION
    assert run["role_turns"] == 4  # two investigators, the analyst, the repair agent
    assert run["tool_calls"] == 5 and run["failed_tool_calls"] == 0 and run["retries"] == 0
    assert run["input_tokens"] == 4000 and run["cached_input_tokens"] == 1600
    assert float(run["est_cost_usd"]) == pytest.approx(0.004)


def test_prompts_never_contain_the_scenario_or_its_answer_key(ds01):
    provider = ScriptedProvider()
    run_investigation(ds01, provider=provider)
    for call in provider.calls:
        assert "DS-01" not in call.prompt
        assert "scenario" not in call.prompt.lower()
        assert "ground_truth" not in call.prompt


def test_a_failed_turn_is_retried_once_and_its_evidence_is_kept(ds01):
    provider = ScriptedProvider(
        {"source_investigator": [raises(RequiredToolMissing("required tool call(s) did not succeed"))]}
    )
    assert run_investigation(ds01, provider=provider) == "awaiting_approval"
    first, second = (c.prompt for c in provider.calls if c.role == "source_investigator")
    assert "A PREVIOUS ATTEMPT AT THIS TASK WAS REJECTED" not in first
    assert "A PREVIOUS ATTEMPT AT THIS TASK WAS REJECTED" in second
    run = incident(ds01)["investigation_runs"][0]
    assert run["retries"] == 1 and run["role_turns"] == 5
    events = [e for e in incident(ds01)["events"] if e["agent"] == "source_investigator"]
    assert [e["status"] for e in events if e["event_type"] == "agent_run"] == [
        "running", "failed", "completed",
    ]  # fmt: skip


def test_an_analysis_citing_unknown_evidence_is_rejected_and_retried(ds01):
    provider = ScriptedProvider({"root_cause_analyst": [diagnosis(cite=lambda: ["E-999"])]})
    assert run_investigation(ds01, provider=provider) == "awaiting_approval"
    assert provider.turns("root_cause_analyst") == 2
    retry_prompt = [c.prompt for c in provider.calls if c.role == "root_cause_analyst"][1]
    assert "these evidence IDs do not exist: ['E-999']" in retry_prompt


def test_a_confirmed_cause_must_cite_supporting_evidence(ds01):
    uncited = diagnosis(cite=lambda: [])
    provider = ScriptedProvider({"root_cause_analyst": [uncited, uncited]})
    assert run_investigation(ds01, provider=provider) == "escalated"
    assert incident(ds01)["status"] == "escalated"
    assert incident(ds01)["proposal"] is None


def test_unconfirmed_analysis_triggers_one_targeted_round_then_diagnoses(ds01):
    provider = ScriptedProvider(
        {"root_cause_analyst": [diagnosis(confirmed=False, needs_more_evidence=True), diagnosis()]}
    )
    assert run_investigation(ds01, provider=provider) == "awaiting_approval"
    assert provider.turns("targeted_investigator") == 1
    assert provider.turns("root_cause_analyst") == 2
    assert "read the pipeline checkpoint" in next(
        c.prompt for c in provider.calls if c.role == "targeted_investigator"
    )
    assert any(e["collected_by"] == "targeted_investigator" for e in incident(ds01)["evidence"])


def test_still_unconfirmed_after_the_extra_round_escalates(ds01):
    unsure = diagnosis(confirmed=False, needs_more_evidence=True)
    provider = ScriptedProvider({"root_cause_analyst": [unsure, unsure]})
    assert run_investigation(ds01, provider=provider) == "escalated"
    assert provider.turns("targeted_investigator") == 1
    assert provider.turns("repair_agent") == 0
    assert "does not confirm a root cause" in incident(ds01)["state"]["errors"][0]["reason"]


def test_low_confidence_is_not_treated_as_confirmed(ds01):
    provider = ScriptedProvider({"root_cause_analyst": [diagnosis(confidence=0.4)]})
    assert run_investigation(ds01, provider=provider) == "escalated"
    assert provider.turns("repair_agent") == 0


def test_business_rule_ambiguity_escalates_instead_of_guessing(ds01):
    provider = ScriptedProvider(
        {"root_cause_analyst": [diagnosis(ambiguity="the contract does not say how chargebacks are treated")]}
    )
    assert run_investigation(ds01, provider=provider) == "escalated"
    assert provider.turns("repair_agent") == 0
    assert "needs a human decision" in incident(ds01)["events"][-1]["summary"]


def test_missing_contract_escalates_before_any_diagnosis(ds01, monkeypatch):
    monkeypatch.setattr(contract, "METRIC_CONTRACTS", {})
    provider = ScriptedProvider()
    assert run_investigation(ds01, provider=provider) == "escalated"
    assert provider.calls == []  # escalated before spending any model usage
    assert "No approved definition exists for metric contract 'net_revenue'" in incident(ds01)["events"][-1]["summary"]
    contract_events = [e for e in incident(ds01)["events"] if e["agent"] == "contract_agent"]
    assert contract_events[0]["event_type"] == "tool_call" and contract_events[0]["status"] == "failed"


def test_a_repair_that_fails_staging_is_revised_once(ds01):
    provider = ScriptedProvider({"repair_agent": [repair(WRONG_FIX), repair(FIX["DS-01"])]})
    assert run_investigation(ds01, provider=provider) == "awaiting_approval"
    assert statuses(ds01) == [
        "detected", "investigating", "diagnosed", "repair_proposed", "validating", "validation_failed",
        "repair_proposed", "validating", "awaiting_approval",
    ]  # fmt: skip
    revision_prompt = [c.prompt for c in provider.calls if c.role == "repair_agent"][1]
    assert "YOUR PREVIOUS PROPOSAL WAS NOT ACCEPTED" in revision_prompt
    assert "quality check failed in staging: net_revenue_reconciliation" in revision_prompt
    proposals = db.fetch_all("SELECT proposal_id, status FROM dtm.repair_proposals ORDER BY proposal_id")
    assert [p["status"] for p in proposals] == ["superseded", "validated"]


def test_two_failed_repairs_escalate_and_leave_nothing_approvable(ds01):
    provider = ScriptedProvider({"repair_agent": [repair(WRONG_FIX), repair(WRONG_FIX)]})
    assert run_investigation(ds01, provider=provider) == "escalated"
    assert incident(ds01)["status"] == "escalated"
    assert incident(ds01)["proposal"]["status"] == "validation_failed"


def test_repair_agent_may_decline_and_escalate(ds01):
    provider = ScriptedProvider({"repair_agent": [repair(can_repair=False)]})
    assert run_investigation(ds01, provider=provider) == "escalated"
    assert incident(ds01)["proposal"] is None
    assert "proposes no automatic fix" in incident(ds01)["events"][-1]["summary"]


def test_usage_limit_is_not_retried_and_escalates_with_the_reason(ds01):
    provider = ScriptedProvider({"source_investigator": [raises(UsageLimitReached("usage limit reached"))]})
    assert run_investigation(ds01, provider=provider) == "escalated"
    assert provider.turns("source_investigator") == 1
    assert "UsageLimitReached" in incident(ds01)["events"][-1]["summary"]


def test_one_investigator_failing_twice_does_not_stop_the_other(ds01):
    broken = raises(ProviderError("provider unavailable"))
    provider = ScriptedProvider({"pipeline_investigator": [broken, broken]})
    assert run_investigation(ds01, provider=provider) == "awaiting_approval"
    analyst_prompt = next(c.prompt for c in provider.calls if c.role == "root_cause_analyst")
    assert '"failed": "provider unavailable"' in analyst_prompt


def test_both_investigators_failing_escalates(ds01):
    broken = raises(ProviderError("provider unavailable"))
    provider = ScriptedProvider({"pipeline_investigator": [broken, broken], "source_investigator": [broken, broken]})
    assert run_investigation(ds01, provider=provider) == "escalated"
    assert provider.turns("root_cause_analyst") == 0


def test_investigation_is_refused_without_model_credentials(ds01):
    with pytest.raises(InvestigationRefused) as refusal:
        supervisor.start_investigation(ds01, provider=ScriptedProvider(available=False))
    assert refusal.value.status_code == 503
    assert "codex login" in str(refusal.value)
    assert incident(ds01)["status"] == "detected"


def test_investigation_cap_refuses_further_runs(ds01, monkeypatch):
    run_investigation(ds01, provider=ScriptedProvider({"root_cause_analyst": [diagnosis(ambiguity="x")]}))
    monkeypatch.setenv("DTM_INVESTIGATIONS_PER_HOUR", "1")
    supervisor.get_settings.cache_clear()
    try:
        with pytest.raises(InvestigationRefused) as refusal:
            supervisor.start_investigation(ds01, provider=ScriptedProvider())
        assert refusal.value.status_code == 429
    finally:
        monkeypatch.delenv("DTM_INVESTIGATIONS_PER_HOUR")
        supervisor.get_settings.cache_clear()


def test_an_escalated_incident_can_be_investigated_again(ds01):
    run_investigation(ds01, provider=ScriptedProvider({"root_cause_analyst": [diagnosis(ambiguity="unclear")]}))
    assert incident(ds01)["status"] == "escalated"
    assert run_investigation(ds01, provider=ScriptedProvider()) == "awaiting_approval"
    assert len(incident(ds01)["investigation_runs"]) == 2


@pytest.mark.parametrize("scenario_id", sorted(set(FIX) - {"DS-01"}))
def test_other_scenarios_use_their_own_plan_and_validate(baseline, scenario_id):
    incident_id = demo.inject(scenario_id)["incident_id"]
    provider = ScriptedProvider(
        {"root_cause_analyst": [diagnosis(CATEGORY[scenario_id])], "repair_agent": [repair(FIX[scenario_id])]}
    )
    assert run_investigation(incident_id, provider=provider) == "awaiting_approval"
    plan = routing.plan_for(incident(incident_id)["incident_type"])
    source_call = next(c for c in provider.calls if c.role == "source_investigator")
    assert source_call.required_tools == plan.source_required
    assert incident(incident_id)["proposal"]["validation_result"]["passed"] is True


def test_ds04_is_investigated_as_duplicate_records(baseline):
    incident_id = demo.inject("DS-04")["incident_id"]
    provider = ScriptedProvider(
        {"root_cause_analyst": [diagnosis("duplicate_load")], "repair_agent": [repair(FIX["DS-04"])]}
    )
    assert run_investigation(incident_id, provider=provider) == "awaiting_approval"
    source_call = next(c for c in provider.calls if c.role == "source_investigator")
    assert source_call.required_tools == ("find_duplicate_business_keys",)
    duplicates = next(e for e in incident(incident_id)["evidence"] if e["query_or_tool"] == "find_duplicate_business_keys")
    assert duplicates["result_summary"]["duplicated_keys"] > 0


def test_a_new_source_status_escalates_as_a_business_rule_question(baseline):
    """DS-06: no contract says how a chargeback counts, so nobody proposes a patch."""
    incident_id = demo.inject("DS-06")["incident_id"]
    question = "The net_revenue contract does not define refund_status CHARGEBACK."
    provider = ScriptedProvider(
        {"root_cause_analyst": [diagnosis("schema_drift", confirmed=False, confidence=0.8, ambiguity=question)]}
    )
    assert run_investigation(incident_id, provider=provider) == "escalated"
    found = incident(incident_id)
    assert found["status"] == "escalated" and found["proposal"] is None
    assert question in found["state"]["errors"][-1]["reason"]
    assert provider.turns("repair_agent") == 0
    assert metrics_summary()["trust"] == "under_investigation"


def test_a_defect_in_the_source_escalates_when_the_repair_agent_declines(baseline):
    """DS-09: the cause is confirmed, and the right fix is not a transformation change."""
    incident_id = demo.inject("DS-09")["incident_id"]
    provider = ScriptedProvider(
        {"root_cause_analyst": [diagnosis("source_data_defect")], "repair_agent": [repair(can_repair=False)]}
    )
    assert run_investigation(incident_id, provider=provider) == "escalated"
    found = incident(incident_id)
    assert statuses(incident_id) == ["detected", "investigating", "diagnosed", "escalated"]
    assert found["state"]["selected_root_cause"]["category"] == "source_data_defect"
    assert found["proposal"] is None
    assert "proposes no automatic fix" in found["state"]["errors"][-1]["reason"]
    assert active_transform() == transform_store.baseline_content()
