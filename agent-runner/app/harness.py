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


def available(user_tools: list[dict]) -> dict[str, Harness]:
    """Harnesses this caller can use: those left with at least one tool."""
    return {n: h for n, h in load().items() if tools_for(h, user_tools)}
