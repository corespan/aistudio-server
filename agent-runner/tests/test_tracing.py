"""A run's callbacks reach every agent: one trace holds the whole run."""
from __future__ import annotations

from langchain_core.callbacks import AsyncCallbackHandler

from app import tracing
from app.config import settings
from tests.conftest import agent_name, call, tool_results
from tests.test_runs import run_to_end


class Recorder(AsyncCallbackHandler):
    def __init__(self):
        self.names: list[str] = []
        self.metadata: list[dict] = []

    async def on_chain_start(self, serialized, inputs, *, name=None, metadata=None, **kw):
        self.names.append(name or "")
        self.metadata.append(metadata or {})


async def test_one_trace_holds_the_plan_every_agent_and_their_tool_rounds(fake_llm, fake_mcp, monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(tracing, "run_config", lambda run_id, sub: {
        "callbacks": [rec], "metadata": {"langfuse_session_id": run_id, "langfuse_user_id": sub}})
    scripts, _ = fake_llm
    scripts["planner"] = lambda m: {"goal": "g", "tasks": [
        {"id": "t1", "agent": "analyst", "task": "a"}, {"id": "t2", "agent": "diagnostics", "task": "b"}]}
    scripts["worker"] = lambda m: "x" if tool_results(m) else call(
        "summary" if agent_name(m) == "analyst" else "check_service_health")
    events = await run_to_end("q")
    assert events[-1]["status"] == "ok"
    for name in ("plan", "validate", "schedule", "task", "agent:analyst", "agent:diagnostics",
                 "model", "tools", "summarize"):
        assert name in rec.names, name
    # The session and user ride down to the agents' own spans.
    agent_meta = rec.metadata[rec.names.index("agent:analyst")]
    assert agent_meta.get("langfuse_user_id") == "alice"
    assert agent_meta.get("langfuse_session_id", "").startswith("run-")


async def test_an_unreachable_langfuse_costs_a_run_nothing(fake_llm, fake_mcp, monkeypatch):
    # The real SDK and handler, pointed at a port nothing listens on (as .26 sees .17 today).
    monkeypatch.setattr(settings, "langfuse_host", "http://127.0.0.1:9")
    monkeypatch.setattr(settings, "langfuse_public_key", "pk-lf-test")
    monkeypatch.setattr(settings, "langfuse_secret_key", "sk-lf-test")
    monkeypatch.setattr(settings, "langfuse_timeout_seconds", 1)
    monkeypatch.setattr(tracing, "_client", None)
    monkeypatch.setattr(tracing, "_off", False)
    assert tracing.run_config("run-x", "alice")["callbacks"]
    scripts, _ = fake_llm
    scripts["worker"] = lambda m: "fine"
    events = await run_to_end("is it up?", harness="assistant")
    assert events[-1]["status"] == "ok"
    await tracing.check()       # logs tracing.unreachable, raises nothing
    tracing.shutdown()


class FakeSpan:
    def __init__(self):
        self.trace_io, self.updates = None, []

    def update(self, **kw):
        self.updates.append(kw)

    def set_trace_io(self, *, input=None, output=None):
        self.trace_io = (input, output)


class FakeClient:
    def __init__(self, fail=False):
        self.span, self.fail, self.opened = FakeSpan(), fail, []

    def start_as_current_observation(self, **kw):
        if self.fail:
            raise RuntimeError("exporter broken")
        self.opened.append(kw)
        from contextlib import nullcontext
        return nullcontext(self.span)


async def test_the_trace_gets_the_question_as_input_and_the_answer_as_output(fake_llm, fake_mcp, monkeypatch):
    # Langfuse server v3 shows these trace-level fields; SDK v4 no longer fills them in itself.
    client = FakeClient()
    monkeypatch.setattr(tracing, "_get", lambda: client)
    monkeypatch.setattr(tracing, "run_config", lambda run_id, sub: {})
    scripts, _ = fake_llm
    scripts["worker"] = lambda m: "the api is up"
    events = await run_to_end("is the api up?", harness="assistant")
    assert events[-1]["status"] == "ok"
    assert client.opened[0]["name"] == "agent-run" and client.opened[0]["input"] == "is the api up?"
    assert client.span.trace_io == ("is the api up?", "the api is up")
    assert client.span.updates[-1]["metadata"] == {"status": "ok"}


async def test_a_broken_tracer_costs_a_run_nothing(fake_llm, fake_mcp, monkeypatch):
    monkeypatch.setattr(tracing, "_get", lambda: FakeClient(fail=True))
    monkeypatch.setattr(tracing, "run_config", lambda run_id, sub: {})
    scripts, _ = fake_llm
    scripts["worker"] = lambda m: "fine"
    events = await run_to_end("q", harness="assistant")
    assert events[-1]["status"] == "ok"


def test_tracing_is_off_without_keys(monkeypatch):
    monkeypatch.setattr(settings, "langfuse_host", "")
    monkeypatch.setattr(tracing, "_client", None)
    monkeypatch.setattr(tracing, "_off", False)
    assert tracing.run_config("run-1", "alice") == {}
