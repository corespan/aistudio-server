"""URL checks for the probes that return a response body. Offline."""

from __future__ import annotations

import pytest

from app.config import settings
from app.mcp_server import _safe_error
from app.tools.net_probes import _http, _pinned, _tcp, blocked_url, check_tls_cert


@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
    "http://[fe80::1]/",
    "http://127.0.0.1:8081/rca/actions",
    "http://[::1]/",
    "http://0.0.0.0/",
    "http://[::ffff:127.0.0.1]/",
    "http://[::ffff:169.254.169.254]/",
    "file:///etc/passwd",
    "gopher://10.0.0.1/",
])
async def test_secret_bearing_targets_are_refused(url):
    assert await blocked_url(url), url


async def test_the_docker_api_is_refused_by_address(monkeypatch):
    monkeypatch.setattr(settings, "docker_host", "tcp://10.9.9.9:2375")
    assert "Docker API" in (await blocked_url("http://10.9.9.9:2375/containers/json"))
    assert "Docker API" in (await blocked_url("http://[::ffff:10.9.9.9]:2375/containers/json"))


@pytest.mark.parametrize("url", ["http://10.0.0.5:8002/health", "https://192.168.1.10/"])
async def test_internal_services_stay_reachable(url):
    assert await blocked_url(url) is None


async def test_the_http_probe_does_not_fetch_the_metadata_body():
    out = await _http("http://169.254.169.254/latest/meta-data/", 1.0)
    assert "[REFUSED]" in out and "body:" not in out, out


def test_errors_reach_the_model_without_url_credentials():
    exc = OSError("connect failed: postgresql://amwadmin:s3cret@postgres:5432/aistudio")
    out = _safe_error(exc)
    assert "s3cret" not in out and "postgresql://***@postgres" in out, out


def test_the_request_goes_to_the_checked_address_with_the_original_host():
    url, headers, ext = _pinned("https://user:pw@grafana.internal:3443/d/x?a=1", "10.0.0.9")
    assert url == "https://10.0.0.9:3443/d/x?a=1"
    assert headers == {"Host": "grafana.internal:3443"} and ext == {"sni_hostname": "grafana.internal"}
    assert _pinned("http://[fe80::1]/", "fd00::5")[0] == "http://[fd00::5]/"


async def test_tcp_and_tls_probes_refuse_the_same_targets():
    assert "[REFUSED]" in await _tcp("169.254.169.254", 80, 1.0)
    assert "[REFUSED]" in await check_tls_cert.ainvoke({"host": "127.0.0.1", "port": 443})
