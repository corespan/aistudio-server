"""claude-code agents: the stream becomes events and usage rows, and every limit trips."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from app import claude, graph as run_graph, harness as harnesses, usage
from app.auth import Identity
from app.config import settings
from app.main import app
from app.runlog import RunLog
from app.runs import RunManager

RSINGH = Identity(sub="rsingh", role="admin", token="tok")


def turn(mid: str, content: list, out: int) -> str:
    return json.dumps({"type": "assistant", "message": {"id": mid, "content": content, "usage": {
        "input_tokens": 5, "output_tokens": out, "cache_read_input_tokens": 20_000,
        "cache_creation_input_tokens": 3_000}}})


STREAM = [
    turn("m1", [{"type": "tool_use", "id": "tu1", "name": "Read", "input": {"file_path": "README.md"}}], 30),
    json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "tu1", "content": "# aistudio"}]}}),
    turn("m2", [{"type": "text", "text": "done"}], 20),
    "{not json: a line cut for size",
]
# Claude Code's own totals: more output than the stream showed (P0).
RESULT = {"type": "result", "result": "Added the line.", "is_error": False, "num_turns": 2,
          "total_cost_usd": 0.12, "usage": {"input_tokens": 10, "output_tokens": 300,
                                            "cache_read_input_tokens": 40_000,
                                            "cache_creation_input_tokens": 6_000}}


@pytest.fixture(autouse=True)
def fresh_usage(monkeypatch):
    monkeypatch.setattr(usage, "store", usage.Usage())
    monkeypatch.setattr(claude, "_kept", {})


@pytest.fixture
def sandbox(fake_mcp):
    """sandbox-mcp's workspace tools: a stream, then a result. ``state["claude"]`` overrides it."""
    state = {"claude": {"exit_code": 0, "timed_out": False, "budget_exceeded": False, "weighted": 10_000,
                        "result": RESULT, "snapshot": 1}, "stream": STREAM}

    async def run_claude(args, progress):
        for i, line in enumerate(state["stream"]):
            await progress(i + 1, None, line)
        return json.dumps(state["claude"])
    fake_mcp["answers"].update({
        "workspace_open": lambda a: json.dumps({"id": "ws1", "base": "a" * 40, "snapshot": 0}),
        "workspace_claude": run_claude,
        "workspace_diff": lambda a: json.dumps({"files": ["M\tREADME.md"], "diff": "+line", "truncated": False}),
        "workspace_close": lambda a: json.dumps({"closed": "ws1"})})
    return fake_mcp, state


async def run_coder(message="add a line", who=RSINGH) -> list[dict]:
    runs = RunManager(run_graph.build(InMemorySaver()), RunLog())
    run = await runs.start(who, message, "coder")
    events = []
    async with asyncio.timeout(10):
        async for line in runs.stream(run):
            events.append(json.loads(line.split("data: ", 1)[1]))
    return events


def called(fake_mcp, tool):
    return [args for name, args in fake_mcp["calls"] if name == tool]


async def test_a_coder_step_streams_events_and_records_every_turn(sandbox):
    fake_mcp, _ = sandbox
    events = await run_coder()
    assert events[-1]["status"] == "ok"
    [started] = [e for e in events if e["type"] == "step_started"]
    [result] = [e for e in events if e["type"] == "step_result"]
    assert started["tool"] == "Read" and "README.md" in started["args"] and result["result"] == "# aistudio"
    answer = [e for e in events if e["type"] == "message"][-1]["text"]
    assert answer.startswith("Added the line.") and "M\tREADME.md" in answer and "+line" in answer
    # The agent file sets the step: its tools, turn cap, time and token cap.
    coder = harnesses.load()["coder"]
    [args] = called(fake_mcp, "workspace_claude")
    assert args["tools"] == coder.claude.allowed_tools and args["max_turns"] == 40
    assert args["timeout_seconds"] == 900 and args["max_weighted_tokens"] == 600_000
    assert args["model"] == "claude-opus-5-5"
    assert called(fake_mcp, "workspace_close") == [{"workspace_id": "ws1"}]
    # Two turns and an adjust row that adds up to exactly what Claude Code reported.
    rows = usage.store.rows
    assert [r["kind"] for r in rows] == ["turn", "turn", "adjust"]
    total = {k: sum(r[k] for r in rows) for k in usage.KEYS}
    assert total == usage.counts(RESULT["usage"])
    assert sum(r["cost_usd"] or 0 for r in rows) == 0.12
    [by_run] = await usage.store.report("rsingh", datetime.now(UTC) - timedelta(hours=1), "run")
    assert by_run["weighted"] == round(usage.weighted(total)) and by_run["turns"] == 2


async def test_the_step_cap_stops_a_step_and_is_recorded(sandbox):
    fake_mcp, state = sandbox
    state["claude"] = {**state["claude"], "budget_exceeded": True, "weighted": 600_123, "result": None}
    events = await run_coder()
    assert events[-1]["status"] == "failed"
    assert "budget_exceeded" in [e for e in events if e["type"] == "agent_done"][0]["output"]
    [hit] = usage.store.limits
    assert (hit["name"], hit["action"], hit["cap"]) == ("step", "stopped", 600_000)
    assert called(fake_mcp, "workspace_close")                  # cleaned up even so
    assert [r["kind"] for r in usage.store.rows] == ["turn", "turn"]   # the turns still count


async def test_a_run_past_its_token_cap_refuses_the_next_step(sandbox, monkeypatch):
    fake_mcp, _ = sandbox
    rt = run_graph.Runtime(token="tok", sub="rsingh", user="rsingh")
    rt.budget.weighted = settings.claude_run_max_weighted
    with pytest.raises(claude.LimitHit, match="run"):
        await claude.run(harnesses.load()["coder"], rt, "run-1", "t1", lambda *a, **k: None, "x", {})
    assert usage.store.limits[0]["action"] == "refused" and not called(fake_mcp, "workspace_open")


async def test_the_step_cap_shrinks_to_what_the_windows_have_left(monkeypatch):
    monkeypatch.setattr(settings, "claude_window_5h_weighted", 100_000)
    await usage.store.add(sub="rsingh", run_id="r", task="t", agent="coder", model="m", kind="turn",
                          tokens={"input": 0, "output": 60_000, "cache_read": 0, "cache_write": 0})
    assert await claude.cap(harnesses.load()["coder"], "rsingh", 0) == ("5h", 40_000)


def test_a_used_up_window_refuses_new_runs_with_when_it_frees(signer, monkeypatch):
    monkeypatch.setattr(settings, "claude_day_weighted", 1000)
    asyncio.run(usage.store.add(sub="id-rsingh", run_id="r", task="t", agent="coder", model="m", kind="turn",
                                tokens={"input": 0, "output": 1000, "cache_read": 0, "cache_write": 0}))
    with TestClient(app) as client:
        r = client.post("/runs", json={"message": "x", "harness": "coder"},
                        headers={"Authorization": f"Bearer {signer('rsingh', 'admin')}"})
        assert r.status_code == 429 and "day" in r.json()["detail"] and "frees up from" in r.json()["detail"]
        u = client.get("/usage?by=agent", headers={"Authorization": f"Bearer {signer('rsingh', 'admin')}"}).json()
        assert u["totals"][0]["agent"] == "coder" and u["windows"]["day"]["left"] == 0


async def test_a_claude_chat_agent_gets_its_platform_tools_its_job_and_no_empty_diff(sandbox):
    fake_mcp, _ = sandbox
    fake_mcp["answers"]["workspace_diff"] = lambda a: json.dumps({"files": [], "diff": "", "truncated": False})
    runs = RunManager(run_graph.build(InMemorySaver()), RunLog())
    run = await runs.start(RSINGH, "best throughput on T4?", "analyst-claude")
    events = []
    async with asyncio.timeout(10):
        async for line in runs.stream(run):
            events.append(json.loads(line.split("data: ", 1)[1]))
    assert events[-1]["status"] == "ok"
    h = harnesses.load()["analyst-claude"]
    [args] = called(fake_mcp, "workspace_claude")
    assert args["model"] == "claude-sonnet-5-5" and args["mcp"] == ["aistudio"]
    assert args["tools"] == h.claude.allowed_tools + [f"mcp__aistudio__{t.split('/')[1]}" for t in h.tools]
    assert "mcp__aistudio__summary" in args["tools"] and "You are the analyst-claude agent" in args["prompt"]
    assert args["prompt"].index("You are the") < args["prompt"].index("best throughput on T4?")
    # Nothing changed in the checkout: the answer alone.
    assert [e for e in events if e["type"] == "message"][-1]["text"] == "Added the line."
    # A coder step gets no MCP server.
    assert claude.connectors(harnesses.load()["coder"]) == []


async def ask(harness: str, conversation: str | None = None, message: str = "status?") -> list[dict]:
    log = RunLog()
    if conversation:      # the in-memory run log keeps turns only for a conversation it has
        log.mem_convs[conversation] = {"id": conversation, "sub": RSINGH.sub, "title": "", "deleted": False,
                                       "turns": []}
    runs = RunManager(run_graph.build(InMemorySaver()), log)
    run = await runs.start(RSINGH, message, harness, conversation)
    events = []
    async with asyncio.timeout(10):
        async for line in runs.stream(run):
            events.append(json.loads(line.split("data: ", 1)[1]))
    return events


async def test_a_chat_agent_keeps_one_workspace_per_conversation_and_takes_no_diff(sandbox):
    fake_mcp, _ = sandbox
    for _ in range(2):
        assert (await ask("assistant-claude", "conv-0123456789abcdef"))[-1]["status"] == "ok"
    # One open for both turns, with no repo (assistant-claude has workspace: none); never a diff
    # or a close; neither step keeps its tree.
    assert called(fake_mcp, "workspace_open") == [{"repo": "", "ref": ""}]
    assert called(fake_mcp, "workspace_diff") == [] and called(fake_mcp, "workspace_close") == []
    steps = called(fake_mcp, "workspace_claude")
    assert len(steps) == 2 and {s["workspace_id"] for s in steps} == {"ws1"} and not any(s["keep"] for s in steps)
    # Another conversation, or another agent, gets its own.
    await ask("assistant-claude", "conv-fedcba9876543210")
    await ask("diagnostics-claude", "conv-0123456789abcdef")
    assert called(fake_mcp, "workspace_open")[1:] == [
        {"repo": "", "ref": ""}, {"repo": "corespan/aistudio-server", "ref": "master", "depth": 1}]


async def test_without_a_conversation_the_workspace_is_closed_after_the_turn(sandbox):
    fake_mcp, _ = sandbox
    await ask("analyst-claude")
    assert len(called(fake_mcp, "workspace_open")) == 1 and called(fake_mcp, "workspace_close") == [{"workspace_id": "ws1"}]
    assert called(fake_mcp, "workspace_diff") == []


async def test_a_kept_workspace_past_its_ttl_is_opened_again(sandbox):
    fake_mcp, _ = sandbox
    await ask("assistant-claude", "conv-0123456789abcdef")
    step = fake_mcp["answers"]["workspace_claude"]
    calls = []

    async def gone_once(args, progress):
        calls.append(args["workspace_id"])
        if len(calls) == 1:
            return "Refused: unknown workspace (closed, or expired after 24h)"
        return await step(args, progress)
    fake_mcp["answers"]["workspace_claude"] = gone_once
    assert (await ask("assistant-claude", "conv-0123456789abcdef"))[-1]["status"] == "ok"
    assert len(called(fake_mcp, "workspace_open")) == 2 and len(calls) == 2


def test_a_coding_agent_still_gets_a_full_clone_its_diff_and_a_kept_tree():
    coder = harnesses.load()["coder"]
    assert claude.edits(coder) and coder.claude.workspace == "full"
    assert not any(claude.edits(harnesses.load()[n]) for n in ("analyst-claude", "assistant-claude", "diagnostics-claude"))


def test_someone_elses_subscription_is_refused(signer):
    with TestClient(app) as client:
        r = client.post("/runs", json={"message": "x", "harness": "coder"},
                        headers={"Authorization": f"Bearer {signer('alice', 'admin')}"})
        assert r.status_code == 403


async def test_one_claude_step_runs_at_a_time(sandbox):
    fake_mcp, _ = sandbox
    inflight, peak = [0], [0]

    claude_step = fake_mcp["answers"]["workspace_claude"]

    async def slow_claude(args, progress):
        inflight[0] += 1
        peak[0] = max(peak[0], inflight[0])
        await asyncio.sleep(0.05)
        inflight[0] -= 1
        return await claude_step(args, progress)
    fake_mcp["answers"]["workspace_claude"] = slow_claude
    h = harnesses.load()["coder"]
    rts = [run_graph.Runtime(token="tok", sub="rsingh", user="rsingh") for _ in range(2)]
    await asyncio.gather(*(claude.run(h, rt, f"run-{i}", "t1", lambda *a, **k: None, "x", {})
                           for i, rt in enumerate(rts)))
    assert peak[0] == 1


def test_claude_code_agents_are_never_offered_to_the_planner(fake_mcp):
    offered = set(harnesses.available(fake_mcp["tools"]))
    assert {"analyst", "diagnostics"} <= offered            # the caller has these tools
    # Not even the chat agents, which list the same platform tools: they spend one subscription.
    assert not {"coder", "reviewer", "analyst-claude", "diagnostics-claude"} & offered


class FakeObs:
    def __init__(self, log, **kw):
        self.log, self.kw, self.ended, self.output = log, kw, False, None
        log.append(self)

    def start_observation(self, **kw):
        return FakeObs(self.log, **kw)

    def update(self, output=None, metadata=None):
        self.output, self.metadata = output, metadata

    def end(self):
        self.ended = True


async def test_langfuse_gets_the_same_numbers_as_the_usage_table(sandbox, monkeypatch):
    from app import tracing
    log: list[FakeObs] = []

    class Client:
        def start_observation(self, **kw):
            return FakeObs(log, **kw)
    monkeypatch.setattr(tracing, "_get", lambda: Client())
    await run_coder()
    agent, gens = log[0], [o for o in log if o.kw.get("as_type") == "generation"]
    [tool] = [o for o in log if o.kw.get("as_type") == "tool"]
    assert agent.kw["name"] == "agent:coder" and agent.ended and agent.metadata["cost_equiv_usd"] == 0.12
    assert [g.kw["name"] for g in gens] == ["turn 1", "turn 2", "reported totals"] and all(g.ended for g in gens)
    assert sum(g.kw["usage_details"]["output"] for g in gens) == 300
    assert sum(g.kw["cost_details"]["total"] for g in gens) == 0.12
    assert tool.kw["name"] == "tool Read" and tool.ended and tool.output == "# aistudio"


async def test_a_failed_claude_step_is_the_answer_without_calling_vllm(sandbox, fake_llm):
    _, state = sandbox
    _, calls = fake_llm
    state["claude"] = {**state["claude"], "budget_exceeded": True, "result": None}
    events = await run_coder()
    assert "budget_exceeded" in [e for e in events if e["type"] == "message"][-1]["text"]
    assert not calls          # no summarizer call
