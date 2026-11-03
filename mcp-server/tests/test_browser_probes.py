"""Tests for the frontend_check summariser — pure logic, no browser/Playwright."""

from app.tools.browser_probes import _MAX_ITEMS, _summarize


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
