import asyncio
import logging
import time
import uuid
from datetime import datetime

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import delete as sql_delete, select

from app.config import settings
from app.database import get_db, AsyncSessionLocal
from app.models.workload import Workload
from app.models.node import Node
from app.services.nginx_proxy import remove_jupyter_config
from app.services.ssh_executor import SSHExecutor
from app.utils.sse import task_log_stream

logger = logging.getLogger(__name__)

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


# ── Request models ────────────────────────────────────────────────────────────

class JupyterLaunchRequest(BaseModel):
    node_ip: str


class PersistentJupyterRequest(BaseModel):
    task_id: str


class JupyterLaunchResponse(BaseModel):
    status: str
    task_id: str
    message: str


# ── On-demand Jupyter instances ───────────────────────────────────────────────

@router.get("/api/v1/jupyter/instances")
async def list_jupyter_instances(db: AsyncSession = Depends(get_db)):
    """
    Returns READY Jupyter instances that have a jupyter_url set.
    Persistent instances (is_persistent=true in workload_config) are shown first.
    All instances use their real task_id — no fake 'persistent' row.
    """
    result = await db.execute(
        select(Workload, Node.machine_ip)
        .outerjoin(Node, Node.workload_id == Workload.id)
        .where(
            Workload.model_name == "jupyter",
            Workload.state == "READY",
            Workload.workload_id != "persistent",  # drop legacy fake row if present
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
            "is_persistent": bool((workload.workload_config or {}).get("is_persistent")),
            "created_at": workload.created_at,
            "updated_at": workload.updated_at,
        }
        for workload, node_ip in rows
        if (workload.workload_config or {}).get("jupyter_url")
    ]

    # Persistent instances first, then newest-first
    instances.sort(key=lambda x: (not x["is_persistent"], x["created_at"] or ""), reverse=False)

    return instances


@router.post("/api/v1/jupyter/persistent", status_code=200)
async def set_persistent_jupyter(
    body: PersistentJupyterRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Mark an existing Jupyter instance as persistent.
    Send {"task_id": "jup-20260729-xxx"}.
    The instance keeps its real task_id — no fake 'persistent' row is created.
    Persistent instances appear first in GET /instances and cannot be deleted.
    Any previously persistent instance is un-marked automatically.
    """
    # Target workload must exist
    result = await db.execute(
        select(Workload).where(
            Workload.workload_id == body.task_id,
            Workload.model_name == "jupyter",
        )
    )
    workload = result.scalar_one_or_none()
    if not workload:
        raise HTTPException(status_code=404, detail="Jupyter workload not found: %s" % body.task_id)

    # Un-mark any previously persistent instance
    prev_result = await db.execute(
        select(Workload).where(
            Workload.model_name == "jupyter",
            Workload.workload_id != body.task_id,
        )
    )
    for prev in prev_result.scalars().all():
        cfg = dict(prev.workload_config or {})
        if cfg.get("is_persistent"):
            cfg["is_persistent"] = False
            prev.workload_config = cfg
            prev.updated_at = datetime.utcnow()

    # Also delete the legacy fake 'persistent' row if it exists
    await db.execute(
        sql_delete(Workload).where(Workload.workload_id == "persistent")
    )

    # Mark the target as persistent
    cfg = dict(workload.workload_config or {})
    cfg["is_persistent"] = True
    workload.workload_config = cfg
    workload.updated_at = datetime.utcnow()

    await db.commit()
    return {
        "status": "ok",
        "task_id": body.task_id,
        "jupyter_url": cfg.get("jupyter_url"),
    }


# ── Launch on-demand instance ─────────────────────────────────────────────────

@router.post("/api/v1/jupyter/launch", response_model=JupyterLaunchResponse)
async def launch_jupyter(
    request: JupyterLaunchRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Launch a new on-demand Jupyter instance on the given node.
    The Celery chain (validate → launch) runs asynchronously.
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


# ── Per-instance endpoints ────────────────────────────────────────────────────

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
        "workload_port": workload.workload_port,
        "error_message": workload.error_message,
        "updated_at": workload.updated_at,
    }


@router.get("/api/v1/jupyter/instances/{task_id}/logs/stream")
async def stream_jupyter_logs(task_id: str, request: Request):
    """
    SSE stream of launch logs for a Jupyter workload (validate → launch steps).
    Streams in real-time while the launch is in progress, then closes.
    Supports Last-Event-ID for reconnect without replaying already-seen lines.
    Retries DB lookup up to 5s to handle race where frontend calls this
    immediately after POST /launch before the commit is visible.
    """
    # Retry up to 5 times with 1s delay — the frontend calls this endpoint
    # immediately after POST /launch, but the DB commit may not be visible yet.
    workload_db_id = None
    for _ in range(5):
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Workload.id).where(
                    Workload.workload_id == task_id,
                    Workload.model_name == "jupyter",
                )
            )
            workload_db_id = result.scalar_one_or_none()
        if workload_db_id:
            break
        await asyncio.sleep(1)

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


@router.delete("/api/v1/jupyter/instances/{task_id}", status_code=204)
async def delete_jupyter_instance(task_id: str, db: AsyncSession = Depends(get_db)):
    """
    Stop the Jupyter container on the remote node and remove it from the DB.
    Cascades: Workload → Tasks → TaskLogs, WorkloadEvents, Nodes all deleted.
    Returns 400 if task_id is 'persistent' (not a DB-managed instance).
    SSH errors are logged but do not block the DB deletion — the container
    may already be gone on the node.
    """
    if task_id == "persistent":
        raise HTTPException(status_code=400, detail="Legacy persistent row — delete via DB directly.")


    # Fetch workload + node
    wl_result = await db.execute(
        select(Workload).where(
            Workload.workload_id == task_id,
            Workload.model_name == "jupyter",
        )
    )
    workload = wl_result.scalar_one_or_none()
    if not workload:
        raise HTTPException(status_code=404, detail="Jupyter workload not found")

    if (workload.workload_config or {}).get("is_persistent"):
        raise HTTPException(
            status_code=400,
            detail="Cannot delete a persistent instance. Un-mark it first via POST /api/v1/jupyter/persistent with another task_id.",
        )

    node_result = await db.execute(
        select(Node).where(Node.workload_id == workload.id)
    )
    node = node_result.scalar_one_or_none()

    # SSH into node and stop the container — best-effort, don't block on failure
    if node:
        try:
            container_name = "jupyter-%s" % task_id
            stop_cmd = "docker rm -f %s 2>/dev/null || true" % container_name
            with SSHExecutor(
                node.machine_ip,
                node.machine_username,
                key_filename=settings.SSH_KEY_PATH,
            ) as ssh:
                ssh.run_command(stop_cmd, task_id=None)
        except Exception as exc:
            logger.warning(
                "Could not stop container for %s on %s: %s",
                task_id, node.machine_ip, exc,
            )

    # Delete workload via raw SQL so the DB's ondelete="CASCADE" handles
    # child rows (Nodes, Tasks, TaskLogs, WorkloadEvents) automatically.
    # Using ORM session.delete() in async context triggers lazy-loading of
    # relationships for cascade processing, which fails with MissingGreenlet.
    await db.execute(
        sql_delete(Workload).where(Workload.id == workload.id)
    )
    await db.commit()

    # Remove nginx config if proxy is enabled — best-effort, don't raise.
    try:
        remove_jupyter_config(task_id)
    except Exception as exc:
        logger.warning("Could not remove nginx config for %s: %s", task_id, exc)


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
