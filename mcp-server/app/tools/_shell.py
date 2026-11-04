"""Shared helper: run a command without a shell, with a timeout."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal

# Bytes kept per stream; the rest is drained and dropped.
_MAX_BYTES = 2_000_000


def option_like(*values: str) -> str:
    """Return the first value starting with "-" (option-like), or ""."""
    return next((v for v in values if v.startswith("-")), "")


async def _drain(stream: asyncio.StreamReader, buf: bytearray) -> None:
    while chunk := await stream.read(65536):
        if len(buf) < _MAX_BYTES:
            buf += chunk[:_MAX_BYTES - len(buf)]


def _kill(proc: asyncio.subprocess.Process) -> None:
    # The whole group (the child leads it): a grandchild holding stdout outlives a plain kill.
    if hasattr(os, "killpg"):
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        proc.kill()   # the child itself, too: Windows has no killpg


def _text(buf: bytearray) -> str:
    text = buf.decode("utf-8", "replace")
    return text + ("\n… (output truncated)" if len(buf) >= _MAX_BYTES else "")


async def run_command(*args: str, timeout: int = 15) -> tuple[int, str, str]:
    """(exit code, stdout, stderr). On timeout: 124, with whatever the command printed by then."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,   # own process group, for _kill; ignored on Windows
        )
    except FileNotFoundError:
        return 127, "", f"{args[0]}: not installed"
    except PermissionError:
        # E.g. tcpdump without NET_RAW: an answer, not a crash.
        return 126, "", f"{args[0]}: permission denied (cannot be started here)"
    out, err = bytearray(), bytearray()
    try:
        await asyncio.wait_for(
            asyncio.gather(_drain(proc.stdout, out), _drain(proc.stderr, err), proc.wait()),
            timeout=timeout)
    except asyncio.TimeoutError:
        _kill(proc)
        # Reap it and keep what it wrote before the kill. Bounded: a grandchild that left
        # the group (setsid) can hold the pipes open forever.
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.gather(
                proc.wait(), _drain(proc.stdout, out), _drain(proc.stderr, err)), timeout=2)
        return 124, _text(out), (_text(err) + f"\n{args[0]}: timed out after {timeout}s").strip()
    return proc.returncode or 0, _text(out), _text(err)
