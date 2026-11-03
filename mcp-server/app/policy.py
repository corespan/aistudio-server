"""Tool manifest: each AI Studio MCP tool's minimum role, tier and hints."""
from dataclasses import dataclass
from typing import Optional

from mcp.types import ToolAnnotations

ROLES = ("viewer", "operator", "admin")
_RANK = {r: i for i, r in enumerate(ROLES)}

META_MIN_ROLE = "corespan.ai/minRole"
META_TIER = "corespan.ai/tier"


@dataclass(frozen=True)
class ToolPolicy:
    tier: str
    min_role: str
    read_only: bool
    destructive: bool = False
    idempotent: bool = False


def _read() -> ToolPolicy:
    return ToolPolicy("read", "viewer", read_only=True, idempotent=True)


def _host() -> ToolPolicy:
    return ToolPolicy("host", "admin", read_only=True)


TOOL_POLICY: dict = {
    # --- platform reads ---
    "workload_history":        _read(),
    "throughput_compare":      _read(),
    "check_service_health":    _read(),
    "platform_catalog":        _read(),
    "list_benchmarks":         _read(),
    "get_benchmark":           _read(),
    "summary":                 _read(),
    "model_config":            _read(),
    "get_task_status":         _read(),
    "get_task_logs":           _read(),
    # --- writes ---
    "start_workload":          ToolPolicy("write", "operator", read_only=False),
    "start_jupyter":           ToolPolicy("write", "operator", read_only=False),
    # Operators reach it, but only for tasks they started (app/tools/actions.py); admins for any.
    "stop_workload":           ToolPolicy("write", "operator", read_only=False,
                                          destructive=True, idempotent=True),
    # --- host probes: admin only ---
    "timeline":                _host(),
    "list_containers":         _host(),
    "docker_logs":             _host(),
    "inspect_container":       _host(),
    "container_diff":          _host(),
    "read_file":               _host(),
    "list_dir":                _host(),
    "grep_logs":               _host(),
    "git_log":                 _host(),
    "git_show":                _host(),
    "network_check":           _host(),
    "list_listening_ports":    _host(),
    "tcpdump_capture":         _host(),
    "check_tls_cert":          _host(),
    "check_clock_skew":        _host(),
    "frontend_check":          _host(),
    "queue_status":            _host(),
    "check_ssh_connectivity":  _host(),
    "check_node_docker":       _host(),
    "node_docker_logs":        _host(),
    "node_exec":               _host(),
}


def is_write(name: str) -> bool:
    return not TOOL_POLICY[name].read_only


def annotations(name: str) -> ToolAnnotations:
    p = TOOL_POLICY[name]
    return ToolAnnotations(readOnlyHint=p.read_only,
                           destructiveHint=p.destructive if not p.read_only else False,
                           idempotentHint=p.idempotent,
                           openWorldHint=True)


def meta(name: str) -> dict:
    p = TOOL_POLICY[name]
    return {META_MIN_ROLE: p.min_role, META_TIER: p.tier}


def min_role(name: str) -> Optional[str]:
    """Minimum role for a tool; None = no manifest row (fail closed)."""
    return TOOL_POLICY[name].min_role if name in TOOL_POLICY else None


def role_at_least(role: Optional[str], need: Optional[str]) -> bool:
    return role in _RANK and need in _RANK and _RANK[role] >= _RANK[need]


def capped(role: Optional[str], cap: Optional[str]) -> Optional[str]:
    """The lower of two roles; None when either is not a known role."""
    if role not in _RANK or cap not in _RANK:
        return None
    return ROLES[min(_RANK[role], _RANK[cap])]


def role_allows(role: Optional[str], name: str) -> bool:
    return role_at_least(role, min_role(name))


def tools_for_role(role: Optional[str], registered: Optional[set] = None) -> list:
    """Tools a role reaches per the manifest (what agentgateway will let through)."""
    return [n for n in TOOL_POLICY
            if (registered is None or n in registered) and role_allows(role, n)]


def manifest() -> dict:
    """Every tool this server exposes."""
    return {
        "server": "aistudio",
        "version": 1,
        "roles": list(ROLES),
        "tools": [{"name": n, "minRole": p.min_role, "tier": p.tier,
                   "readOnlyHint": p.read_only,
                   "destructiveHint": p.destructive if not p.read_only else False,
                   "idempotentHint": p.idempotent}
                  for n, p in TOOL_POLICY.items()],
    }
