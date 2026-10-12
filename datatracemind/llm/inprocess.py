"""What the providers that run tools in this process have in common.

The system prompt, how one tool call is validated, run, and recorded,
and how a role's answer is read out of the final message. Keeping these here
means a tool call is filed the same way whichever model asked for it.
"""

from __future__ import annotations

import inspect
import logging
import re
import time
from typing import Any

from pydantic import BaseModel, ValidationError, create_model

from datatracemind.models import ToolCallRecord
from datatracemind.tools.investigation import ToolError
from datatracemind.tools.registry import TOOLS, ToolSpec

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are one role in DataTraceMind, a read-only investigation of an order-to-revenue data pipeline.
Use the tools to gather facts; never invent a tool result. When you have what you need, reply with
a single JSON object, and nothing else, that fits this JSON Schema:

{schema}"""


def clean_schema(node: Any) -> Any:
    if isinstance(node, dict):
        return {key: clean_schema(value) for key, value in node.items() if key != "title"}
    if isinstance(node, list):
        return [clean_schema(item) for item in node]
    return node


def _arguments_model(spec: ToolSpec) -> type[BaseModel]:
    """A Pydantic model of the tool function's parameters."""
    fields: dict[str, Any] = {}
    for name, param in inspect.signature(spec.fn).parameters.items():
        annotation = param.annotation if param.annotation is not inspect.Parameter.empty else Any
        default = param.default if param.default is not inspect.Parameter.empty else ...
        fields[name] = (annotation, default)
    return create_model(f"{spec.name}_arguments", **fields)


_ARGUMENT_MODELS: dict[str, type[BaseModel]] = {name: _arguments_model(spec) for name, spec in TOOLS.items()}


def parameters_schema(name: str) -> dict:
    """JSON Schema of the tool's parameters, as shown to the model."""
    return clean_schema(_ARGUMENT_MODELS[name].model_json_schema())


def run_tool(name: str, arguments: Any) -> tuple[Any, str | None]:
    """Run one tool call. Returns (result, error); error is None on success.

    A tool's own refusals reach the model so it can correct the call. The
    text of any other exception is withheld, which keeps database errors and
    stack details out of the model's context.
    """
    if not isinstance(arguments, dict):
        return None, "arguments must be a JSON object"
    if name not in TOOLS:
        return None, f"unknown tool {name}"
    try:
        parsed = _ARGUMENT_MODELS[name].model_validate(arguments)
    except ValidationError as exc:
        return None, f"invalid arguments: {exc}"
    try:
        result = TOOLS[name].fn(**parsed.model_dump())
    except ToolError as exc:
        return None, str(exc)
    except Exception:
        logger.exception("tool %s failed", name)
        return None, f"Error executing tool {name}: internal error"
    return result, None


def call_tool(name: str, arguments: Any, *, rejected: str | None = None) -> ToolCallRecord:
    """Run one tool call and return it as it will be filed.

    `arguments` is what the model sent, decoded. `rejected` is set when the
    provider could not decode them; the call is then recorded as failed
    without being run.
    """
    began = time.monotonic()
    result, error = (None, rejected) if rejected is not None else run_tool(name, arguments)
    return ToolCallRecord(
        tool=name,
        arguments=arguments if isinstance(arguments, dict) else {},
        result=result,
        status="completed" if error is None else "failed",
        error=error,
        duration_s=round(time.monotonic() - began, 3),
    )


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
