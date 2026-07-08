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
from app.utils.sse import task_log_stream

router = APIRouter(tags=["Jupyter"])


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

    return StreamingResponse(
        task_log_stream(workload_db_id, request),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no"},
    )
