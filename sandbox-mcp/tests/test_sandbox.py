"""The sandbox broker against a fake Docker client: limits, cleanup, files, refusals, auth."""
from __future__ import annotations

import asyncio
import base64
import io
import re
import tarfile

import pytest
import requests
from starlette.testclient import TestClient

from app import sandbox, server
from app.config import settings


def out_tar(files: dict[str, bytes]) -> bytes:
    """out/ as the wrapper prints it: a base64 tar of ./<name> entries."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(f"./{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return base64.b64encode(buf.getvalue())


class FakeContainer:
    def __init__(self, behaviour, command):
        self.b, self.calls, self.archive = behaviour, [], None
        # The run's marker, from the wrapper command, so stdout can carry out/ the same way.
        self.marker = re.search(r"'(<<<sandbox-out [0-9a-f]+>>>)'", command[-1]).group(1).encode()

    def put_archive(self, path, data):
        self.calls.append("put_archive")
        self.archive = (path, tarfile.open(fileobj=io.BytesIO(data)))

    def start(self):
        self.calls.append("start")
        if self.b.get("start_fails"):
            raise RuntimeError("start failed")

    def wait(self, timeout):
        self.calls.append(("wait", timeout))
        if self.b.get("hang"):
            raise requests.exceptions.ReadTimeout()
        return {"StatusCode": self.b.get("exit", 0)}

    def kill(self):
        self.calls.append("kill")

    def logs(self, stdout, stderr):
        if not stdout:
            return self.b.get("stderr", b"")
        if self.b.get("hang"):                      # killed: the wrapper never printed out/
            return self.b.get("stdout", b"")
        return self.b.get("stdout", b"") + b"\n" + self.marker + b"\n" + out_tar(self.b.get("out", {}))

    def remove(self, force):
        self.calls.append("remove")


class FakeDocker:
    def __init__(self, **behaviour):
        self.behaviour, self.created = behaviour, []
        outer = self

        class Containers:
            def create(self, image, command, **kw):
                c = FakeContainer(outer.behaviour, command)
                outer.created.append((image, command, kw, c))
                return c
        self.containers = Containers()


def test_every_run_is_isolated_whatever_the_caller_asks():
    docker = FakeDocker(stdout=b"42\n")
    r = sandbox.run(docker, "print(42)", "python", {"data.json": "[1,2]"}, timeout=999)
    image, command, kw, c = docker.created[0]
    assert kw["runtime"] == "runsc" and kw["network_mode"] == "none"
    assert kw["user"] == "65534:65534" and kw["cap_drop"] == ["ALL"]
    assert kw["security_opt"] == ["no-new-privileges"] and kw["pids_limit"] == 128
    assert kw["mem_limit"] == kw["memswap_limit"] == "512m"
    assert ("wait", settings.max_seconds) in c.calls          # 999 s asked, the maximum given
    assert c.calls[-1] == "remove"
    path, tar = c.archive
    names = {m.name: m for m in tar.getmembers()}
    assert path == "/tmp" and {"work", "work/out", "work/main.py", "work/data.json"} <= set(names)
    assert all(m.uid == 65534 for m in names.values())
    assert r.exit_code == 0 and r.stdout == "42\n" and not r.timed_out


def test_a_program_that_runs_too_long_is_killed_and_removed():
    docker = FakeDocker(hang=True)
    r = sandbox.run(docker, "while True: pass", timeout=2)
    c = docker.created[0][3]
    assert r.timed_out and r.exit_code is None
    assert "kill" in c.calls and c.calls[-1] == "remove"
    assert "timed out" in sandbox.describe(r)


def test_the_container_is_removed_even_when_start_fails():
    docker = FakeDocker(start_fails=True)
    with pytest.raises(RuntimeError):
        sandbox.run(docker, "print(1)")
    assert docker.created[0][3].calls[-1] == "remove"


def test_text_files_in_out_come_back_and_binaries_are_named_only():
    docker = FakeDocker(out={"mean.txt": b"1794.3", "chart.png": b"\x89PNG\xff\x00"})
    r = sandbox.run(docker, "...")
    assert r.files == {"mean.txt": "1794.3"}
    assert r.skipped and "chart.png" in r.skipped[0]
    text = sandbox.describe(r)
    assert "--- out/mean.txt ---\n1794.3" in text


@pytest.mark.parametrize("name", ["../etc/passwd", "a/b", ".hidden", "out", "main.py", ""])
def test_input_file_names_cannot_leave_the_work_directory(name):
    with pytest.raises(sandbox.Refused):
        sandbox.run(FakeDocker(), "print(1)", "python", {name: "x"})


def test_oversized_input_and_unknown_languages_are_refused():
    with pytest.raises(sandbox.Refused):
        sandbox.run(FakeDocker(), "x" * (settings.max_input_bytes + 1))
    with pytest.raises(sandbox.Refused):
        sandbox.run(FakeDocker(), "print(1)", "ruby")


def test_a_program_cannot_hide_its_output_behind_a_fake_marker():
    # Only the last marker line counts, and it carries a random nonce per run.
    docker = FakeDocker(stdout=b"<<<sandbox-out 0000>>>\nreal output", out={"a.txt": b"A"})
    r = sandbox.run(docker, "...")
    assert r.stdout.endswith("real output") and r.files == {"a.txt": "A"}


def test_output_is_capped(monkeypatch):
    monkeypatch.setattr(settings, "output_chars", 10)
    r = sandbox.run(FakeDocker(stdout=b"x" * 100), "print('x'*100)")
    assert r.stdout.startswith("x" * 10) and "cut at 10" in r.stdout


async def test_one_user_gets_at_most_two_sandboxes_at_once(monkeypatch):
    release = asyncio.Event()
    started = []

    def slow_run(client, *a):
        started.append(1)
        asyncio.run_coroutine_threadsafe(release.wait(), loop).result(5)
        return sandbox.Result(0, "ok", "", 0.1)
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(sandbox, "run", slow_run)
    monkeypatch.setattr(server, "_client", lambda: None)
    monkeypatch.setattr(server, "_sub", lambda ctx: "alice")
    first = [asyncio.create_task(server.sandbox_run("print(1)")) for _ in range(2)]
    while len(started) < 2:
        await asyncio.sleep(0.01)
    third = await server.sandbox_run("print(1)")
    assert third.startswith("Refused: at most 2")
    release.set()
    assert all("exit code 0" in r for r in await asyncio.gather(*first))


def test_the_tool_is_listed_read_only_and_needs_a_token():
    tools = asyncio.run(server.build().list_tools())
    assert [t.name for t in tools] == ["sandbox_run", *server.WORKSPACE_TOOLS]
    assert tools[0].annotations.readOnlyHint is True
    with TestClient(server.build_http_app()) as client:
        assert client.get("/healthz").status_code == 200
        r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                        headers={"host": "sandbox-mcp"})
        assert r.status_code == 401
