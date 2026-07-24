import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.config import settings
from app.database import get_db, AsyncSessionLocal
from app.models.workload import Workload
from app.models.node import Node
from app.models.task import Task
from app.utils.sse import task_log_stream

router = APIRouter(tags=["Jupyter"])


# ── Mode 1: Persistent Jupyter Assistant ──────────────────────────────────────

@router.get("/api/v1/jupyter/assistant")
async def get_jupyter_assistant():
    """
    Returns the URL of the pre-configured, always-running Jupyter Lab instance.
    The AI assistant is already set up — just open the URL and start working.
    """
    configured = bool(settings.JUPYTER_ASSISTANT_URL)
    return {
        "url": settings.JUPYTER_ASSISTANT_URL if configured else None,
        "configured": configured,
    }


# ── List all Jupyter instances ────────────────────────────────────────────────

@router.get("/api/v1/jupyter/instances")
async def list_jupyter_instances(db: AsyncSession = Depends(get_db)):
    """
    Returns all Jupyter workload instances (both running and historical).

    Each row includes state, node IP, and jupyter_url (set when state=READY).
    Ordered by created_at DESC — newest first.

    Use this to render the Jupyter sessions table in the UI,
    similar to how benchmark results are shown in the leaderboard.
    """
    result = await db.execute(
        select(Workload, Node.machine_ip)
        .outerjoin(Node, Node.workload_id == Workload.id)
        .where(Workload.model_name == "jupyter")
        .order_by(Workload.created_at.desc())
    )
    rows = result.all()

    return [
        {
            "task_id": workload.workload_id,
            "state": workload.state,
            "node_ip": node_ip,
            "jupyter_url": (workload.workload_config or {}).get("jupyter_url"),
            "error_message": workload.error_message,
            "created_at": workload.created_at,
            "updated_at": workload.updated_at,
        }
        for workload, node_ip in rows
    ]


# ── Mode 2: On-demand Jupyter ─────────────────────────────────────────────────

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
    date_str = datetime.utcnow().strftime("%Y%m%d")
    short_uuid = str(uuid.uuid4())[:6]
    task_id = "jup-%s-%s" % (date_str, short_uuid)

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
        status="success",
        task_id=task_id,
        message="Jupyter workload created and dispatched.",
    )


@router.get("/api/v1/jupyter/{task_id}/status")
async def get_jupyter_status(
    task_id: str,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Workload).where(Workload.workload_id == task_id)
    )
    workload = result.scalar_one_or_none()

    if not workload:
        raise HTTPException(status_code=404, detail="Workload not found")

    cfg = workload.workload_config or {}
    return {
        "task_id": workload.workload_id,
        "state": workload.state,
        "jupyter_url": cfg.get("jupyter_url"),
        "error_message": workload.error_message,
        "updated_at": workload.updated_at,
    }


@router.get("/api/v1/jupyter/{task_id}/logs/stream")
async def stream_jupyter_logs(task_id: str, request: Request):
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Workload.id).where(Workload.workload_id == task_id)
        )
        workload_db_id = result.scalar_one_or_none()

    if not workload_db_id:
        raise HTTPException(status_code=404, detail="Workload not found.")

    last_event_id = request.headers.get("last-event-id", "")

    return StreamingResponse(
        task_log_stream(workload_db_id, request, last_event_id=last_event_id),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no"},
    )
