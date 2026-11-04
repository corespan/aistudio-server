"""Tests for frontend_check: the summariser and the route guard, with fakes — no browser."""

from types import SimpleNamespace

import pytest

from app.config import settings
from app.tools import browser_probes
from app.tools.browser_probes import _MAX_ITEMS, _route_guard, _summarize


def test_clean_page_points_away_from_frontend():
    out = _summarize("http://ui", 200, "aistudio", [], [], [])
    assert "[OK] HTTP 200" in out
    assert "Console errors: none" in out
    assert "NOT in the frontend" in out


def test_console_errors_and_failed_api_flagged():
    out = _summarize(
        "http://ui", 200, "aistudio",
        console=[("error", "Uncaught TypeError: x is undefined"), ("warning", "deprecated")],
        failed=[("GET", "http://api/v1/benchmarks", "net::ERR_CONNECTION_REFUSED")],
        http_errors=[(500, "http://api/v1/health")],
    )
    assert "[ERROR] Uncaught TypeError" in out
    assert "ERR_CONNECTION_REFUSED" in out
    assert "[500] http://api/v1/health" in out
    assert "frontend problems" in out


def test_page_did_not_load():
    out = _summarize("http://ui", None, "", [], [], [])
    assert "no HTTP response" in out


def test_page_returned_http_error():
    out = _summarize("http://ui", 502, "", [], [], [])
    assert "[FAIL] the page returned HTTP 502" in out


def test_truncates_to_max_items():
    many = [("error", f"e{i}") for i in range(30)]
    out = _summarize("http://ui", 200, "t", many, [], [])
    assert out.count("[ERROR]") == _MAX_ITEMS
    assert "Console errors (30)" in out


# ── the route guard: every redirect hop is vetted before the browser follows it
class _Route:
    def __init__(self, url, status=200, headers=None):
        self.request = SimpleNamespace(url=url, method="GET")
        self.resp = SimpleNamespace(status=status, headers=headers or {})
        self.calls = []

    async def fetch(self, **kw):
        self.calls.append(("fetch", kw))
        return self.resp

    async def fulfill(self, **kw):
        self.calls.append(("fulfill", kw))

    async def abort(self, code=None):
        self.calls.append(("abort", code))


@pytest.fixture(autouse=True)
def _no_tcp_docker(monkeypatch):
    monkeypatch.setattr(settings, "docker_host", "unix:///var/run/docker.sock")


@pytest.mark.parametrize("location", [
    "http://169.254.169.254/latest/meta-data/",
    "//127.0.0.1:8081/rca/actions",          # scheme-relative: resolved against the request URL
])
async def test_a_redirect_to_a_refused_target_is_not_followed(location):
    failed = []
    route = _Route("http://93.184.216.34/login", 302, {"location": location})
    await _route_guard(failed)(route)
    assert route.calls[0] == ("fetch", {"max_redirects": 0})
    assert route.calls[1][0] == "fulfill" and route.calls[1][1].get("status") == 403
    assert "response" not in route.calls[1][1]
    assert "refused redirect" in failed[0][2]


async def test_an_allowed_redirect_is_handed_back_for_the_browser_to_reissue():
    failed = []
    route = _Route("http://93.184.216.34/a", 301, {"location": "/b"})
    await _route_guard(failed)(route)
    assert route.calls == [("fetch", {"max_redirects": 0}), ("fulfill", {"response": route.resp})]
    assert not failed


async def test_a_refused_request_is_never_fetched():
    failed = []
    route = _Route("http://[::1]/")
    await _route_guard(failed)(route)
    assert route.calls == [("abort", "blockedbyclient")] and failed


async def test_service_workers_are_blocked(monkeypatch):
    seen = {}

    class _Browser:
        async def new_context(self, **kw):
            seen.update(kw)
            raise RuntimeError("stop")

        async def close(self):
            pass

    class _PW:
        async def __aenter__(self):
            async def connect(cdp, timeout=None):
                return _Browser()
            return SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=connect))

        async def __aexit__(self, *exc):
            return False

    import playwright.async_api
    monkeypatch.setattr(playwright.async_api, "async_playwright", lambda: _PW())
    monkeypatch.setattr(settings, "browser_cdp_url", "http://browser:9222")
    await browser_probes.frontend_check.ainvoke({"url": "http://93.184.216.34/"})
    assert seen.get("service_workers") == "block", seen
