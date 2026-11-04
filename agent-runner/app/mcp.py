"""MCP client: one session per connector (gateway route) per agent, with the caller's JWT."""
from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import AsyncExitStack, asynccontextmanager

import httpx
import structlog
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from app import registry
from app.config import settings

log = structlog.get_logger()


class Toolbox:
    """An agent's open sessions by connector, and which session and tool each model-facing name is."""

    def __init__(self):
        self.sessions: dict[str, ClientSession] = {}
        self.route: dict[str, tuple[ClientSession, str]] = {}


async def _open(url: str, token: str, run_id: str, stack: AsyncExitStack) -> ClientSession:
    """A session to one route; everything it opened closes with ``stack``."""
    headers = {"Authorization": f"Bearer {token}", "Run-Id": run_id}
    http = await stack.enter_async_context(
        # read: a Claude step can go quiet for minutes (a long test run in its box).
        httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(60, read=settings.claude_run_max_seconds)))
    r, w, _ = await stack.enter_async_context(streamable_http_client(url, http_client=http))
    s = await stack.enter_async_context(ClientSession(r, w))
    await s.initialize()
    return s


@asynccontextmanager
async def session(token: str, run_id: str):
    """Every connector in the registry. One that is down is logged and left out: its tools aren't offered."""
    async with AsyncExitStack() as stack:
        box = Toolbox()
        for name in registry.load().connectors:
            url = f"{settings.gateway_url}/mcp/{name}"
            route = AsyncExitStack()
            try:
                box.sessions[name] = await _open(url, token, run_id, route)
            except (Exception, asyncio.CancelledError) as exc:  # one route down must not sink the run
                # A route that answers an error (503: its server is down) surfaces as the MCP
                # transport cancelling its own task group. Closing it ends that cancellation; only
                # a real cancel of this run is still pending after, and that one propagates.
                try:
                    await route.aclose()
                except Exception:  # noqa: BLE001 - half-open transport; nothing more to free
                    pass
                if isinstance(exc, asyncio.CancelledError) and asyncio.current_task().cancelling():
                    raise
                log.warning("mcp.route_unavailable", url=url, error=str(exc) or type(exc).__name__)
                continue
            stack.push_async_callback(route.aclose)
        if not box.sessions:
            raise ConnectionError("no MCP route is reachable")
        yield box


async def list_tools(box: Toolbox) -> list[dict]:
    """The caller's tools on every connector, as agentgateway filtered them for the caller's role.

    Each carries ``connector`` and ``tool``. ``name`` is what the model sees: the tool's own name,
    or ``connector__tool`` when two connectors offer the same name.
    """
    out = []
    for connector, s in box.sessions.items():
        cursor = None
        while True:
            page = await s.list_tools(cursor=cursor) if cursor else await s.list_tools()
            out += [{"connector": connector, "tool": t.name, "description": t.description or "",
                     "inputSchema": t.inputSchema,
                     "annotations": t.annotations.model_dump() if t.annotations else {}}
                    for t in page.tools]
            cursor = page.nextCursor
            if not cursor:
                break
    shared = Counter(t["tool"] for t in out)
    for t in out:
        t["name"] = t["tool"] if shared[t["tool"]] == 1 else f"{t['connector']}__{t['tool']}"
        box.route[t["name"]] = (box.sessions[t["connector"]], t["tool"])
    return out


async def call_tool(box: Toolbox, name: str, args: dict, progress=None, cap: int | None = -1) -> str:
    """The tool's text, capped. A refusal or tool error comes back as text for the agent to read.

    ``progress``: an async (progress, total, message) callback for the tool's progress
    notifications. ``cap=None``: uncapped, for results the runner parses itself.
    """
    s, tool = box.route[name]
    res = await s.call_tool(tool, args, progress_callback=progress)
    text = "\n".join(c.text for c in res.content if getattr(c, "text", None))
    if res.isError:
        text = f"Tool error: {text}"
    cap = settings.tool_result_chars if cap == -1 else cap
    if cap is None or len(text) <= cap:
        return text
    # Stated as a fact, not an order: the rest exists, and filters or a smaller limit reach it.
    return text[:cap] + (f"\n[... cut at {cap} of {len(text)} characters; the tool's filters or a "
                         "smaller limit return a narrower result]")
