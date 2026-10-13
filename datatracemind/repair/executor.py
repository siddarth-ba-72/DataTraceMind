"""Applies an approved repair to the demo environment and verifies recovery.

`apply_approved_patch` trusts nothing it is told. It re-reads the proposal,
the approval, and the validation result from the database and refuses unless
all of them refer to the same patch hash, the patch still applies to the
deployed transformation, and the target is the one allowlisted file. The only
change it can make is to deploy a new version of that file.
"""

from __future__ import annotations

import logging

from datatracemind import db, store
from datatracemind.config import TRANSFORM_FILE
from datatracemind.repair import patch_store
from etl import pipeline, transform_store
from etl.pipeline import pipeline_lock
from etl.transforms.spec import SpecError, parse_spec

logger = logging.getLogger(__name__)

ACTOR = "repair_executor"


class ApplyRefused(RuntimeError):
    """A precondition for applying the patch does not hold."""


def _refusal(conn, incident: dict, proposal: dict, patch_hash: str) -> str | None:
    """First reason this patch may not be applied, or None."""
    if incident["status"] != "awaiting_approval":
        return f"the incident is {incident['status']}, not awaiting approval"
    if proposal["patch_hash"] != patch_hash:
        return "the patch hash does not match the incident's current proposal"
    if proposal["status"] != "approved":
        return f"the proposal is {proposal['status']}, not approved"
    decision = conn.execute(
        "SELECT decision, patch_hash FROM dtm.approvals WHERE proposal_id = %s ORDER BY approval_id DESC LIMIT 1",
        (proposal["proposal_id"],),
    ).fetchone()
    if decision is None or decision["decision"] != "approved" or decision["patch_hash"] != patch_hash:
        return "no human approval is recorded for this exact patch"
    validation = proposal["validation_result"] or {}
    if not validation.get("passed") or validation.get("patch_hash") != patch_hash:
        return "this exact patch has not passed staging validation"
    if proposal["target_file"] != TRANSFORM_FILE:
        return f"{proposal['target_file']} is not an allowed repair target"
    recomputed = patch_store.patch_hash(proposal["target_file"], proposal["base_hash"], proposal["new_content"])
    if recomputed != patch_hash:
        return "the stored patch content does not match the approved hash"
    active = transform_store.get_active(conn)
    if active.version != proposal["base_version"] or active.content_hash != proposal["base_hash"]:
        return (
            f"the deployed transformation changed (now version {active.version}) since this patch was "
            "validated; it must be proposed and validated again"
        )
    try:
        parse_spec(proposal["new_content"])
    except SpecError as exc:
        return str(exc)
    return None


def apply_approved_patch(incident_id: str, patch_hash: str) -> dict:
    """Deploy the approved patch, then run post-fix verification."""
    with pipeline_lock():
        with db.connect() as conn:
            incident = store.get_incident_row(conn, incident_id, for_update=True)
            try:
                proposal = patch_store.get_proposal(conn, incident_id, for_update=True)
            except LookupError as exc:
                raise ApplyRefused(str(exc)) from exc
            reason = _refusal(conn, incident, proposal, patch_hash)
            if reason is not None:
                store.audit(
                    conn, actor=ACTOR, action="repair.apply", target=proposal["proposal_id"], result="refused",
                    details={"incident_id": incident_id, "patch_hash": patch_hash, "reason": reason},
                )  # fmt: skip
                conn.commit()
                raise ApplyRefused(reason)
            approver = conn.execute(
                "SELECT actor FROM dtm.approvals WHERE proposal_id = %s ORDER BY approval_id DESC LIMIT 1",
                (proposal["proposal_id"],),
            ).fetchone()["actor"]
            store.set_status(
                conn, incident_id, "applying", actor=ACTOR,
                note=f"Applying {proposal['proposal_id']} approved by {approver}",
                payload={"proposal_id": proposal["proposal_id"], "patch_hash": patch_hash},
            )  # fmt: skip
            version = transform_store.register_version(
                conn,
                proposal["new_content"],
                author=f"datatracemind (approved by {approver})",
                message=f"Repair for {incident_id}, proposal {proposal['proposal_id']}",
            )
            patch_store.set_proposal_status(
                conn, proposal["proposal_id"], "applied", applied_version=version.version
            )
            store.audit(
                conn, actor=ACTOR, action="repair.apply", target=proposal["proposal_id"], result="applied",
                details={"incident_id": incident_id, "patch_hash": patch_hash, "approved_by": approver,
                         "transform_version": version.version},
            )  # fmt: skip
        return run_post_fix_verification(incident_id)


def _roll_back(incident_id: str, proposal: dict) -> int:
    """Redeploy the transformation the failed repair replaced."""
    with db.connect() as conn:
        base = conn.execute(
            "SELECT content FROM pipeline.transform_versions WHERE version = %s", (proposal["base_version"],)
        ).fetchone()
        version = transform_store.register_version(
            conn,
            base["content"],
            author="datatracemind (automatic rollback)",
            message=f"Roll back {proposal['proposal_id']} for {incident_id}: post-fix verification failed",
        )
    pipeline.run_once(trigger="rollback")
    return version.version


def run_post_fix_verification(incident_id: str) -> dict:
    """Rerun the pipeline with the applied patch and resolve only if every check passes."""
    with pipeline_lock():
        with db.connect() as conn:
            incident = store.get_incident_row(conn, incident_id, for_update=True)
            proposal = patch_store.get_proposal(conn, incident_id)
            if incident["status"] != "applying" or proposal["status"] != "applied":
                raise ApplyRefused(
                    f"nothing to verify: the incident is {incident['status']} and its proposal is {proposal['status']}"
                )
            store.set_status(
                conn, incident_id, "verifying", actor=ACTOR,
                note="Rerunning the pipeline and checking recovery",
                payload={"transform_version": proposal["applied_version"]},
            )  # fmt: skip

        run = pipeline.run_once(trigger="post_fix")
        failed = [check.name for check in run.failed_checks]
        passed = run.status == "SUCCESS" and not failed and run.transform_version == proposal["applied_version"]
        outcome = {
            "passed": passed,
            "etl_run_id": run.run_id,
            "etl_status": run.status,
            "etl_error": run.error_summary,
            "transform_version": run.transform_version,
            "failed_checks": failed,
            "checks": [check.as_dict() for check in run.checks],
        }
        if not passed:
            outcome["rolled_back_to_version"] = _roll_back(incident_id, proposal)

        with db.connect() as conn:
            patch_store.set_proposal_status(
                conn, proposal["proposal_id"], "verified" if passed else "application_failed",
                post_fix_result=db.to_jsonable(outcome),
            )  # fmt: skip
            if passed:
                note = f"Recovery verified by ETL run {run.run_id}: all {len(run.checks)} quality checks pass"
            else:
                note = (
                    f"Post-fix verification failed ({', '.join(failed) or run.error_summary}); "
                    f"the previous transformation was redeployed"
                )
            store.set_status(
                conn, incident_id, "resolved" if passed else "application_failed", actor=ACTOR, note=note,
                payload={k: v for k, v in outcome.items() if k != "checks"},
            )  # fmt: skip
            store.audit(
                conn, actor=ACTOR, action="repair.verify", target=proposal["proposal_id"],
                result="verified" if passed else "failed",
                details={"incident_id": incident_id, "etl_run_id": run.run_id, "failed_checks": failed},
            )  # fmt: skip
        return outcome
