"""The registry: the shipped lock file is current, and a broken pack names every problem."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from app import registry
from app.config import settings


def test_the_shipped_lock_file_is_up_to_date():
    # What CI checks: someone changed a pack and didn't run `make registry`.
    assert registry.main(["registry", "check"]) == 0
    reg = registry.load()
    assert set(reg.agents) == {"analyst", "assistant", "diagnostics", "coder", "reviewer"}
    assert reg.agents["coder"].engine == "claude-code" and reg.repos == ["corespan/aistudio-server"]
    assert set(reg.connectors) == {"aistudio", "sandbox"}
    assert set(reg.models) == {"claude-coder"}


def test_each_connector_renders_a_gateway_route_and_an_audience():
    files = registry.render(registry.build(Path(settings.platform_dir)))
    by_name = {p.name: text for p, text in files.items()}
    routes = yaml.safe_load(by_name["config.yaml"])["binds"][0]["listeners"][0]["routes"]
    policies = {r["matches"][0]["path"]["pathPrefix"]: r["policies"] for r in routes}
    assert set(policies) == {"/llm", "/mcp/aistudio", "/mcp/sandbox", "/github/"}
    # GitHub: the sub and the exact REST calls are checked; the PAT comes from the gateway's env.
    rule = policies["/github/"]["authorization"]["rules"][0]
    assert rule.startswith("jwt.sub in ['rsingh'] && ") and "git/(blobs|trees|commits|refs)" in rule
    assert "receive-pack" not in rule and "DELETE" not in rule and "PUT" not in rule
    # The one update allowed: refs under agent/ (pr-followup's fast-forward). Never master.
    assert "request.method == 'PATCH' && request.path.matches('^/github/repos/(corespan/aistudio-server)/git/refs/heads/agent/" in rule
    assert policies["/github/"]["requestHeaderModifier"]["set"]["authorization"] == "Bearer ${GITHUB_PAT}"
    # Every route checks the JWT; only the browser connector answers CORS.
    assert all(p["jwtAuth"]["mode"] == "strict" for p in policies.values())
    assert "cors" in policies["/mcp/aistudio"] and "cors" not in policies["/mcp/sandbox"]
    assert json.loads(by_name["audiences.json"]) == ["agentgateway", "agent-runner", "mcp:aistudio", "mcp:sandbox"]


def test_an_anthropic_model_renders_the_sandbox_gateway_for_its_users_only():
    files = {p.name: t for p, t in registry.render(registry.build(Path(settings.platform_dir))).items()}
    assert "anthropic" not in files["config.yaml"]   # the main gateway never holds the token
    [bind] = yaml.safe_load(files["sandbox.yaml"])["binds"]
    assert bind["port"] == 8051
    route = bind["listeners"][0]["routes"][0]   # then only read-only git (next test)
    assert route["matches"][0]["path"]["pathPrefix"] == "/llm/anthropic"
    pol = route["policies"]
    assert pol["jwtAuth"]["mode"] == "strict"
    assert pol["authorization"]["rules"] == ["jwt.sub in ['rsingh']"]
    assert pol["localRateLimit"][0]["maxTokens"] == 60
    # The token is substituted by agentgateway from its own env, never rendered into the file.
    assert pol["requestHeaderModifier"]["set"]["authorization"] == "Bearer ${CLAUDE_CODE_OAUTH_TOKEN}"


def test_each_repo_gets_clone_routes_and_nothing_that_could_push():
    files = {p.name: t for p, t in registry.render(registry.build(Path(settings.platform_dir))).items()}
    routes = yaml.safe_load(files["sandbox.yaml"])["binds"][0]["listeners"][0]["routes"]
    git = [r for r in routes if r["matches"][0]["path"].get("exact", "").startswith("/git/")]
    assert {(r["matches"][0]["method"], r["matches"][0]["path"]["exact"]) for r in git} == {
        ("GET", "/git/corespan/aistudio-server.git/info/refs"),
        ("POST", "/git/corespan/aistudio-server.git/git-upload-pack")}
    assert "receive-pack" not in files["sandbox.yaml"]
    for r in git:   # the caller's JWT is checked, then never sent to GitHub
        assert r["policies"]["jwtAuth"]["mode"] == "strict"
        assert r["policies"]["requestHeaderModifier"] == {"remove": ["authorization"]}


def test_a_model_user_that_could_break_the_cel_rule_is_refused(tmp_path):
    _write(tmp_path, {
        "platform.yaml": "name: t\npacks: [one]\n",
        "packs/one/pack.yaml": "kind: pack\nname: one\nversion: '1'\n",
        "packs/one/models/m.yaml": "kind: model\nname: m\nprovider: anthropic\nmodel: x\nusers: [\"a' || true || '\"]\n",
    })
    with pytest.raises(registry.Invalid, match="packs/one/models/m.yaml"):
        registry.build(tmp_path)


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")


def test_a_broken_registry_reports_every_problem(tmp_path):
    _write(tmp_path, {
        "platform.yaml": "name: t\npacks: [one, two]\n",
        "packs/one/pack.yaml": "kind: pack\nname: one\nversion: '1'\nrequires: [three]\n",
        "packs/one/connectors/a.yaml": "kind: connector\nname: a\nupstream: http://a/mcp\nrules: ['true']\n",
        "packs/one/agents/x.yaml": "kind: agent\nname: x\npurpose: p\ntools: [a/ok, b/missing, bare]\n",
        "packs/one/agents/typo.yaml": "kind: agent\nname: y\npurpose: p\ntools: [a/ok]\nbudget: {}\n",
        "packs/one/odd.yaml": "kind: gadget\nname: z\n",
        "packs/two/pack.yaml": "kind: pack\nname: two\nversion: '1'\n",
        "packs/two/connectors/a.yaml": "kind: connector\nname: a\nupstream: http://other/mcp\nrules: ['true']\n",
    })
    with pytest.raises(registry.Invalid) as err:
        registry.build(tmp_path)
    text = str(err.value)
    for problem in ("requires pack 'three'", "names connector 'b'", "'bare' must be connector/tool",
                    "packs/one/agents/typo.yaml", "kind 'gadget'", "connector 'a' is already in"):
        assert problem in text, problem


def test_a_hand_edited_lock_file_is_refused(tmp_path, monkeypatch):
    lock = Path(settings.platform_dir, registry.LOCK).read_text(encoding="utf-8")
    (tmp_path / registry.LOCK).write_text(lock.replace('"steps": 6', '"steps": 60'), encoding="utf-8")
    monkeypatch.setattr(settings, "platform_dir", str(tmp_path))
    registry.load.cache_clear()
    try:
        with pytest.raises(registry.Invalid):
            registry.load()
    finally:
        registry.load.cache_clear()
