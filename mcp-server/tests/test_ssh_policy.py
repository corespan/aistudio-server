"""Node probes: the target allow-list and the host-key policy, the same rules as the workers."""

import asyncio
import os

import asyncssh
import pytest

from app.config import settings
from app.tools import ssh_probes as S


def run(coro):
    return asyncio.run(coro)


# ── target allow-list ──
@pytest.mark.parametrize("target", ["gpu-node-a", "node.example.com", "10.0.0.5/24", "", "10.0.0"])
def test_only_an_ip_literal_is_a_target(target):
    assert "not an IP address" in S._refuse_target(target)


@pytest.mark.parametrize("target", ["127.0.0.1", "::1", "169.254.1.1", "0.0.0.0", "224.0.0.1"])
def test_addresses_no_node_can_have_are_refused(target):
    assert "no connection was attempted" in S._refuse_target(target)


def test_a_real_node_address_is_allowed(monkeypatch):
    monkeypatch.setattr(settings, "node_allowed_cidrs", "10.0.0.0/8, 203.0.113.0/24")
    assert S._refuse_target("10.0.0.26") == ""
    assert S._refuse_target("203.0.113.9") == ""


def test_no_allowlist_means_no_node_probes(monkeypatch):
    # Fail closed: an unset NODE_ALLOWED_CIDRS must not let the mounted SSH key reach any host.
    monkeypatch.setattr(settings, "node_allowed_cidrs", "")
    assert "NODE_ALLOWED_CIDRS is not set" in S._refuse_target("10.0.0.26")


def test_the_cidr_allow_list_applies_when_set(monkeypatch):
    monkeypatch.setattr(settings, "node_allowed_cidrs", "10.0.0.0/24, 192.168.0.0/16")
    assert S._refuse_target("10.0.0.26") == ""
    assert S._refuse_target("192.168.4.4") == ""
    assert "outside NODE_ALLOWED_CIDRS" in S._refuse_target("10.1.0.1")


def test_a_refused_target_never_reaches_asyncssh(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("connected")

    monkeypatch.setattr(S.asyncssh, "connect", boom)
    ok, out = run(S._ssh_exec("gpu-node-a", "uname"))
    assert not ok and out.startswith(S.SSH_LAYER_ERROR) and "not an IP" in out


# ── host keys
def _public_key():
    return asyncssh.generate_private_key("ssh-ed25519").convert_to_public()


@pytest.fixture
def known_hosts(tmp_path, monkeypatch):
    path = tmp_path / "ssh" / "known_hosts"
    monkeypatch.setattr(settings, "ssh_known_hosts", str(path))
    monkeypatch.setattr(settings, "ssh_host_key_policy", "tofu")
    fetched = []

    async def get_key(host, port=22):
        fetched.append(host)
        return _public_key()

    monkeypatch.setattr(S.asyncssh, "get_server_host_key", get_key)
    return path, fetched


def test_tofu_records_a_new_node_once(known_hosts):
    path, fetched = known_hosts
    assert run(S._known_hosts_for("10.0.0.5", 5)) == str(path)
    assert path.read_text().startswith("10.0.0.5 ssh-ed25519 ")
    assert fetched == ["10.0.0.5"]
    run(S._known_hosts_for("10.0.0.5", 5))
    assert fetched == ["10.0.0.5"] and path.read_text().count("10.0.0.5") == 1
    if os.name == "posix":
        assert (path.stat().st_mode & 0o777) == 0o600


def test_strict_refuses_a_node_it_has_not_recorded(known_hosts, monkeypatch):
    path, fetched = known_hosts
    monkeypatch.setattr(settings, "ssh_host_key_policy", "strict")
    run(S._known_hosts_for("10.0.0.5", 5))
    assert not path.exists() and not fetched


def test_an_unknown_policy_is_an_error_not_a_default(monkeypatch):
    monkeypatch.setattr(settings, "ssh_host_key_policy", "yolo")
    with pytest.raises(ValueError, match="tofu"):
        run(S._known_hosts_for("10.0.0.5", 5))


def test_a_changed_key_is_refused_with_the_reset_step(known_hosts, monkeypatch):
    path, _ = known_hosts

    class _Changed:
        async def __aenter__(self):
            raise asyncssh.HostKeyNotVerifiable("changed")

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(S.asyncssh, "connect", lambda *a, **k: _Changed())
    monkeypatch.setattr(settings, "node_allowed_cidrs", "10.0.0.0/24")
    ok, out = run(S._ssh_exec("10.0.0.5", "uname", key_path="/nonexistent"))
    assert not ok and "not the host recorded" in out and "deleting its line" in out
    assert str(path) in out


def test_the_default_user_needs_no_database(monkeypatch):
    monkeypatch.setattr(settings, "node_ssh_user", "nodeuser")
    assert run(S._resolve_node_user("10.0.0.5")) == "nodeuser"
    # The caller never picks the SSH user (no override parameter on any probe).
    for t in (S.check_ssh_connectivity, S.check_node_docker, S.node_exec, S.node_docker_logs):
        assert "username" not in t.args, t.name


# ── output is streamed and capped, not buffered whole
class _Proc:
    """A fake SSHClientProcess: stdout from `chunks`, stderr silent until the channel closes."""

    def __init__(self, chunks, exit_status=None, stderr=""):
        self.chunks, self.exit_status, self.reads = iter(chunks), exit_status, 0
        self._closed = asyncio.Event()
        self._stderr = stderr
        proc = self

        class _Out:
            async def read(self, n=-1):
                proc.reads += 1
                return "" if proc._closed.is_set() else next(proc.chunks, "")

        class _Err:
            async def read(self, n=-1):
                if proc._stderr:
                    err, proc._stderr = proc._stderr, ""
                    return err
                if proc.exit_status is None:
                    await proc._closed.wait()   # a still-running command: no EOF until we close
                return ""

        self.stdout, self.stderr = _Out(), _Err()

    def close(self):
        self._closed.set()

    async def wait_closed(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.close()


def _fake_node(monkeypatch, proc):
    class _Conn:
        async def create_process(self, command):
            return proc

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    async def known_hosts(node_ip, timeout):
        return "/nonexistent"

    monkeypatch.setattr(S.asyncssh, "connect", lambda *a, **k: _Conn())
    monkeypatch.setattr(S, "_known_hosts_for", known_hosts)
    monkeypatch.setattr(settings, "node_allowed_cidrs", "10.0.0.0/24")


def test_endless_output_is_cut_at_the_cap_not_buffered(monkeypatch):
    import itertools
    proc = _Proc(itertools.repeat("x" * 65536))      # cat /proc/kpageflags: effectively endless
    _fake_node(monkeypatch, proc)
    ok, out = run(S._ssh_exec("10.0.0.5", "cat /proc/kpageflags", key_path="/nonexistent"))
    assert ok and out.startswith("x" * 1000) and "truncated" in out
    assert len(out) < S._MAX_OUTPUT + 200
    assert proc._closed.is_set() and proc.reads <= S._MAX_OUTPUT // 65536 + 2


def test_a_finished_command_keeps_the_old_result_shape(monkeypatch):
    _fake_node(monkeypatch, _Proc(["hello\n"], exit_status=0))
    assert run(S._ssh_exec("10.0.0.5", "uname", key_path="/nonexistent")) == (True, "hello")
    _fake_node(monkeypatch, _Proc([], exit_status=2, stderr="boom\n"))
    ok, out = run(S._ssh_exec("10.0.0.5", "uname", key_path="/nonexistent"))
    assert not ok and out == "Exit code 2. stderr: boom. stdout: "
