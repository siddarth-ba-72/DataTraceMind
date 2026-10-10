"""The shared incident state the roles read and the supervisor advances.

The database is the store of record (see `datatracemind.store`); this is the
typed view of it that one workflow run works with.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict

from datatracemind.llm.base import Provider, UsageTotals


class IncidentState(TypedDict, total=False):
    incident_id: str
    scenario_id: str | None
    status: Literal[
        "detected", "investigating", "diagnosed", "repair_proposed", "validating", "validation_failed",
        "awaiting_approval", "applying", "application_failed", "verifying", "resolved", "rejected", "escalated",
    ]  # fmt: skip
    trigger: dict[str, Any]
    source_metrics: dict[str, Any]
    target_metrics: dict[str, Any]
    evidence: list[dict[str, Any]]
    hypotheses: list[dict[str, Any]]
    selected_root_cause: dict[str, Any]
    approved_business_rules: list[dict[str, Any]]
    proposed_patch: dict[str, Any] | None
    validation_result: dict[str, Any] | None
    approval: dict[str, Any] | None
    post_fix_result: dict[str, Any] | None
    errors: list[dict[str, Any]]
    trace_ref: dict[str, Any] | None


@dataclass
class RunContext:
    """Everything one investigation run carries from role to role."""

    incident: dict[str, Any]
    provider: Provider
    totals: UsageTotals = field(default_factory=UsageTotals)
    # Investigator reports, keyed by role, for the analyst.
    notes: dict[str, Any] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def incident_id(self) -> str:
        return self.incident["incident_id"]
