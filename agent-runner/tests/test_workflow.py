"""Workflows: the fixed steps, the review loop, the check, the USD cap, and stopping at the gate."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from app import github, graph as run_graph, registry, usage
from app.auth import Identity
from app.config import settings
from app.main import app
from app.runlog import RunLog
from app.runs import RunManager, TooManyRuns
from tests.test_claude import RESULT, STREAM

RSINGH = Identity(sub="rsingh", role="admin", token="tok")
APPROVE = json.dumps({"verdict": "approve", "summary": "fine", "findings": []})
CHANGES = json.dumps({"verdict": "changes_requested", "summary": "a bug",
                      "findings": [{"severity": "high", "file": "app/x.py", "line": 3, "issue": "off by one"}]})
HUNTED = json.dumps({"verdict": "changes_requested", "summary": "three bugs", "findings": [
    {"severity": "high", "file": "app/a.py", "line": 1, "issue": "status never set to FAILED"},
    {"severity": "low", "file": "app/b.py", "line": 2, "issue": "a log line is wrong"},
    {"severity": "medium", "file": "app/c.py", "line": 3, "issue": "a leaked session"}]})


@pytest.fixture(autouse=True)
def fresh_usage(monkeypatch):
    monkeypatch.setattr(usage, "store", usage.Usage())


@pytest.fixture
def box(fake_mcp):
    """A scripted sandbox: ``answers[mode]`` is a list of Claude answers per mode, used in order."""
    state = {"answers": {"plan": ["the plan"], "plan-review": [APPROVE], "code": ["changed it"],
                         "review": [APPROVE], "learn": ["noted the test database"],
                         "hunt": [HUNTED], "fix": ["fixed them"], "address": ["addressed it"]},
             "check": [0], "prompts": [], "tools": []}

    def mode_of(prompt):
        if "write a plan" in prompt:
            return "plan"
        if "Review the plan" in prompt:
            return "plan-review"
        if "Make the change, following" in prompt:
            return "code"
        if "Write down what you had to discover" in prompt:
            return "learn"
        if "Look for real bugs" in prompt:
            return "hunt"
        if "Fix them." in prompt:
            return "fix"
        if "Make the changes the comments ask for" in prompt:
            return "address"
        return "review"

    async def run_claude(args, progress):
        mode = mode_of(args["prompt"])
        state["prompts"].append((mode, args["prompt"]))
        state["tools"].append((mode, args["tools"]))
        for i, line in enumerate(STREAM):
            await progress(i + 1, None, line)
        answers = state["answers"][mode]
        text = answers.pop(0) if len(answers) > 1 else answers[0]
        return json.dumps({"exit_code": 0, "timed_out": False, "budget_exceeded": False, "weighted": 1,
                           "snapshot": 1, "result": {**RESULT, "result": text}})

    def run_exec(args):
        codes = state["check"]
        code = codes.pop(0) if len(codes) > 1 else codes[0]
        return json.dumps({"exit_code": code, "timed_out": False, "stdout": f"pytest exit {code}", "stderr": ""})
    fake_mcp["answers"].update({
        "workspace_open": lambda a: json.dumps({"id": "ws1", "base": "b" * 40, "snapshot": 0}),
        "workspace_claude": run_claude, "workspace_exec": run_exec,
        "workspace_diff": lambda a: json.dumps({"files": ["M\tapp/x.py", "D\told.py", "R100\ta.py\tb.py"],
                                                "diff": "+fix", "truncated": False, "snapshot": 3}),
        "workspace_files": lambda a: json.dumps({"files": state["files"](a)}),
        "workspace_close": lambda a: json.dumps({"closed": "ws1"})})
    # Snapshot 3 is what the gate showed; the learn step makes 4 with a CLAUDE.md line.
    snaps = {3: {"app/x.py": "fixed", "b.py": "moved", "CLAUDE.md": "old"},
             4: {"app/x.py": "edited after approval", "b.py": "moved", "CLAUDE.md": "old\nnew fact",
                 ".agents/knowledge/db.md": "notes"}}

    def files(a):
        snap = snaps[a.get("snapshot", 4)]
        out = {}
        for p in a["paths"]:
            if p.endswith("/"):
                out |= {k: {"mode": "100644", "content": v} for k, v in snap.items() if k.startswith(p)}
            else:
                out[p] = {"mode": "100644", "content": snap[p]} if p in snap else None
        return out
    state["files"] = files
    return fake_mcp, state


@pytest.fixture(autouse=True)
def gh(monkeypatch):
    """GitHub, recorded: every write the runner makes."""
    calls = []

    async def pull(token, repo, n):
        calls.append(("pull", n))
        head = {"sha": "h" * 40, "ref": "agent/fix-x" if n == 9 else "someone/branch", "repo": {"full_name": repo}}
        return {"title": "Fix x", "html_url": f"https://github.com/{repo}/pull/{n}", "state": "open",
                "number": n, "head": head, "base": {"sha": "c" * 40, "ref": "master"}}

    async def feedback(token, repo, pr, trusted):
        calls.append(("feedback", pr["number"], trusted))
        return {"since": "2026-11-04T00:00:00Z", "ignored": 1, "failed_checks": [],
                "comments": [] if pr["number"] == 10 else [
                    {"author": "rsingh-alt", "path": "app/x.py", "line": 3, "body": "rename this variable"}]}

    async def push(token, repo, branch, head_sha, commits):
        calls.append(("push", {"branch": branch, "head_sha": head_sha, "commits": commits}))
        return {"sha": "p" * 40, "commits": 1, "branch": branch}

    async def request_reviewers(token, repo, n, reviewers):
        calls.append(("request_reviewers", n, reviewers))
        if "nobody" in reviewers:
            raise github.GitHubError("POST requested_reviewers: 422 Reviews may only be requested from collaborators")
        return reviewers

    async def comment(token, repo, n, body):
        calls.append(("comment", n, body))
        return {"url": f"https://github.com/{repo}/pull/{n}#c", "id": 2}
    for name, fn in (("feedback", feedback), ("push", push), ("request_reviewers", request_reviewers),
                     ("comment", comment)):
        monkeypatch.setattr(github, name, fn)

    async def open_pr(token, repo, base_sha, base_ref, branch, commits, title, body):
        calls.append(("open_pr", {"base_sha": base_sha, "base_ref": base_ref, "branch": branch,
                                  "commits": commits, "title": title, "body": body}))
        return {"url": f"https://github.com/{repo}/pull/99", "number": 99, "branch": branch, "commits": 2}

    async def review(token, repo, n, commit_id, body, comments):
        calls.append(("review", {"n": n, "commit_id": commit_id, "body": body, "comments": comments}))
        return {"url": f"https://github.com/{repo}/pull/{n}#review", "id": 1}
    monkeypatch.setattr(github, "pull", pull)
    monkeypatch.setattr(github, "open_pr", open_pr)
    monkeypatch.setattr(github, "review", review)
    return calls


async def drain(runs, run, after=0) -> list[dict]:
    events = []
    async with asyncio.timeout(10):
        async for line in runs.stream(run, after):
            events.append(json.loads(line.split("data: ", 1)[1]))
    return events


async def run_workflow(name="code-change", inputs=None, runs=None) -> list[dict]:
    runs = runs or RunManager(run_graph.build(InMemorySaver()), RunLog())
    inputs = inputs or {"task": "fix the off-by-one in x"}
    run = await runs.start_workflow(RSINGH, name, inputs.get("task") or "review", inputs)
    return await drain(runs, run)


def called(fake_mcp, tool):
    return [args for name, args in fake_mcp["calls"] if name == tool]


def modes(state):
    return [m for m, _ in state["prompts"]]


async def test_code_change_runs_every_step_and_stops_at_the_gate(box):
    fake_mcp, state = box
    events = await run_workflow()
    assert events[-1]["status"] == "awaiting_approval"
    assert modes(state) == ["plan", "plan-review", "code", "review"]
    assert called(fake_mcp, "workspace_exec")[0]["command"].startswith("with-postgres ")
    # The plan step gets read-only tools; the code step the coder's own.
    tools = dict(state["tools"])
    assert set(tools["plan"]) <= {"Read", "Glob", "Grep", "Bash(git *)"} and "Edit" in tools["code"]
    # The coder is told the check command and may run it.
    code_prompt = [p for m, p in state["prompts"] if m == "code"][0]
    assert "`with-postgres env POSTGRES_HOST=" in code_prompt and "Bash(with-postgres *)" in tools["code"]
    answer = [e for e in events if e["type"] == "message"][-1]["text"]
    assert "waiting for your approval" in answer and "+fix" in answer and "exit code 0" in answer
    assert "verdict: approve" in answer
    assert not called(fake_mcp, "workspace_close")     # kept for the PR (P5); the TTL is the backstop
    # Later steps get earlier ones as fenced data.
    review_prompt = state["prompts"][-1][1]
    assert "[UNTRUSTED DATA from step code" in review_prompt and "git diff " + "b" * 40 in review_prompt


async def test_requested_changes_go_back_to_the_coder_with_the_findings(box):
    _, state = box
    state["answers"]["review"] = [CHANGES, APPROVE]
    events = await run_workflow()
    assert events[-1]["status"] == "awaiting_approval"
    assert modes(state) == ["plan", "plan-review", "code", "review", "code", "review"]
    second_code = [p for m, p in state["prompts"] if m == "code"][1]
    assert "was sent back" in second_code and "off by one" in second_code
    assert "Revision rounds: code: 1" in [e for e in events if e["type"] == "message"][-1]["text"]


async def test_a_failed_check_sends_the_code_back_even_if_the_reviewer_approves(box):
    _, state = box
    state["check"] = [1, 1, 1]          # never passes
    events = await run_workflow()
    # Two rounds back, then the gate anyway: you decide with the failing check in view.
    assert modes(state).count("code") == 3 and events[-1]["status"] == "awaiting_approval"
    second_code = [p for m, p in state["prompts"] if m == "code"][1]
    assert "The check failed" in second_code and "pytest exit 1" in second_code


async def test_the_plan_review_loop_is_bounded(box):
    _, state = box
    state["answers"]["plan-review"] = [CHANGES]      # never satisfied
    await run_workflow()
    assert modes(state)[:6] == ["plan", "plan-review"] * 3


async def test_the_usd_cap_stops_the_run_before_the_next_step(box, monkeypatch):
    fake_mcp, state = box
    wf = registry.load().workflows["code-change"]
    monkeypatch.setattr(wf, "max_usd", 0.2)          # each fake step reports $0.12
    events = await run_workflow()
    assert events[-1]["status"] == "failed" and modes(state) == ["plan", "plan-review"]
    assert usage.store.limits[0]["name"] == "usd" and usage.store.limits[0]["action"] == "refused"
    assert called(fake_mcp, "workspace_close")       # a failed run cleans up


async def test_a_reviewer_answer_that_is_not_a_verdict_fails_the_run(box):
    fake_mcp, state = box
    state["answers"]["plan-review"] = ["looks good to me!"]
    events = await run_workflow()
    assert events[-1]["status"] == "failed"
    assert "step plan-review" in [e for e in events if e["type"] == "error"][0]["detail"]
    assert called(fake_mcp, "workspace_close")


async def test_code_review_checks_out_the_pr_against_its_own_base(box):
    fake_mcp, state = box
    events = await run_workflow("code-review", {"pr": 7})
    assert events[-1]["status"] == "awaiting_approval"
    # The PR's base commit from GitHub: right for a merged PR too, where the merge base is the head.
    assert called(fake_mcp, "workspace_open") == [{"repo": "corespan/aistudio-server", "ref": "pull/7/head",
                                                   "base": "c" * 40}]
    assert modes(state) == ["review"] and "pull request #7" in state["prompts"][0][1]


async def test_one_workflow_at_a_time_per_user(box):
    _, state = box
    runs = RunManager(run_graph.build(InMemorySaver()), RunLog())
    await runs.start_workflow(RSINGH, "code-change", "x", {"task": "x"})
    with pytest.raises(TooManyRuns, match="1 workflow at a time"):
        await runs.start_workflow(RSINGH, "code-change", "y", {"task": "y"})
    for r in runs.runs.values():
        runs.cancel(r)


def test_the_api_checks_who_and_what(signer):
    with TestClient(app) as client:
        me = {"Authorization": f"Bearer {signer('rsingh', 'admin')}"}
        assert {w["name"] for w in client.get("/workflows", headers=me).json()} == {
            "code-change", "code-review", "bug-hunt", "pr-followup"}
        assert client.post("/workflows/code-review/runs", json={"task": "x"}, headers=me).status_code == 422
        alice = {"Authorization": f"Bearer {signer('alice', 'admin')}"}
        assert client.get("/workflows", headers=alice).json() == []
        assert client.post("/workflows/code-change/runs", json={"task": "x"}, headers=alice).status_code == 404


def test_a_broken_workflow_names_every_problem(tmp_path):
    root = Path(settings.platform_dir)
    for rel in ("packs/devflow/models/claude-coder.yaml", "packs/devflow/agents/coder.yaml",
                "packs/devflow/pack.yaml"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text((root / rel).read_text())
    (tmp_path / "platform.yaml").write_text("name: t\npacks: [devflow]\nrepos: [a/b]\n")
    (tmp_path / "packs/devflow/workflows").mkdir()
    (tmp_path / "packs/devflow/workflows/w.yaml").write_text("""
kind: workflow
name: w
purpose: p
input: task
users: [me]
repo: c/d
steps:
  - { id: code, agent: coder, mode: code, needs: [later] }
  - { id: review, agent: nobody, mode: review, revise: { step: future } }
  - { id: future, run: "true" }
""")
    with pytest.raises(registry.Invalid) as err:
        registry.build(tmp_path)
    for problem in ("repo 'c/d'", "needs 'later'", "'nobody' is not a claude-code agent", "revise goes to 'future'"):
        assert problem in str(err.value), problem


async def test_nothing_is_written_before_approval_and_a_reject_writes_nothing(box, gh):
    fake_mcp, state = box
    runs = RunManager(run_graph.build(InMemorySaver()), RunLog())
    events = await run_workflow(runs=runs)
    assert events[-1]["status"] == "awaiting_approval" and not [c for c in gh if c[0] != "pull"]
    run = next(iter(runs.runs.values()))
    assert run.status == "awaiting_approval"
    await runs.decide(RSINGH, run.id, {"approved": False, "reason": "not now"})
    after = await drain(runs, run, len(events))
    assert after[-1]["status"] == "rejected" and "Nothing was written" in after[-2]["text"]
    assert gh == [] and called(fake_mcp, "workspace_close")
    assert "learn" not in [m for m, _ in state["prompts"]]


async def test_approval_runs_learn_then_opens_a_draft_pr_with_exactly_the_approved_tree(box, gh):
    fake_mcp, state = box
    runs = RunManager(run_graph.build(InMemorySaver()), RunLog())
    events = await run_workflow(runs=runs)
    run = next(iter(runs.runs.values()))
    await runs.decide(RSINGH, run.id, {"approved": True})
    after = await drain(runs, run, len(events))
    assert after[-1]["status"] == "ok" and "Draft PR opened: https://github.com/" in after[-2]["text"]
    assert [m for m, _ in state["prompts"]][-1] == "learn"
    learn_tools = dict(state["tools"])["learn"]
    assert "Edit" not in learn_tools and "Edit(CLAUDE.md)" in learn_tools
    [(kind, pr)] = gh
    assert kind == "open_pr" and pr["branch"] == f"agent/{run.id}" and pr["base_ref"] == "master"
    assert pr["base_sha"] == "b" * 40                                  # the workspace's base
    (msg1, change), (msg2, knowledge) = pr["commits"]
    # Commit 1 is snapshot 3, the one you saw: not the edit the learn step made to app/x.py later.
    assert change == {"app/x.py": {"mode": "100644", "content": "fixed"}, "b.py": {"mode": "100644", "content": "moved"},
                      "old.py": None, "a.py": None}
    # Commit 2: only CLAUDE.md and .agents/knowledge/ changes since then.
    assert set(knowledge) == {"CLAUDE.md", ".agents/knowledge/db.md"}
    assert pr["title"] == "agent: fix the off-by-one in x" and "approval by rsingh" in pr["body"]
    assert called(fake_mcp, "workspace_close")


async def test_an_approved_review_is_posted_without_the_dropped_findings(box, gh):
    fake_mcp, state = box
    two = json.dumps({"verdict": "changes_requested", "summary": "two things", "findings": [
        {"severity": "high", "file": "app/x.py", "line": 3, "issue": "off by one"},
        {"severity": "low", "file": "app/y.py", "line": 9, "issue": "naming"}]})
    state["answers"]["review"] = [two]
    runs = RunManager(run_graph.build(InMemorySaver()), RunLog())
    events = await run_workflow("code-review", {"pr": 7}, runs=runs)
    assert "1. [low] app/y.py naming" in [e for e in events if e["type"] == "message"][-1]["text"]
    run = next(iter(runs.runs.values()))
    await runs.decide(RSINGH, run.id, {"approved": True, "drop": [1]})
    after = await drain(runs, run, len(events))
    assert after[-1]["status"] == "ok"
    [(kind, r)] = [c for c in gh if c[0] == "review"]
    assert r["n"] == 7 and r["commit_id"] == "h" * 40
    assert r["comments"] == [{"path": "app/x.py", "line": 3, "side": "RIGHT", "body": "**high**: off by one"}]
    assert "A comment only" in r["body"]


async def test_only_the_owner_decides_and_only_once(box):
    from app.runs import NotWaiting
    runs = RunManager(run_graph.build(InMemorySaver()), RunLog())
    events = await run_workflow(runs=runs)
    run = next(iter(runs.runs.values()))
    assert await runs.decide(Identity(sub="alice", role="admin", token="t"), run.id, {"approved": True}) is None
    await runs.decide(RSINGH, run.id, {"approved": False})
    await drain(runs, run, len(events))
    with pytest.raises(NotWaiting):
        await runs.decide(RSINGH, run.id, {"approved": True})


def test_github_name_status_lines_become_reads_and_deletes():
    assert github.changes(["M\ta.py", "A\tb.py", "D\tc.py", "R087\td.py\te.py"]) == (["a.py", "b.py", "e.py"], ["c.py", "d.py"])


def test_publish_must_come_after_the_gate(tmp_path):
    root = Path(settings.platform_dir)
    for rel in ("packs/devflow/models/claude-coder.yaml", "packs/devflow/agents/coder.yaml",
                "packs/devflow/pack.yaml"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text((root / rel).read_text())
    (tmp_path / "platform.yaml").write_text("name: t\npacks: [devflow]\nrepos: [a/b]\n")
    (tmp_path / "packs/devflow/workflows").mkdir()
    (tmp_path / "packs/devflow/workflows/w.yaml").write_text("""
kind: workflow
name: w
purpose: p
input: task
users: [me]
repo: a/b
steps:
  - { id: pr, publish: pr }
  - { id: ok, gate: write }
  - { id: post, publish: review }
""")
    with pytest.raises(registry.Invalid) as err:
        registry.build(tmp_path)
    assert "step pr: publish comes only after a write gate" in str(err.value)
    assert "can't publish a review" in str(err.value)


async def test_a_failed_publish_goes_back_to_the_gate_and_a_second_approve_retries_only_it(box, gh, monkeypatch):
    fake_mcp, state = box
    real_open_pr, tries = github.open_pr, []

    async def flaky(*a, **k):
        tries.append(1)
        if len(tries) == 1:
            raise github.GitHubError("POST repos/x/git/blobs: 403 Resource not accessible by personal access token")
        return await real_open_pr(*a, **k)
    monkeypatch.setattr(github, "open_pr", flaky)
    runs = RunManager(run_graph.build(InMemorySaver()), RunLog())
    events = await run_workflow(runs=runs)
    run = next(iter(runs.runs.values()))
    await runs.decide(RSINGH, run.id, {"approved": True})
    first = await drain(runs, run, len(events))
    assert first[-1]["status"] == "awaiting_approval" and "Publishing failed" in first[-2]["text"]
    assert not called(fake_mcp, "workspace_close")                 # the workspace is kept
    learned = [m for m, _ in state["prompts"]].count("learn")
    await runs.decide(RSINGH, run.id, {"approved": True})
    second = await drain(runs, run, len(events) + len(first))
    assert second[-1]["status"] == "ok" and "Draft PR opened" in second[-2]["text"]
    assert [m for m, _ in state["prompts"]].count("learn") == learned   # learn didn't run again
    assert len(tries) == 2 and called(fake_mcp, "workspace_close")



async def approve_through(runs, run, events, decision=None):
    """Approve the gate the run stopped at; returns the events after it."""
    await runs.decide(RSINGH, run.id, decision or {"approved": True})
    return await drain(runs, run, len(events))


async def test_the_branch_is_named_under_agent_and_reviewers_are_asked(box, gh, monkeypatch):
    wf = registry.load().workflows["code-change"]
    monkeypatch.setattr(wf, "reviewers", ["team-lead"])
    runs = RunManager(run_graph.build(InMemorySaver()), RunLog())
    events = await run_workflow(inputs={"task": "fix x", "branch": "fix-status", "reviewers": ["alice-gh"]}, runs=runs)
    run = next(iter(runs.runs.values()))
    after = await approve_through(runs, run, events)
    [pr] = [c[1] for c in gh if c[0] == "open_pr"]
    assert pr["branch"] == "agent/fix-status"
    assert ("request_reviewers", 99, ["team-lead", "alice-gh"]) in gh
    assert "Review requested from team-lead, alice-gh" in after[-2]["text"]


async def test_a_refused_review_request_leaves_the_pr_open_and_says_so(box, gh):
    runs = RunManager(run_graph.build(InMemorySaver()), RunLog())
    events = await run_workflow(inputs={"task": "fix x", "reviewers": ["nobody"]}, runs=runs)
    run = next(iter(runs.runs.values()))
    after = await approve_through(runs, run, events)
    assert after[-1]["status"] == "ok"
    assert "Draft PR opened" in after[-2]["text"] and "Requesting review from nobody failed" in after[-2]["text"]


async def test_bug_hunt_fixes_only_the_findings_you_keep(box, gh):
    fake_mcp, state = box
    runs = RunManager(run_graph.build(InMemorySaver()), RunLog())
    events = await run_workflow("bug-hunt", {"task": "app/"}, runs=runs)
    assert events[-1]["status"] == "awaiting_approval"
    triage = [e for e in events if e["type"] == "message"][-1]["text"]
    assert "found 3 possible bugs" in triage and "1. [low] app/b.py:2 a log line is wrong" in triage
    assert modes(state) == ["hunt"]                                   # nothing fixed before you choose
    hunt_tools = dict(state["tools"])["hunt"]
    assert "Bash(with-postgres *)" in hunt_tools and not {"Edit", "Write"} & set(hunt_tools)
    run = next(iter(runs.runs.values()))
    second = await approve_through(runs, run, events, {"approved": True, "drop": [1]})
    assert second[-1]["status"] == "awaiting_approval"                # the write gate, with the diff
    fix_prompt = [p for m, p in state["prompts"] if m == "fix"][0]
    assert "status never set to FAILED" in fix_prompt and "a leaked session" in fix_prompt
    assert "a log line is wrong" not in fix_prompt
    assert not [c for c in gh if c[0] == "open_pr"]
    third = await approve_through(runs, run, events + second)
    assert third[-1]["status"] == "ok"
    [pr] = [c[1] for c in gh if c[0] == "open_pr"]
    assert pr["title"] == "agent: fix 2 bugs (app/)" and "a leaked session" in pr["body"]
    assert "a log line is wrong" not in pr["body"]


async def test_dropping_every_finding_ends_the_hunt_with_nothing_written(box, gh):
    fake_mcp, state = box
    runs = RunManager(run_graph.build(InMemorySaver()), RunLog())
    events = await run_workflow("bug-hunt", {"task": "app/"}, runs=runs)
    run = next(iter(runs.runs.values()))
    after = await approve_through(runs, run, events, {"approved": True, "drop": [0, 1, 2]})
    assert after[-1]["status"] == "ok" and "Nothing left to fix" in after[-2]["text"]
    assert modes(state) == ["hunt"] and not [c for c in gh if c[0] != "pull"]
    assert called(fake_mcp, "workspace_close")


async def test_followup_works_only_on_the_agents_own_prs(box, gh):
    fake_mcp, _ = box
    events = await run_workflow("pr-followup", {"pr": 7})        # someone/branch, not agent/
    assert events[-1]["status"] == "failed"
    assert "not an open PR from an agent/ branch" in [e for e in events if e["type"] == "error"][0]["detail"]
    assert not called(fake_mcp, "workspace_open")


async def test_followup_with_nothing_to_address_opens_nothing(box, gh):
    fake_mcp, state = box
    gh_pull = github.pull

    async def agent_pr(token, repo, n):
        p = await gh_pull(token, repo, n)
        p["head"]["ref"] = "agent/fix-x"
        return p
    github.pull = agent_pr
    try:
        events = await run_workflow("pr-followup", {"pr": 10})
    finally:
        github.pull = gh_pull
    assert events[-1]["status"] == "ok" and "Nothing to address on PR #10" in events[-2]["text"]
    assert not called(fake_mcp, "workspace_open") and not state["prompts"]


async def test_followup_addresses_trusted_comments_and_pushes_to_the_same_branch(box, gh):
    fake_mcp, state = box
    runs = RunManager(run_graph.build(InMemorySaver()), RunLog())
    events = await run_workflow("pr-followup", {"pr": 9}, runs=runs)
    assert events[-1]["status"] == "awaiting_approval"
    assert ("feedback", 9, ["rsingh-alt"]) in gh
    # The branch head is the workspace's base: the diff and the review are only what's new.
    assert called(fake_mcp, "workspace_open") == [{"repo": "corespan/aistudio-server", "ref": "agent/fix-x"}]
    address = [p for m, p in state["prompts"] if m == "address"][0]
    assert "rename this variable" in address and "[UNTRUSTED DATA from the PR's feedback" in address
    assert not [c for c in gh if c[0] in ("push", "comment")]         # nothing before approval
    run = next(iter(runs.runs.values()))
    after = await approve_through(runs, run, events)
    assert after[-1]["status"] == "ok" and "Pushed pppppppp to agent/fix-x" in after[-2]["text"]
    [push] = [c[1] for c in gh if c[0] == "push"]
    assert push["branch"] == "agent/fix-x" and push["head_sha"] == "h" * 40
    [(_, n, body)] = [c for c in gh if c[0] == "comment"]
    assert n == 9 and "addressing 1 comment(s)" in body and "ignored (1 this time)" in body


async def test_a_branch_name_that_git_would_refuse_is_refused(signer):
    with TestClient(app) as client:
        me = {"Authorization": f"Bearer {signer('rsingh', 'admin')}"}
        for bad in ("../master", "a..b", "x/", "-x", "x.lock"):
            r = client.post("/workflows/code-change/runs", json={"task": "x", "branch": bad}, headers=me)
            assert r.status_code == 422, bad
