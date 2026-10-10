"""The scenario lab: repeatable, resettable faults for the demo and evaluator."""

from __future__ import annotations

from datatracemind.scenarios.base import FaultScenario, InjectionContext
from datatracemind.scenarios.duplicate_load import DuplicateIngestionOnRetry
from datatracemind.scenarios.join_loss import JoinDropsOrders
from datatracemind.scenarios.new_status import NewStatusValue
from datatracemind.scenarios.payment_logic import FailedPaymentsCounted
from datatracemind.scenarios.refund_logic import PartialRefundOverSubtracted, RefundLogicOmitted
from datatracemind.scenarios.schema_drift import NullAmounts, SchemaDrift
from datatracemind.scenarios.timezone import TimezoneBoundaryMismatch

# IDs follow the product brief's scenario table; DS-16 and DS-17 are additions to it.
SCENARIOS: dict[str, FaultScenario] = {
    scenario.scenario_id: scenario
    for scenario in (
        RefundLogicOmitted(),
        SchemaDrift(),
        JoinDropsOrders(),
        DuplicateIngestionOnRetry(),
        NewStatusValue(),
        TimezoneBoundaryMismatch(),
        NullAmounts(),
        PartialRefundOverSubtracted(),
        FailedPaymentsCounted(),
    )
}

__all__ = ["SCENARIOS", "FaultScenario", "InjectionContext"]
