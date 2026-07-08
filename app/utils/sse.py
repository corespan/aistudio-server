"""
app/utils/sse.py — Shared SSE log-streaming helper.

Both /benchmarks/{task_id}/logs/stream and /jupyter/{task_id}/logs/stream
use the same polling loop to stream TaskLog rows to the browser.  This module
extracts that loop so neither router duplicates it.
"""

import asyncio
import uuid
from typing import AsyncGenerator

from fastapi import Request
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.task import Task
from app.models.task_log import TaskLog
from app.models.workload import Workload


async def task_log_stream(
    workload_db_id: uuid.UUID,
    request: Request,
    *,
    filter_bench_result: bool = False,
) -> AsyncGenerator[str, None]:
    """
    SSE generator that streams TaskLog lines for all tasks under a workload.

    workload_db_id      — internal UUID (Workload.id) resolved by the caller.
    filter_bench_result — if True, skips lines starting with "BENCH_RESULT:"
                          (used by the benchmark router; not needed for Jupyter).

    Opens a fresh short-lived DB session per poll iteration to avoid holding a
    connection-pool slot for the full stream duration.

    Yields SSE-formatted strings.  Closes the stream when the workload reaches
    a terminal state (READY or FAILED) and there are no more new log rows.
    """
    last_seen_date = None

    while True:
        if await request.is_disconnected():
            break

        async with AsyncSessionLocal() as db:
            # Collect logs across ALL tasks for this workload in time order.
            query = (
                select(TaskLog)
                .join(Task, TaskLog.task_id == Task.id)
                .where(Task.workload_id == workload_db_id)
                .order_by(TaskLog.logged_at.asc())
            )
            if last_seen_date:
                query = query.where(TaskLog.logged_at > last_seen_date)

            result = await db.execute(query)
            logs = result.scalars().all()

            for log in logs:
                # Advance the cursor even for filtered lines so we never re-fetch them.
                if filter_bench_result and log.line.startswith("BENCH_RESULT:"):
                    last_seen_date = log.logged_at
                    continue
                yield "data: %s\n\n" % log.line
                last_seen_date = log.logged_at

            state_result = await db.execute(
                select(Workload.state).where(Workload.id == workload_db_id)
            )
            workload_state = state_result.scalar()

        if workload_state in ("READY", "FAILED") and not logs:
            yield "event: close\ndata: stream closed\n\n"
            break

        await asyncio.sleep(1.0)
