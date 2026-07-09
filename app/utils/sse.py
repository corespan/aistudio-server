"""
app/utils/sse.py — Shared SSE log-streaming helper.
"""

import asyncio
import uuid
from datetime import datetime
from typing import AsyncGenerator, Optional

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
    last_event_id: str = "",
) -> AsyncGenerator[str, None]:
    """
    SSE generator that streams TaskLog lines for all tasks under a workload.

    Each event carries an `id:` field (ISO timestamp). On reconnect the browser
    sends Last-Event-ID; the server resumes strictly after that timestamp so no
    log line is ever duplicated across reconnects.

    Within a single connection, same-timestamp rows are deduplicated by tracking
    already-sent IDs.
    """
    # On reconnect: resume strictly AFTER the last-seen timestamp (> not >=).
    # Within the connection: once we have a cursor, use >= and deduplicate by ID.
    last_seen_date: Optional[datetime] = None
    is_reconnect = False

    if last_event_id:
        try:
            last_seen_date = datetime.fromisoformat(last_event_id)
            is_reconnect = True
        except (ValueError, TypeError):
            pass

    # Tracks row IDs sent at exactly `last_seen_date` to skip duplicates.
    last_sent_ids: set = set()

    while True:
        if await request.is_disconnected():
            break

        async with AsyncSessionLocal() as db:
            query = (
                select(TaskLog)
                .join(Task, TaskLog.task_id == Task.id)
                .where(Task.workload_id == workload_db_id)
                .order_by(TaskLog.logged_at.asc())
            )
            if last_seen_date:
                if is_reconnect:
                    # Strict > on first poll after reconnect — don't re-send the
                    # last event the browser already received.
                    query = query.where(TaskLog.logged_at > last_seen_date)
                    is_reconnect = False  # switch to >= mode for subsequent polls
                else:
                    # >= within a running connection to catch same-timestamp rows.
                    query = query.where(TaskLog.logged_at >= last_seen_date)

            result = await db.execute(query)
            logs = result.scalars().all()
            new_logs = [log for log in logs if str(log.id) not in last_sent_ids]

            for log in new_logs:
                if filter_bench_result and log.line.startswith("BENCH_RESULT:"):
                    last_seen_date = log.logged_at
                    last_sent_ids.add(str(log.id))
                    continue
                ts = log.logged_at.isoformat() if log.logged_at else ""
                yield "id: %s\ndata: %s\n\n" % (ts, log.line)
                if log.logged_at != last_seen_date:
                    last_sent_ids.clear()
                    last_seen_date = log.logged_at
                last_sent_ids.add(str(log.id))

            state_result = await db.execute(
                select(Workload.state).where(Workload.id == workload_db_id)
            )
            workload_state = state_result.scalar()

        if workload_state in ("READY", "FAILED") and not new_logs:
            # Send the terminal state as the close event data so the frontend
            # can show "Completed" vs "Failed" without polling the status API.
            close_reason = "FAILED" if workload_state == "FAILED" else "READY"
            yield "event: close\ndata: %s\n\n" % close_reason
            break

        await asyncio.sleep(1.0)
