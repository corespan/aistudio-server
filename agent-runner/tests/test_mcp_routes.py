"""One gateway route per connector: each tool goes to its own route, and a route that is down is skipped."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app import mcp, registry
from app.config import settings


class FakeSession:
    def __init__(self, names):
        self.names, self.called = names, []

    async def list_tools(self, cursor=None):
        return SimpleNamespace(nextCursor=None, tools=[
            SimpleNamespace(name=n, description="", inputSchema={}, annotations=None) for n in self.names])

    async def call_tool(self, name, args, progress_callback=None):
        self.called.append(name)
        return SimpleNamespace(isError=False, content=[SimpleNamespace(text=f"{name} done")])


def _connectors(monkeypatch, *names):
    monkeypatch.setattr(settings, "gateway_url", "http://gw")
    monkeypatch.setattr(registry, "load", lambda: SimpleNamespace(connectors=dict.fromkeys(names)))


@pytest.fixture
def routes(monkeypatch):
    sessions = {"http://gw/mcp/aistudio": FakeSession(["summary", "shared"]),
                "http://gw/mcp/sandbox": FakeSession(["sandbox_run", "shared"])}
    _connectors(monkeypatch, "aistudio", "sandbox", "down")

    async def fake_open(url, token, run_id, stack):
        if url not in sessions:
            raise ConnectionError("route down")
        return sessions[url]
    monkeypatch.setattr(mcp, "_open", fake_open)
    return sessions


async def test_each_tool_is_called_on_the_connector_that_listed_it(routes):
    async with mcp.session("tok", "run-1") as box:
        tools = await mcp.list_tools(box)
        # A name two connectors share gets the connector in front, so neither one shadows the other.
        assert [t["name"] for t in tools] == ["summary", "aistudio__shared", "sandbox_run", "sandbox__shared"]
        assert [(t["connector"], t["tool"]) for t in tools][-1] == ("sandbox", "shared")
        assert await mcp.call_tool(box, "sandbox_run", {}) == "sandbox_run done"
        await mcp.call_tool(box, "sandbox__shared", {})
        await mcp.call_tool(box, "aistudio__shared", {})
    assert routes["http://gw/mcp/sandbox"].called == ["sandbox_run", "shared"]
    assert routes["http://gw/mcp/aistudio"].called == ["shared"]


async def test_no_reachable_route_is_an_error(monkeypatch):
    _connectors(monkeypatch, "down")

    async def down(*a):
        raise ConnectionError("route down")
    monkeypatch.setattr(mcp, "_open", down)
    with pytest.raises(ConnectionError):
        async with mcp.session("tok", "run-1"):
            pass


async def test_a_route_whose_transport_cancels_itself_is_skipped_but_a_real_cancel_is_not(monkeypatch):
    # agentgateway answering 503 (server down) surfaces as the MCP transport cancelling itself.
    _connectors(monkeypatch, "aistudio", "sandbox")
    ok = FakeSession(["sandbox_run"])

    async def fake_open(url, token, run_id, stack):
        if url.endswith("aistudio"):
            raise asyncio.CancelledError("Cancelled via cancel scope")
        return ok
    monkeypatch.setattr(mcp, "_open", fake_open)
    async with mcp.session("tok", "run-1") as box:
        assert list(box.sessions) == ["sandbox"]

    async def cancelled_run():
        async with mcp.session("tok", "run-1"):
            pass

    async def slow_open(url, token, run_id, stack):
        await asyncio.sleep(10)
    monkeypatch.setattr(mcp, "_open", slow_open)
    task = asyncio.create_task(cancelled_run())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
