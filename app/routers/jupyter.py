import time
import uuid
from datetime import datetime

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.config import settings
from app.database import get_db, AsyncSessionLocal
from app.models.workload import Workload
from app.models.node import Node
from app.utils.sse import task_log_stream

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
    The image already has jupyter-ai installed — the assistant is ready as soon
    as this URL is accessible.
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


# ── On-demand Jupyter instances ───────────────────────────────────────────────

@router.get("/api/v1/jupyter/instances")
async def list_jupyter_instances(db: AsyncSession = Depends(get_db)):
    """
    Returns READY Jupyter instances that have a jupyter_url set, plus the
    persistent assistant entry (JUPYTER_ASSISTANT_URL) if configured.

    The persistent assistant is not a workload in the DB — it's prepended
    as a synthetic entry with task_id='persistent' so the UI can always
    show it alongside on-demand instances.
    """
    result = await db.execute(
        select(Workload, Node.machine_ip)
        .outerjoin(Node, Node.workload_id == Workload.id)
        .where(
            Workload.model_name == "jupyter",
            Workload.state == "READY",
        )
        .order_by(Workload.created_at.desc())
    )
    rows = result.all()

    instances = [
        {
            "task_id": workload.workload_id,
            "state": workload.state,
            "node_ip": node_ip,
            "jupyter_url": (workload.workload_config or {}).get("jupyter_url"),
            "created_at": workload.created_at,
            "updated_at": workload.updated_at,
        }
        for workload, node_ip in rows
        if (workload.workload_config or {}).get("jupyter_url")
    ]

    # Prepend persistent assistant if configured and not already in the list
    if settings.JUPYTER_ASSISTANT_URL:
        from urllib.parse import urlparse
        parsed = urlparse(settings.JUPYTER_ASSISTANT_URL)
        node_ip = parsed.hostname
        already_listed = any(i["jupyter_url"] == settings.JUPYTER_ASSISTANT_URL for i in instances)
        if not already_listed:
            instances.insert(0, {
                "task_id": "persistent",
                "state": "READY",
                "node_ip": node_ip,
                "jupyter_url": settings.JUPYTER_ASSISTANT_URL,
                "created_at": None,
                "updated_at": None,
            })

    return instances


# ── Launch on-demand instance ─────────────────────────────────────────────────

class JupyterLaunchRequest(BaseModel):
    node_ip: str


class JupyterLaunchResponse(BaseModel):
    status: str
    task_id: str
    message: str


@router.post("/api/v1/jupyter/launch", response_model=JupyterLaunchResponse)
async def launch_jupyter(
    request: JupyterLaunchRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Launch a new on-demand Jupyter instance on the given node.
    The Celery chain (validate → install → launch) runs asynchronously.
    Once the workload reaches READY state with a jupyter_url, it appears
    automatically in GET /api/v1/jupyter/instances.
    """
    date_str   = datetime.utcnow().strftime("%Y%m%d")
    short_uuid = str(uuid.uuid4())[:6]
    task_id    = "jup-%s-%s" % (date_str, short_uuid)

    workload = Workload(
        workload_id=task_id,
        model_name="jupyter",
        workload_config={"workload_type": "jupyter"},
        state="CREATED",
    )
    db.add(workload)
    await db.flush()

    node = Node(
        workload_id=workload.id,
        machine_id="node-%s-0" % short_uuid,
        machine_ip=request.node_ip,
        machine_username=settings.SSH_DEFAULT_USER,
        state="ADDED",
    )
    db.add(node)
    await db.commit()

    from app.worker import start_jupyter_chain
    start_jupyter_chain.delay(task_id)

    return JupyterLaunchResponse(
        status="accepted",
        task_id=task_id,
        message=(
            "Jupyter launch started. Poll GET /api/v1/jupyter/instances "
            "— the instance appears once it reaches READY state."
        ),
    )


@router.get("/api/v1/jupyter/instances/{task_id}/status")
async def get_jupyter_instance_status(task_id: str, db: AsyncSession = Depends(get_db)):
    """
    Returns the current state of a Jupyter launch workload.
    Useful for polling while the SSE stream is not available.
    States: CREATED → VALIDATING → VALIDATED → INSTALLING → READY | FAILED
    """
    result = await db.execute(
        select(Workload).where(
            Workload.workload_id == task_id,
            Workload.model_name == "jupyter",
        )
    )
    workload = result.scalar_one_or_none()
    if not workload:
        raise HTTPException(status_code=404, detail="Jupyter workload not found")

    return {
        "task_id": task_id,
        "state": workload.state,
        "jupyter_url": (workload.workload_config or {}).get("jupyter_url"),
        "error_message": workload.error_message,
        "updated_at": workload.updated_at,
    }


@router.get("/api/v1/jupyter/instances/{task_id}/logs/stream")
async def stream_jupyter_logs(task_id: str, request: Request):
    """
    SSE stream of launch logs for a Jupyter workload (validate → launch steps).
    Streams in real-time while the launch is in progress, then closes.
    Supports Last-Event-ID for reconnect without replaying already-seen lines.
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Workload.id).where(
                Workload.workload_id == task_id,
                Workload.model_name == "jupyter",
            )
        )
        workload_db_id = result.scalar_one_or_none()

    if not workload_db_id:
        raise HTTPException(status_code=404, detail="Jupyter workload not found")

    last_event_id = request.headers.get("last-event-id", "")

    return StreamingResponse(
        task_log_stream(
            workload_db_id, request,
            filter_bench_result=False,
            last_event_id=last_event_id,
        ),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no"},
    )


@router.get("/api/v1/jupyter/instances/{task_id}/health")
async def check_instance_health(task_id: str, db: AsyncSession = Depends(get_db)):
    """
    Pings the jupyter_url for a specific READY instance.
    Returns {"task_id", "url", "healthy", "latency_ms"}.
    """
    result = await db.execute(
        select(Workload).where(
            Workload.workload_id == task_id,
            Workload.model_name == "jupyter",
        )
    )
    workload = result.scalar_one_or_none()
    if not workload:
        raise HTTPException(status_code=404, detail="Jupyter workload not found")

    jupyter_url = (workload.workload_config or {}).get("jupyter_url")
    if not jupyter_url:
        return {"task_id": task_id, "healthy": False, "url": None, "latency_ms": None}

    health = await _ping_url(jupyter_url)
    return {"task_id": task_id, "url": jupyter_url, **health}
