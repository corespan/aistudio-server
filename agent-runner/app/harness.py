"""Agents from the registry: which tools, model and budget each kind of agent gets."""
from __future__ import annotations

from fnmatch import fnmatchcase

from app import registry
from app.contract import Harness


def load() -> dict[str, Harness]:
    return registry.load().agents


def allowed(tool: dict) -> bool:
    """Phase 1 binds read-only tools only; writes wait for plan approval (Phase 2)."""
    return bool((tool.get("annotations") or {}).get("readOnlyHint"))


def tools_for(harness: Harness, user_tools: list[dict]) -> list[dict]:
    """The caller's tools (already role-filtered by agentgateway) that this harness lists.

    Patterns match connector/tool. Tools only shrink: nothing is added that the caller's
    tools/list didn't return.
    """
    return [t for t in user_tools
            if allowed(t) and any(fnmatchcase(f"{t['connector']}/{t['tool']}", p) for p in harness.tools)]


def panel_tools(harness: Harness) -> list[dict]:
    """The panel tools (panel/<name>) this harness lists, from panel-tools.json. The browser runs
    them; none writes anything, so a role doesn't narrow them."""
    panel = registry.load().panel
    names = [n for p in harness.tools if p.startswith("panel/") for n in sorted(panel)
             if fnmatchcase(f"panel/{n}", p)]
    return [{"name": f"panel__{n}", "connector": "panel", "tool": n, **panel[n]}
            for n in dict.fromkeys(names)]          # in the harness's order, each once


REQUEST = "request_"


def write_tools(harness: Harness, user_tools: list[dict]) -> list[dict]:
    """The writes this agent may ask for, as ``request_<tool>``: calling one only records the
    request. Only the caller's own write tools (agentgateway filtered them by role)."""
    return [{"name": f"{REQUEST}{t['tool']}", "connector": "approval", "tool": t["tool"],
             "target": t["connector"], "inputSchema": t["inputSchema"],
             "description": (f"Ask the user to approve this; nothing happens until they click Approve, "
                             f"after your answer. Say in your answer what you asked for. {t['description']}")}
            for t in user_tools
            if not allowed(t) and f"{t['connector']}/{t['tool']}" in harness.writes]


def chat_claude(user: str) -> dict[str, Harness]:
    """The Claude chat agents this user may run: claude-code agents with panel tools (the coding
    agents have none) on a model the user is listed on."""
    models = registry.load().models
    return {n: h for n, h in load().items()
            if h.engine == "claude-code" and panel_tools(h)
            and h.model in models and user in models[h.model].users}


def available(user_tools: list[dict]) -> dict[str, Harness]:
    """The langgraph harnesses this caller can use (what the planner is offered): those left with
    at least one tool. claude-code agents never: they spend one person's subscription."""
    return {n: h for n, h in load().items() if h.engine == "langgraph" and tools_for(h, user_tools)}
