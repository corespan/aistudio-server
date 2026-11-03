"""Diagnostic tools that probe GPU nodes over SSH."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import posixpath
import re
import shlex

import asyncssh
import structlog
from langchain_core.tools import tool

from app.config import settings

logger = structlog.get_logger()

# Tier 1: allowed with any arguments.
_SAFE_ANY_ARGS = frozenset({
    "lscpu", "nproc", "free", "uptime", "df", "uname", "getconf", "nvcc",
    "lspci", "lstopo-no-graphics",
})

# Tier 2: allowed command prefixes, with free-form arguments after them.
_SAFE_PREFIXES: tuple[tuple[str, ...], ...] = tuple(
    tuple(p.split()) for p in (
        "nvidia-smi -L", "nvidia-smi -q", "nvidia-smi topo", "nvidia-smi nvlink",
        "nvidia-smi pmon", "nvidia-smi dmon", "nvidia-smi --list-gpus",
        "nvidia-smi --query-gpu", "nvidia-smi --query-compute-apps",
        "dcgmi discovery", "dcgmi stats", "dcgmi dmon", "dcgmi topo",
        "numactl --hardware", "numactl --show",
        "ip link show", "ip -s link show", "ip addr show", "ip -s addr show",
        "ip -br addr show", "ip route show", "ip neigh show",
        "ss -tlnp", "ss -tunlp", "ss -tn", "ss -s",
        "ethtool -i", "ethtool -k", "ethtool -S", "ethtool -a", "ethtool -c",
        "ethtool -g", "ethtool -T",
        "sensors -A", "sensors -u", "sensors -f",
    )
)

# Commands allowed only with no arguments.
_SAFE_EXACT: frozenset[tuple[str, ...]] = frozenset({("sensors",), ("ss",), ("ip",)})

# Roots `cat` may read.
_CAT_ROOTS = ("/sys/", "/proc/")
# Path parts refused under the cat roots.
_CAT_FORBIDDEN_PARTS = frozenset({
    "root", "cwd", "exe", "fd", "fdinfo", "map_files", "environ", "cmdline", "mem",
    "pagemap", "kcore", "kmem", "kmsg", "efivars",
})

# Shell operators, globs and escapes.
_SHELL_META = re.compile(r"[;&|`$<>()\n\\*?\[\]{}~]|>>")

# Write flags per binary.
_WRITE_FLAGS: dict[str, frozenset[str]] = {
    "ss": frozenset({"-K", "--kill"}),
    "dcgmi": frozenset({"-e", "--enable", "-d", "--disable", "-s", "--jstart", "-x",
                        "--jstop", "-r", "--jremove", "--jremoveall", "--set"}),
    "nvidia-smi": frozenset({"-r", "--gpu-reset", "-pm", "--persistence-mode", "-pl",
                             "--power-limit", "-c", "--compute-mode", "-e", "--ecc-config",
                             "-p", "--reset-ecc-errors", "-lgc", "--lock-gpu-clocks",
                             "-rgc", "--reset-gpu-clocks", "-lmc", "--lock-memory-clocks",
                             "-rmc", "--reset-memory-clocks", "-ac", "--applications-clocks",
                             "-rac", "--reset-applications-clocks", "-mig",
                             "--multi-instance-gpu", "-am", "--accounting-mode", "-caa",
                             "--clear-accounted-apps", "--auto-boost-default",
                             "--auto-boost-permission", "-f", "--filename"}),
    "ethtool": frozenset({"-s", "--change", "-K", "--features", "--offload", "-A",
                          "--pause", "-C", "--coalesce", "-G", "--set-ring", "-E",
                          "--change-eeprom", "-r", "--negotiate", "-p", "--identify",
                          "-f", "--flash", "-L", "--set-channels", "--reset"}),
    "numactl": frozenset(),
    "sensors": frozenset({"-s", "--set", "-c", "--config-file"}),
}


def _has_write_flag(prefix: tuple[str, ...], rest: list[str]) -> str:
    """Return the first write flag after the prefix, or ""."""
    binary = prefix[0]
    bad = _WRITE_FLAGS.get(binary, frozenset())
    sub = prefix[1] if len(prefix) > 1 else ""
    if binary == "dcgmi" and sub != "stats":
        bad = frozenset()
    if binary == "nvidia-smi" and sub in ("dmon", "pmon"):
        bad = bad - {"-c"}
    for t in rest:
        name = t.split("=", 1)[0]
        if name in bad:
            return t
        # Combined short flags containing K.
        if binary == "ss" and t.startswith("-") and not t.startswith("--") and "K" in t:
            return t
    return ""


def _opt_names(tokens: list[str]) -> list[str]:
    """Strip "=value" from each option token."""
    return [t.split("=", 1)[0] for t in tokens]


def _gate(tokens: list[str]) -> str | None:
    """Return None if the argv may run on a node, else the refusal message."""
    binary = os.path.basename(tokens[0])
    rest = tokens[1:]

    if binary in _SAFE_ANY_ARGS:
        return None

    if binary == "cat":
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

    names = _opt_names([binary, *rest])
    if tuple(names) in _SAFE_EXACT:
        return None
    prefix = next((p for p in _SAFE_PREFIXES if names[:len(p)] == list(p)), None)
    if prefix is not None:
        extra = tokens[len(prefix):]
        flag = _has_write_flag(prefix, extra)
        if flag:
            return (f"node_exec: {flag!r} makes {' '.join(prefix)!r} change node state, "
                    f"so it is refused. Rejected: {' '.join(tokens)!r}")
        if binary == "numactl" and any(not t.startswith("-") for t in extra):
            return f"node_exec: numactl takes no program here. Rejected: {' '.join(tokens)!r}"
        return None

    allowed = sorted(
        [" ".join(p) for p in _SAFE_PREFIXES if p[0] == binary]
        + [" ".join(p) + " (no arguments)" for p in _SAFE_EXACT if p[0] == binary]
    )
    if allowed:
        return (
            f"node_exec: {binary!r} has argument forms that change node state, so only "
            f"its read-only ones are allowed: {'; '.join(allowed)}. Rejected: "
            f"{' '.join(tokens)!r}"
        )
    return (
        f"node_exec: {binary!r} is not in the read-only allowlist "
        f"({', '.join(sorted(_SAFE_ANY_ARGS))}, cat under /sys and /proc, and the "
        f"read-only forms of {', '.join(sorted({p[0] for p in _SAFE_PREFIXES}))}). "
        "Use a diagnostic (read-only) command."
    )


# Prefix of every SSH-layer failure message.
SSH_LAYER_ERROR = "SSH connection failed"


def _is_ssh_failure(output: str) -> bool:
    return output.startswith(SSH_LAYER_ERROR)


# RFC 5737 documentation ranges.
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
    """The SSH username: always the configured one. A caller never picks it, so a probe cannot log in as
    another account (root, say) that the mounted key happens to open."""
    return settings.node_ssh_user or settings.ssh_default_user


# Max output characters kept.
_MAX_OUTPUT = 200_000


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
    if not cidrs:   # fail closed: the mounted SSH key must not reach hosts nobody listed
        return "NODE_ALLOWED_CIDRS is not set, so node probes are off: no connection was attempted."
    if not any(ip in ipaddress.ip_network(c, strict=False) for c in cidrs):
        return f"{ip} is outside NODE_ALLOWED_CIDRS, so no connection was attempted."
    return ""


# Serialises recording of first-seen host keys.
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
            result = await asyncio.wait_for(
                conn.run(command, check=False),
                timeout=30,
            )
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

    success, output = await _run_ssh_command(
        node_ip,
        "docker run --rm --gpus all nvidia/cuda:12.0-base nvidia-smi "
        "--query-gpu=name,memory.total,memory.free --format=csv,noheader 2>&1 "
        "| head -5",
        username=user,
    )
    if success:
        results.append(f"  [SUCCESS] NVIDIA Container Toolkit working. GPUs:\n    {output}")
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


# Note appended when no container matches.
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
    # Remote script: resolve the container, print status, then logs.
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
