"""Tool manifest (app/policy.py -> tools/list _meta + tool-manifest.json)."""
from pathlib import Path

import pytest

from app import manifest as manifest_mod
from app import policy
from app.mcp_server import build
from app.tools.registry import ALL_TOOLS


def test_committed_manifest_is_up_to_date():
    with open(manifest_mod.MANIFEST_PATH, encoding="utf-8") as fh:
        assert fh.read() == manifest_mod.render(), "run: python -m app.manifest"


def test_every_registered_tool_has_a_row_and_no_row_is_orphaned():
    assert {t.name for t in ALL_TOOLS} == set(policy.TOOL_POLICY)
    names = [t["name"] for t in policy.manifest()["tools"]]
    assert len(names) == len(set(names)) == len(ALL_TOOLS) == 35


def test_role_mapping():
    by = {t["name"]: t for t in policy.manifest()["tools"]}
    host = {n for n, p in policy.TOOL_POLICY.items() if p.tier == "host"}
    writes = {n for n, p in policy.TOOL_POLICY.items() if p.tier == "write"}
    assert writes == {"start_workload", "start_jupyter", "stop_jupyter", "delete_run_results"}
    for name in writes:
        # stop_jupyter and delete_run_results too: the creator rule, not the role, keeps an operator to their own tasks.
        assert by[name]["minRole"] == "operator" and by[name]["readOnlyHint"] is False
    for name in host:
        assert by[name]["minRole"] == "admin" and by[name]["readOnlyHint"] is True
    for name in set(by) - host - writes:
        assert by[name]["minRole"] == "viewer" and by[name]["readOnlyHint"] is True
    assert [n for n, t in by.items() if t["destructiveHint"]] == ["stop_jupyter", "delete_run_results"]


def test_tools_for_role_is_cumulative():
    viewer = set(policy.tools_for_role("viewer"))
    operator = set(policy.tools_for_role("operator"))
    admin = set(policy.tools_for_role("admin"))
    assert viewer < operator < admin == set(policy.TOOL_POLICY)
    assert "node_exec" not in operator and "start_workload" not in viewer
    assert policy.tools_for_role(None) == [] and policy.tools_for_role("root") == []


async def test_tools_list_publishes_the_manifest_row_on_every_tool():
    tools = await build().list_tools()
    assert {t.name for t in tools} == set(policy.TOOL_POLICY)
    for t in tools:
        row = policy.TOOL_POLICY[t.name]
        assert t.annotations is not None, f"{t.name} has no annotations (would count as a write)"
        assert t.annotations.readOnlyHint is row.read_only
        assert t.annotations.destructiveHint is (row.destructive if not row.read_only else False)
        assert t.meta == {policy.META_MIN_ROLE: row.min_role, policy.META_TIER: row.tier}


# agentgateway rules

def test_rules_cover_every_tool_exactly_once():
    rules = " ".join(manifest_mod.cel_rules())
    for name in policy.TOOL_POLICY:
        assert rules.count(f"'{name}'") == 1


def test_rules_use_the_aistudio_role_claim_and_clear_a_role_and_above():
    rules = manifest_mod.cel_rules()
    assert all(r.startswith("jwt.roles.aistudio in ") for r in rules)
    joined = "\n".join(rules)
    assert "['viewer', 'operator', 'admin'] && mcp.tool.name in" in joined
    assert "['operator', 'admin'] && mcp.tool.name in" in joined
    assert "['admin'] && mcp.tool.name in" in joined


def test_rewrite_replaces_only_the_generated_block():
    text = ("keep: 1\n  rules:\n  " + manifest_mod.BEGIN + "\n  - \"stale\"\n  "
            + manifest_mod.END + "\nkeep: 2\n")
    out = manifest_mod.rewrite(text)
    assert out.startswith("keep: 1\n") and out.endswith("keep: 2\n") and "stale" not in out
    assert manifest_mod.rewrite(out) == out


# agentgateway's config, whose role rules are generated from the manifest.
CONNECTOR = Path(__file__).resolve().parents[2] / "gateway/config.yaml"


@pytest.mark.skipif(not CONNECTOR.exists(), reason="run from the repo, not the mcp-server image")
def test_the_gateway_rules_are_generated_from_the_manifest():
    text = CONNECTOR.read_text(encoding="utf-8")
    assert manifest_mod.rewrite(text) == text, f"run: python -m app.manifest {CONNECTOR}"
    assert "jwt.roles.aistudio" in text
