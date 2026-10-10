"""neatlogs tracing for the DataTraceMind agent workflow.

Every model call and every agent tool call must appear in a neatlogs trace
(product brief, Section 4). Tracing is evidence, not control flow: nothing
here may stop an incident from being investigated, so a missing key or a
failed export degrades to "not traced" instead of raising.

Usage:
    init_tracing()                    # once, at process start, before any
                                      # LLM client or agent framework import
    client = wrap_llm_client(Provider())
    with investigation_run(incident_id, scenario_id=..., version=...) as trace_id:
        ...                           # @neatlogs.span roles and tools nest here
    shutdown_tracing()                # once, at process exit
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import neatlogs
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

WORKFLOW_NAME = "datatracemind"

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
) -> Iterator[str | None]:
    """Root span for one investigation run; yields its trace ID.

    Role and tool spans opened inside nest under it. The incident ID is also
    the neatlogs session ID, so every run for an incident (first attempt,
    re-investigation after a failed validation, post-fix verification) is
    grouped together. `version` labels the workflow/prompt revision, which is
    what makes a before/after pair of runs comparable.
    """
    attributes = {"incident_id": incident_id}
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
