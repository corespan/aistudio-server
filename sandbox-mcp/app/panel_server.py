"""The panel: a stdio MCP server Claude Code starts inside a workspace (workspace.claude, `panel`).

Panel tools change what the user sees in the AI Studio panel: filter the benchmarks, open a run's
logs, prefill a form. The browser runs them, not this server: agent-runner reads each
mcp__panel__* call from Claude Code's stream and sends it to the panel. This server only lists
the tools it is given and acknowledges a call, so Claude Code can call them like any tool.

Runs with the workspace image's runner venv (it has the mcp package):
    /opt/runner-venv/bin/python /tmp/panel.py /tmp/panel.json
"""
import asyncio
import json
import sys

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

TOOLS = json.load(open(sys.argv[1], encoding="utf-8"))
NAMES = {t["name"] for t in TOOLS}
server = Server("panel")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [types.Tool(name=t["name"], description=t["description"], inputSchema=t["inputSchema"])
            for t in TOOLS]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    text = (f"Sent to the user's panel: {name}. The panel shows it; say what the user will see."
            if name in NAMES else f"{name} is not a panel tool.")
    return [types.TextContent(type="text", text=text)]


async def main() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
