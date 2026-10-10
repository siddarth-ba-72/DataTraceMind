"""neatlogs tracing for the DataTraceMind agent workflow.

Every model call and every agent tool call must appear in a neatlogs trace
(product brief, Section 4). Tracing is evidence, not control flow: nothing
here may stop an incident from being investigated, so a missing key or a
failed export degrades to "not traced" instead of raising.

Usage:
    init_tracing()                    # once, at process start, before any
                                      # LLM client or agent framework import
    with investigation_run(incident_id, scenario_id=..., version=...) as trace_id:
        with span("source_investigator", "AGENT", input=task) as agent:
            with span("codex.exec", "LLM") as turn:      # nests under the role
                turn.record_usage(...)
            agent.set_output(report)
    shutdown_tracing()                # once, at process exit
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from typing import Any, TypeVar

import neatlogs
from dotenv import load_dotenv
from opentelemetry.trace import Status, StatusCode

logger = logging.getLogger(__name__)

WORKFLOW_NAME = "datatracemind"
# Spans carry summaries, not payloads: longer values are cut to this length.
MAX_ATTRIBUTE_CHARS = 6000

T = TypeVar("T")

_initialized = False


def init_tracing(*, tags: list[str] | None = None) -> bool:
    """Start neatlogs export. Returns False when runs will not be traced."""
    global _initialized
    if _initialized:
        return True
    # The SDK reads os.environ only; .env has to be loaded before init().
    load_dotenv()
    api_key = os.environ.get("NEATLOGS_API_KEY")
    if not api_key:
        logger.warning("NEATLOGS_API_KEY is not set; agent runs will not be traced")
        return False
    try:
        neatlogs.init(api_key=api_key, workflow_name=WORKFLOW_NAME, tags=tags)
    except Exception:
        logger.exception("neatlogs.init failed; agent runs will not be traced")
        return False
    _initialized = True
    return True


def shutdown_tracing() -> None:
    """Send buffered spans and stop the exporter.

    Call once at exit (FastAPI lifespan shutdown, or the end of a script).
    Short scripts that skip this can exit before their trace is sent.
    """
    global _initialized
    if not _initialized:
        return
    neatlogs.flush()
    neatlogs.shutdown()
    _initialized = False


def wrap_llm_client(client: Any, **workflow_attributes: Any) -> Any:
    """Return `client` with every call traced.

    The LLM adapter builds its provider client through this function and
    nowhere else, so no model call can bypass tracing.
    """
    if not _initialized:
        return client
    return neatlogs.wrap(client, **workflow_attributes)


def langchain_handler() -> Any:
    """Callback handler for a LangGraph/LangChain run.

    Create one per investigation run and pass it at the graph invocation
    (config={"callbacks": [handler]}), not on individual node calls.
    """
    return neatlogs.langchain_handler()


def current_trace_id() -> str | None:
    """Hex ID of the active trace, or None outside a traced run."""
    # neatlogs keeps its active span on a private context key, so the OTel
    # global current-span is empty. Its W3C traceparent header
    # ("00-<trace_id>-<span_id>-<flags>") is the supported way to read it.
    carrier: dict[str, str] = {}
    if not neatlogs.inject_trace_context(carrier):
        return None
    return carrier["traceparent"].split("-")[1]


@contextmanager
def investigation_run(
    incident_id: str,
    *,
    scenario_id: str | None = None,
    version: str | None = None,
    phase: str = "investigation",
) -> Iterator[str | None]:
    """Root span for one investigation run; yields its trace ID.

    Role and tool spans opened inside nest under it. The incident ID is also
    the neatlogs session ID, so every run for an incident (first attempt,
    re-investigation after a failed validation, post-fix verification) is
    grouped together. `version` labels the workflow/prompt revision, which is
    what makes a before/after pair of runs comparable. `phase` tells the
    investigation trace apart from the later approval-and-apply trace.
    """
    if not _initialized:
        yield None
        return
    attributes = {"incident_id": incident_id, "phase": phase}
    if scenario_id is not None:
        attributes["scenario_id"] = scenario_id
    if version is not None:
        # Not trace(version=...): that only versions prompt templates and
        # leaves nothing on the span to search or compare by.
        attributes["workflow_version"] = version
    with neatlogs.trace(
        "investigation_run",
        kind="WORKFLOW",
        session_id=incident_id,
        **attributes,
    ):
        yield current_trace_id()


def _attribute(value: Any) -> str | bool | int | float:
    """Coerce a value into something a span attribute can hold."""
    if isinstance(value, (bool, int, float)):
        return value
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    if len(text) > MAX_ATTRIBUTE_CHARS:
        text = text[:MAX_ATTRIBUTE_CHARS] + f"... [{len(text) - MAX_ATTRIBUTE_CHARS} more characters]"
    return text


class SpanHandle:
    """What a caller gets from `span()`. Every method is safe when tracing is off."""

    def __init__(self, otel_span: Any = None) -> None:
        self._span = otel_span
        self.failed = False

    def set(self, **attributes: Any) -> None:
        if self._span is None:
            return
        try:
            for key, value in attributes.items():
                if value is not None:
                    self._span.set_attribute(key.replace("__", "."), _attribute(value))
        except Exception:
            logger.debug("could not set span attributes", exc_info=True)

    def set_output(self, value: Any) -> None:
        self.set(**{"output.value": value})

    def fail(self, message: str, exc: BaseException | None = None) -> None:
        """Mark the span as an error without raising."""
        self.failed = True
        if self._span is None:
            return
        try:
            if exc is not None:
                self._span.record_exception(exc)
            self._span.set_status(Status(StatusCode.ERROR, message[:500]))
        except Exception:
            logger.debug("could not mark span as failed", exc_info=True)

    def _succeed(self) -> None:
        if self._span is not None and not self.failed:
            try:
                self._span.set_status(Status(StatusCode.OK))
            except Exception:
                logger.debug("could not mark span as ok", exc_info=True)

    def record_usage(
        self,
        *,
        provider: str,
        model: str,
        input_tokens: int,
        cached_input_tokens: int,
        output_tokens: int,
        reasoning_tokens: int,
        cost_usd: float,
    ) -> None:
        """Token usage and estimated cost of one model turn."""
        total = input_tokens + output_tokens
        self.set(
            **{
                "neatlogs.llm.provider": provider,
                "neatlogs.llm.model_name": model,
                "neatlogs.llm.token_count.prompt": input_tokens,
                "neatlogs.llm.token_count.completion": output_tokens,
                "neatlogs.llm.token_count.total": total,
                "neatlogs.llm.token_count.cache_read": cached_input_tokens,
                "neatlogs.llm.token_count.reasoning": reasoning_tokens,
                "neatlogs.llm.cost_usd": cost_usd,
                "llm.provider": provider,
                "llm.model_name": model,
                "llm.token_count.prompt": input_tokens,
                "llm.token_count.completion": output_tokens,
                "llm.token_count.total": total,
                "llm.token_count.prompt_details.cache_read": cached_input_tokens,
                "llm.token_count.completion_details.reasoning": reasoning_tokens,
            }
        )


@contextmanager
def span(name: str, kind: str, *, input: Any = None, **attributes: Any) -> Iterator[SpanHandle]:
    """Open a span under the current run: AGENT, LLM, MCP_TOOL, TOOL, GUARDRAIL, or CHAIN.

    An exception raised inside is recorded on the span with status ERROR and
    re-raised, so a failed step and its retry show up as two spans. A step
    that fails without raising calls `handle.fail(...)`. Attribute names may
    use `__` where the real name has a dot.
    """
    if not _initialized:
        yield SpanHandle()
        return
    values = {key.replace("__", "."): _attribute(v) for key, v in attributes.items() if v is not None}
    if input is not None:
        values["input.value"] = _attribute(input)
    stack = ExitStack()
    try:
        otel_span = stack.enter_context(neatlogs.trace(name, kind=kind, **values))
    except Exception:
        logger.warning("could not open %s span %r; continuing untraced", kind, name, exc_info=True)
        yield SpanHandle()
        return
    with stack:
        handle = SpanHandle(otel_span)
        try:
            yield handle
        except Exception as exc:
            handle.fail(str(exc), exc)
            raise
        else:
            handle._succeed()


def in_current_trace(fn: Callable[..., T]) -> Callable[..., T]:
    """Bind `fn` to the calling thread's trace context.

    Roles that run concurrently execute on worker threads, which start with an
    empty context; without this their spans would become separate traces.
    """
    context = contextvars.copy_context()

    def bound(*args: Any, **kwargs: Any) -> T:
        return context.copy().run(fn, *args, **kwargs)

    return bound
