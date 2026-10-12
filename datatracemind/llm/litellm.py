"""The neatlogs LiteLLM gateway as the model provider.

The gateway speaks the OpenAI Chat Completions API, so each role turn is a
tool-calling loop run here: the model asks for tools, this module runs them
in-process against the read-only database role, and feeds the results back
until the model answers. The result is built from the calls recorded here,
not from what the model says happened. A turn is failed unless every required
tool call succeeded and the final message validates against the role's
output model.

The model sees only the role's allowed tools; it has no shell, files, or web.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import re
import threading
import time
from typing import Any

import openai
from openai import OpenAI
from pydantic import BaseModel, ValidationError, create_model

from datatracemind.config import get_settings
from datatracemind.llm.base import (
    Availability,
    InvalidOutput,
    LLMError,
    NotAuthenticated,
    ProviderError,
    RequiredToolMissing,
    RoleCall,
    RoleResult,
    TurnTimeout,
    UsageLimitReached,
)
from datatracemind.models import TokenUsage, ToolCallRecord
from datatracemind.observability import SpanHandle, span
from datatracemind.tools.investigation import ToolError
from datatracemind.tools.registry import TOOLS, ToolSpec

logger = logging.getLogger(__name__)

API_KEY_ENV = "LITELLM_API_KEY"
BASE_URL_ENV = "LITELLM_BASE_URL"
MODEL_ENV = "LITELLM_MODEL"
DEFAULT_BASE_URL = "https://litellm.vision.aivar.app/v1"
# Changing the model makes earlier runs incomparable: record the change in
# hackathon.md and bump the workflow version.
DEFAULT_MODEL = "claude-sonnet-4.6"
PROVIDER_LABEL = "neatlogs-litellm"

# USD per million tokens (input, cached input, output), list prices of the
# underlying models. The gateway is provided for the hackathon, so a figure
# computed from these is an estimate, not a charge.
PRICES_PER_MILLION = {
    "claude-opus-4.6": (5.00, 0.50, 25.00),
    "claude-sonnet-4.6": (3.00, 0.30, 15.00),
    "claude-haiku": (1.00, 0.10, 5.00),
    "nova-pro": (0.80, 0.20, 3.20),
    "nova-2-lite": (0.30, 0.075, 2.50),
    "nova-lite": (0.06, 0.015, 0.24),
    "nova-micro": (0.035, 0.00875, 0.14),
}

# Bounds on one turn: model requests, and tool calls per model request.
MAX_MODEL_REQUESTS = 24
MAX_TOOL_CALLS_PER_REQUEST = 8
# Times the model is shown why its answer was rejected before the turn fails.
OUTPUT_CORRECTIONS = 1
MAX_OUTPUT_TOKENS = 8000
TOOL_RESULT_CHARS = 30000
AVAILABILITY_TTL_S = 60.0

SYSTEM_PROMPT = """\
You are one role in DataTraceMind, a read-only investigation of an order-to-revenue data pipeline.
Use the tools to gather facts; never invent a tool result. When you have what you need, reply with
a single JSON object, and nothing else, that fits this JSON Schema:

{schema}"""


def estimate_cost_usd(model: str, usage: TokenUsage) -> float:
    """Cost of a turn at list prices. `input_tokens` includes cached ones."""
    fresh, cached, output = PRICES_PER_MILLION.get(
        model, PRICES_PER_MILLION[DEFAULT_MODEL]
    )
    uncached = max(usage.input_tokens - usage.cached_input_tokens, 0)
    return (
        uncached * fresh
        + usage.cached_input_tokens * cached
        + usage.output_tokens * output
    ) / 1_000_000


def _clean_schema(node: Any) -> Any:
    if isinstance(node, dict):
        return {
            key: _clean_schema(value) for key, value in node.items() if key != "title"
        }
    if isinstance(node, list):
        return [_clean_schema(item) for item in node]
    return node


def _arguments_model(spec: ToolSpec) -> type[BaseModel]:
    """A Pydantic model of the tool function's parameters."""
    fields: dict[str, Any] = {}
    for name, param in inspect.signature(spec.fn).parameters.items():
        annotation = (
            param.annotation if param.annotation is not inspect.Parameter.empty else Any
        )
        default = param.default if param.default is not inspect.Parameter.empty else ...
        fields[name] = (annotation, default)
    return create_model(f"{spec.name}_arguments", **fields)


_ARGUMENT_MODELS: dict[str, type[BaseModel]] = {
    name: _arguments_model(spec) for name, spec in TOOLS.items()
}


def tool_definition(name: str) -> dict:
    """The tool as an OpenAI function definition."""
    spec = TOOLS[name]
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": spec.description,
            "parameters": _clean_schema(_ARGUMENT_MODELS[name].model_json_schema()),
        },
    }


def run_tool(name: str, raw_arguments: str | None) -> tuple[dict, Any, str | None]:
    """Run one tool call. Returns (arguments, result, error); error is None on success.

    A tool's own refusals reach the model so it can correct the call. The
    text of any other exception is withheld, which keeps database errors and
    stack details out of the model's context.
    """
    try:
        arguments = json.loads(raw_arguments or "{}")
    except json.JSONDecodeError as exc:
        return {}, None, f"arguments are not valid JSON: {exc}"
    if not isinstance(arguments, dict):
        return {}, None, "arguments must be a JSON object"
    if name not in TOOLS:
        return arguments, None, f"unknown tool {name}"
    try:
        parsed = _ARGUMENT_MODELS[name].model_validate(arguments)
    except ValidationError as exc:
        return arguments, None, f"invalid arguments: {exc}"
    try:
        result = TOOLS[name].fn(**parsed.model_dump())
    except ToolError as exc:
        return arguments, None, str(exc)
    except Exception:
        logger.exception("tool %s failed", name)
        return arguments, None, f"Error executing tool {name}: internal error"
    return arguments, result, None


def extract_json(text: str) -> str:
    """The JSON object in a final message, without prose or code fences around it."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]
    return text


def _classify(exc: Exception) -> type[LLMError]:
    if isinstance(exc, openai.APITimeoutError):
        return TurnTimeout
    if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError)):
        return NotAuthenticated
    if isinstance(exc, openai.RateLimitError):
        return UsageLimitReached
    return ProviderError


class LiteLLMProvider:
    name = "litellm"

    def __init__(self, *, http_client: Any = None) -> None:
        get_settings()  # loads .env
        self.model = os.environ.get(MODEL_ENV) or DEFAULT_MODEL
        self.base_url = os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL
        self._http_client = http_client
        self._client: OpenAI | None = None
        self._lock = threading.Lock()

    def _get_client(self) -> OpenAI:
        with self._lock:
            if self._client is None:
                self._client = OpenAI(
                    base_url=self.base_url,
                    api_key=os.environ.get(API_KEY_ENV, ""),
                    max_retries=2,
                    http_client=self._http_client,
                )
            return self._client

    def availability(self) -> Availability:
        """Whether a turn could run now. Never raises."""
        if os.environ.get(API_KEY_ENV):
            return Availability(
                True,
                "api_key",
                f"neatlogs LiteLLM gateway ({self.model}) with {API_KEY_ENV}.",
            )
        return Availability(
            False,
            "none",
            f"Set {API_KEY_ENV} in .env to use the neatlogs LiteLLM gateway.",
        )

    def estimate_cost(self, usage: TokenUsage) -> float:
        return estimate_cost_usd(self.model, usage)

    # --- One turn ------------------------------------------------------------

    def run_turn(self, call: RoleCall) -> RoleResult:
        unknown = [
            tool
            for tool in (*call.allowed_tools, *call.required_tools)
            if tool not in TOOLS
        ]
        if unknown:
            raise ValueError(f"role {call.role} names unknown tools: {unknown}")
        if not os.environ.get(API_KEY_ENV):
            raise NotAuthenticated(f"{call.role}: {API_KEY_ENV} is not set")
        attributes = {
            "neatlogs.llm.provider": PROVIDER_LABEL,
            "neatlogs.llm.model_name": self.model,
            "llm.model_name": self.model,
            "litellm.role": call.role,
            "litellm.allowed_tools": list(call.allowed_tools),
            "litellm.required_tools": list(call.required_tools),
        }
        with span(
            f"litellm.chat {call.role}", "LLM", input=call.prompt, **attributes
        ) as turn_span:
            return self._run(call, turn_span)

    def _run(self, call: RoleCall, turn_span: SpanHandle) -> RoleResult:
        started = time.monotonic()
        deadline = started + call.timeout_s
        tool_calls: list[ToolCallRecord] = []
        usage = TokenUsage()
        client = self._get_client()
        schema = json.dumps(_clean_schema(call.output_model.model_json_schema()))
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT.format(schema=schema)},
            {"role": "user", "content": call.prompt},
        ]
        tools = [tool_definition(name) for name in call.allowed_tools]
        corrections = 0

        def fail(kind: type[LLMError], message: str) -> LLMError:
            self._record(turn_span, usage, tool_calls, started)
            return kind(f"{call.role}: {message}", tool_calls=tool_calls, usage=usage,
                        duration_s=time.monotonic() - started)  # fmt: skip

        for _ in range(MAX_MODEL_REQUESTS):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise fail(TurnTimeout, f"no result after {call.timeout_s:.0f}s")
            request: dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "max_tokens": MAX_OUTPUT_TOKENS,
            }
            if tools:
                request["tools"] = tools
            try:
                response = client.chat.completions.create(**request, timeout=remaining)
            except openai.OpenAIError as exc:
                raise fail(_classify(exc), str(exc)) from exc
            self._add_usage(usage, response.usage)
            if not response.choices:
                raise fail(ProviderError, "the gateway returned no choices")
            message = response.choices[0].message
            requested = list(message.tool_calls or [])

            if requested:
                messages.append(
                    {
                        "role": "assistant",
                        "content": message.content or "",
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {
                                    "name": tc.function.name,
                                    "arguments": tc.function.arguments or "{}",
                                },
                            }
                            for tc in requested
                        ],
                    }
                )
                for index, tc in enumerate(requested):
                    if (
                        index >= MAX_TOOL_CALLS_PER_REQUEST
                        or tc.function.name not in call.allowed_tools
                    ):
                        reason = (
                            "too many tool calls in one step"
                            if index >= MAX_TOOL_CALLS_PER_REQUEST
                            else f"tool {tc.function.name} is not available to this role"
                        )
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": f"Error: {reason}",
                            }
                        )
                        continue
                    record = self._call_tool(
                        tc.id, tc.function.name, tc.function.arguments
                    )
                    tool_calls.append(record)
                    content = (
                        json.dumps(record.result, default=str)
                        if record.status == "completed"
                        else f"Error: {record.error}"
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": content[:TOOL_RESULT_CHARS],
                        }
                    )
                continue

            final = message.content or ""
            try:
                output = call.output_model.model_validate_json(extract_json(final))
            except ValidationError as exc:
                if corrections >= OUTPUT_CORRECTIONS:
                    raise fail(
                        InvalidOutput, f"final message does not fit the schema: {exc}"
                    ) from exc
                corrections += 1
                messages.append({"role": "assistant", "content": final})
                messages.append(
                    {
                        "role": "user",
                        "content": f"That answer does not fit the schema: {exc}\n"
                        "Reply again with only the corrected JSON object.",
                    }
                )
                continue

            succeeded = {c.tool for c in tool_calls if c.status == "completed"}
            missing = [tool for tool in call.required_tools if tool not in succeeded]
            if missing:
                # An answer without these calls rests on invented values.
                raise fail(
                    RequiredToolMissing,
                    f"required tool call(s) did not succeed: {missing}",
                )
            duration = self._record(turn_span, usage, tool_calls, started)
            turn_span.set_output(output.model_dump())
            return RoleResult(
                output=output,
                tool_calls=tool_calls,
                usage=usage,
                duration_s=duration,
                model=self.model,
                reasoning_effort="default",
                est_cost_usd=estimate_cost_usd(self.model, usage),
            )
        raise fail(
            InvalidOutput, f"no final answer after {MAX_MODEL_REQUESTS} model requests"
        )

    def _record(
        self,
        turn_span: SpanHandle,
        usage: TokenUsage,
        tool_calls: list[ToolCallRecord],
        started: float,
    ) -> float:
        duration = time.monotonic() - started
        turn_span.record_usage(
            provider=PROVIDER_LABEL,
            model=self.model,
            input_tokens=usage.input_tokens,
            cached_input_tokens=usage.cached_input_tokens,
            output_tokens=usage.output_tokens,
            reasoning_tokens=usage.reasoning_output_tokens,
            cost_usd=estimate_cost_usd(self.model, usage),
        )
        turn_span.set(
            **{
                "litellm.duration_s": round(duration, 2),
                "litellm.tool_calls": len(tool_calls),
                "litellm.failed_tool_calls": sum(
                    1 for c in tool_calls if c.status == "failed"
                ),
                "litellm.cost_basis": "estimate at the underlying models' list prices",
            }
        )
        return duration

    @staticmethod
    def _add_usage(usage: TokenUsage, reported: Any) -> None:
        if reported is None:
            return
        usage.input_tokens += int(getattr(reported, "prompt_tokens", 0) or 0)
        usage.output_tokens += int(getattr(reported, "completion_tokens", 0) or 0)
        prompt_details = getattr(reported, "prompt_tokens_details", None)
        usage.cached_input_tokens += int(
            getattr(prompt_details, "cached_tokens", 0) or 0
        )
        completion_details = getattr(reported, "completion_tokens_details", None)
        usage.reasoning_output_tokens += int(
            getattr(completion_details, "reasoning_tokens", 0) or 0
        )

    @staticmethod
    def _call_tool(
        call_id: str, name: str, raw_arguments: str | None
    ) -> ToolCallRecord:
        spec = TOOLS[name]
        began = time.monotonic()
        with span(
            name,
            "MCP_TOOL",
            input=raw_arguments,
            **{
                "neatlogs.mcp_tool.name": name,
                "neatlogs.mcp_tool.description": spec.description,
                "neatlogs.mcp_tool.call_id": call_id,
            },
        ) as handle:
            arguments, result, error = run_tool(name, raw_arguments)
            record = ToolCallRecord(
                tool=name,
                arguments=arguments,
                result=result,
                status="completed" if error is None else "failed",
                error=error,
                duration_s=round(time.monotonic() - began, 3),
            )
            handle.set(
                **{
                    "neatlogs.mcp_tool.arguments": arguments,
                    "mcp.tool.duration_s": record.duration_s,
                }
            )
            if error is None:
                handle.set(
                    **{"output.value": result, "neatlogs.mcp_tool.output": result}
                )
            else:
                handle.fail(error)
        return record
