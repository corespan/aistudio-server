"""One agent's loop, and which tools it is given."""
from __future__ import annotations

from app import agent, harness
from app.contract import Budgets, Harness
from tests.conftest import TOOLS, call, tool_results


async def test_a_repeated_call_runs_once_and_the_agent_must_then_answer(fake_llm, fake_mcp):
    scripts, calls = fake_llm

    def worker(messages):
        if messages[-1].content.startswith("Your tool budget"):
            return "answer from what I have"
        return call("summary", {"id": "r1"})                     # the same call, every round
    scripts["worker"] = worker
    h = Harness(name="analyst", purpose="p", tools=["aistudio/summary"], budgets=Budgets(steps=10))
    out = await agent.run(h, harness.tools_for(h, TOOLS), None, agent.Budget(),
                          lambda *a, **k: None, "task", {})
    assert out == "answer from what I have"
    assert fake_mcp["calls"] == [("summary", {"id": "r1"})]
    nudges = [r for r in tool_results(calls[-1][1]) if r.startswith("You already called")]
    assert len(nudges) == 3


async def test_the_step_budget_ends_the_loop(fake_llm, fake_mcp):
    scripts, calls = fake_llm
    n = iter(range(100))

    def worker(messages):
        if messages[-1].content.startswith("Your tool budget"):
            return "done"
        return call("summary", {"id": f"r{next(n)}"})
    scripts["worker"] = worker
    h = Harness(name="analyst", purpose="p", tools=["aistudio/summary"], budgets=Budgets(steps=3))
    budget = agent.Budget()
    assert await agent.run(h, harness.tools_for(h, TOOLS), None, budget,
                           lambda *a, **k: None, "t", {}) == "done"
    assert len(fake_mcp["calls"]) == 3 and budget.steps == 3


async def test_running_short_of_time_ends_in_an_answer(fake_llm, fake_mcp, monkeypatch):
    import asyncio
    from app.config import settings
    scripts, _ = fake_llm
    n = iter(range(100))
    scripts["worker"] = lambda m: ("what I have" if m[-1].content.startswith("Your tool budget")
                                   else call("summary", {"id": f"r{next(n)}"}))

    async def slow(args):
        await asyncio.sleep(0.3)
        return "row"
    fake_mcp["answers"]["summary"] = slow
    # 1 s for the task, 0.5 s kept for the answering call: tools stop after the first round or two.
    monkeypatch.setattr(settings, "llm_timeout_seconds", 0.5)
    h = Harness(name="analyst", purpose="p", tools=["aistudio/summary"], budgets=Budgets(steps=50, seconds=1))
    out = await agent.run(h, harness.tools_for(h, TOOLS), None, agent.Budget(),
                          lambda *a, **k: None, "t", {})
    assert out == "what I have"
    assert 1 <= len(fake_mcp["calls"]) <= 2


async def test_a_tool_outside_the_agents_set_is_refused(fake_llm, fake_mcp):
    scripts, _ = fake_llm
    scripts["worker"] = lambda m: "ok" if tool_results(m) else call("docker_logs")
    h = Harness(name="analyst", purpose="p", tools=["aistudio/summary"])
    await agent.run(h, harness.tools_for(h, TOOLS), None, agent.Budget(), lambda *a, **k: None, "t", {})
    assert fake_mcp["calls"] == []


def test_tools_only_shrink_and_writes_are_never_bound():
    everything = Harness(name="x", purpose="p", tools=["*/*"])
    names = [t["name"] for t in harness.tools_for(everything, TOOLS)]
    assert "start_workload" not in names and "summary" in names
    # A harness can't add a tool the caller's tools/list didn't return.
    assert harness.tools_for(Harness(name="x", purpose="p", tools=["aistudio/node_exec"]), TOOLS) == []


def test_a_viewer_without_host_tools_gets_no_diagnostics_beyond_their_tools():
    viewer_tools = [t for t in TOOLS if t["name"] in ("list_benchmarks", "summary")]
    available = harness.available(viewer_tools)
    assert "analyst" in available and "diagnostics" not in available


def test_the_shipped_harnesses_load_and_list_only_read_tools():
    import json
    from pathlib import Path
    manifest = json.loads((Path(__file__).resolve().parents[2] / "mcp-server" / "tool-manifest.json")
                          .read_text())
    tiers = {t["name"]: t["tier"] for t in manifest["tools"]}
    # sandbox-mcp's tools, from the annotations it serves them with.
    import re
    server = (Path(__file__).resolve().parents[2] / "sandbox-mcp" / "app" / "server.py").read_text()
    tiers.update((n, "read") for n in re.findall(r'"(\w+)": ToolAnnotations\(readOnlyHint=True', server))
    assert "sandbox_run" in tiers
    for h in harness.load().values():
        for pattern in h.tools:
            name = pattern.partition("/")[2]
            assert tiers.get(name) in ("read", "host"), f"{h.name}: {pattern} is not a read tool"
