"""Docker-level diagnostic tools."""

from __future__ import annotations

import asyncio
import difflib
import io
import os
import posixpath
import tarfile
from datetime import datetime, timedelta, timezone

import docker
import docker.errors
import structlog
from langchain_core.tools import tool

from app.config import settings
from app.tools.fs_probes import _is_secret, _readable_roots, _resolve_within_roots

logger = structlog.get_logger()

_EXIT_HINTS = {
    125: "docker run itself failed (bad flag/image)",
    126: "container command not executable",
    127: "container command not found",
    137: "SIGKILL — usually OOM-killed or `docker kill`",
    139: "SIGSEGV — segfault",
    143: "SIGTERM — graceful stop",
}

_ERROR_KWS = (
    "error", "fail", "exception", "traceback", "refused", "denied",
    "timeout", "oom", "killed", "critical", "fatal", "panic",
)

_client: docker.DockerClient | None = None


def _docker() -> docker.DockerClient:
    global _client
    if _client is None:
        _client = docker.DockerClient(
            base_url=settings.docker_host,
            use_ssh_client=settings.docker_host.startswith("ssh://"),
        )
    return _client


def _fmt_exit(code: int | None) -> str:
    if code is None:
        return "?"
    hint = _EXIT_HINTS.get(code)
    return f"{code} ({hint})" if hint else str(code)


def _events_raw(since: datetime, until: datetime) -> list[dict]:
    return list(
        _docker().events(
            since=since, until=until, filters={"type": "container"}, decode=True,
        )
    )


def _resolve_container(name: str):
    """Find a container by exact name/ID, else by unique substring."""
    client = _docker()
    try:
        return client.containers.get(name)
    except docker.errors.NotFound:
        matches = [c for c in client.containers.list(all=True) if name.lower() in c.name.lower()]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            names = ", ".join(sorted(c.name for c in matches))
            raise docker.errors.NotFound(
                f"{name!r} matches multiple containers ({names}) — retry with the full name."
            )
        have = ", ".join(sorted(c.name for c in client.containers.list(all=True))) or "none"
        raise docker.errors.NotFound(
            f"no container named or matching {name!r} on this host (containers: {have})"
        )


def _inspect_raw(name: str) -> dict:
    return _resolve_container(name).attrs


def _logs_raw(name: str, since: datetime, tail: int) -> str:
    c = _resolve_container(name)
    kwargs = {"timestamps": True, "tail": tail}
    if c.status == "running":
        kwargs["since"] = since
    raw = c.logs(**kwargs)
    return raw.decode("utf-8", errors="replace")


def _extract_one_file(raw: bytes, cap: int = 1_000_000) -> tuple[str, str]:
    """Parse a tar blob holding one file into ("file", text), or ("dir", "")."""
    with tarfile.open(fileobj=io.BytesIO(raw)) as tf:
        members = [m for m in tf.getmembers() if m.isfile()]
        if len(members) != 1:
            return "dir", ""
        extracted = tf.extractfile(members[0])
        data = extracted.read(cap) if extracted else b""
    return "file", data.decode("utf-8", errors="replace")


def _container_read_raw(name: str, path: str, cap: int = 1_000_000) -> tuple[str, str]:
    """("file", text), ("dir", ""), ("link", target) unfollowed, or ("big", size) over `cap`."""
    c = _resolve_container(name)
    bits, stat = c.get_archive(path)
    early = (("link", stat["linkTarget"]) if stat.get("linkTarget") else
             ("big", str(stat.get("size"))) if (stat.get("size") or 0) > cap else None)
    if early:
        bits.close()
        return early
    buf = io.BytesIO()
    for chunk in bits:
        buf.write(chunk)
        if buf.tell() > cap + 200_000:
            break
    return _extract_one_file(buf.getvalue(), cap)


def _container_roots() -> list[str]:
    return [r.strip() for r in settings.container_readable_roots.split(",") if r.strip()]


def _container_path_allowed(path: str) -> bool:
    """Return whether `path` is inside a container root (textual check)."""
    if not path.startswith("/"):
        return False
    real = posixpath.normpath(path)
    return any(real == r or real.startswith(r.rstrip("/") + "/") for r in _container_roots())


def _is_failure(ev: dict) -> bool:
    action = ev.get("Action") or ev.get("status") or ""
    if action == "die":
        return str(ev.get("Actor", {}).get("Attributes", {}).get("exitCode", "0")) != "0"
    return action in ("kill", "oom") or "unhealthy" in action


def _ev_time(ev: dict) -> datetime:
    return datetime.fromtimestamp(ev.get("time", 0), tz=timezone.utc)


@tool
async def list_containers(include_stopped: bool = True) -> str:
    """List containers with status, health, exit code and ports; `include_stopped` adds exited ones."""
    logger.info("tool.docker.list_containers", include_stopped=include_stopped)

    def _blocking() -> list[str]:
        rows: list[str] = []
        for c in _docker().containers.list(all=include_stopped):
            state = c.attrs.get("State", {})
            health = (state.get("Health") or {}).get("Status")
            line = f"{c.name}  [{c.status}]"
            if health:
                line += f" health={health}"
            if c.status in ("exited", "dead"):
                line += f" exit={_fmt_exit(state.get('ExitCode'))}"
                if state.get("FinishedAt", "").startswith("2"):
                    line += f" finished={state['FinishedAt']}"
            # Not c.image: it 404s once the tag has moved on.
            line += f"  image={c.attrs.get('Config', {}).get('Image') or '<none>'}"
            pub = sorted(f"{b['HostPort']}->{port}"
                         for port, binds in (c.ports or {}).items() for b in binds or [])
            if pub:
                line += "  ports=" + ",".join(pub)
            rows.append(line)
        return rows

    try:
        rows = await asyncio.to_thread(_blocking)
    except docker.errors.DockerException as exc:
        return f"Cannot reach the Docker daemon: {exc}"
    if not rows:
        return "No containers found on this host."
    return "Containers on host:\n" + "\n".join(f"  {r}" for r in rows)


@tool
async def docker_logs(name: str, since_minutes: int = 15, tail: int = 200) -> str:
    """Fetch container `name`'s logs from the last `since_minutes` minutes, at most `tail` lines."""
    since_minutes = max(1, min(since_minutes, 60 * 24 * 30))
    tail = max(1, min(tail, 5000))
    logger.info("tool.docker.logs", name=name, since_minutes=since_minutes, tail=tail)
    since = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
    try:
        text = await asyncio.to_thread(_logs_raw, name, since, tail)
    except docker.errors.NotFound as exc:
        return str(exc)
    except docker.errors.DockerException as exc:
        return f"Cannot read logs for {name!r}: {exc}"

    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return f"No logs for {name!r} in the last {since_minutes} min."
    error_lines = [ln for ln in lines if any(kw in ln.lower() for kw in _ERROR_KWS)]
    out = [f"Logs for {name!r} (last {since_minutes} min, {len(lines)} line(s)):"]
    if error_lines:
        out.append(f"\n⚠️  {len(error_lines)} error/warning line(s):")
        out += [f"  • {ln}" for ln in error_lines[-15:]]
    out.append("\nMost recent lines:")
    out += [f"  {ln}" for ln in lines[-30:]]
    return "\n".join(out)


def _format_inspect(name: str, a: dict) -> str:
    state = a.get("State", {})
    health = state.get("Health") or {}
    parts = [
        f"Inspect {name!r}:",
        f"  status: {state.get('Status')}",
        f"  exit code: {_fmt_exit(state.get('ExitCode'))}",
        f"  OOMKilled: {state.get('OOMKilled')}",
        f"  restart count: {a.get('RestartCount')}",
        f"  restart policy: {a.get('HostConfig', {}).get('RestartPolicy', {}).get('Name')}",
        f"  started: {state.get('StartedAt')}",
        f"  finished: {state.get('FinishedAt')}",
        f"  image: {a.get('Config', {}).get('Image')}",
    ]
    if health:
        parts.append(f"  health: {health.get('Status')}")
        log = health.get("Log") or []
        if log:
            parts.append(f"  last health check: {log[-1].get('Output', '').strip()[:300]}")
    return "\n".join(parts)


@tool
async def inspect_container(name: str) -> str:
    """Inspect container `name`: exit code, OOM, restarts, health."""
    logger.info("tool.docker.inspect", name=name)
    try:
        a = await asyncio.to_thread(_inspect_raw, name)
    except docker.errors.NotFound as exc:
        return str(exc)
    except docker.errors.DockerException as exc:
        return f"Cannot inspect {name!r}: {exc}"
    return _format_inspect(name, a)


@tool
async def container_diff(container: str, path: str, repo_path: str = "", max_lines: int = 120) -> str:
    """Diff file `path` inside `container` against its repo copy `repo_path` (same file name) to detect stale code; `max_lines` caps output."""
    logger.info("tool.docker.container_diff", container=container, path=path, repo_path=repo_path)
    if not container.strip():
        return ("Refused: container is required — `list_containers` names every container "
                "on this host and its published ports.")
    if not _container_path_allowed(path):
        return (
            f"Refused: {path!r} is outside the container roots this tool may read "
            f"({', '.join(_container_roots())}). Those hold deployed code and assets, "
            "which is what a stale-code check compares; everything else in a "
            "container's filesystem — credentials and keys included — is out of scope."
        )
    if not repo_path:
        return (
            f"Refused: container_diff needs the repo file to compare against — it diffs "
            f"a deployed file against its source, it is not a file reader. Pass "
            f"repo_path=/repo/…/{posixpath.basename(path)} (`list_dir` finds it)."
        )
    if _is_secret(path) or _is_secret(repo_path):
        return f"Refused: {posixpath.basename(path)!r} is a secrets file."
    if posixpath.basename(path) != os.path.basename(repo_path):
        return (
            f"Refused: {posixpath.basename(path)!r} and {os.path.basename(repo_path)!r} "
            "are different files, so a diff of them says nothing about stale code. "
            "Compare a container file against its OWN source file."
        )
    try:
        kind, text = await asyncio.to_thread(_container_read_raw, container, path)
    except docker.errors.NotFound as exc:
        return f"Not found: {exc} (container missing, or {path!r} does not exist inside it)."
    except docker.errors.DockerException as exc:
        return (f"Cannot read {path!r} from {container!r}: {exc} "
                "(if the socket proxy blocks the archive GET, this tool is unavailable).")
    except tarfile.TarError as exc:
        return f"Cannot read {path!r} from {container!r}: the archive was unreadable ({exc})."
    if kind == "dir":
        return f"{path!r} in {container!r} is a directory (or empty) — pass a single file path."
    if kind == "link":
        return (f"Refused: {path!r} in {container!r} is a symlink (to {text!r}); pass the "
                "file it points to, if that is inside the container roots.")
    if kind == "big":
        return f"{path!r} in {container!r} is {text} bytes, over the 1 MB this tool compares."

    roots = _readable_roots()
    real = _resolve_within_roots(repo_path, roots)
    if real is None:
        return f"Refused: repo_path {repo_path!r} is outside the allowed roots {roots}."
    if not os.path.isfile(real):
        return f"Repo file not found: {repo_path!r} — `list_dir` the repo to find the real path."
    if _is_secret(real):
        return f"Refused: {repo_path!r} is a secrets file."

    def _read_repo() -> str:
        with open(real, "rb") as f:
            return f.read(1_000_000).decode("utf-8", errors="replace")

    repo_text = await asyncio.to_thread(_read_repo)
    if repo_text == text:
        return (f"IDENTICAL — {path!r} in {container!r} matches {repo_path} byte-for-byte. "
                "The container is running the current source; the stale-code theory is ruled "
                "out — look elsewhere.")
    diff = list(difflib.unified_diff(
        repo_text.splitlines(), text.splitlines(),
        fromfile=f"repo:{repo_path}", tofile=f"container:{container}:{path}", lineterm="",
    ))
    if len(diff) > max_lines:
        diff = diff[:max_lines] + [f"… ({len(diff) - max_lines} more diff line(s) truncated)"]
    return (f"DIFFERS — {path!r} in {container!r} is NOT the same as {repo_path} "
            "(the container may be running stale code):\n" + "\n".join(diff))


@tool
async def timeline(since_minutes: int = 60) -> str:
    """Find the most recent container failure in the last `since_minutes` minutes and show its inspection and logs."""
    since_minutes = max(1, min(int(since_minutes), 60 * 24 * 7))
    logger.info("tool.docker.timeline", since_minutes=since_minutes)
    now = datetime.now(timezone.utc)
    since = now - timedelta(minutes=since_minutes)
    try:
        events = await asyncio.wait_for(asyncio.to_thread(_events_raw, since, now), timeout=15.0)
    except asyncio.TimeoutError:
        return "Timed out reading docker events."
    except docker.errors.DockerException as exc:
        return f"Cannot reach the Docker daemon: {exc}"

    if not events:
        return f"No container events in the last {since_minutes} min (nothing started/stopped/failed)."

    failures = [e for e in events if _is_failure(e)]
    if not failures:
        recent = []
        for e in events[-15:]:
            attrs = e.get("Actor", {}).get("Attributes", {})
            recent.append(f"  {_ev_time(e).isoformat()}  {e.get('Action')}  {attrs.get('name', '?')}")
        return (
            f"{len(events)} container event(s) in the last {since_minutes} min, but no failures "
            "(no non-zero exits, OOMs, or unhealthy states). Recent events:\n" + "\n".join(recent)
        )

    ev = failures[-1]
    attrs = ev.get("Actor", {}).get("Attributes", {})
    name = attrs.get("name", "?")
    ts = _ev_time(ev)
    action = ev.get("Action")
    exit_note = f" exit={_fmt_exit(int(attrs['exitCode']))}" if "exitCode" in attrs else ""

    out = [
        f"⏱  Most recent failure: container {name!r} — {action}{exit_note} at {ts.isoformat()}",
        f"   ({len(failures)} failure event(s) total in the last {since_minutes} min)",
    ]

    try:
        a = await asyncio.to_thread(_inspect_raw, name)
        out.append("\n" + _format_inspect(name, a))
    except docker.errors.DockerException:
        out.append(f"\n(cannot inspect {name!r} — likely removed after exit)")

    try:
        log_text = await asyncio.to_thread(_logs_raw, name, ts - timedelta(minutes=3), 80)
        lines = [ln for ln in log_text.splitlines() if ln.strip()]
        if lines:
            out.append(f"\nLogs for {name!r} around the failure (last {len(lines)} line(s)):")
            out += [f"  {ln}" for ln in lines[-30:]]
    except docker.errors.DockerException:
        out.append(f"\n(no logs available for {name!r})")

    return "\n".join(out)
