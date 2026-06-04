import uuid
from datetime import datetime

import asyncio
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db, AsyncSessionLocal
from app.models.workload import Workload
from app.models.node import Node
from app.models.task import Task
from app.models.task_log import TaskLog
from app.schemas.benchmark import BenchmarkStartRequest, BenchmarkStartResponse

router = APIRouter(tags=["Benchmarks"])


@router.post("/api/v1/benchmarks/start", response_model=BenchmarkStartResponse)
async def start_benchmark(
    request: BenchmarkStartRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Called by the AIStudio UI to trigger a new benchmark run.

    1. Creates Workload and Node records in PostgreSQL.
    2. Dispatches a Celery task to begin the orchestration chain.
    3. Returns the task_id immediately so the UI can start polling.
    """
    date_str = datetime.utcnow().strftime("%Y%m%d")
    short_uuid = str(uuid.uuid4())[:6]
    task_id = "wl-%s-%s" % (date_str, short_uuid)

    workload = Workload(
        workload_id=task_id,
        model_name=request.model_name,
        workload_config=request.config,
        state="CREATED",
    )
    db.add(workload)
    await db.flush()

    for i, ip in enumerate(request.node_ips):
        node = Node(
            workload_id=workload.id,
            machine_id="node-%s-%d" % (short_uuid, i),
            machine_ip=ip,
            machine_username="ubuntu",
            state="ADDED",
        )
        db.add(node)

    await db.commit()

    from app.worker import start_benchmark_chain
    start_benchmark_chain.delay(task_id)

    return BenchmarkStartResponse(
        status="success",
        task_id=task_id,
        message="Benchmark workload created and dispatched to Celery.",
    )


@router.get("/api/v1/benchmarks/{task_id}/status")
async def get_benchmark_status(
    task_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Polled by the UI every few seconds to check workload progress."""
    result = await db.execute(
        select(Workload).where(Workload.workload_id == task_id)
    )
    workload = result.scalar_one_or_none()

    if not workload:
        raise HTTPException(status_code=404, detail="Workload not found")

    return {
        "task_id": workload.workload_id,
        "state": workload.state,
        "error_message": workload.error_message,
        "updated_at": workload.updated_at,
    }


@router.get("/api/v1/benchmarks/{task_id}/logs/stream")
async def stream_benchmark_logs(
    task_id: str,
    request: Request,
):
    """
    Server-Sent Events (SSE) endpoint to stream live task logs to the UI.

    Uses short-lived DB sessions per poll iteration to avoid holding a
    connection from the pool for the entire stream duration.
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Task.id, Task.status)
            .join(Workload)
            .where(Workload.workload_id == task_id)
        )
        row = result.first()

    if not row:
        raise HTTPException(
            status_code=404,
            detail="No active execution task found for this workload.",
        )

    resolved_task_id = row[0]

    async def log_generator():
        last_seen_date = None

        while True:
            if await request.is_disconnected():
                break

            async with AsyncSessionLocal() as db:
                query = (
                    select(TaskLog)
                    .where(TaskLog.task_id == resolved_task_id)
                    .order_by(TaskLog.logged_at.asc())
                )
                if last_seen_date:
                    query = query.where(TaskLog.logged_at > last_seen_date)

                result = await db.execute(query)
                logs = result.scalars().all()

                for log in logs:
                    yield "data: %s\n\n" % log.line
                    last_seen_date = log.logged_at

                status_result = await db.execute(
                    select(Task.status).where(Task.id == resolved_task_id)
                )
                current_status = status_result.scalar()

            if current_status in ("success", "failed") and not logs:
                yield "event: close\ndata: stream closed\n\n"
                break

            await asyncio.sleep(1.0)

    return StreamingResponse(
        log_generator(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no"},
    )
