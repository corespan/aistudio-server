import time

import httpx
from fastapi import APIRouter

from app.config import settings

router = APIRouter(tags=["Jupyter"])


async def _ping_url(url: str) -> dict:
    """
    HTTP-GET url with a 5-second timeout.
    Returns {"healthy": bool, "latency_ms": int | None}.
    Any response below 500 is considered healthy.
    """
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.get(url, timeout=5.0)
        latency_ms = int((time.monotonic() - start) * 1000)
        return {"healthy": resp.status_code < 500, "latency_ms": latency_ms}
    except Exception:
        return {"healthy": False, "latency_ms": None}


# ── Persistent Jupyter Assistant ──────────────────────────────────────────────

@router.get("/api/v1/jupyter/assistant")
async def get_jupyter_assistant():
    """
    Returns the URL of the pre-configured, always-running Jupyter Lab instance.
    Configured via JUPYTER_ASSISTANT_URL environment variable.
    """
    configured = bool(settings.JUPYTER_ASSISTANT_URL)
    return {
        "url": settings.JUPYTER_ASSISTANT_URL if configured else None,
        "configured": configured,
    }


@router.get("/api/v1/jupyter/assistant/health")
async def check_assistant_health():
    """
    Pings the persistent Jupyter assistant URL and returns its health status.
    Returns {"url", "healthy", "latency_ms"} — never raises an error response.
    """
    url = settings.JUPYTER_ASSISTANT_URL
    if not url:
        return {"healthy": False, "url": None, "latency_ms": None}
    result = await _ping_url(url)
    return {"url": url, **result}
