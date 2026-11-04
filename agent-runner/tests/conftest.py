"""Fakes: a scripted chat model per role, an MCP gateway that answers from a dict, a signing key."""
from __future__ import annotations

import json
import time
import uuid
from contextlib import asynccontextmanager

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage

from app import auth, llm, mcp

READ = {"readOnlyHint": True}
TOOLS = [
    {"connector": "aistudio", "tool": n, "name": n, "description": f"{n} tool", "annotations": READ,
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}}}
    for n in ("list_benchmarks", "get_benchmark", "summary", "workload_history", "get_task_status",
              "check_service_health", "docker_logs")
] + [{"connector": "aistudio", "tool": "start_workload", "name": "start_workload",
      "description": "start a run", "annotations": {"readOnlyHint": False},
      "inputSchema": {"type": "object", "properties": {}}}]


def agent_name(messages) -> str:
    """Which harness a worker call is for, from its system prompt."""
    text = next(m.content for m in messages if isinstance(m, SystemMessage))
    return text.split("`")[1]


def tool_results(messages) -> list[str]:
    return [m.content for m in messages if isinstance(m, ToolMessage)]


def call(name: str, args: dict | None = None) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args or {}, "id": uuid.uuid4().hex}])


class FakeModel:
    """Answers each call with ``script(messages)``; records every call."""

    def __init__(self, script, role: str, calls: list):
        self.script, self.role, self.calls, self.bound = script, role, calls, None

    def bind_tools(self, schemas):
        m = FakeModel(self.script, self.role, self.calls)
        m.bound = [s["function"]["name"] for s in schemas]
        return m

    def with_structured_output(self, schema, **kw):
        outer = self

        class _Structured:
            async def ainvoke(self, messages):
                outer.calls.append((outer.role, messages, None))
                plan = outer.script(messages)
                if plan is None:
                    return {"raw": AIMessage(content="nonsense"), "parsed": None, "parsing_error": "bad"}
                return {"raw": AIMessage(content=""), "parsed": schema.model_validate(plan),
                        "parsing_error": None}
        return _Structured()

    async def ainvoke(self, messages):
        self.calls.append((self.role, messages, self.bound))
        out = self.script(messages)
        if hasattr(out, "__await__"):
            out = await out
        return out if isinstance(out, AIMessage) else AIMessage(content=str(out))


class Calls(list):
    """Every model call as (role, messages, bound tools); ``tokens``: the JWT each model was made with."""
    tokens: list


@pytest.fixture
def fake_llm(monkeypatch):
    """``scripts["planner" | "worker" | "summarizer"] = fn(messages)``; ``calls`` records all."""
    scripts: dict = {"summarizer": lambda m: "summary"}
    calls = Calls()
    calls.tokens = []

    def chat(role, token):
        calls.tokens.append(token)
        return FakeModel(lambda m: scripts[role](m), role, calls)
    monkeypatch.setattr(llm, "chat", chat)
    return scripts, calls


@pytest.fixture
def fake_mcp(monkeypatch):
    """``answers[tool] = str | fn(args)``; ``tools`` is what tools/list returns; ``calls`` records."""
    state = {"tools": list(TOOLS), "answers": {}, "calls": []}

    @asynccontextmanager
    async def session(token, run_id):
        yield {"token": token}

    async def list_tools(s):
        return state["tools"]

    async def call_tool(s, name, args, progress=None, cap=-1):
        state["calls"].append((name, args))
        answer = state["answers"].get(name, f"{name} ok")
        # An answer function that takes two arguments also gets the progress callback.
        if callable(answer):
            answer = answer(args, progress) if answer.__code__.co_argcount == 2 else answer(args)
        if hasattr(answer, "__await__"):
            answer = await answer
        return answer

    monkeypatch.setattr(mcp, "session", session)
    monkeypatch.setattr(mcp, "list_tools", list_tools)
    monkeypatch.setattr(mcp, "call_tool", call_tool)
    return state


@pytest.fixture
def signer(tmp_path, monkeypatch):
    """``signer(sub, role="viewer", aud=...)`` makes a token the runner accepts by default."""
    key = ec.generate_private_key(ec.SECP256R1())
    jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="test", alg="ES256", use="sig")
    path = tmp_path / "jwks.json"
    path.write_text(json.dumps({"keys": [jwk]}))
    monkeypatch.setattr(auth, "_jwks", auth._FileJWKS(str(path)))

    def make(sub="alice", role="viewer", aud=("agentgateway", "mcp:aistudio", "agent-runner")):
        now = int(time.time())
        # As Keycloak issues them: a user id as sub, the username in preferred_username.
        return jwt.encode({"iss": "aistudio", "aud": list(aud), "sub": f"id-{sub}", "preferred_username": sub,
                           "jti": uuid.uuid4().hex, "iat": now, "exp": now + 300, "roles": {"aistudio": role}},
                          key, algorithm="ES256", headers={"kid": "test"})
    return make
