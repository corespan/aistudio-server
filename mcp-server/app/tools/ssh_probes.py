"""Diagnostic tools that probe GPU nodes over SSH."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import posixpath
import re
import shlex
from dataclasses import dataclass

import asyncssh
import structlog
from langchain_core.tools import tool

from app.config import settings

logger = structlog.get_logger()

# A non-option argument: ids, device names, field lists. Never "/", so never a file name.
_VALUE = re.compile(r"^[A-Za-z0-9_.,:=@+ -]{1,200}$")
_VALUE_OR_PATH = re.compile(r"^(?:/[A-Za-z0-9_./-]{0,200}|[A-Za-z0-9_.,:=@+-]{1,200})$")
_DEVICE = re.compile(r"^[A-Za-z0-9_.:@-]{1,32}$")


@dataclass(frozen=True)
class _Spec:
    """A read-only command form; any option not listed is refused, so unknown flags fail closed."""
    flags: frozenset
    args: re.Pattern | None = None     # None = no arguments
    max_args: int = 10
    combine: bool = False              # short options may be bundled (ss -tlnp)


def _spec(flags: str, args: re.Pattern | None = None, max_args: int = 10,
          combine: bool = False) -> _Spec:
    return _Spec(frozenset(flags.split()), args, max_args, combine)


# Bare names only (a path never runs); every option listed only prints.
_SPECS: dict[tuple[str, str | None], _Spec] = {
    ("lscpu", None): _spec("-a --all -b --online -c --offline -e --extended -p --parse -J --json "
                           "-x --hex -y --physical -B --bytes -C --caches"),
    ("nproc", None): _spec("--all"),
    ("free", None): _spec("-b -k -m -g -h -w -t -l --bytes --kibi --mebi --gibi --human --wide "
                          "--total --lohi --si", combine=True),
    ("uptime", None): _spec("-p -s --pretty --since"),
    ("df", None): _spec("-a -h -H -i -k -l -m -P -T -t -x --all --human-readable --si --inodes "
                        "--local --portability --print-type --type --exclude-type --output",
                        _VALUE_OR_PATH, combine=True),
    ("uname", None): _spec("-a -s -n -r -v -m -p -i -o --all --kernel-name --nodename "
                           "--kernel-release --kernel-version --machine --processor "
                           "--hardware-platform --operating-system", combine=True),
    ("getconf", None): _spec("-a", re.compile(r"^[A-Z0-9_]{1,64}$")),
    ("lspci", None): _spec("-v -n -t -k -D -s -d -x -m -b -P", _VALUE, combine=True),
    ("lstopo-no-graphics", None): _spec("-p -l -v -s --physical --logical --verbose --silent "
                                        "--no-io --no-bridges --whole-io --no-caches --merge"),
    ("nvcc", None): _spec("--version -V"),
    ("nvidia-smi", None): _spec("-L --list-gpus -q --query -x --xml-format -d --display -i --id "
                                "-u --unit --query-gpu --query-compute-apps --format", _VALUE),
    ("nvidia-smi", "topo"): _spec("-m --matrix -mp --matrix_pci -p2p -i --id", _VALUE),
    ("nvidia-smi", "nvlink"): _spec("-s --status -c --capabilities -i --id -l --link", _VALUE),
    ("nvidia-smi", "dmon"): _spec("-i -d -c -s -o", _VALUE),
    ("nvidia-smi", "pmon"): _spec("-i -d -c -s -o", _VALUE),
    ("dcgmi", "discovery"): _spec("-l --list -i --info -g --group -v --verbose", _VALUE),
    ("dcgmi", "dmon"): _spec("-e --field-id -d --delay -c --count -i --gpu-id -g --group-id "
                             "-l --list", _VALUE),
    ("dcgmi", "topo"): _spec("-g --group -i --gpuid -j --json", _VALUE),
    ("dcgmi", "stats"): _spec("-j -p -v -g --verbose", _VALUE),
    ("numactl", None): _spec("-H --hardware -s --show"),
    ("ss", None): _spec("-t -u -l -n -p -a -s -e -i -m -o -x -w -4 -6 -H --tcp --udp --listening "
                        "--numeric --processes --all --summary --extended --info --memory "
                        "--options --unix --raw --ipv4 --ipv6 --no-header", _VALUE, combine=True),
    ("ethtool", None): _spec("-i --driver -k --show-features --show-offload -S --statistics "
                             "-a --show-pause -c --show-coalesce -g --show-ring "
                             "-T --show-time-stamping -l --show-channels -P --show-permaddr "
                             "--show-fec --show-eee", _DEVICE, max_args=1),
    ("sensors", None): _spec("-A --no-adapter -u -f --fahrenheit -j", _VALUE),
}
_HAS_SUBCOMMAND = frozenset({"nvidia-smi", "dcgmi"})

# ip: global options, object, optional show verb, filter values.
_IP_GLOBAL = frozenset("-s -stats -statistics -d -details -br -brief -4 -6 -j -json -p -pretty "
                       "-o -oneline".split())
_IP_OBJECTS = frozenset({"link", "addr", "address", "route", "neigh", "neighbour", "rule"})
_IP_SHOW = frozenset({"show", "list", "ls", "lst"})
_IP_WRITE_VERBS = frozenset({"set", "add", "del", "delete", "change", "replace", "flush",
                             "append", "prepend", "save", "restore"})

_CAT_ROOTS = ("/sys/", "/proc/")
_CAT_FORBIDDEN_PARTS = frozenset({
    "root", "cwd", "exe", "fd", "fdinfo", "map_files", "environ", "cmdline", "mem",
    "pagemap", "kcore", "kmem", "kmsg", "efivars",
})

_SHELL_META = re.compile(r"[;&|`$<>()\n\\*?\[\]{}~]|>>")


def _usage(binary: str) -> str:
    """The read-only forms of `binary`, for a refusal."""
    if binary == "ip":
        return ("ip link show, ip addr show, ip route show, ip neigh show, ip rule show "
                f"(options before the object: {' '.join(sorted(_IP_GLOBAL))})")
    if binary == "cat":
        return "cat <path under /sys or /proc>"
    return "; ".join(f"{b}{' ' + s if s else ''} [{' '.join(sorted(spec.flags))}]"
                     for (b, s), spec in _SPECS.items() if b == binary)


def _check_args(spec: _Spec, rest: list[str]) -> str:
    """Return the first argument `spec` does not allow, or ""."""
    n_args = 0
    for t in rest:
        if t.startswith("-") and t != "-":
            name, eq, value = t.partition("=")
            if eq and not _VALUE.fullmatch(value):
                return t
            if name in spec.flags:
                continue
            if (spec.combine and not t.startswith("--") and not eq
                    and all(f"-{c}" in spec.flags for c in t[1:])):
                continue
            return t
        n_args += 1
        if spec.args is None or n_args > spec.max_args or not spec.args.fullmatch(t):
            return t
    return ""


def _gate_ip(rest: list[str]) -> str:
    """Return the first token that makes this `ip` call more than a show, or ""."""
    i = 0
    while i < len(rest) and rest[i] in _IP_GLOBAL:
        i += 1
    if i >= len(rest) or rest[i] not in _IP_OBJECTS:
        return rest[i] if i < len(rest) else "(no object)"
    i += 1
    if i < len(rest) and rest[i] in _IP_SHOW:
        i += 1
    return next((t for t in rest[i:] if t.startswith("-") or not _VALUE.fullmatch(t)
                 or t in _IP_WRITE_VERBS), "")


def _gate_cat(rest: list[str]) -> str | None:
    if not rest:
        return "node_exec: `cat` needs a file path."
    for path in rest:
        if path.startswith("-"):
            return f"node_exec: `cat` takes plain paths, not options ({path!r})."
        norm = posixpath.normpath(path)
        if not norm.startswith(_CAT_ROOTS):
            return (
                f"node_exec: `cat` is limited to {' and '.join(_CAT_ROOTS)} on a node "
                f"(refused {path!r}). Those hold the NIC/GPU/CPU counters this probe "
                "is for; read_file reads ordinary files, under its own allowed roots."
            )
        bad = _CAT_FORBIDDEN_PARTS.intersection(norm.split("/"))
        if bad:
            return (
                f"node_exec: `cat` refused {path!r}: {sorted(bad)[0]!r} leads out of "
                "the counters (to the node's own filesystem or another process's "
                "memory, environment or arguments)."
            )
    return None


def _gate(tokens: list[str]) -> str | None:
    """Return None if the argv may run on a node, else the refusal message."""
    binary, rest = tokens[0], tokens[1:]
    if binary == "cat":
        return _gate_cat(rest)
    known = sorted({b for b, _ in _SPECS} | {"cat", "ip"})
    if binary == "ip":
        bad = _gate_ip(rest)
    else:
        sub = None
        if binary in _HAS_SUBCOMMAND and rest and not rest[0].startswith("-"):
            sub, rest = rest[0], rest[1:]
        spec = _SPECS.get((binary, sub))
        if spec is None:
            if binary in known:
                return (f"node_exec: `{binary}{' ' + sub if sub else ''}` is not one of its "
                        f"read-only forms ({_usage(binary)}). Rejected: {' '.join(tokens)!r}")
            return (f"node_exec: {binary!r} is not in the read-only allowlist "
                    f"({', '.join(known)}; called by bare name, not by path). "
                    "Use a diagnostic (read-only) command.")
        bad = _check_args(spec, rest)
    if bad:
        return (f"node_exec: {bad!r} is not a read-only argument of {binary} here, so the "
                f"command is refused. Read-only forms: {_usage(binary)}. "
                f"Rejected: {' '.join(tokens)!r}")
    return None


SSH_LAYER_ERROR = "SSH connection failed"


def _is_ssh_failure(output: str) -> bool:
    return output.startswith(SSH_LAYER_ERROR)


_DOC_RANGES = tuple(
    ipaddress.ip_network(n)
    for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")
)


def _unroutable_note(node_ip: str) -> str:
    """Return a note explaining an address that can never be a real node, or ""."""
    try:
        ip = ipaddress.ip_address(node_ip)
    except ValueError:
        return (
            f" NOTE: {node_ip!r} is not a valid IP address, so no connection was "
            "attempted. An address that cannot be parsed reaches nothing; whatever "
            "supplied it recorded a malformed value."
        )
    kind = None
    if any(ip in net for net in _DOC_RANGES):
        kind = ("in a range reserved for documentation and examples (RFC 5737), "
                "which is deliberately not routable")
    elif ip.is_loopback:
        kind = ("a loopback address — it refers to whichever machine is asking, so "
                "it can never reach a separate node")
    elif ip.is_link_local:
        kind = ("a link-local address (RFC 3927), typically what a host assigns "
                "itself when DHCP fails — not a routable node address")
    elif ip.is_unspecified or ip.is_multicast or ip.is_reserved:
        kind = "not a valid unicast host address"
    if not kind:
        return ""
    return (
        f" NOTE: {node_ip} is {kind}. No host can be reached at this address, so this "
        "probe failure is expected and carries no information about node health. An "
        "address in this range means the node record holds a placeholder or "
        "misconfigured value rather than a real machine. Restarting the node, "
        "reinstalling drivers, or retrying the workload cannot change this outcome; "
        "only correcting the recorded address can."
    )


async def _resolve_node_user(node_ip: str) -> str:
    """Always the configured user: a caller never picks it."""
    return settings.node_ssh_user or settings.ssh_default_user


_MAX_OUTPUT = 200_000
# Seconds a command may run once connected.
_RUN_TIMEOUT = 30


def _capped(text: str | None) -> str:
    text = (text or "").strip()
    if len(text) <= _MAX_OUTPUT:
        return text
    return text[:_MAX_OUTPUT] + f"\n… ({len(text) - _MAX_OUTPUT} more character(s) truncated)"


def _refuse_target(node_ip: str) -> str:
    """Return why a probe may not connect to this address, or "" when it may."""
    try:
        ip = ipaddress.ip_address(node_ip.strip())
    except ValueError:
        return f"{node_ip!r} is not an IP address, and node probes take an IP only."
    if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast:
        return f"{ip} is not an address a GPU node can have, so no connection was attempted."
    cidrs = [c.strip() for c in settings.node_allowed_cidrs.split(",") if c.strip()]
    if not cidrs:   # fail closed
        return "NODE_ALLOWED_CIDRS is not set, so node probes are off: no connection was attempted."
    if not any(ip in ipaddress.ip_network(c, strict=False) for c in cidrs):
        return f"{ip} is outside NODE_ALLOWED_CIDRS, so no connection was attempted."
    return ""


_known_hosts_lock = asyncio.Lock()


async def _known_hosts_for(node_ip: str, timeout: int) -> str:
    """Return the known_hosts path for this node, recording its key on first use under the tofu policy."""
    policy = settings.ssh_host_key_policy.strip().lower()
    if policy not in ("tofu", "strict"):
        raise ValueError(f"SSH_HOST_KEY_POLICY must be 'tofu' or 'strict', not {policy!r}")
    path = os.path.expanduser(settings.ssh_known_hosts)
    async with _known_hosts_lock:
        known = os.path.exists(path) and asyncssh.read_known_hosts(path).match(node_ip, node_ip, 22)[0]
        if not known and policy == "tofu":
            key = await asyncio.wait_for(asyncssh.get_server_host_key(node_ip, 22), timeout=timeout)
            if key is None:
                raise OSError(f"{node_ip} offered no host key")
            os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(f"{node_ip} {key.export_public_key('openssh').decode().strip()}\n")
            os.chmod(path, 0o600)
            logger.info("ssh.host_key_recorded", node_ip=node_ip, path=path)
    return path


async def _ssh_exec(
    node_ip: str,
    command: str,
    timeout: int | None = None,
    username: str | None = None,
    key_path: str | None = None,
) -> tuple[bool, str]:
    """Run a single command over SSH and return (success, output)."""
    connect_timeout = timeout or settings.ssh_connect_timeout
    user = username or settings.ssh_default_user
    key = os.path.expanduser(key_path or settings.ssh_key_path)
    refusal = _refuse_target(node_ip)
    if refusal:
        logger.warning("ssh.target_refused", node_ip=node_ip, reason=refusal)
        return False, f"{SSH_LAYER_ERROR}: {refusal}"
    logger.info("ssh.command_start", node_ip=node_ip, username=user, command=command)
    try:
        known_hosts = await _known_hosts_for(node_ip, connect_timeout)
        async with asyncssh.connect(
            node_ip,
            username=user,
            client_keys=[key],
            known_hosts=known_hosts,
            connect_timeout=connect_timeout,
        ) as conn:
            try:
                result = await asyncio.wait_for(conn.run(command, check=False),
                                                timeout=_RUN_TIMEOUT)
            except asyncio.TimeoutError:
                # Not an SSH-layer failure: the node answered.
                return False, f"the command did not finish within {_RUN_TIMEOUT}s"
            if result.exit_status == 0:
                logger.debug("ssh.command_success", node_ip=node_ip)
                return True, _capped(result.stdout)
            else:
                stderr = _capped(result.stderr)
                stdout = _capped(result.stdout)
                logger.warning("ssh.command_failed", node_ip=node_ip, exit_status=result.exit_status)
                return False, f"Exit code {result.exit_status}. stderr: {stderr}. stdout: {stdout}"

    except asyncssh.HostKeyNotVerifiable:
        logger.error("ssh.host_key_mismatch", node_ip=node_ip)
        return False, (
            f"{SSH_LAYER_ERROR}: {node_ip} is not the host recorded in "
            f"{settings.ssh_known_hosts} (unknown under strict, or its key changed). A "
            "changed key means either a re-imaged node or something answering for its "
            "address. An admin accepts a re-imaged node by deleting its line from that file."
        )
    except ValueError as exc:
        return False, f"{SSH_LAYER_ERROR}: {exc}"
    except asyncssh.PermissionDenied:
        logger.error("ssh.permission_denied", node_ip=node_ip, username=user)
        return False, (
            f"{SSH_LAYER_ERROR}: permission denied for {user}@{node_ip}. "
            f"The SSH key may not be authorized for user {user!r} on this node."
        )
    except (OSError, asyncssh.DisconnectError, ConnectionRefusedError) as exc:
        return False, (
            f"{SSH_LAYER_ERROR}: cannot connect to {node_ip}: {exc}. "
            "The node may be down, unreachable, or SSH daemon not running."
        )
    except asyncio.TimeoutError:
        return False, (
            f"{SSH_LAYER_ERROR}: timed out connecting to {node_ip} after {connect_timeout}s. "
            "The node may be unreachable (network/firewall issue)."
        )
    except Exception as exc:
        return False, f"{SSH_LAYER_ERROR}: unexpected {type(exc).__name__}: {exc}"


async def _run_ssh_command(
    node_ip: str,
    command: str,
    timeout: int | None = None,
    username: str | None = None,
    key_path: str | None = None,
) -> tuple[bool, str]:
    """Run `_ssh_exec`, adding a note when the address is unroutable."""
    ok, output = await _ssh_exec(node_ip, command, timeout, username, key_path)
    if not ok and _is_ssh_failure(output):
        output += _unroutable_note(node_ip)
    return ok, output


@tool
async def check_ssh_connectivity(node_ip: str) -> str:
    """Test SSH connectivity to GPU node `node_ip`."""
    logger.info("tool.ssh.check_connectivity", node_ip=node_ip)
    user = await _resolve_node_user(node_ip)
    success, output = await _run_ssh_command(node_ip, "echo OK", username=user)
    if success and "OK" in output:
        return f"[SUCCESS] SSH to {node_ip} is working. Connection successful."
    else:
        return f"[FAILED] SSH to {node_ip} failed. Error: {output}"


@tool
async def check_node_docker(node_ip: str) -> str:
    """Check Docker, the NVIDIA Container Toolkit and running containers on GPU node `node_ip`."""
    logger.info("tool.ssh.check_node_docker", node_ip=node_ip)
    user = await _resolve_node_user(node_ip)
    results: list[str] = [f"Docker diagnostic for node {node_ip}:"]

    success, output = await _run_ssh_command(
        node_ip, "docker info --format '{{.ServerVersion}}'", username=user
    )
    if success:
        results.append(f"  [SUCCESS] Docker daemon running (version {output})")
    elif _is_ssh_failure(output):
        results.append(f"  [UNKNOWN] Node not reachable over SSH: {output}")
        results.append(
            "  → Docker state is UNKNOWN — no command ran on the node. "
            f"Fix: check SSH access for {user}@{node_ip} (key present in "
            f"~{user}/.ssh/authorized_keys on the node, NODE_SSH_USER correct "
            "for this node, node up)."
        )
        return "\n".join(results)
    else:
        results.append(f"  [FAILED] Docker daemon NOT running: {output}")
        results.append(
            "  → Fix: Run 'sudo systemctl start docker' on the node."
        )
        return "\n".join(results)

    # Not a test container: that would pull an image and grab every GPU mid-run.
    success, output = await _run_ssh_command(
        node_ip, "nvidia-container-cli info 2>&1 | head -20", username=user
    )
    if success:
        results.append(f"  [SUCCESS] NVIDIA Container Toolkit sees the GPUs:\n    {output}")
    elif _is_ssh_failure(output):
        results.append(f"  [UNKNOWN] NVIDIA Container Toolkit not checked: {output}")
    else:
        results.append(
            f"  [FAILED] NVIDIA Container Toolkit issue: {output}"
        )

    success, output = await _run_ssh_command(
        node_ip,
        "docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}' 2>&1 "
        "| head -15",
        username=user,
    )
    if success:
        results.append(f"  Running containers:\n    {output}")
    elif _is_ssh_failure(output):
        results.append(f"  Container list UNKNOWN: {output}")
    else:
        results.append(f"  Could not list containers: {output}")

    return "\n".join(results)


@tool
async def node_exec(node_ip: str, command: str) -> str:
    """Run one read-only command on GPU node `node_ip` over SSH (e.g. `nvidia-smi topo -m`, `nvidia-smi -q`, `numactl --hardware`, `lscpu`, `lspci -tv`, `cat /sys/class/net/eth0/speed`, `ethtool eth0`); no shell chaining, `cat` only under /sys and /proc."""
    logger.info("tool.ssh.node_exec", node_ip=node_ip, command=command)
    cmd = command.strip()
    if not cmd:
        return "node_exec: empty command."
    if _SHELL_META.search(cmd):
        return (
            "node_exec: shell operators (; | & ` $() <> ) are not allowed — run a "
            "single read-only command and read its full output. "
            f"Rejected: {command!r}"
        )
    try:
        tokens = shlex.split(cmd)
    except ValueError as exc:
        return f"node_exec: could not parse command ({exc}): {command!r}"
    if not tokens:
        return "node_exec: empty command."
    refusal = _gate(tokens)
    if refusal:
        logger.warning("tool.ssh.node_exec.refused", node_ip=node_ip, command=cmd)
        return refusal
    user = await _resolve_node_user(node_ip)
    success, output = await _run_ssh_command(node_ip, shlex.join(tokens), username=user,
                                             timeout=20)
    if not success:
        if _is_ssh_failure(output):
            return (
                f"[node {node_ip}] `{cmd}` did NOT run — {output} "
                "Nothing was observed about the node."
            )
        return f"[node {node_ip}] `{cmd}` failed (user {user}): {output}"
    return f"[node {node_ip}] `{cmd}`:\n{output}" if output else f"[node {node_ip}] `{cmd}`: (no output)"


_CONTAINER_REAPED = (
    "Note: a node lists only the containers it still holds, and it does not keep them "
    "indefinitely. An absence here has two readings — a container that ran and was "
    "later reaped, and a run that failed before any container existed. get_task_logs("
    "task_id) reads aistudio-server's archived log stream, which is retained after the "
    "container is gone; workload_history records which stage the run reached."
)


@tool
async def node_docker_logs(
    node_ip: str,
    container: str,
    tail: int = 50,
    since_minutes: int = 30,
) -> str:
    """Fetch status and logs of container `container` (name or substring, e.g. the workload id) on GPU node `node_ip`: last `tail` lines from the last `since_minutes` minutes."""
    logger.info("tool.ssh.node_docker_logs", node_ip=node_ip, container=container)
    user = await _resolve_node_user(node_ip)
    tail = max(1, min(int(tail), 2000))
    since_minutes = max(1, min(int(since_minutes), 60 * 24 * 7))
    cq = shlex.quote(container)
    remote = (
        f"c=$(docker ps -a --filter name={cq} --format '{{{{.Names}}}}' | head -1); "
        f'if [ -z "$c" ]; then echo NO_MATCH; '
        f"docker ps -a --format '{{{{.Names}}}}\\t{{{{.Status}}}}' | head -20; exit 0; fi; "
        f'echo "container: $c"; '
        f"docker inspect --format 'status={{{{.State.Status}}}} exit={{{{.State.ExitCode}}}} "
        f"oom={{{{.State.OOMKilled}}}} restarts={{{{.RestartCount}}}}' \"$c\"; "
        f'echo "--- logs ---"; '
        f'docker logs --tail {int(tail)} --since {int(since_minutes)}m "$c" 2>&1 | tail -{int(tail)}'
    )
    success, output = await _run_ssh_command(node_ip, remote, username=user, timeout=20)
    if not success:
        if _is_ssh_failure(output):
            return (
                f"[node {node_ip}] container {container!r} NOT checked — {output} "
                "Nothing was observed about the container."
            )
        return f"[node {node_ip}] could not read container logs (user {user}): {output}"
    if output.startswith("NO_MATCH"):
        return (
            f"[node {node_ip}] no container matching {container!r}. Containers on the node:\n"
            + output[len("NO_MATCH"):].strip()
            + f"\n\n{_CONTAINER_REAPED}"
        )

    error_lines = [
        ln for ln in output.splitlines()
        if any(kw in ln.lower() for kw in
               ("error", "fail", "exception", "traceback", "refused", "denied",
                "oom", "killed", "critical", "fatal", "cuda"))
    ]
    parts = [f"[node {node_ip}] {container}:"]
    if error_lines:
        parts.append(f"⚠️  {len(error_lines)} error/warning line(s):")
        parts += [f"  • {el}" for el in error_lines[-12:]]
        parts.append("--- full tail ---")
    parts.append(output)
    return "\n".join(parts)
