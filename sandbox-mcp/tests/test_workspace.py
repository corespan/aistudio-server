"""Workspaces against a fake Docker: state carried by snapshots, cleanup on failure, the env allowlist."""
from __future__ import annotations

import base64
import io
import json
import re
import tarfile
import time

import pytest

from app import workspace
from app.config import settings

SHA = "a" * 40


def tgz(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def files_in(data: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
        return {m.name: tar.extractfile(m).read() for m in tar if m.isfile()}


class FakeContainer:
    def __init__(self, step: dict, command: list[str], kw: dict):
        self.step, self.kw, self.command, self.calls, self.archives = step, kw, command, [], []

    def put_archive(self, path, data):
        self.archives.append((path, data))

    def start(self):
        self.calls.append("start")
        if self.step.get("start_fails"):
            raise RuntimeError("start failed")

    def logs(self, stdout=True, stderr=False, stream=False, follow=False):
        if not stdout:
            return self.step.get("stderr", b"")
        body = self.step.get("stdout", b"")
        if not stream:
            return body
        # Like Docker with a slow reader: the follow stream can end before the last bytes.
        body = body[:-10] if self.step.get("cut_stream") else body
        return iter([body[i:i + 7] for i in range(0, len(body), 7)])   # odd chunk sizes on purpose

    def get_archive(self, path):
        """/out/snap.tgz as Docker returns it: a tar holding the one file (or a link to it)."""
        if "snapshot" not in self.step:
            raise RuntimeError("404 no such file")
        assert path == "/out/snap.tgz"
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo("snap.tgz")
            if self.step.get("snapshot_is_link"):
                info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
                tar.addfile(info)
            else:
                info.size = len(self.step["snapshot"])
                tar.addfile(info, io.BytesIO(self.step["snapshot"]))
        data = buf.getvalue()
        return iter([data[:100], data[100:]]), {"size": len(self.step["snapshot"])}

    def wait(self, timeout):
        return {"StatusCode": self.step.get("exit", 0)}

    def kill(self):
        self.calls.append("kill")

    def remove(self, force, v=False):
        self.calls.append("remove")


class FakeDocker:
    """Each created container plays the next scripted step."""
    def __init__(self, *steps: dict):
        self.steps, self.created = list(steps), []
        outer = self

        class Containers:
            def create(self, image, command, **kw):
                c = FakeContainer(outer.steps.pop(0), command, kw)
                outer.created.append(c)
                return c
        self.containers = Containers()


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    return tmp_path


def opened(docker=None, owner="rsingh") -> dict:
    docker = docker or FakeDocker({"stdout": SHA.encode(), "snapshot": tgz({"work/a.py": b"v0"})})
    return workspace.open_(docker, owner, "jwt", "corespan/aistudio-server", "master")


def test_steps_share_state_through_snapshots():
    ws = opened()
    after_claude = tgz({"work/a.py": b"v1 by claude"})
    docker = FakeDocker(
        {"stdout": b'{"type":"assistant"}\n{"type":"result","result":"done","is_error":false}',
         "snapshot": after_claude},
        {"stdout": b"3 passed"},
        {"stdout": b"--- a/a.py\n+++ b/a.py\n<<<diff 00>>>\nM\ta.py"})
    lines = []
    r = workspace.claude(docker, "rsingh", "jwt", ws["id"], "fix it", "claude-opus-5-5",
                         ["Read", "Edit", "Bash(make *)"], 30, 900, lines.append)
    assert r["snapshot"] == 1 and r["result"]["result"] == "done"
    assert lines == ['{"type":"assistant"}', '{"type":"result","result":"done","is_error":false}']
    claude_box = docker.created[0]
    assert files_in(claude_box.archives[0][1]) == {"work/a.py": b"v0"}        # restored snapshot 0
    assert files_in(claude_box.archives[1][1])["tools.txt"] == b"Read\nEdit\nBash(make *)"
    workspace.exec_(docker, "rsingh", ws["id"], "make runner-test", 600)
    assert docker.created[1].archives[0][1] == after_claude                     # the check sees claude's tree
    # The exec step doesn't keep a tree: the diff still runs on snapshot 1.
    workspace.diff(docker, "rsingh", ws["id"])
    assert docker.created[2].archives[0][1] == after_claude


def test_every_container_is_removed_and_nothing_saved_when_a_step_fails(store):
    ws = opened()
    docker = FakeDocker({"start_fails": True})
    with pytest.raises(RuntimeError):
        workspace.claude(docker, "rsingh", "jwt", ws["id"], "x", "claude-opus-5-5", ["Read"], 5, 60)
    assert docker.created[0].calls[-1] == "remove"
    assert sorted(p.name for p in (store / ws["id"]).iterdir()) == ["0.tgz", "meta.json"]


def test_a_failed_clone_leaves_no_workspace(store):
    docker = FakeDocker({"exit": 128, "stderr": b"fatal: repository not found", "snapshot": tgz({})})
    with pytest.raises(workspace.Refused, match="repository not found"):
        workspace.open_(docker, "rsingh", "jwt", "corespan/nope", "master")
    assert docker.created[0].calls[-1] == "remove" and not list(store.iterdir())


@pytest.mark.parametrize("entry", ["etc/passwd", "work/../../etc/passwd", "/work/x"])
def test_a_snapshot_that_reaches_outside_work_is_refused(entry):
    ws = opened()
    docker = FakeDocker({"stdout": b"{}", "snapshot": tgz({entry: b"x"})})
    with pytest.raises(workspace.Refused, match="outside /work"):
        workspace.claude(docker, "rsingh", "jwt", ws["id"], "x", "claude-opus-5-5", ["Read"], 5, 60)


def test_program_output_never_touches_the_snapshot_and_a_link_is_not_one():
    # Output that looks like an archive, or a step that leaves things running, can't reach the
    # snapshot: it comes from /out as a file, not from stdout.
    ws = opened()
    real = tgz({"work/a.py": b"real"})
    docker = FakeDocker({"stdout": base64.b64encode(tgz({"work/a.py": b"fake"})), "snapshot": real})
    workspace.claude(docker, "rsingh", "jwt", ws["id"], "x", "claude-opus-5-5", ["Read"], 5, 60)
    assert docker.created[0].kw["volumes"] == ["/out"]
    assert docker.created[0].calls[-1] == "remove"
    docker2 = FakeDocker({"stdout": b""})
    workspace.exec_(docker2, "rsingh", ws["id"], "true", 10)
    assert docker2.created[0].archives[0][1] == real
    assert docker2.created[0].kw["volumes"] is None             # nothing kept, no volume
    linked = FakeDocker({"stdout": b"", "snapshot": real, "snapshot_is_link": True})
    r = workspace.claude(linked, "rsingh", "jwt", ws["id"], "x", "claude-opus-5-5", ["Read"], 5, 60)
    assert r["snapshot"] == 1                                    # not saved: still the last one


def test_containers_get_only_allowlisted_env_and_checks_get_no_token(monkeypatch):
    monkeypatch.setenv("SANDBOX_SECRET", "s3cret")                 # nothing from our own env leaks
    docker = FakeDocker({"stdout": SHA.encode(), "snapshot": tgz({"work/a": b""})},
                        {"stdout": b"", "snapshot": tgz({"work/a": b""})}, {"stdout": b""})
    ws = workspace.open_(docker, "rsingh", "jwt", "corespan/aistudio-server", "master")
    workspace.claude(docker, "rsingh", "jwt", ws["id"], "x", "claude-opus-5-5", ["Read"], 5, 60)
    workspace.exec_(docker, "rsingh", ws["id"], "make runner-test", 60)
    envs = [c.kw["environment"] for c in docker.created]
    assert all(set(e) <= workspace.ENV for e in envs)
    assert not any("s3cret" in v for e in envs for v in e.values())
    assert envs[1]["ANTHROPIC_AUTH_TOKEN"] == "jwt" and envs[1]["ANTHROPIC_BASE_URL"].endswith("/llm/anthropic")
    assert "ANTHROPIC_AUTH_TOKEN" not in envs[2] and "GIT_CONFIG_VALUE_0" not in envs[2]
    with pytest.raises(workspace.Refused, match="allowlist"):
        workspace.env(secret="x")


def test_every_container_is_isolated_whatever_the_caller_asks():
    docker = FakeDocker({"stdout": SHA.encode(), "snapshot": tgz({"work/a": b""})}, {"stdout": b""})
    ws = workspace.open_(docker, "rsingh", "jwt", "corespan/aistudio-server", "master")
    workspace.exec_(docker, "rsingh", ws["id"], "sleep 99999", 99999)
    for c in docker.created:
        kw = c.kw
        assert kw["runtime"] == "runsc" and kw["network"] == "aistudio-sandbox-egress"
        assert kw["extra_hosts"] == {"agentgateway-sandbox": "172.30.51.10"}
        assert kw["user"] == "1000:1000" and kw["cap_drop"] == ["ALL"]
        assert kw["security_opt"] == ["no-new-privileges"] and kw["mem_limit"] == "2g"
    assert f"timeout -k 5 {settings.workspace_max_seconds} " in docker.created[1].command[-1]   # 99999 asked


def test_one_user_cannot_touch_another_users_workspace():
    ws = opened(owner="bob")
    with pytest.raises(workspace.Refused, match="unknown workspace"):
        workspace.diff(FakeDocker(), "alice", ws["id"])
    with pytest.raises(workspace.Refused):
        workspace.close("alice", ws["id"])
    with pytest.raises(workspace.Refused):
        workspace.close("bob", "../../etc")


def test_close_and_the_ttl_delete_snapshots(store):
    a, b = opened(), opened()
    workspace.close("rsingh", a["id"])
    assert not (store / a["id"]).exists()
    meta = store / b["id"] / "meta.json"
    meta.write_text(json.dumps({**json.loads(meta.read_text()), "created": time.time() - 25 * 3600}))
    assert workspace.sweep() == [b["id"]] and not list(store.iterdir())


@pytest.mark.parametrize("ref", ["--upload-pack=x", "a..b", "a b", ""])
def test_refs_that_could_be_options_or_ranges_are_refused(ref):
    with pytest.raises(workspace.Refused):
        workspace.open_(FakeDocker(), "rsingh", "jwt", "corespan/aistudio-server", ref)


@pytest.mark.parametrize("tools", [[], ["Bash(rm -rf /)\nRead"], ["Read; curl"]])
def test_tool_rules_are_checked(tools):
    ws = opened()
    with pytest.raises(workspace.Refused, match="tools"):
        workspace.claude(FakeDocker(), "rsingh", "jwt", ws["id"], "x", "claude-opus-5-5", tools, 5, 60)


def test_a_step_past_its_token_cap_is_killed_mid_run_and_keeps_no_tree():
    ws = opened()
    turn = lambda i: json.dumps({"type": "assistant", "message": {"id": f"m{i}", "usage": {
        "input_tokens": 10, "output_tokens": 990, "cache_read_input_tokens": 10_000}}})
    stdout = "\n".join(turn(i) for i in range(5)).encode()      # 2000 weighted each
    docker = FakeDocker({"stdout": stdout, "snapshot": tgz({"work/a.py": b"never kept"})})
    seen = []
    r = workspace.claude(docker, "rsingh", "jwt", ws["id"], "x", "claude-opus-5-5", ["Read"], 50, 60,
                         seen.append, max_weighted=5000)
    assert r["budget_exceeded"] and r["weighted"] == 6000 and r["snapshot"] == 0
    assert len(seen) == 3 and "kill" in docker.created[0].calls          # stopped at the third turn
    assert docker.created[0].calls[-1] == "remove"
    under = FakeDocker({"stdout": stdout, "snapshot": tgz({"work/a.py": b"kept"})})
    r = workspace.claude(under, "rsingh", "jwt", ws["id"], "x", "claude-opus-5-5", ["Read"], 50, 60,
                         max_weighted=50_000)
    assert not r["budget_exceeded"] and r["weighted"] == 10_000 and r["snapshot"] == 1


def test_a_cut_progress_stream_still_returns_the_whole_output():
    # Docker can end a follow stream early with a slow reader; the result comes from the full log.
    ws = opened()
    out = b'{"type":"assistant"}\n{"type":"result","result":"done"}\n'
    docker = FakeDocker({"stdout": out, "cut_stream": True, "snapshot": tgz({"work/a.py": b"x"})})
    r = workspace.claude(docker, "rsingh", "jwt", ws["id"], "x", "claude-opus-5-5", ["Read"], 5, 60)
    assert r["result"] == {"type": "result", "result": "done"} and r["snapshot"] == 1


def test_files_come_from_a_snapshot_with_modes_and_never_git():
    def gz(entries):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for name, data, mode in entries:
                info = tarfile.TarInfo(name)
                info.size, info.mode = len(data), mode
                tar.addfile(info, io.BytesIO(data))
        return buf.getvalue()
    snap = gz([("work/app.py", b"print(1)", 0o644), ("work/run.sh", b"#!/bin/sh", 0o755),
               ("work/.agents/knowledge/a.md", b"A", 0o644), ("work/.git/config", b"secret", 0o644)])
    ws = opened(FakeDocker({"stdout": SHA.encode(), "snapshot": snap}))
    got = workspace.files("rsingh", ws["id"], ["app.py", "run.sh", "gone.py", ".agents/knowledge/", ".git/"])
    f = got["files"]
    assert base64.b64decode(f["app.py"]["content"]) == b"print(1)" and f["app.py"]["mode"] == "100644"
    assert f["run.sh"]["mode"] == "100755" and f["gone.py"] is None
    assert set(f) == {"app.py", "run.sh", "gone.py", ".agents/knowledge/a.md"}      # nothing from .git
    with pytest.raises(workspace.Refused):
        workspace.files("alice", ws["id"], ["app.py"])
    with pytest.raises(workspace.Refused):
        workspace.files("rsingh", ws["id"], ["../etc/passwd"])
    with pytest.raises(workspace.Refused):
        workspace.files("rsingh", ws["id"], ["app.py"], snapshot=5)
