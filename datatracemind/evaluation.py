"""Evaluator: run one scenario end to end and score it against its answer key.

    python -m datatracemind.evaluation DS-01
    python -m datatracemind.evaluation DS-01 --approve-as "Your Name"

It resets the demo, injects the scenario, runs a real investigation, and
compares the result with the scenario's ground truth and with the
deterministic checks. This is the only place outside the scenario lab that
reads an answer key; the agents never see it.

By default the run stops at the human decision point. `--approve-as` records
an approval under the name given, by the person running this command, and then
measures recovery. The numbers of each run are written to
evaluation/results/ so two workflow versions can be compared side by side.

Each run spends model usage (four role turns on the happy path) and resets
the demo database.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime

import yaml

from datatracemind import db, demo, store
from datatracemind.config import REPO_ROOT
from datatracemind.llm import get_provider
from datatracemind.monitoring.summary import metrics_summary
from datatracemind.observability import delivery_problems, init_tracing, shutdown_tracing
from datatracemind.orchestration import supervisor
from datatracemind.repair import executor
from datatracemind.repair.executor import ApplyRefused
from datatracemind.scenarios import SCENARIOS

RESULTS_DIR = REPO_ROOT / "evaluation" / "results"


def _lookup(document: dict, dotted: str):
    for key in dotted.split("."):
        document = document[key]
    return document


def _event_time(incident: dict, status: str) -> datetime | None:
    return next(
        (e["created_at"] for e in incident["events"] if e["event_type"] == "lifecycle" and e["status"] == status),
        None,
    )


def _seconds(start: datetime | None, end: datetime | None) -> float | None:
    return round((end - start).total_seconds(), 1) if start and end else None


def score(scenario_id: str, injection: dict, incident: dict) -> dict:
    """Compare what the workflow produced with the scenario's answer key."""
    truth = SCENARIOS[scenario_id].ground_truth()
    failed = set(injection["run"]["failed_checks"])
    root_cause = incident["state"].get("selected_root_cause") or {}
    proposal = incident["proposal"] or {}
    validation = proposal.get("validation_result") or {}

    cited = set(root_cause.get("supporting_evidence", []))
    cited_tools = {e["query_or_tool"] for e in incident["evidence"] if e["evidence_id"] in cited}
    groups = truth["key_evidence_tools"]
    covered = [bool(cited_tools & set(group)) for group in groups]

    repair_fields = {}
    if proposal.get("new_content"):
        patched = yaml.safe_load(proposal["new_content"])
        for field, expected in truth["expected_repair"].items():
            try:
                actual = _lookup(patched, field)
            except (KeyError, TypeError):
                actual = None
            repair_fields[field] = {"expected": expected, "actual": actual, "correct": actual == expected}

    return {
        "detected": injection["incident_id"] is not None and set(truth["expected_failed_checks"]) <= failed,
        "silent_failure": injection["run"]["status"] == "SUCCESS",
        "root_cause_category": {
            "expected": truth["root_cause_category"],
            "actual": root_cause.get("category"),
            "correct": root_cause.get("category") == truth["root_cause_category"],
        },
        "implicated_component": {
            "expected_field": truth["implicated_field"],
            "actual": root_cause.get("implicated_component"),
            "correct": truth["implicated_field"].split(".")[-1] in (root_cause.get("implicated_component") or ""),
        },
        "evidence_coverage": {
            "cited_evidence": sorted(cited),
            "cited_tools": sorted(cited_tools),
            "groups_covered": sum(covered),
            "groups_expected": len(groups),
        },
        "repair": {
            "proposed": bool(proposal),
            "fields": repair_fields,
            "correct": bool(repair_fields) and all(f["correct"] for f in repair_fields.values()),
            "diff": proposal.get("diff"),
        },
        "staging_validation_passed": bool(validation.get("passed")),
        "regression_cases_passed": sum(1 for c in validation.get("regression", []) if c["passed"]),
        "regression_cases_total": len(validation.get("regression", [])),
    }


def run(scenario_id: str, approve_as: str | None) -> dict:
    provider = get_provider()
    availability = provider.availability()
    if not availability.available:
        raise SystemExit(f"The agent is unavailable. {availability.detail}")

    demo.reset()
    injection = demo.inject(scenario_id)
    incident_id = injection["incident_id"]
    if incident_id is None:
        raise SystemExit(f"{scenario_id} was injected but the monitor opened no incident")

    started = time.monotonic()
    outcome = supervisor.run_investigation(incident_id, provider=provider)
    investigation_s = round(time.monotonic() - started, 1)
    incident = store.get_incident(incident_id)
    result = {
        "scenario_id": scenario_id,
        "evaluated_at": datetime.now(UTC).isoformat(),
        "workflow_version": supervisor.WORKFLOW_VERSION,
        "model": provider.model,
        "incident_id": incident_id,
        "outcome": outcome,
        "investigation_seconds": investigation_s,
        "seconds_detected_to_diagnosed": _seconds(incident["detected_at"], _event_time(incident, "diagnosed")),
        "score": score(scenario_id, injection, incident),
        "run": db.to_jsonable(incident["investigation_runs"][-1]),
        "role_events": [
            {"agent": e["agent"], "status": e["status"], "summary": e["summary"]}
            for e in incident["events"]
            if e["event_type"] == "agent_run" and e["status"] != "running"
        ],
        "failed_tool_calls": [
            {"agent": e["agent"], "summary": e["summary"]}
            for e in incident["events"]
            if e["event_type"] == "tool_call" and e["status"] == "failed"
        ],
        "escalation": incident["state"].get("errors"),
    }

    proposal = incident["proposal"]
    if outcome == "awaiting_approval":
        # Safety check: the patch is validated but nobody approved it yet.
        try:
            executor.apply_approved_patch(incident_id, proposal["patch_hash"])
        except ApplyRefused as refusal:
            result["approval_enforced"] = {"apply_before_approval_refused": True, "reason": str(refusal)}
        else:
            result["approval_enforced"] = {"apply_before_approval_refused": False}
        if approve_as:
            post_fix = supervisor.approve_and_apply(incident_id, proposal["patch_hash"], actor=approve_as)
            final = store.get_incident(incident_id)
            result["recovery"] = {
                "approved_by": approve_as,
                "post_fix_passed": post_fix["passed"],
                "final_status": final["status"],
                "metrics_reconciled": metrics_summary()["reconciliation"]["reconciled"],
                "seconds_detected_to_resolved": _seconds(final["detected_at"], final["resolved_at"]),
            }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("scenario_id", choices=sorted(SCENARIOS))
    parser.add_argument("--approve-as", metavar="NAME", help="record an approval under this name, then verify recovery")
    args = parser.parse_args()

    traced = init_tracing(tags=["evaluation", args.scenario_id])
    try:
        result = run(args.scenario_id, args.approve_as)
    finally:
        # A script that exits without this can lose its trace.
        shutdown_tracing()
    result["traced"] = traced
    result["trace_delivery_problems"] = delivery_problems() if traced else None

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = RESULTS_DIR / f"{stamp}-{args.scenario_id}-{result['workflow_version']}.json"
    path.write_text(json.dumps(db.to_jsonable(result), indent=2) + "\n")
    summary = {key: value for key, value in result.items() if key not in ("role_events", "run")}
    summary["score"] = {k: v for k, v in result["score"].items() if k != "repair"} | {
        "repair_correct": result["score"]["repair"]["correct"]
    }
    json.dump(db.to_jsonable(summary), sys.stdout, indent=2)
    print(f"\n\nrun totals: {json.dumps(db.to_jsonable(result['run']))}\nwritten to {path.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
