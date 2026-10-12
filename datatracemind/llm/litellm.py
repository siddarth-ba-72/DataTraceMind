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

import json
import logging
import os
import threading
import time
from typing import Any

import openai
from openai import OpenAI
from pydantic import ValidationError

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
from datatracemind.llm.inprocess import (
    SYSTEM_PROMPT,
    call_tool,
    clean_schema,
    extract_json,
    parameters_schema,
)
from datatracemind.models import TokenUsage, ToolCallRecord
from datatracemind.tools.registry import TOOLS

logger = logging.getLogger(__name__)

API_KEY_ENV = "LITELLM_API_KEY"
BASE_URL_ENV = "LITELLM_BASE_URL"
MODEL_ENV = "LITELLM_MODEL"
DEFAULT_BASE_URL = "https://litellm.vision.aivar.app/v1"
# Changing the model makes earlier runs incomparable: record the change in
# hackathon.md and bump the workflow version.
DEFAULT_MODEL = "claude-sonnet-4.6"

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


def tool_definition(name: str) -> dict:
    """The tool as an OpenAI function definition."""
    spec = TOOLS[name]
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": spec.description,
            "parameters": parameters_schema(name),
        },
    }


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
        started = time.monotonic()
        deadline = started + call.timeout_s
        tool_calls: list[ToolCallRecord] = []
        usage = TokenUsage()
        client = self._get_client()
        schema = json.dumps(clean_schema(call.output_model.model_json_schema()))
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT.format(schema=schema)},
            {"role": "user", "content": call.prompt},
        ]
        tools = [tool_definition(name) for name in call.allowed_tools]
        corrections = 0

        def fail(kind: type[LLMError], message: str) -> LLMError:
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
                    record = self._call_tool(tc.function.name, tc.function.arguments)
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
            return RoleResult(
                output=output,
                tool_calls=tool_calls,
                usage=usage,
                duration_s=time.monotonic() - started,
                model=self.model,
                reasoning_effort="default",
                est_cost_usd=estimate_cost_usd(self.model, usage),
            )
        raise fail(
            InvalidOutput, f"no final answer after {MAX_MODEL_REQUESTS} model requests"
        )

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
    def _call_tool(name: str, raw_arguments: str | None) -> ToolCallRecord:
        try:
            arguments = json.loads(raw_arguments or "{}")
        except json.JSONDecodeError as exc:
            return call_tool(name, {}, rejected=f"arguments are not valid JSON: {exc}")
        return call_tool(name, arguments)
