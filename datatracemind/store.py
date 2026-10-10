"""Persistence for incidents, the agent timeline, evidence, and the audit log.

Lifecycle rules live here, not in the callers: `set_status` refuses a
transition the lifecycle does not allow, so no code path (or API call) can,
for example, move an incident to `resolved` without passing through
`verifying`.
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from datatracemind.db import connect

TERMINAL = {"resolved"}
# Statuses from which a human may (re)start an investigation.
RESTARTABLE = {
    "detected",
    "diagnosed",
    "repair_proposed",
    "validation_failed",
    "awaiting_approval",
    "application_failed",
    "rejected",
    "escalated",
}

TRANSITIONS: dict[str, set[str]] = {
    "detected": {"investigating"},
    "investigating": {"diagnosed", "escalated"},
    "diagnosed": {"repair_proposed", "escalated", "investigating"},
    "repair_proposed": {"validating", "investigating", "escalated"},
    "validating": {"awaiting_approval", "validation_failed"},
    "validation_failed": {"repair_proposed", "investigating", "escalated"},
    "awaiting_approval": {"applying", "rejected", "investigating"},
    "applying": {"verifying", "application_failed"},
    "application_failed": {"investigating", "escalated"},
    "verifying": {"resolved", "application_failed"},
    "rejected": {"investigating"},
    "escalated": {"investigating"},
    "resolved": set(),
}


class IncidentNotFound(LookupError):
    pass


class IllegalTransition(RuntimeError):
    """The requested status change is not allowed from the current status."""


def audit(
    conn: psycopg.Connection, *, actor: str, action: str, target: str, result: str, details: dict | None = None
) -> None:
    conn.execute(
        "INSERT INTO dtm.audit_log (actor, action, target, result, details) VALUES (%s, %s, %s, %s, %s)",
        (actor, action, target, result, Jsonb(details or {})),
    )


def add_event(
    conn: psycopg.Connection,
    incident_id: str,
    *,
    agent: str,
    event_type: str,
    status: str,
    summary: str,
    payload: dict | None = None,
) -> None:
    conn.execute(
        "INSERT INTO dtm.agent_events (incident_id, agent, event_type, status, summary, payload) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (incident_id, agent, event_type, status, summary, Jsonb(payload or {})),
    )


def create_incident(
    conn: psycopg.Connection,
    *,
    incident_type: str,
    severity: str,
    summary: str,
    trigger: dict,
    source_metrics: dict,
    target_metrics: dict,
    scenario_id: str | None,
) -> str:
    number = conn.execute("SELECT nextval('dtm.incident_seq') AS n").fetchone()["n"]
    incident_id = f"INC-{number:04d}"
    conn.execute(
        """
        INSERT INTO dtm.incidents
            (incident_id, incident_type, severity, status, summary, trigger, source_metrics, target_metrics, scenario_id)
        VALUES (%s, %s, %s, 'detected', %s, %s, %s, %s, %s)
        """,
        (
            incident_id,
            incident_type,
            severity,
            summary,
            Jsonb(trigger),
            Jsonb(source_metrics),
            Jsonb(target_metrics),
            scenario_id,
        ),
    )
    add_event(
        conn, incident_id, agent="monitor", event_type="lifecycle", status="detected", summary=summary,
        payload={"failed_checks": [c["check_name"] for c in trigger.get("failed_checks", [])]},
    )  # fmt: skip
    audit(conn, actor="monitor", action="incident.open", target=incident_id, result="detected",
          details={"incident_type": incident_type, "severity": severity})  # fmt: skip
    return incident_id


def get_incident_row(conn: psycopg.Connection, incident_id: str, *, for_update: bool = False) -> dict:
    query = "SELECT * FROM dtm.incidents WHERE incident_id = %s" + (" FOR UPDATE" if for_update else "")
    row = conn.execute(query, (incident_id,)).fetchone()
    if row is None:
        raise IncidentNotFound(incident_id)
    return row


def set_status(
    conn: psycopg.Connection, incident_id: str, status: str, *, actor: str, note: str, payload: dict | None = None
) -> None:
    """Move an incident to `status`, or raise IllegalTransition."""
    current = get_incident_row(conn, incident_id, for_update=True)["status"]
    if status not in TRANSITIONS[current]:
        raise IllegalTransition(f"{incident_id}: cannot move from {current} to {status}")
    conn.execute(
        "UPDATE dtm.incidents SET status = %s, updated_at = now(), "
        "resolved_at = CASE WHEN %s = 'resolved' THEN now() ELSE resolved_at END WHERE incident_id = %s",
        (status, status, incident_id),
    )
    add_event(conn, incident_id, agent=actor, event_type="lifecycle", status=status, summary=note, payload=payload)
    audit(conn, actor=actor, action="incident.status", target=incident_id, result=status,
          details={"from": current, "note": note})  # fmt: skip


def update_state(conn: psycopg.Connection, incident_id: str, **values: Any) -> None:
    """Merge keys into the incident's workflow state."""
    conn.execute(
        "UPDATE dtm.incidents SET state = state || %s, updated_at = now() WHERE incident_id = %s",
        (Jsonb(values), incident_id),
    )


def add_evidence(
    conn: psycopg.Connection,
    incident_id: str,
    *,
    kind: str,
    source: str,
    description: str,
    query_or_tool: str,
    arguments: dict,
    result_summary: Any,
    collected_by: str,
) -> str:
    """Store one evidence item and return its ID (E-001, E-002, ...)."""
    # Serialise ID allocation per incident: investigators run concurrently.
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"evidence:{incident_id}",))
    count = conn.execute(
        "SELECT count(*) AS n FROM dtm.evidence WHERE incident_id = %s", (incident_id,)
    ).fetchone()["n"]
    evidence_id = f"E-{count + 1:03d}"
    conn.execute(
        """
        INSERT INTO dtm.evidence
            (incident_id, evidence_id, kind, source, description, query_or_tool, arguments, result_summary, collected_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (incident_id, evidence_id, kind, source, description, query_or_tool, Jsonb(arguments),
         Jsonb(result_summary), collected_by),
    )  # fmt: skip
    return evidence_id


def list_evidence(conn: psycopg.Connection, incident_id: str) -> list[dict]:
    return conn.execute(
        "SELECT evidence_id, kind, source, description, query_or_tool, arguments, result_summary, "
        "collected_by, observed_at FROM dtm.evidence WHERE incident_id = %s ORDER BY evidence_id",
        (incident_id,),
    ).fetchall()


def list_events(conn: psycopg.Connection, incident_id: str) -> list[dict]:
    return conn.execute(
        "SELECT event_id, agent, event_type, status, summary, payload, created_at "
        "FROM dtm.agent_events WHERE incident_id = %s ORDER BY event_id",
        (incident_id,),
    ).fetchall()


def latest_proposal(conn: psycopg.Connection, incident_id: str) -> dict | None:
    return conn.execute(
        "SELECT * FROM dtm.repair_proposals WHERE incident_id = %s ORDER BY created_at DESC, proposal_id DESC LIMIT 1",
        (incident_id,),
    ).fetchone()


# What a list view needs; the evaluator-only scenario_id is deliberately absent.
_SUMMARY_COLUMNS = (
    "incident_id, incident_type, severity, status, summary, detected_at, updated_at, resolved_at"
)


def list_incidents(conn: psycopg.Connection) -> list[dict]:
    return conn.execute(f"SELECT {_SUMMARY_COLUMNS} FROM dtm.incidents ORDER BY detected_at DESC").fetchall()


def unresolved_incident(conn: psycopg.Connection) -> dict | None:
    return conn.execute(
        f"SELECT {_SUMMARY_COLUMNS} FROM dtm.incidents WHERE status <> 'resolved' ORDER BY detected_at DESC LIMIT 1"
    ).fetchone()


def get_incident(incident_id: str) -> dict:
    """Everything the incident page shows."""
    with connect() as conn:
        incident = get_incident_row(conn, incident_id)
        incident.pop("scenario_id")
        incident["evidence"] = list_evidence(conn, incident_id)
        incident["events"] = list_events(conn, incident_id)
        incident["proposal"] = latest_proposal(conn, incident_id)
        incident["approvals"] = conn.execute(
            "SELECT proposal_id, patch_hash, decision, actor, reason, decided_at "
            "FROM dtm.approvals WHERE incident_id = %s ORDER BY approval_id",
            (incident_id,),
        ).fetchall()
        incident["investigation_runs"] = conn.execute(
            "SELECT * FROM dtm.investigation_runs WHERE incident_id = %s ORDER BY run_id", (incident_id,)
        ).fetchall()
    return incident
