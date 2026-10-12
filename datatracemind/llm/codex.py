"""Codex CLI as the model provider. See docs/CODEX_LLM_PROVIDER.md.

Each role turn is one `codex exec` process. Codex runs the tool loop itself
against our MCP server; this module reads its JSON event stream and builds
the result from what was recorded there, not from what the model says
happened. A turn is failed unless every required tool call succeeded and the
final message validates against the role's output model.

The Codex shell and web search are disabled and the sandbox is read-only, so
the MCP tools are the model's only way to touch anything.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from datatracemind.config import REPO_ROOT
from datatracemind.llm.base import (
    Availability,
    InvalidOutput,
    LLMError,
    NotAuthenticated,
    ProviderError,
    RequiredToolMissing,
    RoleCall,
    RoleResult,
    ToolServerFailed,
    TurnTimeout,
    UsageLimitReached,
)
from datatracemind.models import TokenUsage, ToolCallRecord
from datatracemind.observability import SpanHandle, span
from datatracemind.tools.registry import TOOLS

logger = logging.getLogger(__name__)

# Fixed for every call. Changing either makes earlier runs incomparable:
# record the change in hackathon.md and bump the workflow version.
MODEL = "gpt-6-sol"
REASONING_EFFORT = "medium"

MCP_SERVER_NAME = "dtm"
MCP_SERVER_SCRIPT = REPO_ROOT / "datatracemind" / "tools" / "mcp_server.py"
# Read-only settings the tool server may inherit from this process.
FORWARDED_ENV = ("DTM_READONLY_DATABASE_URL", "DTM_POSTGRES_PORT")
# Never handed to the Codex process.
WITHHELD_ENV = ("NEATLOGS_API_KEY", "LITELLM_API_KEY", "DTM_DATABASE_URL", "DTM_DEMO_ACCESS_CODE")

# USD per million tokens (input, cached input, output), API list prices
# published 10 October 2026. ChatGPT Plus is flat-rate, so a figure computed
# from these is an estimate, not a charge.
PRICES_PER_MILLION = {
    "gpt-6-sol": (2.00, 0.20, 10.00),
    "gpt-6-luna": (0.10, 0.01, 0.50),
}

AVAILABILITY_TTL_S = 60.0
STDERR_TAIL_CHARS = 2000
# Optional debugging aid: a directory that receives each turn's raw event
# stream. The events contain tool results, so keep it out of the repository.
EVENT_LOG_ENV = "DTM_CODEX_EVENT_LOG_DIR"


def estimate_cost_usd(model: str, usage: TokenUsage) -> float:
    """Cost of a turn at API list prices. `input_tokens` includes cached ones."""
    fresh, cached, output = PRICES_PER_MILLION.get(model, PRICES_PER_MILLION[MODEL])
    uncached = max(usage.input_tokens - usage.cached_input_tokens, 0)
    return (uncached * fresh + usage.cached_input_tokens * cached + usage.output_tokens * output) / 1_000_000


def strict_schema(model: type[BaseModel]) -> dict:
    """JSON Schema in the strict form Codex's --output-schema accepts."""
    dropped = {"title", "default", "minimum", "maximum"}

    def clean(node: Any) -> Any:
        if isinstance(node, dict):
            return {key: clean(value) for key, value in node.items() if key not in dropped}
        if isinstance(node, list):
            return [clean(item) for item in node]
        return node

    return clean(model.model_json_schema())


def _toml(value: Any) -> str:
    """A string or list of strings as a TOML value (JSON is valid TOML here)."""
    return json.dumps(value)


def _tool_result(item: dict) -> Any:
    """The structured result of a completed mcp_tool_call item."""
    result = item.get("result") or {}
    structured = result.get("structured_content", result.get("structuredContent"))
    if structured is not None:
        # A non-object return value arrives wrapped as {"result": ...}.
        if isinstance(structured, dict) and set(structured) == {"result"}:
            return structured["result"]
        return structured
    for block in result.get("content") or []:
        if block.get("type") == "text":
            try:
                return json.loads(block["text"])
            except (json.JSONDecodeError, TypeError):
                return block.get("text")
    return None


def _tool_error(item: dict) -> str:
    """Why a tool call failed.

    Codex fills `error.message` when it could not make the call (for example
    approval refused). When the tool itself reports an error, `error` is null
    and the message is the text of the result.
    """
    message = (item.get("error") or {}).get("message")
    if message:
        return message
    for block in (item.get("result") or {}).get("content") or []:
        if block.get("type") == "text" and block.get("text"):
            return block["text"]
    return f"tool call ended with status {item.get('status')}"


def _classify_failure(message: str) -> type[LLMError]:
    text = message.lower()
    if any(marker in text for marker in ("usage limit", "rate limit", "quota", "too many requests", "429")):
        return UsageLimitReached
    if any(marker in text for marker in ("not logged in", "401", "unauthorized", "login", "authentication")):
        return NotAuthenticated
    if "mcp" in text and any(marker in text for marker in ("failed to start", "required", "startup", "timed out")):
        return ToolServerFailed
    return ProviderError


class CodexProvider:
    name = "codex"
    model = MODEL

    def __init__(self) -> None:
        self._availability: tuple[float, Availability] | None = None
        self._lock = threading.Lock()

    # --- Authentication ------------------------------------------------------

    def availability(self) -> Availability:
        """Whether a turn could run now. Cached briefly; never raises."""
        with self._lock:
            if self._availability and time.monotonic() - self._availability[0] < AVAILABILITY_TTL_S:
                return self._availability[1]
            result = self._check_availability()
            self._availability = (time.monotonic(), result)
            return result

    def _check_availability(self) -> Availability:
        fix = "Install the Codex CLI and run `codex login`, or set CODEX_API_KEY."
        if shutil.which("codex") is None:
            return Availability(False, "none", f"The Codex CLI is not installed. {fix}")
        if os.environ.get("CODEX_API_KEY"):
            return Availability(True, "api_key", "Codex CLI authenticated with CODEX_API_KEY.")
        try:
            status = subprocess.run(
                ["codex", "login", "status"], capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return Availability(False, "none", f"Could not check the Codex sign-in ({type(exc).__name__}). {fix}")
        if status.returncode == 0:
            return Availability(True, "chatgpt_login", "Codex CLI signed in with ChatGPT.")
        return Availability(False, "none", f"The Codex CLI is not signed in. {fix}")

    def estimate_cost(self, usage: TokenUsage) -> float:
        return estimate_cost_usd(MODEL, usage)

    # --- One turn ------------------------------------------------------------

    def _command(self, call: RoleCall, workdir: Path, schema_file: Path) -> list[str]:
        server = f"mcp_servers.{MCP_SERVER_NAME}"
        forwarded = [name for name in FORWARDED_ENV if os.environ.get(name)]
        command = [
            "codex", "exec", "--json", "--ephemeral", "--skip-git-repo-check", "--ignore-user-config",
            "-m", MODEL, "-c", f"model_reasoning_effort={_toml(REASONING_EFFORT)}",
            "-s", "read-only", "-C", str(workdir),
            "--disable", "shell_tool", "-c", 'web_search="disabled"',
        ]  # fmt: skip
        # A role with no tools gets no tool server at all.
        if call.allowed_tools:
            command += [
                "-c", f"{server}.command={_toml(sys.executable)}",
                "-c", f"{server}.args={_toml([str(MCP_SERVER_SCRIPT)])}",
                "-c", f"{server}.required=true",
                "-c", f'{server}.default_tools_approval_mode="approve"',
                "-c", f"{server}.enabled_tools={_toml(list(call.allowed_tools))}",
            ]  # fmt: skip
            if forwarded:
                command += ["-c", f"{server}.env_vars={_toml(forwarded)}"]
        command += ["--output-schema", str(schema_file), call.prompt]
        return command

    def run_turn(self, call: RoleCall) -> RoleResult:
        unknown = [tool for tool in (*call.allowed_tools, *call.required_tools) if tool not in TOOLS]
        if unknown:
            raise ValueError(f"role {call.role} names unknown tools: {unknown}")
        attributes = {
            "neatlogs.llm.provider": "openai-codex-cli",
            "neatlogs.llm.model_name": MODEL,
            "llm.model_name": MODEL,
            "codex.reasoning_effort": REASONING_EFFORT,
            "codex.role": call.role,
            "codex.allowed_tools": list(call.allowed_tools),
            "codex.required_tools": list(call.required_tools),
        }
        with span(f"codex.exec {call.role}", "LLM", input=call.prompt, **attributes) as turn_span:
            return self._run(call, turn_span)

    def _run(self, call: RoleCall, turn_span: SpanHandle) -> RoleResult:
        started = time.monotonic()
        tool_calls: list[ToolCallRecord] = []
        usage = TokenUsage()
        state: dict[str, Any] = {"thread_id": None, "final": None, "failure": None, "timed_out": False}
        open_tools: dict[str, tuple[Any, SpanHandle, float]] = {}

        def fail(kind: type[LLMError], message: str) -> LLMError:
            return kind(message, tool_calls=tool_calls, usage=usage, duration_s=time.monotonic() - started)

        with tempfile.TemporaryDirectory(prefix="dtm-codex-") as scratch:
            workdir = Path(scratch) / "work"
            workdir.mkdir()
            schema_file = Path(scratch) / "schema.json"
            schema_file.write_text(json.dumps(strict_schema(call.output_model)))
            env = {key: value for key, value in os.environ.items() if key not in WITHHELD_ENV}
            try:
                process = subprocess.Popen(
                    self._command(call, workdir, schema_file),
                    stdin=subprocess.DEVNULL,  # an open stdin makes `codex exec` wait forever
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                )
            except OSError as exc:
                raise fail(NotAuthenticated, f"could not start the Codex CLI: {exc}") from exc

            stderr_tail: list[str] = []
            stderr_reader = threading.Thread(
                target=lambda: stderr_tail.extend(process.stderr.readlines()), daemon=True
            )
            stderr_reader.start()

            def kill() -> None:
                state["timed_out"] = True
                process.kill()

            watchdog = threading.Timer(call.timeout_s, kill)
            watchdog.start()
            raw_events: list[str] = []
            try:
                for line in process.stdout:
                    raw_events.append(line)
                    self._handle_event(line, state, tool_calls, usage, open_tools)
                process.wait()
            finally:
                watchdog.cancel()
                stderr_reader.join(timeout=5)
                # A tool call still open here never completed (timeout or crash).
                for stack, handle, _ in open_tools.values():
                    handle.fail("the turn ended before this tool call completed")
                    if stack is not None:
                        stack.close()
                self._dump_events(call.role, raw_events, stderr_tail)

        duration = time.monotonic() - started
        cost = estimate_cost_usd(MODEL, usage)
        turn_span.record_usage(
            provider="openai-codex-cli",
            model=MODEL,
            input_tokens=usage.input_tokens,
            cached_input_tokens=usage.cached_input_tokens,
            output_tokens=usage.output_tokens,
            reasoning_tokens=usage.reasoning_output_tokens,
            cost_usd=cost,
        )
        turn_span.set(
            **{
                "codex.thread_id": state["thread_id"],
                "codex.duration_s": round(duration, 2),
                "codex.tool_calls": len(tool_calls),
                "codex.failed_tool_calls": sum(1 for c in tool_calls if c.status == "failed"),
                "codex.cost_basis": "estimate at API list prices; the ChatGPT plan is flat-rate",
            }
        )
        stderr_text = "".join(stderr_tail)[-STDERR_TAIL_CHARS:]

        if state["timed_out"]:
            raise fail(TurnTimeout, f"{call.role}: no result after {call.timeout_s:.0f}s; the turn was killed")
        if state["failure"]:
            raise fail(_classify_failure(state["failure"]), f"{call.role}: {state['failure']}")
        if process.returncode != 0:
            detail = stderr_text.strip().splitlines()[-1] if stderr_text.strip() else "no error output"
            raise fail(
                _classify_failure(stderr_text), f"{call.role}: codex exited with {process.returncode}: {detail}"
            )

        succeeded = {c.tool for c in tool_calls if c.status == "completed"}
        missing = [tool for tool in call.required_tools if tool not in succeeded]
        if missing:
            # Codex answers with invented values when a tool is missing or
            # blocked, so an answer without these calls is not usable.
            raise fail(RequiredToolMissing, f"{call.role}: required tool call(s) did not succeed: {missing}")
        if state["final"] is None:
            raise fail(InvalidOutput, f"{call.role}: the turn produced no final message")
        try:
            output = call.output_model.model_validate_json(state["final"])
        except ValidationError as exc:
            raise fail(InvalidOutput, f"{call.role}: final message does not fit the schema: {exc}") from exc

        turn_span.set_output(output.model_dump())
        return RoleResult(
            output=output,
            tool_calls=tool_calls,
            usage=usage,
            duration_s=duration,
            model=MODEL,
            reasoning_effort=REASONING_EFFORT,
            est_cost_usd=cost,
            thread_id=state["thread_id"],
        )

    @staticmethod
    def _dump_events(role: str, events: list[str], stderr_lines: list[str]) -> None:
        directory = os.environ.get(EVENT_LOG_ENV)
        if not directory:
            return
        try:
            target = Path(directory)
            target.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            (target / f"{stamp}-{role}-{os.getpid()}-{threading.get_ident()}.jsonl").write_text("".join(events))
            if stderr_lines:
                (target / f"{stamp}-{role}-{os.getpid()}-{threading.get_ident()}.stderr").write_text(
                    "".join(stderr_lines)
                )
        except OSError:
            logger.warning("could not write the Codex event log", exc_info=True)

    @staticmethod
    def _tool_span(item: dict):
        """Span for one MCP tool call reported by Codex."""
        name = item.get("tool", "unknown")
        arguments = item.get("arguments") or {}
        spec = TOOLS.get(name)
        return span(
            name,
            "MCP_TOOL",
            input=arguments,
            **{
                "neatlogs.mcp_tool.name": name,
                "neatlogs.mcp_tool.arguments": arguments,
                "neatlogs.mcp_tool.description": spec.description if spec else None,
                "neatlogs.mcp_tool.call_id": item.get("id"),
                "mcp.server": item.get("server"),
            },
        )

    def _handle_event(
        self,
        line: str,
        state: dict[str, Any],
        tool_calls: list[ToolCallRecord],
        usage: TokenUsage,
        open_tools: dict[str, tuple[Any, SpanHandle, float]],
    ) -> None:
        """Apply one line of the JSON event stream. Unknown events are skipped."""
        line = line.strip()
        if not line.startswith("{"):
            return
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        kind = event.get("type")
        item = event.get("item") or {}
        if kind == "thread.started":
            state["thread_id"] = event.get("thread_id")
        elif kind == "item.started" and item.get("type") == "mcp_tool_call":
            key = item.get("id") or item.get("tool", "unknown")
            # A span is opened while the call runs, so the trace shows its real
            # latency. Spans nest by time, so only one can be live at once: a
            # call that overlaps another gets its span when it completes.
            live = None
            if not any(entry[0] is not None for entry in open_tools.values()):
                live = ExitStack()
                handle = live.enter_context(self._tool_span(item))
            else:
                handle = SpanHandle()
            open_tools[key] = (live, handle, time.monotonic())
        elif kind == "item.completed" and item.get("type") == "mcp_tool_call":
            name = item.get("tool", "unknown")
            key = item.get("id") or name
            stack, handle, began = open_tools.pop(key, (None, SpanHandle(), time.monotonic()))
            ok = item.get("status") == "completed" and not item.get("error")
            result = _tool_result(item) if ok else None
            record = ToolCallRecord(
                tool=name,
                arguments=item.get("arguments") or {},
                result=result,
                status="completed" if ok else "failed",
                error=None if ok else _tool_error(item),
                duration_s=round(time.monotonic() - began, 3),
            )
            tool_calls.append(record)
            if stack is None:
                stack = ExitStack()
                handle = stack.enter_context(self._tool_span(item))
            handle.set(**{"mcp.tool.duration_s": record.duration_s})
            if ok:
                handle.set(**{"output.value": result, "neatlogs.mcp_tool.output": result})
            else:
                handle.fail(record.error or "tool call failed")
            stack.close()
        elif kind == "item.completed" and item.get("type") == "agent_message":
            state["final"] = item.get("text")
        elif kind == "turn.completed":
            reported = event.get("usage") or {}
            usage.input_tokens += int(reported.get("input_tokens") or 0)
            usage.cached_input_tokens += int(reported.get("cached_input_tokens") or 0)
            usage.output_tokens += int(reported.get("output_tokens") or 0)
            usage.reasoning_output_tokens += int(reported.get("reasoning_output_tokens") or 0)
        elif kind == "turn.failed":
            state["failure"] = (event.get("error") or {}).get("message") or "the turn failed"
        elif kind == "error":
            state["failure"] = event.get("message") or "the provider reported an error"
