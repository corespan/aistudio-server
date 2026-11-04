"""GitHub, after your approval: the runner builds commits and opens draft PRs, or posts a review.

Through agentgateway's /github route with the caller's JWT. The gateway checks the sub and the
exact REST call and adds the PAT, so the runner holds no GitHub credential. A branch is created
new, or (pr-followup) moved forward, never forced, and only under agent/. Nothing here writes
before the gate.
"""
from __future__ import annotations

import httpx

from app.config import settings


class GitHubError(RuntimeError):
    pass


async def _call(token: str, method: str, path: str, body: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=60) as http:
        r = await http.request(method, f"{settings.gateway_url}/github/{path}", json=body,
                               headers={"Authorization": f"Bearer {token}"})
    if r.status_code >= 300:
        raise GitHubError(f"{method} {path}: {r.status_code} {r.text[:300]}")
    return r.json()


async def pull(token: str, repo: str, number: int) -> dict:
    return await _call(token, "GET", f"repos/{repo}/pulls/{number}")


async def _commits(token: str, repo: str, parent: str, commits: list[tuple[str, dict]]) -> tuple[str, int]:
    """Commits on top of ``parent``, each ``(message, {path: {"mode", "content"} | None})``
    (None deletes). Empty ones are skipped. Returns the last commit and how many were made."""
    tree = (await _call(token, "GET", f"repos/{repo}/git/commits/{parent}"))["tree"]["sha"]
    made = 0
    for message, files in commits:
        if not files:
            continue
        entries = []
        for path, f in sorted(files.items()):
            if f is None:
                entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
                continue
            blob = await _call(token, "POST", f"repos/{repo}/git/blobs",
                               {"content": f["content"], "encoding": "base64"})
            entries.append({"path": path, "mode": f["mode"], "type": "blob", "sha": blob["sha"]})
        tree = (await _call(token, "POST", f"repos/{repo}/git/trees", {"base_tree": tree, "tree": entries}))["sha"]
        parent = (await _call(token, "POST", f"repos/{repo}/git/commits",
                              {"message": message, "tree": tree, "parents": [parent]}))["sha"]
        made += 1
    if not made:
        raise GitHubError("nothing to commit: the workspace has no changes")
    return parent, made


async def open_pr(token: str, repo: str, base_sha: str, base_ref: str, branch: str,
                  commits: list[tuple[str, dict]], title: str, body: str) -> dict:
    """Commits on top of ``base_sha`` (see _commits) on a new branch, then a draft PR into
    ``base_ref``. A branch that already exists makes GitHub refuse: nothing is overwritten."""
    parent, made = await _commits(token, repo, base_sha, commits)
    await _call(token, "POST", f"repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": parent})
    pr = await _call(token, "POST", f"repos/{repo}/pulls",
                     {"title": title, "head": branch, "base": base_ref, "body": body, "draft": True})
    return {"url": pr["html_url"], "number": pr["number"], "branch": branch, "commits": made}


async def review(token: str, repo: str, number: int, commit_id: str, body: str,
                 comments: list[dict]) -> dict:
    """A COMMENT review (you decide approve or request changes). Inline comments need lines in
    the PR's diff; if GitHub refuses them, they go into the review's body instead."""
    args = {"commit_id": commit_id, "event": "COMMENT", "body": body}
    try:
        r = await _call(token, "POST", f"repos/{repo}/pulls/{number}/reviews", {**args, "comments": comments})
    except GitHubError as exc:
        if not comments or " 422 " not in str(exc):
            raise
        listed = "\n".join(f"- `{c['path']}:{c['line']}` {c['body']}" for c in comments)
        r = await _call(token, "POST", f"repos/{repo}/pulls/{number}/reviews",
                        {**args, "body": f"{body}\n\n{listed}"})
    return {"url": r.get("html_url"), "id": r.get("id")}


async def push(token: str, repo: str, branch: str, head_sha: str, commits: list[tuple[str, dict]]) -> dict:
    """pr-followup: commits on top of the PR's head, then the branch moved forward to them.
    Never forced: if someone pushed meanwhile, GitHub refuses and nothing moves."""
    if not branch.startswith("agent/"):
        raise GitHubError(f"only agent/ branches are pushed to, not {branch!r}")
    parent, made = await _commits(token, repo, head_sha, commits)
    await _call(token, "PATCH", f"repos/{repo}/git/refs/heads/{branch}", {"sha": parent, "force": False})
    return {"sha": parent, "commits": made, "branch": branch}


async def request_reviewers(token: str, repo: str, number: int, reviewers: list[str]) -> list[str]:
    if not reviewers:
        return []
    r = await _call(token, "POST", f"repos/{repo}/pulls/{number}/requested_reviewers", {"reviewers": reviewers})
    return [u["login"] for u in r.get("requested_reviewers") or []]


async def comment(token: str, repo: str, number: int, body: str) -> dict:
    r = await _call(token, "POST", f"repos/{repo}/issues/{number}/comments", {"body": body})
    return {"url": r.get("html_url"), "id": r.get("id")}


async def feedback(token: str, repo: str, pr: dict, trusted: list[str]) -> dict:
    """What a PR asks of its agent since its head commit: comments from ``trusted`` logins only
    (everyone else's are ignored: they're untrusted text), and the head's failed checks."""
    since = (await _call(token, "GET", f"repos/{repo}/git/commits/{pr['head']['sha']}"))["committer"]["date"]
    n = pr["number"]
    review = await _call(token, "GET", f"repos/{repo}/pulls/{n}/comments")
    talk = await _call(token, "GET", f"repos/{repo}/issues/{n}/comments")
    comments = [{"author": c["user"]["login"], "path": c.get("path"), "line": c.get("line"), "body": c["body"]}
                for c in [*review, *talk]
                if c["user"]["login"] in trusted and c["created_at"] > since
                and "agent-runner's" not in c["body"]]          # not our own reports
    ignored = sum(c["user"]["login"] not in trusted for c in [*review, *talk])
    runs = (await _call(token, "GET", f"repos/{repo}/commits/{pr['head']['sha']}/check-runs"))["check_runs"]
    failed = []
    for run in runs:
        if run.get("conclusion") not in ("failure", "timed_out"):
            continue
        notes = await _call(token, "GET", f"repos/{repo}/check-runs/{run['id']}/annotations")
        failed.append({"name": run["name"], "summary": ((run.get("output") or {}).get("summary") or "")[:2000],
                       "annotations": [f"{a['path']}:{a['start_line']} {a['message']}"[:500] for a in notes[:20]]})
    return {"since": since, "comments": comments, "ignored": ignored, "failed_checks": failed}


def changes(name_status: list[str]) -> tuple[list[str], list[str]]:
    """``git diff --name-status`` lines -> (paths to read, paths deleted). A rename is both."""
    read, gone = [], []
    for line in name_status:
        status, *paths = line.split("\t")
        if status.startswith("D"):
            gone.append(paths[0])
        elif status.startswith("R"):
            gone.append(paths[0])
            read.append(paths[1])
        else:
            read.append(paths[-1])
    return read, gone
