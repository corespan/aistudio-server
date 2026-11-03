"""Shared helper: run a command without a shell, with a timeout."""

from __future__ import annotations

import asyncio


def option_like(*values: str) -> str:
    """Return the first value starting with "-" (option-like), or ""."""
    return next((v for v in values if v.startswith("-")), "")


async def run_command(*args: str, timeout: int = 15) -> tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return 127, "", f"{args[0]}: not installed"
    except PermissionError:
        # The kernel refuses a binary whose file capability the container lacks (tcpdump without
        # NET_RAW, if a deployment drops it): an answer, not a crash.
        return 126, "", f"{args[0]}: permission denied (cannot be started here)"
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "", f"{args[0]}: timed out after {timeout}s"
    return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
