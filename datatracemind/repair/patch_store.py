"""Repair proposals: turning a proposed file into an immutable, hashed patch.

A proposal is never edited. A revised repair is a new proposal with a new
hash, and the earlier one is marked superseded, so an approval can only ever
refer to the exact content that was validated.
"""

from __future__ import annotations

import difflib
import hashlib

import psycopg
from psycopg.types.json import Jsonb

from datatracemind import store
from datatracemind.config import TRANSFORM_FILE
from datatracemind.db import connect
from datatracemind.models import RepairPlan
from etl import transform_store
from etl.transforms.spec import SpecError, parse_spec

# A repair is meant to be minimal. A patch touching more lines than this is
# a rewrite, and is sent back.
MAX_CHANGED_LINES = 8
OPEN_STATUSES = ("proposed", "validated", "validation_failed", "approved")


class PatchRejected(ValueError):
    """The proposed change cannot become a patch."""

    def __init__(self, message: str, diff: str = "") -> None:
        super().__init__(message)
        self.diff = diff


def patch_hash(target_file: str, base_hash: str, new_content: str) -> str:
    """Identity of a patch: the file, the version it applies to, and the result."""
    return hashlib.sha256(f"{target_file}\n{base_hash}\n{new_content}".encode()).hexdigest()


def unified_diff(old: str, new: str, path: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True), new.splitlines(keepends=True), fromfile=f"a/{path}", tofile=f"b/{path}"
        )
    )


def _changed_lines(diff: str) -> int:
    return sum(
        1 for line in diff.splitlines() if line[:1] in "+-" and not line.startswith(("+++", "---"))
    )


def supersede_open(conn: psycopg.Connection, incident_id: str) -> None:
    conn.execute(
        "UPDATE dtm.repair_proposals SET status = 'superseded', updated_at = now() "
        "WHERE incident_id = %s AND status = ANY(%s)",
        (incident_id, list(OPEN_STATUSES)),
    )


def create_proposal(incident_id: str, plan: RepairPlan, *, cited_evidence: list[str]) -> dict:
    """Check a repair plan and store it as a proposal. Raises PatchRejected."""
    if plan.target_file.strip() != TRANSFORM_FILE:
        raise PatchRejected(f"only {TRANSFORM_FILE} may be changed, not {plan.target_file.strip()!r}")
    new_content = plan.new_content.rstrip("\n") + "\n"
    with connect() as conn:
        active = transform_store.get_active(conn)
        diff = unified_diff(active.content, new_content, TRANSFORM_FILE)
        if not diff:
            raise PatchRejected("the proposed file is identical to the deployed one")
        try:
            parse_spec(new_content)
        except SpecError as exc:
            raise PatchRejected(str(exc), diff) from exc
        changed = _changed_lines(diff)
        if changed > MAX_CHANGED_LINES:
            raise PatchRejected(
                f"the patch changes {changed} lines; a repair may change at most {MAX_CHANGED_LINES}. "
                "Keep every unrelated line and comment exactly as deployed.",
                diff,
            )
        supersede_open(conn, incident_id)
        number = conn.execute("SELECT nextval('dtm.proposal_seq') AS n").fetchone()["n"]
        digest = patch_hash(TRANSFORM_FILE, active.content_hash, new_content)
        proposal = conn.execute(
            """
            INSERT INTO dtm.repair_proposals
                (proposal_id, incident_id, target_file, base_version, base_hash, new_content, diff, patch_hash,
                 rationale, risk, rollback_plan, cited_evidence, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'proposed')
            RETURNING *
            """,
            (
                f"RP-{number:04d}", incident_id, TRANSFORM_FILE, active.version, active.content_hash, new_content,
                diff, digest, plan.rationale, plan.risk, plan.rollback_plan,
                Jsonb({"evidence": cited_evidence, "affected_metrics": plan.affected_metrics,
                       "regression_cases": plan.regression_cases}),
            ),
        ).fetchone()  # fmt: skip
        store.audit(
            conn, actor="repair_agent", action="repair.propose", target=proposal["proposal_id"], result="proposed",
            details={"incident_id": incident_id, "patch_hash": digest, "base_version": active.version},
        )  # fmt: skip
    return proposal


def get_proposal(conn: psycopg.Connection, incident_id: str, *, for_update: bool = False) -> dict:
    """The incident's current (latest) proposal."""
    row = conn.execute(
        "SELECT * FROM dtm.repair_proposals WHERE incident_id = %s ORDER BY created_at DESC, proposal_id DESC LIMIT 1"
        + (" FOR UPDATE" if for_update else ""),
        (incident_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"{incident_id} has no repair proposal")
    return row


def set_proposal_status(conn: psycopg.Connection, proposal_id: str, status: str, **columns: object) -> None:
    allowed = {"validation_result", "post_fix_result", "applied_version"}
    assignments = ["status = %s", "updated_at = now()"]
    values: list[object] = [status]
    for name, value in columns.items():
        if name not in allowed:
            raise ValueError(f"cannot set {name}")
        assignments.append(f"{name} = %s")
        values.append(Jsonb(value) if name.endswith("_result") else value)
    conn.execute(
        f"UPDATE dtm.repair_proposals SET {', '.join(assignments)} WHERE proposal_id = %s", (*values, proposal_id)
    )
