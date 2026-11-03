"""The write switch (MCP_WRITES_ENABLED) and dedup of retried writes."""
import asyncio
import contextlib
import hashlib
import json
import threading
import time
from typing import Any, Optional

from app.config import settings
from app.policy import TOOL_POLICY

# ponytail: in-process, so one replica only; move to Redis to run several.
_lock = threading.Lock()
_recent: dict = {}       # dedup key -> (expires, result, done_at)
_inflight: dict = {}     # dedup key -> [lock, holders]


@contextlib.asynccontextmanager
async def serialized(key: Optional[tuple]):
    """Run identical writes one at a time, so a retry waits and then replays the first result."""
    if key is None:
        yield
        return
    entry = _inflight.setdefault(key, [asyncio.Lock(), 0])
    entry[1] += 1
    try:
        async with entry[0]:
            yield
    finally:
        entry[1] -= 1
        if not entry[1]:
            _inflight.pop(key, None)


class Refused(Exception):
    """A refused write; `reason` is a short code for the audit line."""

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


def check_write(tool: str) -> None:
    """Return if this write may run now; raise Refused otherwise."""
    row = TOOL_POLICY.get(tool)
    if row is None or row.read_only:
        return
    if not settings.mcp_writes_enabled:   # backstop: off, the tool is not even registered
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
    """Prefix for a replayed result, so it is not taken for a new action."""
    return (f"ALREADY DONE: an identical {tool} call (same arguments) succeeded {seconds_ago} s ago, so nothing "
            f"ran this time. To do it again on purpose, retry after {settings.mcp_write_dedup_seconds} s. "
            "That call's result:\n")


def remember(key: Optional[tuple], result: Any) -> None:
    if key is not None:
        now = time.time()
        with _lock:
            _sweep(now)   # ponytail: O(entries) per write; fine while writes are rare
            _recent[key] = (now + settings.mcp_write_dedup_seconds, result, now)


def reset() -> None:
    """Tests only."""
    with _lock:
        _recent.clear()
