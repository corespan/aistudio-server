"""Network diagnostic tools."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import httpx
import structlog
from cryptography import x509
from langchain_core.tools import tool

from app.config import settings
from app.tools._shell import option_like, run_command
from app.tools.ssh_probes import _resolve_node_user, _run_ssh_command

logger = structlog.get_logger()


async def _dns(host: str) -> str:
    try:
        infos = await asyncio.get_event_loop().getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        return f"DNS: [FAIL] cannot resolve {host!r}: {exc}"
    ips = sorted({info[4][0] for info in infos})
    return f"DNS: [OK] {host} → {', '.join(ips)}"


_TCP_FAIL_IS_AMBIGUOUS = (
    ". A listener bound to 127.0.0.1 accepts connections over loopback and rejects "
    "every other address, so a service can answer locally and be unreachable from "
    "another host with nothing wrong in between; a filtered path and a missing "
    "process produce this same line. list_listening_ports shows which address each "
    "listener is actually bound to on this host."
)


async def _tcp(host: str, port: int, timeout: float) -> str:
    refused, ips = await _vet(host)
    if refused:
        return f"TCP {host}:{port}: [REFUSED] {refused}"
    start = time.monotonic()
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(ips[0], port), timeout=timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return f"TCP {host}:{port}: [OK] reachable ({int((time.monotonic() - start) * 1000)} ms)"
    except asyncio.TimeoutError:
        return (f"TCP {host}:{port}: [FAIL] timed out after {timeout}s"
                f"{_TCP_FAIL_IS_AMBIGUOUS}")
    except ConnectionRefusedError:
        return (f"TCP {host}:{port}: [FAIL] refused — the SYN reached a host that had "
                f"no listener on this address{_TCP_FAIL_IS_AMBIGUOUS}")
    except (socket.gaierror, OSError) as exc:
        return f"TCP {host}:{port}: [FAIL] {exc}"


def _docker_api_host() -> str:
    """The Docker API's host when DOCKER_HOST is tcp://, else ""."""
    parsed = urlparse(settings.docker_host)
    return (parsed.hostname or "") if parsed.scheme in ("tcp", "http", "https") else ""


async def _addresses(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return list(dict.fromkeys(i[4][0].split("%")[0] for i in infos))


def _unmapped(raw: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    ip = ipaddress.ip_address(raw)
    return getattr(ip, "ipv4_mapped", None) or ip     # ::ffff:127.0.0.1 is 127.0.0.1


# Metadata services that are not link-local: AWS IMDS over IPv6, Alibaba Cloud.
_METADATA = {ipaddress.ip_address("fd00:ec2::254"), ipaddress.ip_address("100.100.100.200")}


async def _vet(host: str) -> tuple[str | None, list[str]]:
    """(refusal or None, addresses); callers connect to these, never re-resolve (rebinding)."""
    if not host:
        return "no host given", []
    docker = _docker_api_host()
    try:
        ips = await _addresses(host)
    except socket.gaierror as exc:
        return f"cannot resolve {host!r}: {exc}", []
    if not ips:
        return f"cannot resolve {host!r}: no addresses", []
    try:
        docker_ips = {_unmapped(a) for a in await _addresses(docker)} if docker else set()
    except socket.gaierror as exc:
        # Still refused (the target could be the Docker API), but the name that failed is DOCKER_HOST's.
        return f"cannot resolve the Docker API host {docker!r} (DOCKER_HOST) to rule it out: {exc}", []
    for raw in ips:
        ip = _unmapped(raw)
        if ip.is_link_local or ip.is_loopback or ip.is_unspecified or ip.is_multicast:
            return f"{host} resolves to {ip}, a link-local/loopback address (metadata, local admin)", ips
        if ip in _METADATA:
            return f"{host} resolves to {ip}, a cloud metadata service address", ips
        if ip in docker_ips:   # compared unmapped: [::ffff:10.0.0.1] is the same host as 10.0.0.1
            return f"{host} is the Docker API, whose responses carry every container's secrets", ips
    return None, ips


async def _vet_url(url: str) -> tuple[str | None, list[str]]:
    """(refusal or None, addresses) for a URL; fetch from these addresses only."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"only http(s) URLs are fetched, not {parsed.scheme or 'no scheme'!r}", []
    if not parsed.hostname:
        return "the URL has no host", []
    return await _vet(parsed.hostname)


async def blocked_url(url: str) -> str | None:
    """Return why the URL must not be fetched (metadata, loopback, Docker API), or None."""
    return (await _vet_url(url))[0]


def _pinned(url: str, ip: str) -> tuple[str, dict, dict]:
    """`url` aimed at the vetted `ip`: (url, headers, extensions) keeping Host and TLS SNI/verification."""
    parsed = urlparse(url)
    host = f"[{ip}]" if ":" in ip else ip
    netloc = host + (f":{parsed.port}" if parsed.port else "")
    headers = {"Host": parsed.netloc.rsplit("@", 1)[-1]}
    extensions = {"sni_hostname": parsed.hostname} if parsed.scheme == "https" else {}
    return parsed._replace(netloc=netloc).geturl(), headers, extensions


_BODY_CAP = 65536   # bytes read of a probed body; only 200 characters are shown


async def _http(url: str, timeout: float) -> str:
    # One resolution, vetted and then used: a second lookup could answer differently (rebinding).
    refused, ips = await _vet_url(url)
    if refused:
        return f"HTTP {url}: [REFUSED] {refused}"
    target, headers, extensions = _pinned(url, ips[0])

    async def fetch() -> tuple[httpx.Response, bytes]:
        # Streamed and capped: a huge body is never read, and the stream closes early.
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            async with client.stream("GET", target, headers=headers, extensions=extensions) as resp:
                body = b""
                async for chunk in resp.aiter_bytes():
                    body += chunk
                    if len(body) > _BODY_CAP:
                        break
                return resp, body

    start = time.monotonic()
    try:
        # httpx's timeout is per read, so a server trickling bytes would never trip it.
        resp, body = await asyncio.wait_for(fetch(), timeout)
        ms = int((time.monotonic() - start) * 1000)
        ctype = resp.headers.get("content-type", "?").split(";")[0].strip() or "?"
        size = f"≥{_BODY_CAP}" if len(body) > _BODY_CAP else str(len(body))
        snippet = " ".join(body[:800].decode(resp.encoding or "utf-8", "replace")[:200].split())
        line = f"HTTP {url}: [{resp.status_code}] {ctype} ({ms} ms, {size} bytes)"
        if snippet:
            line += f"\n    body: {snippet}"
        return line
    except httpx.ConnectError as exc:
        return f"HTTP {url}: [FAIL] cannot connect: {exc}"
    except (httpx.TimeoutException, asyncio.TimeoutError):
        return f"HTTP {url}: [FAIL] timed out after {timeout}s"
    except Exception as exc:
        return f"HTTP {url}: [FAIL] {exc}"


async def _ping(host: str) -> str:
    if option_like(host):
        return f"PING: refused — {host!r} is a ping option, not a host."
    refused, ips = await _vet(host)
    if refused:
        return f"PING {host}: [REFUSED] {refused}"
    rc, out, err = await run_command("ping", "-c", "3", "-w", "8", ips[0], timeout=11)
    if rc == 127:
        return "PING: (ping not installed in image)"
    tail = out.strip().splitlines()[-2:] if out.strip() else [err.strip()]
    verdict = "[OK]" if rc == 0 else "[FAIL] (note: ICMP is often firewalled — not proof host is down)"
    return f"PING {host}: {verdict}\n  " + "\n  ".join(tail)


@tool
async def network_check(host: str = "", port: int = 0, url: str = "", timeout: int = 0) -> str:
    """Check reachability of `host` (DNS, TCP on `port`, ping) and `url` (HTTP GET); `timeout` is seconds per check."""
    t = max(1, min(timeout or settings.net_probe_timeout, 30))
    if not host and url:
        host = urlparse(url).hostname or ""
    if not host and not url:
        return "Provide at least a host or a url to check."
    logger.info("tool.net.network_check", host=host, port=port, url=url)

    checks: list = []
    if host:
        checks.append(_dns(host))
    if host and port:
        checks.append(_tcp(host, port, t))
    if url:
        checks.append(_http(url, t))
    if host:
        checks.append(_ping(host))

    results = await asyncio.gather(*checks, return_exceptions=True)
    lines = [r if isinstance(r, str) else f"[error] {r}" for r in results]
    return f"Network check for {host or url}:\n" + "\n".join(lines)


@tool
async def list_listening_ports() -> str:
    """List TCP ports listening in this MCP server's own network namespace (not the Docker host's; node_exec `ss -tlnp` reads a GPU node's)."""
    logger.info("tool.net.list_listening_ports")
    rc, out, err = await run_command("ss", "-tlnp")
    if rc == 127:
        return "ss is not installed (add iproute2 to the image)."
    if rc != 0:
        return f"ss failed: {err.strip()}"
    return ("Listening TCP sockets in the aistudio-mcp container's network namespace (the Docker "
            "host's own listeners are not visible from here; list_containers shows published "
            "ports):\n" + out.strip())


@tool
async def tcpdump_capture(
    bpf_filter: str, iface: str = "any", count: int = 100, timeout: int = 15
) -> str:
    """Capture packets matching BPF expression `bpf_filter` on `iface` (up to `count` packets, `timeout` seconds); last resort."""
    count = max(1, min(count, 500))
    timeout = max(1, min(timeout, 60))
    if not bpf_filter.strip():
        return "Refused: provide a BPF filter (e.g. 'host X and port Y') — don't capture everything."
    bad = option_like(*bpf_filter.split(), iface)
    if bad:
        return (f"Refused: {bad!r} is a tcpdump option, not filter syntax. A BPF filter "
                "is made of expressions like 'host 10.0.0.1 and port 8002'.")
    logger.info("tool.net.tcpdump", iface=iface, filter=bpf_filter, count=count)
    # -l keeps packets seen before a timeout kill. -q and a 96-byte snaplen keep it to headers:
    # by default tcpdump decodes HTTP request lines, DNS names and other payload summaries.
    rc, out, err = await run_command(
        "tcpdump", "-l", "-q", "-s", "96", "-i", iface, "-c", str(count), "-nn", "-tttt",
        *bpf_filter.split(),
        timeout=timeout,
    )
    if rc == 127:
        return "tcpdump is not installed in the agent image."
    if rc != 0 and "permission" in err.lower():
        return "tcpdump: permission denied — the container needs the NET_RAW capability."
    if rc == 124:
        if not out.strip():
            return f"tcpdump captured no packets matching {bpf_filter!r} in {timeout}s."
        return (f"tcpdump ({bpf_filter}): {len(out.strip().splitlines())} pkt in the {timeout}s "
                f"window (fewer than {count}):\n{out.strip()}")
    body = out.strip() or err.strip()
    return f"tcpdump ({bpf_filter}), up to {count} pkt:\n{body}" if body else \
        "tcpdump captured no packets in the window (no matching traffic)."


_TLS_IS_NOT_REACHABILITY = (
    "\n  Reading this chain required a TCP connection and a TLS handshake, so the "
    "listener is up and answering. A plain HTTP probe against the same port reports "
    "a rejected handshake as 'cannot connect', which is the same text an unreachable "
    "host produces — the two are separable only by whether a certificate came back. "
    "Validity windows above are compared against this container's own clock; "
    "check_clock_skew separates a genuinely expired certificate from a local clock "
    "that is wrong."
)


def _read_cert(host: str, ip: str, port: int, timeout: float) -> tuple[bytes | None, str, str]:
    """Return (DER bytes, TLS version, error) from `ip`, presenting `host`, without validating the chain."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((ip, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                return tls.getpeercert(binary_form=True), tls.version() or "?", ""
    except Exception as exc:
        return None, "", f"{type(exc).__name__}: {exc}"


def _validate_cert(host: str, ip: str, port: int, timeout: float) -> str:
    """Return "" if the default trust store accepts the chain, else the reason."""
    try:
        with socket.create_connection((ip, port), timeout=timeout) as sock:
            with ssl.create_default_context().wrap_socket(sock, server_hostname=host):
                return ""
    except ssl.SSLCertVerificationError as exc:
        return f"{exc.verify_message} (openssl code {exc.verify_code})"
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


def _cert_lines(der: bytes, now: datetime) -> list[str]:
    cert = x509.load_der_x509_certificate(der)
    subject, issuer = cert.subject.rfc4514_string(), cert.issuer.rfc4514_string()
    start, end = cert.not_valid_before_utc, cert.not_valid_after_utc
    lines = [
        f"  subject:   {subject}",
        f"  issuer:    {issuer}"
        + ("   (self-signed: subject == issuer)" if subject == issuer else ""),
        f"  valid:     {start:%Y-%m-%d %H:%M} UTC  ->  {end:%Y-%m-%d %H:%M} UTC",
    ]
    if now > end:
        lines.append(
            f"  EXPIRED {(now - end).days} day(s) ago "
            f"(container clock now: {now:%Y-%m-%d %H:%M} UTC)"
        )
    elif now < start:
        lines.append(
            f"  NOT YET VALID — starts in {(start - now).days} day(s) "
            f"(container clock now: {now:%Y-%m-%d %H:%M} UTC)"
        )
    else:
        lines.append(f"  in window, {(end - now).days} day(s) of validity left")
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        names = [str(n.value) for n in san]
        lines.append(f"  SANs:      {', '.join(names) if names else '(none)'}")
    except x509.ExtensionNotFound:
        lines.append("  SANs:      (no SubjectAlternativeName extension)")
    return lines


@tool
async def check_tls_cert(host: str, port: int = 443) -> str:
    """Inspect the TLS certificate of `host`:`port` (expiry, trust, hostname match)."""
    if not host.strip():
        return "Provide a host to check."
    port = max(1, min(port, 65535))
    t = float(settings.net_probe_timeout)
    logger.info("tool.net.check_tls_cert", host=host, port=port)
    refused, ips = await _vet(host.strip())
    if refused:
        return f"TLS certificate for {host}:{port}: [REFUSED] {refused}"

    der, version, err = await asyncio.to_thread(_read_cert, host, ips[0], port, t)
    if der is None:
        return (
            f"TLS certificate for {host}:{port}\n"
            f"  no certificate could be read: {err}\n"
            "  Nothing came back from the handshake, so this alone does not separate "
            "an unreachable host from a closed port or a listener speaking something "
            "other than TLS here."
        )

    reason = await asyncio.to_thread(_validate_cert, host, ips[0], port, t)
    out = [f"TLS certificate for {host}:{port}", f"  handshake: {version}"]
    out += _cert_lines(der, datetime.now(timezone.utc))
    out.append(
        f"  trust-store validation: {'PASSED' if not reason else 'FAILED — ' + reason}"
    )
    return "\n".join(out) + _TLS_IS_NOT_REACHABILITY


_SKEW_BREAKS_AUTH_AND_TIMELINES = (
    "\n  Certificate and token validity windows (JWT iat/exp, Kerberos tickets, "
    "signed URLs) are each evaluated against the clock of the host doing the "
    "checking, so an offset larger than a token's lifetime rejects credentials that "
    "are themselves valid. Those rejections arrive as 401/403, not as a time error. "
    "Skew also reorders log lines collected from different hosts, so a merged "
    "timeline can place an effect before its cause."
)


def _skew_line(label: str, remote: float, local: float, noise: float, max_ok: int) -> str:
    offset = remote - local
    tag = "OK" if abs(offset) <= max_ok else "SKEWED"
    detail = (
        f"within +/-{noise:g}s measurement noise"
        if abs(offset) <= noise
        else f"{offset:+.1f}s relative to this container"
    )
    return f"  [{tag}] {label}: {detail}"


async def _api_clock(max_ok: int, timeout: float) -> str:
    """Skew against the API's HTTP Date header."""
    url = settings.aistudio_api_url.rstrip("/") + "/health"
    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
    except Exception as exc:
        return f"  [UNKNOWN] aistudio API ({url}): {type(exc).__name__}: {exc}"
    t1 = time.time()
    raw = resp.headers.get("Date", "")
    if not raw:
        return f"  [UNKNOWN] aistudio API: answered {resp.status_code} with no Date header"
    try:
        server = parsedate_to_datetime(raw).timestamp()
    except Exception:
        return f"  [UNKNOWN] aistudio API: unparseable Date header {raw!r}"
    return _skew_line(
        f"aistudio API ({urlparse(url).hostname})", server, (t0 + t1) / 2, 2.0, max_ok
    )


async def _node_clock(node_ip: str, max_ok: int) -> str:
    user = await _resolve_node_user(node_ip)    # NODE_SSH_USER, like every other node probe
    t0 = time.time()
    ok, out = await _run_ssh_command(node_ip, "date -u +%s", username=user)
    t1 = time.time()
    if not ok:
        first = out.splitlines()[0] if out else "no output"
        return f"  [UNKNOWN] node {node_ip}: clock not read — {first}"
    try:
        remote = float(out.strip().split()[0])
    except (ValueError, IndexError):
        return f"  [UNKNOWN] node {node_ip}: unexpected date output {out.strip()[:60]!r}"
    return _skew_line(f"node {node_ip}", remote, (t0 + t1) / 2, 1.0, max_ok)


_MAX_CLOCK_NODES = 20


@tool
async def check_clock_skew(node_ips: str = "", max_ok_seconds: int = 60) -> str:
    """Compare clocks of this container, the aistudio API and GPU nodes `node_ips` (comma-separated); offsets above `max_ok_seconds` are flagged."""
    max_ok = max(1, max_ok_seconds)
    ips = [ip.strip() for ip in node_ips.split(",") if ip.strip()]
    ips, dropped = ips[:_MAX_CLOCK_NODES], ips[_MAX_CLOCK_NODES:]   # one SSH session each, all at once
    logger.info("tool.net.check_clock_skew", nodes=len(ips), max_ok=max_ok)

    now = datetime.now(timezone.utc)
    checks = [_api_clock(max_ok, float(settings.net_probe_timeout))]
    checks += [_node_clock(ip, max_ok) for ip in ips]
    results = await asyncio.gather(*checks, return_exceptions=True)
    lines = [r if isinstance(r, str) else f"  [error] {r}" for r in results]

    head = (
        f"Clock comparison (reference = this container: "
        f"{now:%Y-%m-%d %H:%M:%S} UTC, flagging offsets over {max_ok}s)"
    )
    tail = (
        "\n  No node addresses were supplied, so only the API clock was compared."
        if not ips
        else ""
    )
    if dropped:
        tail += f"\n  Only the first {_MAX_CLOCK_NODES} nodes were checked; {len(dropped)} more were not."
    return head + "\n" + "\n".join(lines) + tail + _SKEW_BREAKS_AUTH_AND_TIMELINES
