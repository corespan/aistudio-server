"""Workspaces: a repo checkout that lives across calls as snapshots (docs/sandbox.md#workspaces).

Still one container per call. A call starts a fresh gVisor container from the latest snapshot,
runs one step, and (for steps that change the tree) writes the new /work as a tar.gz to /out,
an anonymous volume: gVisor keeps writes to the container's own filesystem in its memory, so
Docker can copy out only what lands on a mount. sandbox-mcp keeps the snapshots in
workspace_dir/<id>/<n>.tgz.

The runner calls these as workflow steps. They are never offered to a model as tools: the model
only ever runs inside workspace_claude, which is the one step that gets an LLM.
"""
from __future__ import annotations

import base64
import io
import json
import re
import secrets
import shlex
import shutil
import tarfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from app.config import settings

UID = "1000:1000"           # the image's node user, which owns /work
SNAP = "/out/snap.tgz"      # where a step leaves its snapshot (an anonymous volume)
GATEWAY = "http://agentgateway-sandbox:8051"
REPO = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")
REF = re.compile(r"^(?!-)[A-Za-z0-9._/-]{1,200}$")           # branch, tag, sha, pull/<n>/head
TOOL = re.compile(r"^[A-Za-z_]+(\([^()\n]{1,200}\))?$")      # Read, Bash(git diff *)
MODEL = re.compile(r"^[a-z0-9.-]{1,64}$")
PANEL_TOOL = re.compile(r"^[a-z][a-z0-9_]{0,63}$")                # a panel tool: filter_benchmarks
PANEL_SERVER = (Path(__file__).parent / "panel_server.py").read_text(encoding="utf-8")
CONNECTOR = re.compile(r"^[a-z0-9-]{1,40}$")         # an MCP server at GATEWAY/mcp/<name>
ID = re.compile(r"^[0-9a-f]{16}$")
# Every variable a workspace container can get; nothing outside this set, ever (tests check it).
# The JWT goes only to the clone and the Claude step, not to checks running the repo's own code.
ENV = frozenset({"GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0", "ANTHROPIC_BASE_URL",
                 "ANTHROPIC_AUTH_TOKEN", "WS_URL", "WS_REF", "WS_BASE", "WS_MODEL", "WS_TURNS"})


class Refused(ValueError):
    """The request breaks a rule; the message says which."""


@dataclass
class Step:
    exit_code: int | None
    timed_out: bool
    seconds: float
    stdout: str
    stderr: str
    snapshot: bytes = b""


def env(token: str | None = None, **ws: str) -> dict[str, str]:
    """A workspace container's whole environment (the image sets PATH, HOME and the rest)."""
    out = {f"WS_{k.upper()}": v for k, v in ws.items()}
    if token:
        out |= {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraHeader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Bearer {token}",
                "ANTHROPIC_BASE_URL": f"{GATEWAY}/llm/anthropic", "ANTHROPIC_AUTH_TOKEN": token}
    if extra := set(out) - ENV:
        raise Refused(f"not in the workspace env allowlist: {sorted(extra)}")
    return out


# --- snapshots ---------------------------------------------------------------------------------

def _dir(ws_id: str) -> Path:
    if not ID.match(ws_id or ""):
        raise Refused("unknown workspace")
    return Path(settings.workspace_dir) / ws_id


def _meta(ws_id: str, owner: str) -> dict:
    try:
        meta = json.loads((_dir(ws_id) / "meta.json").read_text(encoding="utf-8"))
    except OSError:
        raise Refused("unknown workspace (closed, or expired after "
                      f"{settings.workspace_ttl_hours}h)") from None
    if meta["owner"] != owner:
        raise Refused("unknown workspace")      # someone else's: say nothing more
    return meta


def _save(ws_id: str, meta: dict, data: bytes) -> int:
    """Store the next snapshot after checking it can only ever restore into /work."""
    if len(data) > settings.workspace_snapshot_bytes:
        raise Refused(f"the workspace is over {settings.workspace_snapshot_bytes} bytes")
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for m in tar:
                parts = PurePosixPath(m.name).parts
                if not parts or parts[0] != "work" or ".." in parts or m.isdev():
                    raise Refused(f"snapshot entry {m.name!r} is outside /work")
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise Refused(f"the step returned no usable snapshot ({exc})") from None
    meta["head"] = meta.get("head", -1) + 1
    folder = _dir(ws_id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{meta['head']}.tgz").write_bytes(data)
    (folder / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return meta["head"]


def sweep(now: float | None = None) -> list[str]:
    """Delete workspaces older than the TTL: runs that ended without workspace_close."""
    cutoff, gone = (now or time.time()) - settings.workspace_ttl_hours * 3600, []
    for folder in Path(settings.workspace_dir).glob("*"):
        try:
            created = json.loads((folder / "meta.json").read_text(encoding="utf-8"))["created"]
        except (OSError, ValueError, KeyError):
            created = folder.stat().st_mtime
        if created < cutoff:
            shutil.rmtree(folder, ignore_errors=True)
            gone.append(folder.name)
    return gone


# --- one container -----------------------------------------------------------------------------

def _files(entries: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, text in entries.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.uid, info.gid = len(data), 0o644, 1000, 1000
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _run(client, script: str, environment: dict[str, str], seconds: int, *, restore: bytes = b"",
         files: dict[str, str] | None = None, save: bool = False,
         on_line: Callable[[str], bool | None] | None = None) -> Step:
    """Run ``script`` in /work. Blocking (the Docker SDK is); call it from a thread.

    ``on_line`` sees each stdout line as it comes; returning True kills the container (a cap hit).
    """
    seconds = max(1, min(int(seconds), settings.workspace_max_seconds))
    # The snapshot goes to /out, an anonymous volume: a mount is outside gVisor's in-memory
    # overlay, so Docker can copy the file out. Not stdout: anything a step leaves running could
    # write into it there.
    snap = f"rm -f {SNAP} && tar -C / -czf {SNAP} work" if save else "true"
    wrapper = f"cd /work && timeout -k 5 {seconds} bash -c {shlex.quote(script)}; rc=$?; {snap}; exit $rc"
    container = client.containers.create(
        settings.workspace_image, ["bash", "-c", wrapper],
        runtime=settings.runtime, network=settings.workspace_network,
        extra_hosts={"agentgateway-sandbox": settings.workspace_gateway_ip}, user=UID,
        mem_limit=settings.workspace_memory, memswap_limit=settings.workspace_memory,
        nano_cpus=int(settings.workspace_cpus * 1e9), pids_limit=settings.workspace_pids,
        cap_drop=["ALL"], security_opt=["no-new-privileges"], environment=environment,
        volumes=["/out"] if save else None, labels={"aistudio.workspace": "1"}, detach=True)
    # Backstop if `timeout` inside never fires (the container itself wedged).
    killer = threading.Timer(seconds + 60, lambda: _quiet(container.kill))
    try:
        if restore:
            container.put_archive("/", restore)          # Docker takes the gzip as is
        if files:
            container.put_archive("/tmp", _files(files))
        started = time.monotonic()
        container.start()
        killer.start()
        pending, killed = b"", False
        # The live stream is for progress only: with a slow reader, Docker can end a follow
        # stream before the last bytes, so the output itself is read once the container is done.
        for chunk in container.logs(stream=True, follow=True, stdout=True, stderr=False):
            if killed or not on_line:
                continue
            *lines, pending = (pending + chunk).split(b"\n")
            for line in lines:
                if on_line(line.decode(errors="replace")):
                    killed = True
                    _quiet(container.kill)      # no snapshot: the last one stays the latest
                    break
        if pending and on_line and not killed:  # a last line with no newline after it
            killed = bool(on_line(pending.decode(errors="replace")))
        exit_code = container.wait(timeout=60)["StatusCode"]
        seconds_taken = time.monotonic() - started
        stdout = container.logs(stdout=True, stderr=False).decode(errors="replace").removesuffix("\n")
        stderr = container.logs(stdout=False, stderr=True).decode(errors="replace")
        snapshot = b"" if killed or not save else _snapshot(container)
        return Step(exit_code, exit_code == 124, seconds_taken, _tail(stdout), _tail(stderr), snapshot)
    finally:
        killer.cancel()
        _quiet(lambda: container.remove(force=True, v=True))     # v: the /out volume too


def _snapshot(container) -> bytes:
    """/out/snap.tgz as a file, or nothing (a step killed before it wrote one). It is checked
    in _save: only entries under work/, and a size cap."""
    try:
        bits, stat = container.get_archive(SNAP)
    except Exception:  # noqa: BLE001 - docker.errors.NotFound and friends: no snapshot
        return b""
    if stat.get("size", 0) > settings.workspace_snapshot_bytes:
        raise Refused(f"the workspace is over {settings.workspace_snapshot_bytes} bytes")
    with tarfile.open(fileobj=io.BytesIO(b"".join(bits)), mode="r:") as tar:
        member = tar.next()
        # A model could have replaced the file with a link: only a regular file counts.
        return tar.extractfile(member).read() if member is not None and member.isfile() else b""


def _quiet(fn) -> None:
    try:
        fn()
    except Exception:  # noqa: BLE001 - cleanup of a container that may already be gone
        pass


def _tail(text: str) -> str:
    """The end matters most (test summaries, the final result line)."""
    n = settings.output_chars
    return text if len(text) <= n else f"[... first {len(text) - n} characters cut]\n" + text[-n:]


def _latest(ws_id: str, meta: dict) -> bytes:
    return (_dir(ws_id) / f"{meta['head']}.tgz").read_bytes()


# --- the five steps ----------------------------------------------------------------------------

def _empty() -> bytes:
    """Snapshot 0 of a workspace with no repo: an archive with nothing in it."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz"):
        pass
    return buf.getvalue()


def open_(client, owner: str, token: str, repo: str, ref: str, base: str | None = None,
          depth: int | None = None) -> dict:
    """Clone ``repo`` at ``ref`` through the gateway. The diff base is ``ref`` itself, or its
    merge base with ``base`` (a PR head against its target branch).

    ``repo`` empty: an empty /work and no container at all (chat agents that need no code).
    ``depth``: a shallow clone of that many commits; not with ``base``, which needs history."""
    if not repo:
        sweep()
        ws_id = secrets.token_hex(8)
        meta = {"owner": owner, "repo": "", "ref": "", "base": "", "created": time.time()}
        return {"id": ws_id, "repo": "", "ref": "", "base": "", "snapshot": _save(ws_id, meta, _empty())}
    if not REPO.match(repo):
        raise Refused("repo must be owner/name")
    if depth is not None and (base or not 1 <= depth <= 1000):
        raise Refused("depth: 1 to 1000 commits, and only without base")
    for r in (ref, base) if base else (ref,):
        if not REF.match(r) or ".." in r:
            raise Refused(f"ref {r!r}: a branch, tag, commit or pull/<n>/head")
    sweep()
    shallow = f" --depth {int(depth)}" if depth else ""
    script = (f'git clone -q{shallow} "$WS_URL" /work && git fetch -q{shallow} origin "$WS_REF" '
              '&& git checkout -q -B work FETCH_HEAD '
              '&& git config user.name agent && git config user.email agent@aistudio.invalid '
              '&& if [ -n "$WS_BASE" ]; then git fetch -q origin "$WS_BASE" && git merge-base FETCH_HEAD HEAD; '
              'else git rev-parse HEAD; fi')
    step = _run(client, script, env(token, url=f"{GATEWAY}/git/{repo}.git", ref=ref, base=base or ""),
                settings.workspace_max_seconds, save=True)
    sha = step.stdout.strip().splitlines()[-1:] or [""]
    if step.exit_code != 0 or not re.fullmatch(r"[0-9a-f]{40}", sha[0]):
        raise Refused(f"clone failed (exit {step.exit_code}): {step.stderr[-500:] or step.stdout[-500:]}")
    ws_id = secrets.token_hex(8)
    meta = {"owner": owner, "repo": repo, "ref": ref, "base": sha[0], "created": time.time()}
    head = _save(ws_id, meta, step.snapshot)
    return {"id": ws_id, "repo": repo, "ref": ref, "base": sha[0], "snapshot": head}


def weighted(usage: dict) -> float:
    """The limit unit (agent-runner's usage record): cache reads count a tenth, as they cost."""
    return (usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)
            + usage.get("output_tokens", 0) + 0.1 * usage.get("cache_read_input_tokens", 0))


def claude(client, owner: str, token: str, ws_id: str, prompt: str, model: str, tools: list[str],
           max_turns: int, seconds: int, on_line: Callable[[str], None] | None = None,
           max_weighted: int | None = None, mcp: list[str] | None = None, keep: bool = True,
           panel: list[dict] | None = None) -> dict:
    """One headless Claude Code run on the latest snapshot; the tree it leaves is the next one.

    ``mcp``: MCP servers Claude Code connects to, each at agentgateway-sandbox's /mcp/<name> with
    the caller's token; that route allows only the tools the agents name. ``tools`` says which
    of them this step may call (mcp__<name>__<tool>). ``keep`` False: the tree it leaves is not
    saved (a read-only agent), so the next step starts from the same snapshot, and no time goes
    on packing it.

    ``max_weighted``: the runner's cap for this step. Past it the container is killed mid-run,
    nothing is saved, and the result says budget_exceeded.

    ``panel``: panel tools ({name, description, inputSchema}) offered as the local MCP server
    `panel` (panel_server.py), which only acknowledges calls; the runner sends them to the browser.
    """
    meta = _meta(ws_id, owner)
    if not prompt.strip() or len(prompt) > 100_000:
        raise Refused("prompt must be 1 to 100000 characters")
    if not MODEL.match(model):
        raise Refused("model must be a model id such as claude-opus-5-5")
    if not tools or len(tools) > 30 or not all(TOOL.match(t) for t in tools):
        raise Refused("tools: 1 to 30 Claude Code tool rules, such as Read or Bash(git diff *)")
    if not 1 <= max_turns <= 200:
        raise Refused("max_turns must be 1 to 200")
    mcp = mcp or []
    if len(mcp) > 5 or not all(CONNECTOR.match(c) for c in mcp):
        raise Refused("mcp: up to 5 connector names, such as aistudio")
    # Only these servers: --strict-mcp-config ignores any .mcp.json in the repo.
    servers = {c: {"type": "http", "url": f"{GATEWAY}/mcp/{c}", "headers": {"Authorization": f"Bearer {token}"}}
               for c in mcp}
    files = {"prompt.txt": prompt, "tools.txt": "\n".join(tools)}
    if panel:
        if "panel" in mcp:
            raise Refused("mcp: panel is the local panel server, not a gateway connector")
        if len(panel) > 30 or not all(isinstance(t, dict) and PANEL_TOOL.match(str(t.get("name")))
                                      and isinstance(t.get("description"), str)
                                      and isinstance(t.get("inputSchema"), dict) for t in panel):
            raise Refused("panel: up to 30 tools, each {name, description, inputSchema}")
        panel_json = json.dumps([{k: t[k] for k in ("name", "description", "inputSchema")} for t in panel])
        if len(panel_json) > 50_000:
            raise Refused("panel: the tool list is over 50000 characters")
        servers["panel"] = {"type": "stdio", "command": "/opt/runner-venv/bin/python",
                            "args": ["/tmp/panel.py", "/tmp/panel.json"]}
        files |= {"panel.py": PANEL_SERVER, "panel.json": panel_json}
    files["mcp.json"] = json.dumps({"mcpServers": servers})
    script = ('mapfile -t T < /tmp/tools.txt; claude -p --output-format stream-json --verbose '
              '--model "$WS_MODEL" --max-turns "$WS_TURNS" --mcp-config /tmp/mcp.json --strict-mcp-config '
              '--allowedTools "${T[@]}" < /tmp/prompt.txt')
    turns: dict = {}      # message id -> its usage; streamed parts of one message repeat the id
    used = [0.0]

    def watch(line: str) -> bool:
        if on_line:
            on_line(line)
        try:
            event = json.loads(line)
        except ValueError:
            return False
        if isinstance(event, dict) and event.get("type") == "assistant":
            msg = event.get("message") or {}
            turns[msg.get("id")] = msg.get("usage") or {}
            used[0] = sum(weighted(u) for u in turns.values())
            return max_weighted is not None and used[0] > max_weighted
        return False

    step = _run(client, script, env(token, model=model, turns=str(max_turns)), seconds,
                restore=_latest(ws_id, meta), files=files,
                save=keep, on_line=watch)
    over = max_weighted is not None and used[0] > max_weighted
    result = None
    for line in reversed(step.stdout.splitlines()):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            result = event
            break
    head = _save(ws_id, meta, step.snapshot) if step.snapshot and not over else meta["head"]
    return {"snapshot": head, "exit_code": step.exit_code, "timed_out": step.timed_out,
            "seconds": round(step.seconds, 1), "result": result,
            "budget_exceeded": over, "weighted": round(used[0]),
            "stderr": step.stderr[-2000:] if step.exit_code else ""}


def exec_(client, owner: str, ws_id: str, command: str, seconds: int) -> dict:
    """The workflow's own fixed command (a check). No token, and the tree isn't kept."""
    meta = _meta(ws_id, owner)
    if not command.strip() or len(command) > 2000:
        raise Refused("command must be 1 to 2000 characters")
    step = _run(client, command, env(), seconds, restore=_latest(ws_id, meta))
    return {"exit_code": step.exit_code, "timed_out": step.timed_out, "seconds": round(step.seconds, 1),
            "stdout": step.stdout, "stderr": step.stderr}


def diff(client, owner: str, ws_id: str) -> dict:
    """Everything that changed since the base: commits and the working tree, new files included."""
    meta = _meta(ws_id, owner)
    sep = f"<<<diff {secrets.token_hex(8)}>>>"
    # The file list last: a long diff is cut from the front, the list never.
    script = (f'git add -A && git diff --cached "$WS_BASE" && echo "{sep}" '
              f'&& git diff --cached --name-status "$WS_BASE"')
    step = _run(client, script, env(base=meta["base"]), 120, restore=_latest(ws_id, meta))
    patch, _, files = step.stdout.rpartition(sep)
    if step.exit_code != 0:
        raise Refused(f"diff failed (exit {step.exit_code}): {step.stderr[-500:]}")
    return {"base": meta["base"], "snapshot": meta["head"], "files": [f for f in files.splitlines() if f.strip()],
            "diff": patch.strip("\n"), "truncated": step.stdout.startswith("[... first")}


def files(owner: str, ws_id: str, paths: list[str], snapshot: int | None = None) -> dict:
    """File contents from a snapshot, for the runner to commit through GitHub: no container.

    ``paths``: files, or a prefix ending in / for everything under it. Each comes back as
    {"mode", "content" (base64)}, or None when it isn't there (deleted). Never .git.
    """
    meta = _meta(ws_id, owner)
    n = meta["head"] if snapshot is None else snapshot
    if (not 0 <= n <= meta["head"] or len(paths) > 500
            or any(".." in p.split("/") or p.startswith("/") for p in paths)):
        raise Refused("snapshot or paths out of range")
    wanted = {p: None for p in paths if not p.endswith("/")}
    prefixes = [p for p in paths if p.endswith("/")]
    out: dict = dict(wanted)
    total = 0
    with tarfile.open(_dir(ws_id) / f"{n}.tgz", mode="r:gz") as tar:
        for m in tar:
            rel = m.name.removeprefix("work/")
            if rel.startswith(".git/") or not (rel in wanted or any(rel.startswith(p) for p in prefixes)):
                continue
            if m.issym():
                mode, data = "120000", m.linkname.encode()
            elif m.isfile():
                mode, data = ("100755" if m.mode & 0o111 else "100644"), tar.extractfile(m).read()
            else:
                continue
            total += len(data)
            if total > settings.workspace_files_bytes:
                raise Refused(f"the files are over {settings.workspace_files_bytes} bytes")
            out[rel] = {"mode": mode, "content": base64.b64encode(data).decode()}
    return {"snapshot": n, "files": out}


def close(owner: str, ws_id: str) -> dict:
    _meta(ws_id, owner)
    shutil.rmtree(_dir(ws_id), ignore_errors=True)
    return {"closed": ws_id}
