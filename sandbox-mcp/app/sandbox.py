"""One sandbox run: a disposable gVisor container with no network, then gone.

Every limit is set here, from settings, never from the caller: the caller chooses only the
program, its input files and a timeout up to the maximum.
"""
from __future__ import annotations

import base64
import io
import secrets
import tarfile
import time
from dataclasses import dataclass, field

import requests

from app.config import settings

NOBODY = 65534
WORK = "/tmp/work"          # /tmp exists in every image; the program runs here as nobody
_PROGRAM = {"python": ("main.py", ["python", "-I", "main.py"]),
            "sh": ("main.sh", ["sh", "main.sh"])}


class Refused(ValueError):
    """The request breaks a rule; the message says which, for the model to read."""


@dataclass
class Result:
    exit_code: int | None
    stdout: str
    stderr: str
    seconds: float
    timed_out: bool = False
    files: dict[str, str] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)


def _check(code: str, language: str, files: dict[str, str], timeout: int) -> int:
    if language not in _PROGRAM:
        raise Refused(f"language must be one of {sorted(_PROGRAM)}")
    if not code.strip():
        raise Refused("code is empty")
    size = len(code.encode()) + sum(len(v.encode()) for v in files.values())
    if size > settings.max_input_bytes:
        raise Refused(f"code and files are {size} bytes; at most {settings.max_input_bytes}")
    for name in files:
        # Plain names only: no directories, no way out of the work directory.
        if not name or "/" in name or "\\" in name or name in (".", "..", "out") or name.startswith("."):
            raise Refused(f"file name {name!r}: use a plain name like data.json")
        if name == _PROGRAM[language][0]:
            raise Refused(f"file name {name!r} is taken by the program itself")
    return max(1, min(int(timeout), settings.max_seconds))


def _tar(entries: dict[str, str]) -> bytes:
    """work/ with the program, the inputs and an empty out/, owned by nobody."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for d in ("work", "work/out"):
            info = tarfile.TarInfo(d)
            info.type, info.mode, info.uid, info.gid = tarfile.DIRTYPE, 0o755, NOBODY, NOBODY
            tar.addfile(info)
        for name, text in entries.items():
            data = text.encode()
            info = tarfile.TarInfo(f"work/{name}")
            info.size, info.mode, info.uid, info.gid = len(data), 0o644, NOBODY, NOBODY
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _cap(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n[... cut at {limit} characters]"


def _wrapper(command: list[str], marker: str) -> str:
    """Run the program, then print out/ as a base64 tar after ``marker`` and keep its exit code.

    gVisor keeps writes to the container's root filesystem in its own memory, so Docker's
    copy-out never sees them: the sandbox has to hand out/ back itself. The tar is capped at
    twice the text budget; a cut tar returns the files that fit.
    """
    return (f"cd {WORK} && {' '.join(command)}; rc=$?; echo; echo '{marker}'; "
            f"tar -C out -cf - . 2>/dev/null | head -c {2 * settings.file_chars} | base64; exit $rc")


def _split(stdout: bytes, marker: str) -> tuple[bytes, bytes]:
    """(the program's stdout, the base64 tar of out/). No marker: killed before it printed one."""
    head, sep, tail = stdout.rpartition(b"\n" + marker.encode() + b"\n")
    return (head, tail) if sep else (stdout, b"")


def _collect(encoded: bytes) -> tuple[dict[str, str], list[str]]:
    """Text files the program wrote to out/; anything else is named, not returned."""
    files, skipped, budget = {}, [], settings.file_chars
    if not encoded.strip():
        return files, skipped
    members = []
    try:
        with tarfile.open(fileobj=io.BytesIO(base64.b64decode(encoded)), mode="r") as tar:
            for m in tar:               # a cut tar ends early; keep what was complete
                if m.isfile():
                    members.append((m.name.removeprefix("./"), m.size, tar.extractfile(m).read()))
    except (tarfile.TarError, ValueError, OSError):
        skipped.append("the rest of out/ (over the size cap)")
    for name, size, raw in members:
        try:
            text = raw.decode()
        except UnicodeDecodeError:
            skipped.append(f"{name} ({size} bytes, not text)")
            continue
        if len(text) > budget:
            skipped.append(f"{name} ({size} bytes, over the size cap)")
            continue
        files[name], budget = text, budget - len(text)
    return files, skipped


def run(client, code: str, language: str = "python", files: dict[str, str] | None = None,
        timeout: int | None = None) -> Result:
    """Run ``code`` once. Blocking (the Docker SDK is); call it from a thread."""
    files = files or {}
    timeout = _check(code, language, files, settings.default_seconds if timeout is None else timeout)
    program, command = _PROGRAM[language]
    marker = f"<<<sandbox-out {secrets.token_hex(8)}>>>"
    container = client.containers.create(
        settings.image, ["sh", "-c", _wrapper(command, marker)],
        runtime=settings.runtime, network_mode="none", user=f"{NOBODY}:{NOBODY}",
        mem_limit=settings.memory, memswap_limit=settings.memory,
        nano_cpus=int(settings.cpus * 1e9), pids_limit=settings.pids,
        cap_drop=["ALL"], security_opt=["no-new-privileges"],
        environment={"HOME": WORK, "PYTHONDONTWRITEBYTECODE": "1"},
        labels={"aistudio.sandbox": "1"}, detach=True)
    try:
        container.put_archive("/tmp", _tar({program: code, **files}))
        started = time.monotonic()
        container.start()
        timed_out, exit_code = False, None
        try:
            exit_code = container.wait(timeout=timeout)["StatusCode"]
        except requests.exceptions.RequestException:
            timed_out = True
            container.kill()
        seconds = time.monotonic() - started
        stdout, encoded = _split(container.logs(stdout=True, stderr=False), marker)
        stderr = container.logs(stdout=False, stderr=True).decode(errors="replace")
        out, skipped = _collect(encoded)
        return Result(exit_code, _cap(stdout.decode(errors="replace"), settings.output_chars),
                      _cap(stderr, settings.output_chars), seconds, timed_out, out, skipped)
    finally:
        container.remove(force=True)


def describe(r: Result) -> str:
    """The result as text for a model."""
    status = (f"timed out after {r.seconds:.0f}s and was killed" if r.timed_out
              else f"exit code {r.exit_code} in {r.seconds:.1f}s")
    parts = [status, "--- stdout ---", r.stdout or "(empty)"]
    if r.stderr:
        parts += ["--- stderr ---", r.stderr]
    for name, text in r.files.items():
        parts += [f"--- out/{name} ---", text]
    if r.skipped:
        parts += ["--- out/ not returned ---", *r.skipped]
    return "\n".join(parts)
