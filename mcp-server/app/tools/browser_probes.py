"""Frontend diagnostic tools — the evidence Docker/SSH can't see."""

from __future__ import annotations

import structlog
from langchain_core.tools import tool

from app.config import settings
from app.tools.net_probes import blocked_url

logger = structlog.get_logger()

# Max findings shown per category.
_MAX_ITEMS = 10


def _summarize(
    url: str,
    status: int | None,
    title: str,
    console: list[tuple[str, str]],
    failed: list[tuple[str, str, str]],
    http_errors: list[tuple[int, str]],
) -> str:
    """Format captured browser signals as a summary."""
    errors = [t for (lvl, t) in console if lvl == "error"]
    warnings = [t for (lvl, t) in console if lvl == "warning"]

    lines = [f"Frontend check: {url}"]

    if status is None:
        lines.append("  Load: [FAIL] no HTTP response — the page itself did not load (DNS/refused/timeout).")
    elif status >= 400:
        lines.append(f"  Load: [FAIL] the page returned HTTP {status}.")
    else:
        lines.append(f"  Load: [OK] HTTP {status} — title: {title!r}")

    if errors:
        lines.append(f"  Console errors ({len(errors)}):")
        lines += [f"    [ERROR] {t[:300]}" for t in errors[:_MAX_ITEMS]]
    else:
        lines.append("  Console errors: none")
    if warnings:
        lines.append(f"  Console warnings: {len(warnings)} (first: {warnings[0][:200]})")

    if failed:
        lines.append(f"  Failed network requests ({len(failed)}):")
        lines += [f"    [FAIL] {m} {u} — {why}" for (m, u, why) in failed[:_MAX_ITEMS]]
    if http_errors:
        lines.append(f"  HTTP error responses ({len(http_errors)}):")
        lines += [f"    [{st}] {u}" for (st, u) in http_errors[:_MAX_ITEMS]]

    clean = (status is not None and status < 400 and not errors and not failed and not http_errors)
    if clean:
        lines.append("  Verdict: page loaded cleanly — no console errors or failed requests. "
                     "The problem is likely NOT in the frontend itself.")
    else:
        lines.append("  Verdict: frontend problems above — start with the console errors and "
                     "failed API calls; a failed API call points back to the backend.")
    return "\n".join(lines)


@tool
async def frontend_check(url: str, wait_seconds: int = 3) -> str:
    """Load `url` in a headless browser and report HTTP status, console errors and failed requests; waits `wait_seconds` for late calls."""
    cdp = settings.browser_cdp_url
    if not cdp:
        return ("frontend_check unavailable: the browser sandbox is not configured "
                "(BROWSER_CDP_URL is unset). Point it at a headless browser's CDP endpoint.")

    refused = await blocked_url(url)
    if refused:
        return f"frontend_check: [REFUSED] {url}: {refused}"
    wait_seconds = max(0, min(int(wait_seconds), 30))

    console: list[tuple[str, str]] = []
    failed: list[tuple[str, str, str]] = []
    http_errors: list[tuple[int, str]] = []

    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(cdp, timeout=15000)
            try:
                context = await browser.new_context(ignore_https_errors=True)
                page = await context.new_page()

                page.on("console", lambda m: console.append((m.type, m.text)))
                page.on(
                    "requestfailed",
                    lambda r: failed.append((r.method, r.url, (r.failure or "failed"))),
                )
                page.on(
                    "response",
                    lambda resp: http_errors.append((resp.status, resp.url)) if resp.status >= 400 else None,
                )

                resp = await page.goto(url, wait_until="load", timeout=30000)
                await page.wait_for_timeout(max(0, int(wait_seconds)) * 1000)
                title = await page.title()
                status = resp.status if resp else None
            finally:
                await browser.close()
    except Exception as exc:
        logger.error("frontend_check.error", url=url, error=str(exc))
        return f"frontend_check: [FAIL] could not drive the browser for {url}: {exc}"

    logger.info("frontend_check.done", url=url, console=len(console), failed=len(failed))
    return _summarize(url, status, title, console, failed, http_errors)
