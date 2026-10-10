"""Common interface for fault scenarios.

A scenario introduces exactly one deterministic fault into the demo
environment. It also carries the answer key (`expected_symptoms`,
`ground_truth`) used to score a diagnosis. The answer key is for the
evaluator and the demo harness only: nothing in `datatracemind.tools`,
`datatracemind.agents`, or any prompt may import or read it.
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

    @abstractmethod
    def inject(self, context: InjectionContext) -> None:
        """Introduce one deterministic fault."""

    @abstractmethod
    def expected_symptoms(self) -> list[str]:
        """Evaluator-only: observations the fault should produce."""

    @abstractmethod
    def ground_truth(self) -> dict:
        """Evaluator-only: cause, expected failing checks, and expected repair."""

    def public_view(self) -> dict:
        """What the Scenario Lab page lists. Contains no answer key."""
        return {
            "scenario_id": self.scenario_id,
            "name": self.name,
            "description": self.description,
            "failure_type": self.failure_type,
            "priority": self.priority,
        }
