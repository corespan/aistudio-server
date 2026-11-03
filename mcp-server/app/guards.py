"""Who may run a write tool, and dedup of retried writes.

No write tool runs unless the site sets MCP_WRITES_ENABLED (mcp_server.build does not even register them).
Which role reaches which write is the manifest's job (app/policy.py), enforced by agentgateway and again by
the backstop in mcp_server. A retried write (same caller, client, tool and arguments) within
MCP_WRITE_DEDUP_SECONDS returns the first result instead of running again.
"""
import hashlib
import json
import threading
import time
from typing import Any, Optional

from app.config import settings
from app.policy import TOOL_POLICY

# ponytail: in-process store with one lock. Fine for one aistudio-mcp replica (compose runs one); with
# several, a retry landing on another replica runs again -- move it to Redis then.
_lock = threading.Lock()
_recent: dict = {}       # dedup key -> (expires, result, done_at)


class Refused(Exception):
    """A write this caller may not make now. The message is for the model (and through it, the user);
    `reason` is a short code for the audit line."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def args_hash(args: dict) -> str:
    return hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _key(ident, tool: str, args: dict) -> tuple:
    return (ident.sub, ident.client_id or "", tool, args_hash(args))


def _sweep(now: float) -> None:
    for k in [k for k, v in _recent.items() if v[0] <= now]:
        del _recent[k]


def check_write(ident, tool: str, args: dict) -> None:
    """Return if this write may run now; raise Refused otherwise."""
    row = TOOL_POLICY.get(tool)
    if row is None or row.read_only:
        return
    if not settings.mcp_writes_enabled:   # backstop: with the switch off the tool is not even registered
        raise Refused("writes_off", f"{tool} is turned off on this deployment (MCP_WRITES_ENABLED is off).")


def write_key(ident, tool: str, args: dict) -> Optional[tuple]:
    row = TOOL_POLICY.get(tool)
    if ident is None or row is None or row.read_only or settings.mcp_write_dedup_seconds <= 0:
        return None
    return _key(ident, tool, args)


def recall(key: Optional[tuple]) -> Optional[tuple]:
    """(seconds ago, result) of the same write made within the dedup window, or None."""
    if key is None:
        return None
    now = time.time()
    with _lock:
        _sweep(now)
        hit = _recent.get(key)
    return (int(now - hit[2]), hit[1]) if hit else None


def replay_note(tool: str, seconds_ago: int) -> str:
    """Said before a replayed result, so neither the model nor the user takes it for a new action: a
    deliberate repeat (the same run started twice) learns that nothing ran, and can retry after the window."""
    return (f"ALREADY DONE: an identical {tool} call (same arguments) succeeded {seconds_ago} s ago, so nothing "
            f"ran this time. To do it again on purpose, retry after {settings.mcp_write_dedup_seconds} s. "
            "That call's result:\n")


def remember(key: Optional[tuple], result: Any) -> None:
    if key is not None:
        now = time.time()
        with _lock:
            _sweep(now)   # ponytail: O(entries) per write; writes are rare, fine until they are not
            _recent[key] = (now + settings.mcp_write_dedup_seconds, result, now)


def reset() -> None:
    """Tests only."""
    with _lock:
        _recent.clear()
