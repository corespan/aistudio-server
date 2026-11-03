"""Shared helper: run a command without a shell, with a timeout."""

from __future__ import annotations

import asyncio

# Bytes kept per stream; the rest is drained and dropped.
_MAX_BYTES = 2_000_000


def option_like(*values: str) -> str:
    """Return the first value starting with "-" (option-like), or ""."""
    return next((v for v in values if v.startswith("-")), "")


async def _drain(stream: asyncio.StreamReader, buf: bytearray) -> None:
    while chunk := await stream.read(65536):
        if len(buf) < _MAX_BYTES:
            buf += chunk[:_MAX_BYTES - len(buf)]


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
        proc.kill()
        await proc.wait()   # reap it: no zombie
        # Keep what it wrote before the kill.
        await asyncio.gather(_drain(proc.stdout, out), _drain(proc.stderr, err))
        return 124, _text(out), (_text(err) + f"\n{args[0]}: timed out after {timeout}s").strip()
    return proc.returncode or 0, _text(out), _text(err)
