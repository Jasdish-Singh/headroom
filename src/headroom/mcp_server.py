"""stdio MCP server. Tools are the functions in ``mcp_tools``.

The server never deletes, moves, or disables startup entries. Action tools
return a dry-run plan and tell the assistant to confirm in Headroom.
The ``health_check`` prompt is the same text as ``docs/ai-starter-prompt.md``.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from headroom.mcp_prompts import HEALTH_CHECK_NAME, HEALTH_CHECK_PROMPT
from headroom.mcp_tools import TOOLS


def build_server() -> MCPServer:
    server = MCPServer("Headroom", instructions=(
        "Headroom tools are local and read-only. clean, move, and startup_disable "
        "return a dry-run plan. They do not delete or move files. The person confirms "
        "on the PC with headroom clean, headroom review --apply, or headroom startup disable."
    ))
    for name, description, fn in TOOLS:
        server.tool(name=name, description=description)(fn)

    @server.prompt(
        name=HEALTH_CHECK_NAME,
        title="PC health check",
        description="Read RAM, disk, duplicates, and startup, then summarize. Plans only.",
    )
    def health_check() -> str:
        """Read-only health check. Cleanup tools are asked for a plan, not an action."""
        return HEALTH_CHECK_PROMPT

    return server


def serve() -> None:
    build_server().run(transport="stdio")
