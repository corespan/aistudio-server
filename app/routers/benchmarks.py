import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.config import settings
from app.database import get_db, AsyncSessionLocal
from app.models.workload import Workload
from app.models.node import Node
from app.schemas.benchmark import BenchmarkStartRequest, BenchmarkStartResponse
from app.utils.sse import task_log_stream

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
    dataset_path = (request.config.get("dataset_path") or "").strip()
    if not dataset_path:
        # Previously this was only caught deep inside worker.py, after the
        # Workload/Node rows were already committed and the Celery task
        # dispatched — the run would sit as "running" for two steps before
        # failing. Reject it here instead, at submit time.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="config.dataset_path is required: an absolute path to a "
                   "ShareGPT-format JSON file that already exists on the "
                   "target GPU node.",
        )

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
            machine_username=settings.SSH_DEFAULT_USER,
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
        # Kept alongside task_id (same value) for callers that key off the
        # DB column name directly — e.g. Composer lifecycle-event tracking,
        # which correlates status polls against workload_id elsewhere.
        "workload_id": workload.workload_id,
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
    Server-Sent Events (SSE) endpoint that streams logs from ALL steps
    (validate → install → benchmark) for a workload in chronological order.

    Supports Last-Event-ID: on browser reconnect, only logs after the last
    received event are sent — the full history is NOT replayed.

    Uses short-lived DB sessions per poll to avoid holding a pool connection
    for the full stream duration.
    """
    # Resolve the workload's internal UUID once upfront
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Workload.id).where(Workload.workload_id == task_id)
        )
        workload_db_id = result.scalar_one_or_none()

    if not workload_db_id:
        raise HTTPException(status_code=404, detail="Workload not found.")

    last_event_id = request.headers.get("last-event-id", "")

    # BENCH_RESULT: lines are internal markers used for DB ingestion;
    # the worker writes a human-readable summary instead — filter them from the stream.
    return StreamingResponse(
        task_log_stream(
            workload_db_id, request,
            filter_bench_result=True,
            last_event_id=last_event_id,
        ),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no"},
    )
