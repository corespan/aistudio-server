"""net_probes hardening: capped/timed HTTP bodies, metadata denylist, DOCKER_HOST errors, node fan-out. Offline."""

from __future__ import annotations

import asyncio
import socket

import httpx
import pytest

from app.config import settings
from app.tools import net_probes
from app.tools.net_probes import _BODY_CAP, _http, blocked_url, check_clock_skew, tcpdump_capture


@pytest.fixture
def public_name(monkeypatch):
    """Every name resolves to one public address; no Docker API over TCP."""
    async def addresses(host):
        return ["93.184.216.34"]

    monkeypatch.setattr(net_probes, "_addresses", addresses)
    monkeypatch.setattr(settings, "docker_host", "unix:///var/run/docker.sock")


def _serve(monkeypatch, content):
    """Route _http's client through a MockTransport answering 200 with `content`."""
    real = httpx.AsyncClient
    mock = httpx.MockTransport(lambda req: httpx.Response(200, content=content(),
                                                          headers={"content-type": "text/plain"}))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=mock, **kw))


async def test_the_http_probe_sends_to_the_address_it_vetted(monkeypatch):
    # Same check as test_fetch_targets' rebinding test, patched at send() (the stream path skips get()).
    answers = iter([["93.184.216.34"], ["169.254.169.254"]])

    async def addresses(host):
        return next(answers)

    sent = []

    async def send(self, request, **kw):
        sent.append(str(request.url))
        raise httpx.ConnectError("stop")

    monkeypatch.setattr(net_probes, "_addresses", addresses)
    monkeypatch.setattr(settings, "docker_host", "unix:///var/run/docker.sock")
    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    await _http("http://rebind.test/latest/meta-data/", 1.0)
    assert sent == ["http://93.184.216.34/latest/meta-data/"], sent


async def test_the_http_probe_stops_reading_at_the_cap(monkeypatch, public_name):
    pulled = []

    async def huge():
        for i in range(1000):            # 8 MB if read to the end
            pulled.append(i)
            yield b"x" * 8192

    _serve(monkeypatch, huge)
    out = await _http("http://big.test/", 5.0)
    assert f"≥{_BODY_CAP} bytes" in out and "body: xxx" in out, out
    assert len(pulled) <= _BODY_CAP // 8192 + 2, len(pulled)


async def test_a_trickling_server_still_times_out(monkeypatch, public_name):
    async def trickle():
        while True:                      # one byte at a time: no single read ever times out
            yield b"x"
            await asyncio.sleep(0.1)

    _serve(monkeypatch, trickle)
    out = await asyncio.wait_for(_http("http://slow.test/", 0.5), 5)
    assert "[FAIL] timed out after 0.5s" in out, out


@pytest.mark.parametrize("url", [
    "http://[fd00:ec2::254]/latest/meta-data/",
    "http://100.100.100.200/latest/meta-data/",
    "http://[::ffff:100.100.100.200]/",
])
async def test_cloud_metadata_outside_link_local_is_refused(url):
    assert "metadata" in (await blocked_url(url) or ""), url


async def test_an_unresolvable_docker_host_is_named_not_the_target(monkeypatch):
    async def addresses(host):
        if host == "docker.internal":
            raise socket.gaierror("Name or service not known")
        return ["10.0.0.5"]

    monkeypatch.setattr(net_probes, "_addresses", addresses)
    monkeypatch.setattr(settings, "docker_host", "tcp://docker.internal:2375")
    why = await blocked_url("http://grafana.test/")
    assert why and "docker.internal" in why and "DOCKER_HOST" in why, why
    assert "grafana.test" not in why, why


async def test_clock_skew_uses_the_node_user_and_caps_the_fan_out(monkeypatch):
    calls = []

    async def ssh(node_ip, command, timeout=None, username=None, key_path=None):
        calls.append((node_ip, username))
        return True, "1700000000"

    async def api_clock(max_ok, timeout):
        return "  [OK] api"

    monkeypatch.setattr(net_probes, "_run_ssh_command", ssh)
    monkeypatch.setattr(net_probes, "_api_clock", api_clock)
    monkeypatch.setattr(settings, "node_ssh_user", "nodeuser")
    ips = ",".join(f"10.0.0.{i}" for i in range(1, 26))
    out = await check_clock_skew.ainvoke({"node_ips": ips})
    assert len(calls) == net_probes._MAX_CLOCK_NODES, len(calls)
    assert {u for _, u in calls} == {"nodeuser"}, calls
    assert "5 more were not" in out, out


async def test_tcpdump_captures_headers_only(monkeypatch):
    argv = []

    async def run(*args, timeout=None):
        argv.extend(args)
        return 0, "", ""

    monkeypatch.setattr(net_probes, "run_command", run)
    await tcpdump_capture.ainvoke({"bpf_filter": "host 10.0.0.1"})
    assert "-q" in argv, argv
    snap = argv[argv.index("-s") + 1]
    assert 0 < int(snap) <= 128, argv
