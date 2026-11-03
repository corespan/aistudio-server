"""Write rules: the site switch, retried writes, and the role backstop on writes."""
import time
import types

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from app import guards
from app.mcp_server import _bridge
from app.tools.actions import Done
from conftest import ident, make_ctx


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    guards.reset()
    monkeypatch.setattr("app.config.settings.mcp_write_dedup_seconds", 600)
    yield
    guards.reset()


def _tool(name, calls, result="ok"):
    """A stand-in for a real write tool, with its real parameter (the bridge builds a schema from it)."""
    async def stop_workload(task_id: str) -> str:
        calls.append({"task_id": task_id})
        return result

    async def start_jupyter(node_ip: str = "") -> str:
        calls.append({"node_ip": node_ip})
        return result

    fn = {"stop_workload": stop_workload, "start_jupyter": start_jupyter}[name]
    return _bridge(types.SimpleNamespace(name=name, coroutine=fn))


async def refusal(call) -> str:
    """A refused call is a tool error (isError), not a result a model could read as success."""
    with pytest.raises(ToolError) as exc:
        await call
    assert str(exc.value).startswith("refused: ")
    return str(exc.value)


# ── the site switch (MCP_WRITES_ENABLED) ──
async def test_writes_off_hides_the_write_tools_and_refuses_a_call_anyway(monkeypatch):
    from app.mcp_server import WRITES_NOTE, build
    monkeypatch.setattr("app.config.settings.mcp_writes_enabled", False)
    server = build()
    names = {t.name for t in await server.list_tools()}
    assert not names & {"start_workload", "start_jupyter", "stop_workload"} and len(names) == 31
    assert WRITES_NOTE.strip() not in server.instructions
    calls = []
    text = await refusal(_tool("stop_workload", calls)(mcp_ctx=make_ctx(ident(role="admin")), task_id="wl-1"))
    assert "MCP_WRITES_ENABLED" in text and not calls


async def test_writes_on_lists_them():
    from app.mcp_server import build
    assert {"start_workload", "start_jupyter", "stop_workload"} <= {t.name for t in await build().list_tools()}


async def test_the_destructive_write_needs_at_least_operator():
    # The role gate only; the creator rule inside stop_workload is tested in test_actions.
    calls = []
    assert "higher aistudio role" in await refusal(
        _tool("stop_workload", calls)(mcp_ctx=make_ctx(ident(role="viewer")), task_id="wl-1"))
    await _tool("stop_workload", calls)(mcp_ctx=make_ctx(ident(role="operator")), task_id="wl-1")
    assert calls == [{"task_id": "wl-1"}]


# ── retried writes ──
async def test_a_retried_write_runs_once():
    calls = []
    start = _tool("start_jupyter", calls, Done("launching, task_id jup-1"))
    who = ident(role="operator", client_id="claude-code")
    first = await start(mcp_ctx=make_ctx(who), node_ip="gpu-a")
    again = await start(mcp_ctx=make_ctx(who), node_ip="gpu-a")
    assert len(calls) == 1 and first.endswith(again.split("That call's result:\n", 1)[1])
    # Said outside the untrusted-output fence, so a deliberate repeat is not reported as a new action.
    assert again.startswith("ALREADY DONE: an identical start_jupyter call") and "nothing ran this time" in again


def test_expired_writes_are_swept_from_the_dedup_store():
    stale, fresh = ("alice", "", "start_jupyter", "old"), ("alice", "", "start_jupyter", "new")
    guards._recent[stale] = (time.time() - 1, "old result", time.time() - 700)
    guards.remember(fresh, "new result")
    assert stale not in guards._recent and fresh in guards._recent


async def test_a_failed_write_is_not_remembered():
    calls = []
    start = _tool("start_jupyter", calls, "aistudio refused to launch Jupyter on gpu-a (503): busy")
    who = ident(role="operator")
    await start(mcp_ctx=make_ctx(who), node_ip="gpu-a")
    await start(mcp_ctx=make_ctx(who), node_ip="gpu-a")
    assert len(calls) == 2


async def test_dedup_is_per_caller_and_can_be_off(monkeypatch):
    calls = []
    start = _tool("start_jupyter", calls, Done("launching"))
    await start(mcp_ctx=make_ctx(ident(sub="alice", role="operator")), node_ip="gpu-a")
    await start(mcp_ctx=make_ctx(ident(sub="bob", role="operator")), node_ip="gpu-a")
    assert len(calls) == 2
    monkeypatch.setattr("app.config.settings.mcp_write_dedup_seconds", 0)
    await start(mcp_ctx=make_ctx(ident(sub="alice", role="operator")), node_ip="gpu-a")
    assert len(calls) == 3


async def test_reads_are_never_deduplicated():
    assert guards.write_key(ident(), "list_benchmarks", {}) is None
