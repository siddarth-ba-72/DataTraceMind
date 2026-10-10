"""Runs one model-backed role: bounded retries, evidence filing, timeline events.

Evidence is filed from the tool calls the provider recorded, with the tool's
own structured result. The model's prose about a result is never stored as
evidence.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TypeVar

from pydantic import BaseModel

from datatracemind import store
from datatracemind.db import connect
from datatracemind.llm.base import InvalidOutput, LLMError, RoleCall, RoleResult
from datatracemind.models import ToolCallRecord
from datatracemind.observability import span
from datatracemind.orchestration.routing import MAX_ROLE_ATTEMPTS
from datatracemind.orchestration.state import RunContext
from datatracemind.tools.registry import TOOLS

logger = logging.getLogger(__name__)

OutputT = TypeVar("OutputT", bound=BaseModel)


def file_tool_calls(ctx: RunContext, role: str, tool_calls: list[ToolCallRecord]) -> list[str]:
    """Store successful calls as evidence and every call as a timeline event."""
    evidence_ids = []
    with connect() as conn:
        for call in tool_calls:
            spec = TOOLS.get(call.tool)
            description = spec.describe(call.arguments) if spec else f"Tool call {call.tool}"
            if call.status == "completed" and spec is not None:
                evidence_id = store.add_evidence(
                    conn,
                    ctx.incident_id,
                    kind=spec.evidence_kind,
                    source=spec.evidence_source,
                    description=description,
                    query_or_tool=call.tool,
                    arguments=call.arguments,
                    result_summary=call.result,
                    collected_by=role,
                )
                evidence_ids.append(evidence_id)
                store.add_event(
                    conn, ctx.incident_id, agent=role, event_type="tool_call", status="completed",
                    summary=description,
                    payload={"tool": call.tool, "arguments": call.arguments, "evidence_id": evidence_id,
                             "duration_s": call.duration_s},
                )  # fmt: skip
            else:
                # A failed call produced no result. It is recorded as a gap,
                # not as evidence.
                store.add_event(
                    conn, ctx.incident_id, agent=role, event_type="tool_call", status="failed",
                    summary=f"{description}: {call.error}",
                    payload={"tool": call.tool, "arguments": call.arguments, "error": call.error},
                )  # fmt: skip
    return evidence_ids


def _account(ctx: RunContext, *, usage, tool_calls, cost: float, retry: bool) -> None:
    with ctx.lock:
        ctx.totals.add(usage=usage, tool_calls=tool_calls, cost=cost, retry=retry)


def _event(ctx: RunContext, role: str, status: str, summary: str, payload: dict | None = None) -> None:
    with connect() as conn:
        store.add_event(
            conn, ctx.incident_id, agent=role, event_type="agent_run", status=status, summary=summary, payload=payload
        )


def run_role(
    ctx: RunContext,
    *,
    role: str,
    goal: str,
    prompt: str,
    output_model: type[OutputT],
    allowed_tools: tuple[str, ...] = (),
    required_tools: tuple[str, ...] = (),
    validate: Callable[[OutputT], None] | None = None,
) -> tuple[OutputT, list[str]]:
    """Run a role turn, retrying a failed turn up to the configured limit.

    Returns the validated output and the IDs of the evidence its tool calls
    produced. `validate` may raise InvalidOutput to reject an answer that fits
    the schema but breaks a rule the schema cannot express.
    """
    _event(ctx, role, "running", goal)
    evidence_ids: list[str] = []
    feedback = ""
    with span(role, "AGENT", input=goal, **{"agent.name": role, "neatlogs.agent.name": role}) as agent_span:
        for attempt in range(1, MAX_ROLE_ATTEMPTS + 1):
            call = RoleCall(
                role=role,
                prompt=prompt + feedback,
                output_model=output_model,
                allowed_tools=allowed_tools,
                required_tools=required_tools,
            )
            try:
                result: RoleResult = ctx.provider.run_turn(call)
            except LLMError as exc:
                evidence_ids += file_tool_calls(ctx, role, exc.tool_calls)
                _account(
                    ctx,
                    usage=exc.usage,
                    tool_calls=exc.tool_calls,
                    cost=ctx.provider.estimate_cost(exc.usage),
                    retry=attempt > 1,
                )
                failure = exc
            else:
                evidence_ids += file_tool_calls(ctx, role, result.tool_calls)
                _account(
                    ctx, usage=result.usage, tool_calls=result.tool_calls, cost=result.est_cost_usd, retry=attempt > 1
                )
                try:
                    if validate is not None:
                        validate(result.output)
                except InvalidOutput as exc:
                    failure = exc
                else:
                    _event(
                        ctx, role, "completed", f"{goal} — done in {result.duration_s:.0f}s",
                        payload={
                            "attempt": attempt,
                            "tool_calls": [c.tool for c in result.tool_calls],
                            "evidence_ids": evidence_ids,
                            "input_tokens": result.usage.input_tokens,
                            "cached_input_tokens": result.usage.cached_input_tokens,
                            "output_tokens": result.usage.output_tokens,
                            "est_cost_usd": round(result.est_cost_usd, 4),
                            "duration_s": round(result.duration_s, 1),
                            "output": result.output.model_dump(),
                        },
                    )  # fmt: skip
                    agent_span.set(**{"agent.attempts": attempt})
                    agent_span.set_output(result.output.model_dump())
                    return result.output, evidence_ids

            will_retry = failure.retryable and attempt < MAX_ROLE_ATTEMPTS
            _event(
                ctx, role, "failed", f"{type(failure).__name__}: {failure}",
                payload={"attempt": attempt, "will_retry": will_retry},
            )  # fmt: skip
            logger.warning("%s attempt %s failed: %s", role, attempt, failure)
            if not will_retry:
                raise failure
            feedback = (
                "\n\nA PREVIOUS ATTEMPT AT THIS TASK WAS REJECTED\n"
                f"Reason: {type(failure).__name__}: {failure}\nCorrect this in your answer.\n"
            )
    raise AssertionError("unreachable")  # the loop either returns or raises
