"""fs/git/container_diff probes: special files, planted git configs, directory archives."""
import asyncio
import io
import os
import subprocess
import sys
import tarfile
import time
import types

import pytest

from app.tools import docker_probes, fs_probes


@pytest.mark.parametrize("path", ["/home/u/.ssh/id_dsa", "/x/key.ppk", "/home/u/.docker/config.json",
                                  "/home/u/.kube/config", "/repo/kubeconfig", "/x/prod.kubeconfig",
                                  "/home/u/.npmrc", "/home/u/.pypirc", "/home/u/.netrc",
                                  "/home/u/.git-credentials", "/var/log/audit/audit.log",
                                  "/var/log/audit/audit.log.1"])
def test_more_secret_names_are_recognised(path):
    assert fs_probes._is_secret(path)


@pytest.mark.parametrize("path", ["/repo/config.json", "/repo/.kube-notes/config", "/repo/config",
                                  "/repo/id_mapping.py"])
def test_names_only_secret_in_their_dir_are_not_secret_elsewhere(path):
    assert not fs_probes._is_secret(path)


@pytest.mark.skipif(sys.platform == "win32", reason="os.mkfifo is POSIX-only")
def test_grep_skips_a_fifo(tmp_path, monkeypatch):
    os.mkfifo(tmp_path / "trap")
    (tmp_path / "app.log").write_text("needle\n")
    monkeypatch.setattr(fs_probes.settings, "readable_roots", str(tmp_path))
    start = time.monotonic()
    out = asyncio.run(asyncio.wait_for(
        fs_probes.grep_logs.ainvoke({"pattern": "needle", "path": str(tmp_path)}), timeout=10))
    assert "app.log" in out and time.monotonic() - start < 10, out


@pytest.mark.skipif(sys.platform == "win32", reason="os.mkfifo is POSIX-only")
def test_a_fifo_is_never_opened(tmp_path):
    os.mkfifo(tmp_path / "trap")
    with pytest.raises(OSError, match="not a regular file"):
        fs_probes._open_regular(str(tmp_path / "trap"))


# ── git: only the configured repo ──
def _git(repo, *a):
    subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)


@pytest.fixture
def roots(tmp_path, monkeypatch):
    repo, logs = tmp_path / "repo", tmp_path / "log"
    for d in (repo, logs):
        d.mkdir()
        _git(d, "init", "-q")
        _git(d, "config", "user.email", "t@t")
        _git(d, "config", "user.name", "t")
        (d / "f").write_text("x\n")
        _git(d, "add", "f")
        _git(d, "commit", "-qm", "first")
    monkeypatch.setattr(fs_probes.settings, "repo_path", str(repo))
    monkeypatch.setattr(fs_probes.settings, "readable_roots", f"{repo},{logs}")
    return repo, logs


@pytest.mark.parametrize("tool", [fs_probes.git_log, fs_probes.git_show])
def test_git_refuses_a_repo_other_than_the_configured_one(roots, tool):
    # /var/log is readable; a .git/config planted there can name gpg.program, a pager, ...
    out = asyncio.run(tool.ainvoke({"repo": str(roots[1])}))
    assert "not the configured repo" in out, out


def test_git_log_still_reads_the_configured_repo(roots):
    out = asyncio.run(fs_probes.git_log.ainvoke({"repo": str(roots[0])}))
    assert "first" in out, out


def test_git_runs_with_execution_settings_pinned(roots, monkeypatch):
    seen = []

    async def fake_run(*argv, **kw):
        seen.append(argv)
        return 0, "", ""
    monkeypatch.setattr(fs_probes, "run_command", fake_run)
    asyncio.run(fs_probes.git_log.ainvoke({}))
    asyncio.run(fs_probes.git_show.ainvoke({}))
    real = os.path.realpath(roots[0])
    for argv in seen:
        assert "safe.directory=*" not in argv and f"safe.directory={real}" in argv, argv
        for pin in ("log.showSignature=false", "gpg.program=false", "core.pager=cat",
                    "diff.external=", "core.fsmonitor="):
            assert pin in argv, (pin, argv)


# ── container_diff: a directory is not a file ──
def _tar(*members):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in members:
            info = tarfile.TarInfo(name)
            if data is None:
                info.type = tarfile.DIRTYPE
                tf.addfile(info)
            else:
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _container(monkeypatch, raw):
    c = types.SimpleNamespace(
        get_archive=lambda path: (iter([raw]), {"size": 4096, "linkTarget": ""}))
    monkeypatch.setattr(docker_probes, "_resolve_container", lambda name: c)


def test_container_diff_refuses_a_directory_holding_one_file(tmp_path, monkeypatch):
    (tmp_path / "conf").write_text("ok\n")
    monkeypatch.setattr(docker_probes.settings, "readable_roots", str(tmp_path))
    _container(monkeypatch, _tar(("conf", None), ("conf/id_rsa", b"-----BEGIN leak-me-----\n")))
    out = asyncio.run(docker_probes.container_diff.ainvoke(
        {"container": "api", "path": "/app/conf", "repo_path": str(tmp_path / "conf")}))
    assert "directory" in out and "leak-me" not in out, out


def test_container_diff_clamps_max_lines(tmp_path, monkeypatch):
    (tmp_path / "main.py").write_text("a\n")
    monkeypatch.setattr(docker_probes.settings, "readable_roots", str(tmp_path))
    _container(monkeypatch, _tar(("main.py", b"b\n")))
    out = asyncio.run(docker_probes.container_diff.ainvoke(
        {"container": "api", "path": "/app/main.py", "repo_path": str(tmp_path / "main.py"),
         "max_lines": -5}))
    assert "DIFFERS" in out and "--- repo:" in out, out
