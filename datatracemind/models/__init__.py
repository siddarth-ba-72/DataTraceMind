"""Typed schemas shared by the workflow, the store, and the API.

Models under "Agent outputs" are what an LLM-backed role must return. They
are strict (every field required, no extras) because the same JSON Schema is
handed to the model provider as the output schema.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

IncidentStatus = Literal[
    "detected",
    "investigating",
    "diagnosed",
    "repair_proposed",
    "validating",
    "validation_failed",
    "awaiting_approval",
    "applying",
    "application_failed",
    "verifying",
    "resolved",
    "rejected",
    "escalated",
]

IncidentType = Literal[
    "revenue_mismatch",
    "pipeline_failure",
    "record_loss",
    "duplicate_records",
    "rule_violation",
    "schema_drift",
    "stale_data",
    "data_quality",
]

Severity = Literal["critical", "high", "medium", "low"]

RootCauseCategory = Literal[
    "refund_logic",
    "payment_logic",
    "schema_drift",
    "join_loss",
    "duplicate_load",
    "join_fan_out",
    "timezone",
    "watermark",
    "late_arriving_data",
    "stale_source",
    "source_data_defect",
    "unknown",
]

ProposalStatus = Literal[
    "proposed",
    "validated",
    "validation_failed",
    "approved",
    "rejected",
    "applied",
    "verified",
    "application_failed",
    "superseded",
]


# --- Application records -----------------------------------------------------


class Evidence(BaseModel):
    evidence_id: str
    kind: str
    source: str
    description: str
    query_or_tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    result_summary: Any
    collected_by: str
    observed_at: datetime | None = None


class ToolCallRecord(BaseModel):
    """One tool call as recorded from the provider's event stream."""

    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    result: Any = None
    status: Literal["completed", "failed"]
    error: str | None = None
    duration_s: float | None = None


class TokenUsage(BaseModel):
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            reasoning_output_tokens=self.reasoning_output_tokens + other.reasoning_output_tokens,
        )


# --- Agent outputs (strict) --------------------------------------------------


class _AgentOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Observation(_AgentOutput):
    statement: str = Field(description="One factual observation, with the figures that support it.")
    tool: str = Field(description="Name of the tool whose result shows this.")


class InvestigationReport(_AgentOutput):
    observations: list[Observation] = Field(description="Facts read from tool results. No speculation.")
    open_questions: list[str] = Field(description="What could not be determined with the tools available.")


class Hypothesis(_AgentOutput):
    hypothesis_id: str = Field(description="H1, H2, ... in ranked order, most likely first.")
    category: RootCauseCategory
    statement: str
    confidence: float = Field(ge=0, le=1)
    supporting_evidence: list[str] = Field(description="Evidence IDs (E-...) that support it.")
    contradicting_evidence: list[str] = Field(description="Evidence IDs (E-...) that contradict it.")
    reasoning: str


class RootCauseReport(_AgentOutput):
    hypotheses: list[Hypothesis]
    selected_hypothesis_id: str = Field(description="ID of the confirmed hypothesis, or an empty string.")
    confirmed: bool = Field(description="True only if the evidence confirms the selected hypothesis.")
    root_cause_summary: str
    implicated_component: str = Field(description="File and field, table and column, or step at fault.")
    needs_more_evidence: bool
    requested_diagnostics: list[str] = Field(description="Specific checks to run if more evidence is needed.")
    business_rule_ambiguity: str = Field(
        description="A missing or conflicting business definition that a human must decide; else empty."
    )


class RepairPlan(_AgentOutput):
    can_repair: bool = Field(description="False if no change to the transformation file is appropriate.")
    escalation_reason: str = Field(description="Why a human must decide instead; empty if can_repair.")
    target_file: str
    new_content: str = Field(description="Complete new content of the target file.")
    rationale: str
    risk: str
    rollback_plan: str
    affected_metrics: list[str]
    regression_cases: list[str] = Field(description="Cases the fix must be tested against.")


# --- Repair lifecycle --------------------------------------------------------


class RegressionCase(BaseModel):
    name: str
    passed: bool
    expected: Any
    actual: Any


class ValidationResult(BaseModel):
    passed: bool
    patch_hash: str
    staging_run_id: int | None = None
    staging_status: str | None = None
    failures: list[str] = Field(default_factory=list)
    checks: list[dict[str, Any]] = Field(default_factory=list)
    regression: list[RegressionCase] = Field(default_factory=list)
    before_after: dict[str, Any] = Field(default_factory=dict)
    # Outcome of loading the same rows a second time in staging.
    replay: dict[str, Any] = Field(default_factory=dict)
    validated_at: datetime | None = None
