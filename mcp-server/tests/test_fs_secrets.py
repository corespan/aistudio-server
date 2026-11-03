"""Secrets files stay unreadable even inside an allowed root."""

import asyncio

import pytest

from app.tools import fs_probes


@pytest.fixture
def root(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("POSTGRES_ROOT_PASSWORD=leak-me\n")
    (tmp_path / "id_rsa").write_text("-----BEGIN leak-me-----\n")
    (tmp_path / ".env.example").write_text("POSTGRES_ROOT_PASSWORD=\n")
    (tmp_path / "app.log").write_text("ok leak-me\n")
    monkeypatch.setattr(fs_probes.settings, "readable_roots", str(tmp_path))
    return tmp_path


@pytest.mark.parametrize("name", [".env", ".env.prod", "id_rsa", "id_ed25519.pub", "tls.key",
                                  "cert.pem", "gcr-key.json", "service-account-x.json"])
def test_secret_names_are_recognised(name):
    assert fs_probes._is_secret(f"/repo/{name}")


@pytest.mark.parametrize("name", [".env.example", "config.py", "app.log", "README.md"])
def test_ordinary_files_are_not(name):
    assert not fs_probes._is_secret(f"/repo/{name}")


def test_read_file_refuses_a_secrets_file(root):
    out = asyncio.run(fs_probes.read_file.ainvoke({"path": str(root / ".env")}))
    assert "secrets file" in out and "leak-me" not in out


def test_read_file_still_reads_the_example(root):
    out = asyncio.run(fs_probes.read_file.ainvoke({"path": str(root / ".env.example")}))
    assert "POSTGRES_ROOT_PASSWORD=" in out


def test_grep_skips_secrets_files(root):
    out = asyncio.run(fs_probes.grep_logs.ainvoke({"pattern": "leak-me", "path": str(root)}))
    assert "app.log" in out and ".env" not in out and "id_rsa" not in out


def test_grep_pattern_is_plain_text(root):
    # A catastrophic regex must not run as one: it is searched for literally (and finds nothing).
    out = asyncio.run(fs_probes.grep_logs.ainvoke({"pattern": "(a+)+$", "path": str(root)}))
    assert "Invalid regex" not in out and "app.log" not in out
