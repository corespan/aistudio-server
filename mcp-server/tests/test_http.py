"""The served app: the JWT gate, then an MCP session over streamable HTTP."""
from __future__ import annotations

import json

import httpx
import pytest
from starlette.testclient import TestClient


class _Session:
    """A streamable-HTTP MCP session as one user."""

    def __init__(self, client: TestClient, token: str):
        self.c, self.token, self.sid, self.n = client, token, None, 0

    def rpc(self, method: str, params: dict | None = None, notify: bool = False):
        body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            self.n += 1
            body["id"] = self.n
        h = {"authorization": f"Bearer {self.token}",
             "accept": "application/json, text/event-stream",
             "content-type": "application/json"}
        if self.sid:
            h["mcp-session-id"] = self.sid
        r = self.c.post("/mcp", headers=h, content=json.dumps(body))
        assert r.status_code in (200, 202), (r.status_code, r.text)
        self.sid = r.headers.get("mcp-session-id", self.sid)
        if notify:
            return None
        text = r.text
        if "text/event-stream" in r.headers.get("content-type", ""):
            text = next(line[5:] for line in text.splitlines() if line.startswith("data:"))
        return json.loads(text)

    def open(self):
        self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                "clientInfo": {"name": "test", "version": "0"}})
        self.rpc("notifications/initialized", notify=True)
        return self

    def call(self, tool: str, **args) -> str:
        res = self.rpc("tools/call", {"name": tool, "arguments": args})["result"]
        return "".join(c.get("text", "") for c in res["content"])


@pytest.fixture
def served(monkeypatch, jwks_file):
    monkeypatch.setattr("app.config.settings.mcp_host", "0.0.0.0")
    monkeypatch.setattr("app.config.settings.mcp_allowed_hosts", "")

    # Fake aistudio-server recording requests.
    seen: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"status": "ok", "database": "up"})

    monkeypatch.setattr("app.tools.aistudio._client", httpx.AsyncClient(
        base_url="http://aistudio", transport=httpx.MockTransport(upstream)))

    from app.mcp_server import build_http_app
    with TestClient(build_http_app()) as client:
        yield client, seen


def _initialize(client, headers):
    return client.post("/mcp", headers={"accept": "application/json, text/event-stream",
                                        "content-type": "application/json", **headers},
                       content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                           "params": {"protocolVersion": "2025-06-18",
                                                      "capabilities": {},
                                                      "clientInfo": {"name": "t", "version": "0"}}}))


# ── the gate
def test_health_needs_no_token(served):
    client, _ = served
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_no_token_is_401(served):
    client, _ = served
    r = _initialize(client, {})
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Bearer")


def test_a_bad_token_is_401(served, mint):
    client, _ = served
    assert _initialize(client, {"authorization": f"Bearer {mint(ttl=-120)}"}).status_code == 401
    assert _initialize(client, {"authorization": f"Bearer {mint(aud='mcp:composer')}"}).status_code == 401
    assert _initialize(client, {"authorization": "Bearer not-a-jwt"}).status_code == 401


def test_a_token_without_jti_is_401(served, mint):
    client, _ = served
    assert _initialize(client, {"authorization": f"Bearer {mint(jti=None)}"}).status_code == 401


# ── the menu
def test_the_context_parameter_is_not_part_of_any_tool_schema(served, mint):
    client, _ = served
    tools = _Session(client, mint()).open().rpc("tools/list")["result"]["tools"]
    assert len(tools) == 35
    for t in tools:
        assert "mcp_ctx" not in json.dumps(t["inputSchema"]), t["name"]
        assert t["_meta"]["corespan.ai/minRole"] in ("viewer", "operator", "admin")


# ── the call to aistudio-server ──
def test_an_api_tool_reaches_aistudio(served, mint):
    client, seen = served
    text = _Session(client, mint(role="viewer")).open().call("check_service_health")
    assert seen and seen[-1].url.path == "/health" and "API is healthy" in text


# ── the role backstop
def test_a_viewer_cannot_run_a_host_probe(served, mint):
    client, seen = served
    text = _Session(client, mint(role="viewer")).open().call("list_dir", path="/")
    assert "refused" in text and "higher aistudio role" in text and not seen


def test_an_operator_can_write_but_not_probe_hosts(served, mint):
    client, _ = served
    s = _Session(client, mint(role="operator")).open()
    assert "higher aistudio role" in s.call("queue_status")
    assert "higher aistudio role" not in s.call("start_jupyter", node_ip="")
    # stop_jupyter reaches the tool (no role refusal), which then refuses: here no aistudio-server
    # answers, so it cannot read the creator and fails closed.
    out = s.call("stop_jupyter", task_id="jup-20260101-abc123")
    assert "higher aistudio role" not in out and "Refused:" in out and "only an admin" in out


def test_an_admin_is_not_stopped_by_the_backstop(served, mint):
    client, _ = served
    text = _Session(client, mint(role="admin")).open().call("list_dir", path="/")
    assert "higher aistudio role" not in text


def test_a_user_with_no_aistudio_role_gets_nothing(served, mint):
    client, seen = served
    text = _Session(client, mint(roles={"composer": "admin"})).open().call("check_service_health")
    assert "higher aistudio role than none" in text and not seen


def test_the_host_allowlist_takes_a_name_with_or_without_its_port(monkeypatch, jwks_file, mint):
    # agentgateway sends "Host: aistudio-mcp" with no port, so the compose file lists both.
    monkeypatch.setattr("app.config.settings.mcp_host", "0.0.0.0")
    monkeypatch.setattr("app.config.settings.mcp_allowed_hosts", "aistudio-mcp,aistudio-mcp:8081")
    from app.mcp_server import build_http_app
    auth = {"authorization": f"Bearer {mint()}"}
    with TestClient(build_http_app()) as client:
        for host, want in (("aistudio-mcp", 200), ("aistudio-mcp:8081", 200), ("evil.example", 421)):
            assert _initialize(client, {**auth, "host": host}).status_code == want, host


def test_the_401_body_does_not_echo_the_verifier_error(served, mint):
    client, _ = served
    for token in (mint(ttl=-120), mint(aud="mcp:composer"), "not-a-jwt"):
        r = _initialize(client, {"authorization": f"Bearer {token}"})
        assert r.status_code == 401 and r.json()["detail"] == "invalid or expired token", r.text
