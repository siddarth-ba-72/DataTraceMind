"""stdio MCP server `dtm`: the investigation tools, as served to the model.

The model provider starts this file as a subprocess for each agent turn and
stops it afterwards, so it keeps no state. It holds only read-only tools, and
it refuses to open a read-write database connection at all.

Run by absolute path:  python /abs/path/datatracemind/tools/mcp_server.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Started by path from an unrelated working directory, so the repository root
# is not importable until it is added here.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import functools  # noqa: E402
from collections.abc import Callable  # noqa: E402

from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError as McpToolError  # noqa: E402

from datatracemind import db  # noqa: E402
from datatracemind.tools.investigation import ToolError  # noqa: E402
from datatracemind.tools.registry import TOOLS  # noqa: E402


def _expose(fn: Callable[..., dict]) -> Callable[..., dict]:
    """Let a tool's own refusals reach the model so it can correct the call.

    The server library hides the text of any other exception, which keeps
    database errors and stack details out of the model's context.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ToolError as exc:
            raise McpToolError(str(exc)) from exc

    return wrapper


def build_server() -> MCPServer:
    server = MCPServer("dtm", instructions="Read-only diagnostic tools for the order-to-revenue data pipeline.")
    for spec in TOOLS.values():
        server.add_tool(_expose(spec.fn), name=spec.name, description=spec.description, structured_output=True)
    return server


if __name__ == "__main__":
    db.forbid_write_connections()
    build_server().run()
