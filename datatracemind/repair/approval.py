"""Human approval of a validated repair.

An approval names an incident and a patch hash. It is recorded only if that
hash is the incident's current proposal, the proposal passed validation with
that same hash, and the incident is waiting for approval. These functions are
reachable only through the API's approval routes: no agent tool calls them,
and an agent role name is refused as an approver.
"""

from __future__ import annotations

from datatracemind import db, store
from datatracemind.repair import patch_store

# Names the workflow's own components act under. None may approve or reject.
NON_HUMAN_ACTORS = {
    "supervisor", "monitor", "validator", "repair_agent", "repair_executor", "root_cause_analyst",
    "source_investigator", "pipeline_investigator", "targeted_investigator", "contract_agent",
    "datatracemind", "agent", "codex", "system", "demo",
}  # fmt: skip


class ApprovalRefused(RuntimeError):
    """The decision cannot be recorded for this incident and patch."""


def _checked(conn, incident_id: str, patch_hash: str, actor: str) -> dict:
    """Common preconditions for a decision. Returns the locked proposal."""
    actor = actor.strip()
    if not actor or actor.lower() in NON_HUMAN_ACTORS:
        raise ApprovalRefused("a decision must be made by a named person, not by a workflow component")
    incident = store.get_incident_row(conn, incident_id, for_update=True)
    if incident["status"] != "awaiting_approval":
        raise ApprovalRefused(f"{incident_id} is {incident['status']}, not awaiting approval")
    try:
        proposal = patch_store.get_proposal(conn, incident_id, for_update=True)
    except LookupError as exc:
        raise ApprovalRefused(str(exc)) from exc
    if proposal["patch_hash"] != patch_hash:
        raise ApprovalRefused(
            "the patch hash does not match the current proposal; review the current proposal and decide on it"
        )
    return proposal


def approve(incident_id: str, patch_hash: str, *, actor: str) -> dict:
    """Record a human approval of exactly this patch. Does not apply it."""
    with db.connect() as conn:
        proposal = _checked(conn, incident_id, patch_hash, actor)
        validation = proposal["validation_result"] or {}
        if proposal["status"] != "validated" or not validation.get("passed"):
            raise ApprovalRefused(f"{proposal['proposal_id']} has not passed staging validation")
        if validation.get("patch_hash") != patch_hash:
            raise ApprovalRefused("the validation result belongs to a different patch; validate this patch first")
        conn.execute(
            "INSERT INTO dtm.approvals (incident_id, proposal_id, patch_hash, decision, actor) "
            "VALUES (%s, %s, %s, 'approved', %s)",
            (incident_id, proposal["proposal_id"], patch_hash, actor.strip()),
        )
        patch_store.set_proposal_status(conn, proposal["proposal_id"], "approved")
        store.add_event(
            conn, incident_id, agent=f"human:{actor.strip()}", event_type="approval", status="approved",
            summary=f"{actor.strip()} approved {proposal['proposal_id']} (patch {patch_hash[:12]})",
            payload={"proposal_id": proposal["proposal_id"], "patch_hash": patch_hash},
        )  # fmt: skip
        store.audit(
            conn, actor=f"human:{actor.strip()}", action="repair.approve", target=proposal["proposal_id"],
            result="approved", details={"incident_id": incident_id, "patch_hash": patch_hash},
        )  # fmt: skip
    return {"incident_id": incident_id, "proposal_id": proposal["proposal_id"], "decision": "approved"}


def reject(incident_id: str, patch_hash: str, *, actor: str, reason: str | None = None) -> dict:
    """Record a human rejection. The patch can then never be applied."""
    with db.connect() as conn:
        proposal = _checked(conn, incident_id, patch_hash, actor)
        conn.execute(
            "INSERT INTO dtm.approvals (incident_id, proposal_id, patch_hash, decision, actor, reason) "
            "VALUES (%s, %s, %s, 'rejected', %s, %s)",
            (incident_id, proposal["proposal_id"], patch_hash, actor.strip(), reason),
        )
        patch_store.set_proposal_status(conn, proposal["proposal_id"], "rejected")
        store.set_status(
            conn, incident_id, "rejected", actor=f"human:{actor.strip()}",
            note=f"{actor.strip()} rejected {proposal['proposal_id']}" + (f": {reason}" if reason else ""),
            payload={"proposal_id": proposal["proposal_id"], "patch_hash": patch_hash, "reason": reason},
        )  # fmt: skip
        store.audit(
            conn, actor=f"human:{actor.strip()}", action="repair.reject", target=proposal["proposal_id"],
            result="rejected", details={"incident_id": incident_id, "patch_hash": patch_hash, "reason": reason},
        )  # fmt: skip
    return {"incident_id": incident_id, "proposal_id": proposal["proposal_id"], "decision": "rejected"}
