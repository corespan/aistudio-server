"""Filesystem and git diagnostic tools."""

from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import stat

import structlog
from langchain_core.tools import tool

from app.config import settings
from app.tools._shell import option_like, run_command

logger = structlog.get_logger()


def _readable_roots() -> list[str]:
    return [r.strip() for r in settings.readable_roots.split(",") if r.strip()]


# Secret file names (case-insensitive).
_SECRET_NAMES = (".env", ".env.*", "*.env", ".envrc", "*.pem", "*.key", "*.p12", "*.pfx",
                 "*.kdbx", "*.tfvars", "*.ppk", "id_rsa*", "id_dsa*", "id_ecdsa*",
                 "id_ed25519*", "id_xmss*",
                 "*-keyfile.json", "gcr-key.json", "service-account*.json", "credentials*",
                 ".git-credentials", ".netrc", ".pgpass", ".npmrc", ".pypirc",
                 "kubeconfig", "kubeconfig.*", "*.kubeconfig",
                 # Login, sudo and auditd logs from the /var/log mount.
                 "auth.log*", "secure*", "btmp*", "faillog", "audit.log*")

# Parent dir + name: these names are only secret in that dir (config.json is everywhere).
_SECRET_PATHS = (".docker/config.json", ".kube/config")


def _is_secret(path: str) -> bool:
    name = os.path.basename(path).lower()
    parent = os.path.basename(os.path.dirname(path)).lower()
    return name != ".env.example" and (any(fnmatch.fnmatch(name, p) for p in _SECRET_NAMES)
                                       or f"{parent}/{name}" in _SECRET_PATHS)


_GIT_SECRET_EXCLUDES = tuple(f":(exclude,glob,icase)**/{p}"
                             for p in _SECRET_NAMES + _SECRET_PATHS)

_REF_RE = re.compile(r"^[0-9A-Za-z._/~^@{}-]{1,100}$")


def _resolve_within_roots(path: str, roots: list[str]) -> str | None:
    """Return the real path if it is inside an allowed root, else None."""
    try:
        real = os.path.realpath(path)
    except Exception:
        return None
    for root in roots:
        try:
            root_real = os.path.realpath(root)
            if os.path.commonpath([real, root_real]) == root_real:
                return real
        except ValueError:
            continue
    return None


def _open_regular(path: str):
    """Open `path` for binary reading, refusing anything but a regular file. O_NONBLOCK: an
    open() of a FIFO swapped in after an isfile() check would otherwise block forever."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError(f"{path}: not a regular file")
    return os.fdopen(fd, "rb")


@tool
async def read_file(path: str, max_lines: int = 200, from_end: bool = True) -> str:
    """Read text file `path`, returning up to `max_lines` lines from the end (or the start if `from_end` is false)."""
    max_lines = max(1, min(max_lines, 5000))
    logger.info("tool.fs.read_file", path=path, max_lines=max_lines, from_end=from_end)
    roots = _readable_roots()
    real = _resolve_within_roots(path, roots)
    if real is None:
        return f"Refused: {path!r} is outside the allowed roots {roots}."
    if not os.path.isfile(real):
        return f"Not a file: {path!r}."
    if _is_secret(real):
        return f"Refused: {path!r} is a secrets file."

    def _blocking() -> str:
        cap = 1_000_000
        size = os.path.getsize(real)
        with _open_regular(real) as f:
            if from_end and size > cap:
                f.seek(size - cap)
                data = f.read()
                data = data.split(b"\n", 1)[-1] if b"\n" in data else data
            else:
                data = f.read(cap)
        lines = data.decode("utf-8", errors="replace").splitlines()
        chosen = lines[-max_lines:] if from_end else lines[:max_lines]
        header = f"{path} ({size} bytes, showing {len(chosen)} of {len(lines)} line(s)):"
        return header + "\n" + "\n".join(chosen)

    try:
        return await asyncio.to_thread(_blocking)
    except Exception as exc:
        return f"Error reading {path!r}: {exc}"


def _iter_files(root: str, cap: int) -> list[str]:
    """Return up to `cap` file paths under `root`."""
    out: list[str] = []
    for dirpath, _dirs, names in os.walk(root):
        for n in names:
            out.append(os.path.join(dirpath, n))
            if len(out) >= cap:
                return out
    return out


@tool
async def grep_logs(pattern: str, path: str = "", max_matches: int = 50,
                    ignore_case: bool = True) -> str:
    """Search for the text `pattern` (plain text, not a regex) across files under `path` (default first root), returning up to `max_matches` lines; `ignore_case` toggles case."""
    max_matches = max(1, min(max_matches, 500))
    if len(pattern) > 200:
        return "Refused: pattern is over 200 characters; search for a shorter string."
    logger.info("tool.fs.grep_logs", pattern=pattern, path=path)
    # Escaped, never a regex: a model-chosen (a+)+$ would stall the event loop.
    rx = re.compile(re.escape(pattern), re.IGNORECASE if ignore_case else 0)
    roots = _readable_roots()
    target = path or (roots[0] if roots else "")
    real = _resolve_within_roots(target, roots)
    if real is None:
        return f"Refused: {target!r} is outside the allowed roots {roots}."
    if not os.path.exists(real):
        return f"No such path: {target!r}."

    def _blocking() -> str:
        files = [real] if os.path.isfile(real) else _iter_files(real, cap=2000)
        # Regular files only: a FIFO, socket or device under a root would block the read.
        files = [r for r in (_resolve_within_roots(f, roots) for f in files)
                 if r and os.path.isfile(r) and not _is_secret(r)]
        hits: list[str] = []
        scanned = 0
        for fp in files:
            if len(hits) >= max_matches:
                break
            scanned += 1
            try:
                with _open_regular(fp) as f:
                    data = f.read(1_000_000)
            except OSError:
                continue
            for i, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{fp}:{i}: {line.strip()[:300]}")
                    if len(hits) >= max_matches:
                        break
        if not hits:
            return f"No matches for {pattern!r} under {target} ({scanned} file(s) scanned)."
        capped = " (capped — narrow the pattern/path for more)" if len(hits) >= max_matches else ""
        body = (f"{len(hits)} match(es){capped} for {pattern!r} under {target} "
                f"({scanned} file(s) scanned):\n" + "\n".join(hits))
        repo = os.path.realpath(settings.repo_path)
        in_repo = real == repo or real.startswith(repo + os.sep)
        return body + (_SOURCE_IS_NOT_RUNTIME if in_repo else "")

    try:
        return await asyncio.to_thread(_blocking)
    except Exception as exc:
        return f"Error searching {target!r}: {exc}"


@tool
async def list_dir(path: str) -> str:
    """List directory `path` entries with type and size."""
    logger.info("tool.fs.list_dir", path=path)
    roots = _readable_roots()
    real = _resolve_within_roots(path, roots)
    if real is None:
        return f"Refused: {path!r} is outside the allowed roots {roots}."
    if not os.path.isdir(real):
        return f"Not a directory: {path!r}."

    def _blocking() -> str:
        entries = []
        for name in sorted(os.listdir(real))[:200]:
            full = os.path.join(real, name)
            if os.path.isdir(full):
                entries.append(f"  {name}/")
            else:
                try:
                    entries.append(f"  {name}  ({os.path.getsize(full)} b)")
                except OSError:
                    entries.append(f"  {name}")
        return f"{path}:\n" + "\n".join(entries) if entries else f"{path} is empty."

    try:
        return await asyncio.to_thread(_blocking)
    except Exception as exc:
        return f"Error listing {path!r}: {exc}"


def _configured_repo(repo: str) -> str | None:
    """Real path of the configured repo if `repo` is it (or empty), else None. git obeys the
    repo's own config (pager, gpg.program, filters, fsmonitor), so a .git planted anywhere
    else under the readable roots (/var/log) must never be opened."""
    want = os.path.realpath(settings.repo_path)
    return want if os.path.realpath(repo or want) == want else None


def _git(repo: str) -> tuple[str, ...]:
    # Command-line -c beats the repo's config: no setting there can name a program to run.
    return ("git", "--no-pager", "-c", f"safe.directory={repo}", "-c", "core.fsmonitor=",
            "-c", "core.pager=cat", "-c", "log.showSignature=false", "-c", "gpg.program=false",
            "-c", "diff.external=", "-C", repo)


@tool
async def git_log(since_minutes: int = 120, max_commits: int = 20, repo: str = "") -> str:
    """List git commits from the last `since_minutes` minutes (at most `max_commits`) in `repo` (default configured repo)."""
    repo = repo or settings.repo_path
    since_minutes = max(1, min(since_minutes, 60 * 24 * 90))
    max_commits = max(1, min(max_commits, 200))
    logger.info("tool.fs.git_log", repo=repo, since_minutes=since_minutes)
    if _resolve_within_roots(repo, _readable_roots()) is None:
        return f"Refused: repo {repo!r} is outside the allowed roots."
    real = _configured_repo(repo)
    if real is None:
        return f"Refused: {repo!r} is not the configured repo ({settings.repo_path})."

    rc, out, err = await run_command(
        *_git(real), "log",
        f"--since={since_minutes} minutes ago",
        f"-n{max_commits}",
        "--pretty=format:%h  %ci  %an  %s",
    )
    if rc == 127:
        return "git is not installed in the agent image."
    if rc != 0:
        return f"git log failed: {(err or out).strip()}"

    rc2, dirty, _ = await run_command(*_git(real), "status", "--porcelain")
    # Ignore deleted (" D") entries.
    dirty_n = (len([ln for ln in dirty.splitlines() if ln.strip() and not ln.startswith(" D")])
               if rc2 == 0 else -1)

    parts = [f"Commits in the last {since_minutes} min ({repo}):",
             out.strip() or "  (none — no recent commits)"]
    if dirty_n > 0:
        parts.append(
            f"\n⚠️  {dirty_n} uncommitted change(s) in the working tree — "
            "someone may have edited files on the host directly."
        )
    body = "\n".join(parts)
    return body + (_SOURCE_IS_NOT_RUNTIME if out.strip() else "")


_SOURCE_IS_NOT_RUNTIME = (
    "\n\nNote: the above is source, not runtime. It carries no "
    "information about whether the containers currently running include it — a "
    "merged fix and a deployed fix are different things. When a symptom persists "
    "after a fix landed in source, an unchanged running artifact and an incomplete "
    "fix are both live possibilities; container_diff compares a container's copy of "
    "a file against the repo's and distinguishes them."
)


@tool
async def git_show(ref: str = "HEAD", repo: str = "", max_lines: int = 200) -> str:
    """Show the file list and diff of commit `ref` (default HEAD) in `repo`, truncated to `max_lines`."""
    repo = repo or settings.repo_path
    logger.info("tool.fs.git_show", repo=repo, ref=ref)
    if _resolve_within_roots(repo, _readable_roots()) is None:
        return f"Refused: repo {repo!r} is outside the allowed roots."
    real = _configured_repo(repo)
    if real is None:
        return f"Refused: {repo!r} is not the configured repo ({settings.repo_path})."

    if option_like(ref):
        return (f"Refused: {ref!r} is a git option, not a revision. Pass a commit hash, "
                "tag or ref (git_log lists the recent ones).")
    if not _REF_RE.fullmatch(ref):
        return (f"Refused: {ref!r} is not a plain revision (no ':' paths). Pass a commit "
                "hash, tag or ref (git_log lists the recent ones).")
    max_lines = max(1, min(max_lines, 2000))
    rc, out, err = await run_command(
        *_git(real), "show", ref, "--stat", "--patch", "--no-color", "--no-textconv", "--no-ext-diff",
        "--", ".", *_GIT_SECRET_EXCLUDES,
    )
    if rc == 127:
        return "git is not installed in the agent image."
    if rc != 0:
        return f"git show failed: {(err or out).strip()}"

    lines = out.splitlines()
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"… ({len(lines) - max_lines} more line(s) truncated)"]
    if not lines:
        return f"(commit {ref!r} produced no output)"
    return "\n".join(lines) + _SOURCE_IS_NOT_RUNTIME
