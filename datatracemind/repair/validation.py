"""Staging validation of a repair proposal. Deterministic; no model decides.

A proposal passes only if, with the patched transformation:
- the regression fixtures give the contract's answers,
- a full pipeline run into a fresh staging schema succeeds, and
- every quality check passes on that staging target.

The demo's analytics schema and the active transformation are not touched.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from datatracemind import db, store
from datatracemind.models import ValidationResult
from datatracemind.observability import span
from datatracemind.repair import patch_store
from datatracemind.repair.regression import run_regression_suite
from etl import pipeline, run_log, transform_store
from etl.contract import load_revenue_contract
from etl.metrics import reconcile, source_revenue, target_revenue
from etl.pipeline import pipeline_lock
from etl.transforms.spec import SpecError, parse_spec

logger = logging.getLogger(__name__)


class ValidationRefused(RuntimeError):
    """The proposal is not in a state that can be validated."""


def _before_after(contract, as_of) -> dict:
    """Reconciliation of the live analytics target and of staging, against the same source."""
    with db.connect() as conn:
        source = source_revenue(conn, contract, as_of=as_of)
        before = reconcile(source, target_revenue(conn, db.ANALYTICS_SCHEMA), contract.tolerance)
        after = reconcile(source, target_revenue(conn, db.STAGING_SCHEMA), contract.tolerance)
    return {
        "source_rows_as_of": as_of,
        "before": {"target": "analytics (current)", **before},
        "after": {"target": "staging (patched transformation)", **after},
    }


def _evaluate(proposal: dict) -> ValidationResult:
    contract = load_revenue_contract()
    failures: list[str] = []
    result = ValidationResult(passed=False, patch_hash=proposal["patch_hash"])

    active = transform_store.get_active()
    if active.version != proposal["base_version"] or active.content_hash != proposal["base_hash"]:
        failures.append(
            f"the proposal was written against transformation version {proposal['base_version']}, "
            f"but version {active.version} is now deployed"
        )
    recomputed = patch_store.patch_hash(proposal["target_file"], proposal["base_hash"], proposal["new_content"])
    if recomputed != proposal["patch_hash"]:
        failures.append("the stored patch content does not match its hash")
    try:
        spec = parse_spec(proposal["new_content"])
    except SpecError as exc:
        failures.append(str(exc))
        result.failures = failures
        return result

    result.regression = run_regression_suite(spec, contract)
    failures += [f"regression case failed: {case.name}" for case in result.regression if not case.passed]

    with db.connect() as conn:
        db.rebuild_staging(conn)
    run = pipeline.run_once(
        target_schema=db.STAGING_SCHEMA,
        spec_text=proposal["new_content"],
        # Label for staging rows only: the version this patch would become.
        transform_version=proposal["base_version"] + 1,
        full_refresh=True,
        trigger="staging_validation",
    )
    result.staging_run_id = run.run_id
    result.staging_status = run.status
    result.checks = [check.as_dict() for check in run.checks]
    if run.status != "SUCCESS":
        failures.append(f"staging pipeline run failed: {run.error_summary}")
    failures += [f"quality check failed in staging: {check.name}" for check in run.failed_checks]
    if run.status == "SUCCESS":
        result.before_after = db.to_jsonable(_before_after(contract, run.watermark))

    result.failures = failures
    result.passed = not failures
    return result


def validate_proposal(incident_id: str) -> ValidationResult:
    """Validate the incident's current proposal in staging and record the outcome."""
    with pipeline_lock():
        with db.connect() as conn:
            proposal = patch_store.get_proposal(conn, incident_id, for_update=True)
            if proposal["status"] != "proposed":
                raise ValidationRefused(
                    f"{proposal['proposal_id']} is {proposal['status']}; only a newly proposed patch is validated"
                )
            store.set_status(
                conn, incident_id, "validating", actor="validator",
                note=f"Validating {proposal['proposal_id']} in staging",
                payload={"proposal_id": proposal["proposal_id"], "patch_hash": proposal["patch_hash"]},
            )  # fmt: skip

        guard_input = {"proposal_id": proposal["proposal_id"], "patch_hash": proposal["patch_hash"]}
        with span("staging_validation", "GUARDRAIL", input=guard_input) as guard:
            try:
                result = _evaluate(proposal)
            except Exception as exc:  # validation must end in a recorded verdict
                logger.exception("staging validation of %s crashed", proposal["proposal_id"])
                result = ValidationResult(
                    passed=False,
                    patch_hash=proposal["patch_hash"],
                    failures=[f"validation could not complete: {type(exc).__name__}: {exc}"],
                )
            result.validated_at = datetime.now(UTC)
            guard.set_output({"passed": result.passed, "failures": result.failures})
            if not result.passed:
                guard.fail("; ".join(result.failures))

        with db.connect() as conn:
            patch_store.set_proposal_status(
                conn,
                proposal["proposal_id"],
                "validated" if result.passed else "validation_failed",
                validation_result=result.model_dump(mode="json"),
            )
            regression_passed = sum(1 for case in result.regression if case.passed)
            if result.passed:
                note = (
                    f"{proposal['proposal_id']} passed staging validation: {regression_passed} regression cases and "
                    f"{len(result.checks)} quality checks. Waiting for human approval."
                )
            else:
                note = f"{proposal['proposal_id']} failed staging validation: " + "; ".join(result.failures)
            store.set_status(
                conn, incident_id, "awaiting_approval" if result.passed else "validation_failed",
                actor="validator", note=note,
                payload={"proposal_id": proposal["proposal_id"], "patch_hash": proposal["patch_hash"],
                         "failures": result.failures},
            )  # fmt: skip
            store.audit(
                conn, actor="validator", action="repair.validate", target=proposal["proposal_id"],
                result="passed" if result.passed else "failed",
                details={"incident_id": incident_id, "patch_hash": proposal["patch_hash"],
                         "staging_run_id": result.staging_run_id, "failures": result.failures},
            )  # fmt: skip
        return result


def staging_runs(limit: int = 5) -> list[dict]:
    """Recent staging runs, for the repair review page."""
    with db.connect() as conn:
        exists = conn.execute("SELECT to_regclass('staging.etl_run_log') AS t").fetchone()["t"]
        return run_log.recent_runs(conn, db.STAGING_SCHEMA, limit=limit) if exists else []
