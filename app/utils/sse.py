"""
app/utils/sse.py — Shared SSE log-streaming helper.
"""

import asyncio
import re
import uuid
from datetime import datetime
from typing import AsyncGenerator, Optional

from fastapi import Request
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.task import Task
from app.models.task_log import TaskLog
from app.models.workload import Workload
from app.models.workload_event import WorkloadEvent

# Masks the last two octets of any IPv4 address in log lines so node IPs are
# never exposed to the client. e.g. 10.6.12.26 → 10.6.x.x
# The address may be followed by a period ("Connecting to 10.6.12.26..."), but not by
# another number (".5": a version string such as 1.2.3.4.5, not an address).
_IP_RE = re.compile(r'(?<![.\d])(\d{1,3}\.\d{1,3})\.\d{1,3}\.\d{1,3}(?!\.?\d)')


def _mask_ip(text: str) -> str:
    return _IP_RE.sub(r'\1.x.x', text)


def _data(text: str) -> str:
    """A log line as SSE data lines. A line with line breaks in it (a multi-line exception
    message) becomes several `data:` lines, which the browser joins back with newlines;
    so no part of it can end the event early or be read as a field such as `event:` or `id:`."""
    return "".join("data: %s\n" % part for part in re.split(r"\r\n|\r|\n", text))


async def task_log_stream(
    workload_db_id: uuid.UUID,
    request: Request,
    *,
    filter_bench_result: bool = False,
    last_event_id: str = "",
    ready_after: Optional[str] = None,
) -> AsyncGenerator[str, None]:
    """
    SSE generator that streams TaskLog lines for all tasks under a workload.

    Each event carries an `id:` field (ISO timestamp). On reconnect the browser
    sends Last-Event-ID; the server resumes strictly after that timestamp so no
    log line is ever duplicated across reconnects.

    Within a single connection, same-timestamp rows are deduplicated by tracking
    already-sent IDs.

    The stream ends on FAILED, or on READY. READY is not always the end: a benchmark
    goes INSTALLING → READY → RUNNING → READY, and the first READY (install done) can
    sit there while execute_benchmark waits in the Celery queue. With ``ready_after``
    set to a trigger (the benchmark stream passes "execute_benchmark"), READY ends the
    stream only when the workload's latest event says that trigger set it.
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

    # Heartbeat: send a keep-alive comment every HEARTBEAT_INTERVAL polls so
    # proxies and load balancers don't close idle SSE connections (e.g. during
    # long model pulls or installs where no logs are written for >30 s).
    HEARTBEAT_INTERVAL = 15  # seconds (each poll sleeps 1 s → every 15 polls)
    heartbeat_counter = 0

    while True:
        if await request.is_disconnected():
            break

        async with AsyncSessionLocal() as db:
            # The state first, then the logs. Each statement sees what was committed when
            # it started (READ COMMITTED), so a log query that runs after we saw the end
            # state also sees every line the worker wrote before reaching it. The other
            # order could see the end state but miss the last lines, and close without them.
            workload_state = (await db.execute(
                select(Workload.state).where(Workload.id == workload_db_id)
            )).scalar()
            if workload_state is None:
                # The workload was deleted (its tasks and logs go with it): nothing more
                # will come. Close, or the client would poll and reconnect forever.
                yield "event: close\ndata: DELETED\n\n"
                break
            ended = workload_state == "FAILED"
            if workload_state == "READY":
                trigger = (await db.execute(
                    select(WorkloadEvent.trigger)
                    .where(WorkloadEvent.workload_id == workload_db_id)
                    .order_by(WorkloadEvent.event_time.desc())
                    .limit(1)
                )).scalar()
                # No events at all (an old row): nothing better to go on than the state.
                ended = ready_after is None or trigger is None or trigger == ready_after

            query = (
                select(TaskLog)
                .join(Task, TaskLog.task_id == Task.id)
                .where(Task.workload_id == workload_db_id)
                .order_by(TaskLog.logged_at.asc())
            )
            if last_seen_date:
                if is_reconnect:
                    # Strict > after a reconnect — don't re-send the last event the
                    # browser already received. This stays on until a row has been
                    # seen on this connection: a quiet first poll (an idle install)
                    # leaves the cursor at that event, and >= would re-send it.
                    query = query.where(TaskLog.logged_at > last_seen_date)
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
                yield "id: %s\n%s\n" % (ts, _data(_mask_ip(log.line)))
                if log.logged_at != last_seen_date:
                    last_sent_ids.clear()
                    last_seen_date = log.logged_at
                last_sent_ids.add(str(log.id))
            if new_logs:
                is_reconnect = False  # the cursor now points at a row this connection saw

        if ended and not new_logs:
            # Send the terminal state as the close event data so the frontend
            # can show "Completed" vs "Failed" without polling the status API.
            close_reason = "FAILED" if workload_state == "FAILED" else "READY"
            yield "event: close\ndata: %s\n\n" % close_reason
            break

        # Send a keep-alive comment when no logs arrived this poll.
        # Browsers and proxies ignore SSE comment lines (start with ':')
        # but they reset the connection idle timer, preventing silent drops.
        if not new_logs:
            heartbeat_counter += 1
            if heartbeat_counter >= HEARTBEAT_INTERVAL:
                yield ": heartbeat\n\n"
                heartbeat_counter = 0
        else:
            heartbeat_counter = 0

        await asyncio.sleep(1.0)
