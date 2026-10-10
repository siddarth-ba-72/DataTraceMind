"""The scenario lab: repeatable, resettable faults for the demo and evaluator."""

from __future__ import annotations

from datatracemind.scenarios.base import FaultScenario, InjectionContext
from datatracemind.scenarios.join_loss import JoinDropsOrders
from datatracemind.scenarios.refund_logic import RefundLogicOmitted
from datatracemind.scenarios.schema_drift import SchemaDrift

SCENARIOS: dict[str, FaultScenario] = {
    scenario.scenario_id: scenario for scenario in (RefundLogicOmitted(), SchemaDrift(), JoinDropsOrders())
}

__all__ = ["SCENARIOS", "FaultScenario", "InjectionContext"]
