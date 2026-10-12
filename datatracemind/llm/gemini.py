"""Gemini as the model provider, run through Google's Agent Development Kit.

Each role turn is one ADK `LlmAgent`: the role's prompt, the tools it is
allowed, and nothing carried over from another role. ADK runs the
tool-calling loop. The tools are this project's own: every call is validated,
run in-process against the read-only database role, and recorded here, so the
result is built from the calls that were made, not from what the model says
happened. A turn is failed unless every required tool call succeeded and the
final message validates against the role's output model.

The model sees only the role's allowed tools; it has no shell, files, or web.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

from google.adk.agents import LlmAgent, RunConfig
from google.adk.agents.invocation_context import LlmCallsLimitExceededError
from google.adk.models import BaseLlm, Gemini, LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools import BaseTool
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel, ValidationError

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
from datatracemind.llm.inprocess import SYSTEM_PROMPT, call_tool, clean_schema, extract_json, parameters_schema
from datatracemind.models import TokenUsage, ToolCallRecord
from datatracemind.tools.registry import TOOLS

logger = logging.getLogger(__name__)

# GOOGLE_API_KEY is the name Google's own tools use for the same key.
API_KEY_ENVS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
MODEL_ENV = "GEMINI_MODEL"
# Changing the model makes earlier runs incomparable: record the change in
# hackathon.md and bump the workflow version.
# The default is the model a free-tier key can finish an investigation with.
# On 12 October 2026 that tier gave gemini-3.8-flash 5 requests a minute and
# 20 a day, fewer than two investigations; set GEMINI_MODEL to it on a paid key.
DEFAULT_MODEL = "gemini-3.5-flash-lite"
APP_NAME = "datatracemind"

# USD per million tokens (input, cached input, output), standard paid-tier
# list prices read on 12 October 2026; the two Pro models at their rate for
# prompts up to 200k tokens. A key on the free tier is charged nothing, so a
# figure computed from these is an estimate, not a charge.
PRICES_PER_MILLION = {
    "gemini-3.8-flash": (0.75, 0.075, 3.75),
    "gemini-3.6-flash": (0.75, 0.075, 3.75),
    "gemini-3.5-flash-lite": (0.30, 0.03, 2.50),
    "gemini-3.1-flash-lite": (0.25, 0.025, 1.50),
    "gemini-3.1-pro-preview": (2.00, 0.20, 12.00),
    "gemini-2.5-pro": (1.25, 0.125, 10.00),
    "gemini-2.5-flash": (0.30, 0.03, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.01, 0.40),
}

# Bounds on one turn: model requests, and tool calls across them.
MAX_MODEL_REQUESTS = 24
MAX_TOOL_CALLS = 48
# Times the model is shown why its answer was rejected before the turn fails.
OUTPUT_CORRECTIONS = 1
MAX_OUTPUT_TOKENS = 8000
TOOL_RESULT_CHARS = 30000
# A request the service could not take (a server error, or the model being
# busy) is sent again with growing pauses, about half a minute in all. A rate
# limit is not in this list: it is waited out for as long as the API says.
HTTP_RETRIES = types.HttpRetryOptions(
    attempts=5, initial_delay=2.0, max_delay=30.0, http_status_codes=[408, 500, 502, 503, 504]
)
# Waiting for a per-minute quota to refill: the longest single wait accepted,
# and how many of them one request gets. The turn's time limit bounds the total.
RATE_LIMIT_MAX_WAIT_S = 65.0
RATE_LIMIT_WAITS = 6


def api_key() -> str:
    return next((os.environ[name] for name in API_KEY_ENVS if os.environ.get(name)), "")


def estimate_cost_usd(model: str, usage: TokenUsage) -> float:
    """Cost of a turn at list prices. `input_tokens` includes cached ones."""
    fresh, cached, output = PRICES_PER_MILLION.get(model, PRICES_PER_MILLION[DEFAULT_MODEL])
    uncached = max(usage.input_tokens - usage.cached_input_tokens, 0)
    return (uncached * fresh + usage.cached_input_tokens * cached + usage.output_tokens * output) / 1_000_000


@dataclass
class _Turn:
    """What one turn has recorded so far. Outlives a failure, so that a failed turn's evidence is kept."""

    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    usage: TokenUsage = field(default_factory=TokenUsage)
    model_requests: int = 0

    def count(self, callback_context: Any, llm_response: LlmResponse) -> None:
        """ADK after-model callback: one model request answered."""
        self.model_requests += 1
        reported = llm_response.usage_metadata
        if reported is None:
            return
        thoughts = int(reported.thoughts_token_count or 0)
        self.usage.input_tokens += int(reported.prompt_token_count or 0)
        self.usage.cached_input_tokens += int(reported.cached_content_token_count or 0)
        # Gemini reports thinking apart from the answer and bills both as output.
        self.usage.output_tokens += int(reported.candidates_token_count or 0) + thoughts
        self.usage.reasoning_output_tokens += thoughts


class _Rejected(Exception):
    """The final message did not fit the role's output model, after correction."""


class _ModelStopped(Exception):
    """The model ended a request without an answer (blocked, cut off, or a malformed tool call)."""


def _rate_limit_wait(exc: Exception) -> float | None:
    """Seconds until a request refused for a per-minute quota may be sent again.

    None when waiting will not help: another error, a daily quota, or no
    refill time given.
    """
    if getattr(exc, "code", None) != 429:
        return None
    body = getattr(exc, "details", None)
    details = body.get("error", {}).get("details", []) if isinstance(body, dict) else []
    details = [item for item in details if isinstance(item, dict)]
    quotas = [violation.get("quotaId", "") for item in details for violation in item.get("violations", [])]
    delay = next((item["retryDelay"] for item in details if "retryDelay" in item), None)
    if delay is None or any("PerDay" in quota for quota in quotas):
        return None
    try:
        seconds = float(str(delay).removesuffix("s"))
    except ValueError:
        return None
    return seconds if seconds <= RATE_LIMIT_MAX_WAIT_S else None


class PatientGemini(Gemini):
    """Gemini that waits out a per-minute rate limit instead of failing the request.

    The free tier allows a handful of requests a minute, fewer than one
    investigation makes. The API says when the quota refills, so the request
    is sent again then.
    """

    async def generate_content_async(self, llm_request, stream=False):
        for waits in itertools.count():
            try:
                async for response in super().generate_content_async(llm_request, stream):
                    yield response
                return
            except genai_errors.ClientError as exc:
                wait = _rate_limit_wait(exc)
                if wait is None or waits >= RATE_LIMIT_WAITS:
                    raise
                logger.info("Gemini rate limit reached; sending the request again in %.0fs", wait + 1)
                await asyncio.sleep(wait + 1)


def _shown(result: Any) -> dict[str, Any]:
    """A tool result as the model sees it: plain JSON, and bounded in size."""
    text = json.dumps(result, default=str)
    if len(text) > TOOL_RESULT_CHARS:
        return {"truncated": True, "result": text[:TOOL_RESULT_CHARS]}
    shown = json.loads(text)
    return shown if isinstance(shown, dict) else {"result": shown}


class InvestigationTool(BaseTool):
    """One investigation tool, offered to the model for a single turn."""

    def __init__(self, name: str, turn: _Turn) -> None:
        super().__init__(name=name, description=TOOLS[name].description)
        self._turn = turn

    def _get_declaration(self) -> types.FunctionDeclaration:
        return types.FunctionDeclaration(
            name=self.name, description=self.description, parameters_json_schema=parameters_schema(self.name)
        )

    async def run_async(self, *, args: dict[str, Any], tool_context: Any) -> dict[str, Any]:
        if len(self._turn.tool_calls) >= MAX_TOOL_CALLS:
            return {"error": "this turn has used all its tool calls; answer with what you have"}
        # Off the event loop: a tool waits on the database, and calls the model
        # asks for together should not queue behind each other.
        record = await asyncio.to_thread(call_tool, self.name, args)
        self._turn.tool_calls.append(record)
        if record.status != "completed":
            return {"error": record.error}
        return _shown(record.result)


def _describe(exc: genai_errors.APIError) -> str:
    """The API's own account of an error, without the payload around it."""
    return f"{exc.code} {exc.status}: {exc.message}"


def _classify(exc: Exception) -> type[LLMError]:
    code = getattr(exc, "code", None)
    text = str(exc).lower()
    if isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower():
        return TurnTimeout
    # The Gemini API answers an unusable key with 400, not 401.
    if code in (401, 403) or "api key" in text or "api_key" in text:
        return NotAuthenticated
    if code == 429:
        return UsageLimitReached
    return ProviderError


class GeminiProvider:
    name = "gemini"

    def __init__(self, *, llm: BaseLlm | None = None) -> None:
        """`llm` replaces the Gemini client, which is how the tests run a turn without the API."""
        get_settings()  # loads .env
        self._llm = llm
        self.model = llm.model if llm is not None else os.environ.get(MODEL_ENV) or DEFAULT_MODEL

    def _get_llm(self) -> BaseLlm:
        # Built for each turn: ADK keeps one API client per event loop, and
        # every turn runs on a loop of its own.
        return self._llm or PatientGemini(
            model=self.model, client_kwargs={"api_key": api_key()}, retry_options=HTTP_RETRIES
        )

    def availability(self) -> Availability:
        """Whether a turn could run now. Never raises."""
        if api_key():
            return Availability(True, "api_key", f"Gemini ({self.model}) through Google ADK with {API_KEY_ENVS[0]}.")
        return Availability(False, "none", f"Set {API_KEY_ENVS[0]} in .env to use Gemini.")

    def estimate_cost(self, usage: TokenUsage) -> float:
        return estimate_cost_usd(self.model, usage)

    # --- One turn ------------------------------------------------------------

    def run_turn(self, call: RoleCall) -> RoleResult:
        unknown = [tool for tool in (*call.allowed_tools, *call.required_tools) if tool not in TOOLS]
        if unknown:
            raise ValueError(f"role {call.role} names unknown tools: {unknown}")
        if self._llm is None and not api_key():
            raise NotAuthenticated(f"{call.role}: {API_KEY_ENVS[0]} is not set")
        started = time.monotonic()
        turn = _Turn()

        def fail(kind: type[LLMError], message: str) -> LLMError:
            return kind(f"{call.role}: {message}", tool_calls=turn.tool_calls, usage=turn.usage,
                        duration_s=time.monotonic() - started)  # fmt: skip

        try:
            # A loop per turn: roles run on worker threads, one turn each.
            output = asyncio.run(asyncio.wait_for(self._converse(call, turn), timeout=call.timeout_s))
        except TimeoutError as exc:
            raise fail(TurnTimeout, f"no result after {call.timeout_s:.0f}s") from exc
        except LlmCallsLimitExceededError as exc:
            raise fail(InvalidOutput, f"no final answer after {MAX_MODEL_REQUESTS} model requests") from exc
        except _Rejected as exc:
            raise fail(InvalidOutput, f"final message does not fit the schema: {exc}") from exc
        except _ModelStopped as exc:
            raise fail(ProviderError, f"the model stopped without an answer: {exc}") from exc
        except genai_errors.APIError as exc:
            raise fail(_classify(exc), _describe(exc)) from exc
        except Exception as exc:
            # A transport failure, or anything else ADK raised mid-turn.
            logger.exception("%s: the Gemini turn failed", call.role)
            raise fail(_classify(exc), f"{type(exc).__name__}: {exc}") from exc

        succeeded = {c.tool for c in turn.tool_calls if c.status == "completed"}
        missing = [tool for tool in call.required_tools if tool not in succeeded]
        if missing:
            # An answer without these calls rests on invented values.
            raise fail(RequiredToolMissing, f"required tool call(s) did not succeed: {missing}")
        return RoleResult(
            output=output,
            tool_calls=turn.tool_calls,
            usage=turn.usage,
            duration_s=time.monotonic() - started,
            model=self.model,
            reasoning_effort="default",
            est_cost_usd=estimate_cost_usd(self.model, turn.usage),
        )

    async def _converse(self, call: RoleCall, turn: _Turn) -> BaseModel:
        """Run the role's agent to an answer that fits its output model."""
        schema = json.dumps(clean_schema(call.output_model.model_json_schema()))
        system_prompt = SYSTEM_PROMPT.format(schema=schema)
        agent = LlmAgent(
            name=call.role,
            model=self._get_llm(),
            # A function, so that ADK sends the text as written: a plain string
            # would have its braces read as placeholders, and the schema is full of them.
            instruction=lambda _context: system_prompt,
            tools=[InvestigationTool(name, turn) for name in call.allowed_tools],
            generate_content_config=types.GenerateContentConfig(max_output_tokens=MAX_OUTPUT_TOKENS),
            after_model_callback=turn.count,
            disallow_transfer_to_parent=True,
            disallow_transfer_to_peers=True,
        )
        runner = Runner(
            agent=agent, app_name=APP_NAME, session_service=InMemorySessionService(), auto_create_session=True
        )
        message = call.prompt
        for corrections in range(OUTPUT_CORRECTIONS + 1):
            final = await self._ask(runner, call.role, message, turn)
            try:
                return call.output_model.model_validate_json(extract_json(final))
            except ValidationError as exc:
                if corrections == OUTPUT_CORRECTIONS:
                    raise _Rejected(str(exc)) from exc
                # Same session, so the model sees its own answer above this.
                message = (
                    f"That answer does not fit the schema: {exc}\nReply again with only the corrected JSON object."
                )
        raise AssertionError("unreachable")  # the loop either returns or raises

    @staticmethod
    async def _ask(runner: Runner, role: str, message: str, turn: _Turn) -> str:
        """Send one message and let ADK run the tool loop. Returns the final text."""
        remaining = MAX_MODEL_REQUESTS - turn.model_requests
        if remaining <= 0:
            raise LlmCallsLimitExceededError(f"{MAX_MODEL_REQUESTS} model requests used")
        final, stopped = "", None
        # Read to the end rather than leaving at the first problem: when a
        # request fails, ADK reports it as an event and then raises the
        # error itself, and that error says more than the event does.
        async for event in runner.run_async(
            user_id="supervisor",
            session_id=role,
            new_message=types.Content(role="user", parts=[types.Part(text=message)]),
            run_config=RunConfig(max_llm_calls=remaining),
        ):
            if event.error_code:
                stopped = f"{event.error_code} {event.error_message or ''}".strip()
            elif event.is_final_response() and event.content:
                final = "".join(part.text for part in event.content.parts or [] if part.text and not part.thought)
        if stopped:
            raise _ModelStopped(stopped)
        return final
