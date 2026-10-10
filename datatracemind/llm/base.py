"""Provider-neutral contract for one model-backed role turn.

In: a role name, a prompt built from typed state, the tools the role may use
and the ones it must use, and the Pydantic model its answer must fit.
Out: the validated answer, the tool calls as the provider recorded them,
token usage, and timing. Nothing here names a provider; replacing Codex means
writing another class with the same `run_turn` and `availability`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from pydantic import BaseModel

from datatracemind.models import TokenUsage, ToolCallRecord


@dataclass(frozen=True)
class RoleCall:
    role: str
    prompt: str
    output_model: type[BaseModel]
    allowed_tools: tuple[str, ...] = ()
    # The turn fails unless each of these was called at least once and succeeded.
    required_tools: tuple[str, ...] = ()
    timeout_s: float = 240.0


@dataclass
class RoleResult:
    output: BaseModel
    tool_calls: list[ToolCallRecord]
    usage: TokenUsage
    duration_s: float
    model: str
    reasoning_effort: str
    est_cost_usd: float
    thread_id: str | None = None


@dataclass(frozen=True)
class Availability:
    available: bool
    method: str  # "chatgpt_login" | "api_key" | "none"
    detail: str


class LLMError(Exception):
    """A role turn failed. Carries whatever was recorded before it failed."""

    retryable = True

    def __init__(
        self,
        message: str,
        *,
        tool_calls: list[ToolCallRecord] | None = None,
        usage: TokenUsage | None = None,
        duration_s: float = 0.0,
    ) -> None:
        super().__init__(message)
        self.tool_calls = tool_calls or []
        self.usage = usage or TokenUsage()
        self.duration_s = duration_s


class NotAuthenticated(LLMError):
    """No model credentials on this machine."""

    retryable = False


class UsageLimitReached(LLMError):
    """The account's usage allowance is exhausted."""

    retryable = False


class TurnTimeout(LLMError):
    """The turn did not finish in time and was killed."""


class ToolServerFailed(LLMError):
    """The tool server did not start, so the model had no tools."""


class RequiredToolMissing(LLMError):
    """A tool the role depends on was not called, or did not succeed."""


class InvalidOutput(LLMError):
    """The final message did not fit the role's output model."""


class ProviderError(LLMError):
    """The provider reported a failure not covered above."""


class Provider(Protocol):
    name: str
    model: str

    def availability(self) -> Availability: ...

    def run_turn(self, call: RoleCall) -> RoleResult: ...

    def estimate_cost(self, usage: TokenUsage) -> float: ...


@dataclass
class UsageTotals:
    """Running totals for one investigation run."""

    role_turns: int = 0
    tool_calls: int = 0
    failed_tool_calls: int = 0
    retries: int = 0
    est_cost_usd: float = 0.0
    usage: TokenUsage = field(default_factory=TokenUsage)

    def add(self, *, usage: TokenUsage, tool_calls: list[ToolCallRecord], cost: float, retry: bool) -> None:
        self.role_turns += 1
        self.retries += int(retry)
        self.usage = self.usage + usage
        self.tool_calls += len(tool_calls)
        self.failed_tool_calls += sum(1 for call in tool_calls if call.status == "failed")
        self.est_cost_usd += cost
