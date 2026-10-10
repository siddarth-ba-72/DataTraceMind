"""Common interface for fault scenarios.

A scenario introduces exactly one deterministic fault into the demo
environment. It carries two different things about that fault:

- a briefing (`injection`, `impact`, `resolution_note`, `expected_symptoms`,
  ...) written for the person operating the Scenario Lab, which the lab page
  shows before and after injecting; and
- an answer key (`ground_truth`) that the evaluator scores a diagnosis
  against, which is never served by the API.

Neither reaches an agent: nothing in `datatracemind.tools`,
`datatracemind.agents`, `datatracemind.orchestration`, or any prompt may
import this package, and an incident never says which scenario caused it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import psycopg

from etl import transform_store


@dataclass
class InjectionContext:
    """What a scenario may touch while injecting its fault."""

    conn: psycopg.Connection

    def deploy_transform(self, old: str, new: str, *, author: str, message: str) -> None:
        """Deploy a new transformation version that differs by one replacement."""
        current = transform_store.get_active(self.conn).content
        if current.count(old) != 1:
            raise RuntimeError(f"cannot inject: expected exactly one {old!r} in the active transformation")
        transform_store.register_version(self.conn, current.replace(old, new), author=author, message=message)


class FaultScenario(ABC):
    scenario_id: str
    name: str
    description: str
    failure_type: str
    priority: str

    # --- Briefing for the lab operator ---------------------------------------
    # Where the fault lives: Transformation | Load path | Source schema | Source data.
    layer: str
    # What the ETL job reports while the fault is active: SUCCESS (silent) or FAILED.
    etl_reports: str
    # What the lab changes, concretely.
    injection: str
    # Why it matters to whoever reads the numbers.
    impact: str
    # How the incident is expected to end: "repair" (a patch for approval) or
    # "escalation" (the workflow hands it to a human).
    resolution: str = "repair"
    resolution_note: str

    # Trigger recorded on the pipeline run that follows `after_first_run`.
    second_run_trigger: str = "schedule"

    @abstractmethod
    def inject(self, context: InjectionContext) -> None:
        """Introduce one deterministic fault."""

    def after_first_run(self, context: InjectionContext) -> bool:
        """Second step, for a fault that only shows on a later run.

        Called after the pipeline has run once with the fault in place. Return
        True if this changed anything; the pipeline then runs again.
        """
        return False

    @abstractmethod
    def expected_symptoms(self) -> list[str]:
        """Observations the fault should produce. For the operator and the evaluator."""

    @abstractmethod
    def ground_truth(self) -> dict:
        """Evaluator-only: cause, expected failing checks, expected outcome and repair."""

    def public_view(self) -> dict:
        """What the Scenario Lab page shows. Contains no scoring key."""
        return {
            "scenario_id": self.scenario_id,
            "name": self.name,
            "description": self.description,
            "failure_type": self.failure_type,
            "priority": self.priority,
            "layer": self.layer,
            "etl_reports": self.etl_reports,
            "injection": self.injection,
            "impact": self.impact,
            "symptoms": self.expected_symptoms(),
            "expected_failed_checks": self.ground_truth()["expected_failed_checks"],
            "resolution": self.resolution,
            "resolution_note": self.resolution_note,
        }
