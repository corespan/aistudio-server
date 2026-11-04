"""
tests/test_sse.py — the SSE log streamer: when a stream ends, the order of its reads, IP masking.

The stream reads the database through a scripted fake session: each poll gets the workload
state, the trigger of its latest event, and the log rows the test gives it.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import uuid

import pytest

from app.utils import sse

T0 = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)


def log(n: int, line: str):
    return SimpleNamespace(id=uuid.UUID(int=n), line=line, logged_at=T0 + timedelta(seconds=n))


class Result:
    def __init__(self, value):
        self.value = value

    def scalar(self):
        return self.value

    def scalars(self):
        return SimpleNamespace(all=lambda: self.value)


class FakeDB:
    """One poll per session: ``polls`` is a list of (state, latest trigger, new log rows)."""

    def __init__(self, polls, reads):
        self.polls, self.reads = polls, reads

    def __call__(self):
        return self

    async def __aenter__(self):
        self.poll = self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, query):
        sql = str(query)
        state, trigger, logs = self.poll
        if "task_logs" in sql:
            self.reads.append("logs")
            return Result(logs(sql) if callable(logs) else logs)
        if "workload_events" in sql:
            self.reads.append("trigger")
            return Result(trigger)
        self.reads.append("state")
        return Result(state)


class Request:
    async def is_disconnected(self):
        return False


async def run(monkeypatch, polls, **kw) -> tuple[list[str], list[str]]:
    reads: list[str] = []
    monkeypatch.setattr(sse, "AsyncSessionLocal", FakeDB(list(polls), reads))

    async def no_wait(_):
        return None
    monkeypatch.setattr(sse.asyncio, "sleep", no_wait)
    out = []
    async for event in sse.task_log_stream(uuid.uuid4(), Request(), **kw):
        out.append(event)
        assert len(out) < 50, "the stream never ended"
    return out, reads


async def test_a_benchmark_stream_stays_open_through_the_ready_after_install(monkeypatch):
    polls = [
        ("INSTALLING", None, [log(1, "installing vllm")]),
        ("READY", "install_dependencies", []),       # install done; execute_benchmark still queued
        ("READY", "install_dependencies", []),
        ("RUNNING", None, [log(2, "benchmark started")]),
        ("READY", "execute_benchmark", []),          # the benchmark's own READY: the end
    ]
    out, _ = await run(monkeypatch, polls, ready_after="execute_benchmark")
    assert [e for e in out if e.startswith("id:")] == [
        "id: %s\ndata: installing vllm\n\n" % log(1, "").logged_at.isoformat(),
        "id: %s\ndata: benchmark started\n\n" % log(2, "").logged_at.isoformat()]
    assert out[-1] == "event: close\ndata: READY\n\n"


async def test_ready_still_ends_a_stream_without_ready_after(monkeypatch):
    # Jupyter: INSTALLING → READY is the whole lifecycle.
    out, _ = await run(monkeypatch, [("INSTALLING", None, [log(1, "pulling image")]),
                                     ("READY", "install_dependencies", [])])
    assert out[-1] == "event: close\ndata: READY\n\n"


async def test_failed_always_ends_the_stream(monkeypatch):
    out, _ = await run(monkeypatch, [("FAILED", None, [])], ready_after="execute_benchmark")
    assert out == ["event: close\ndata: FAILED\n\n"]


async def test_a_ready_with_no_events_at_all_ends_the_stream(monkeypatch):
    # An old workload with no event rows: the state is all there is to go on.
    out, _ = await run(monkeypatch, [("READY", None, [])], ready_after="execute_benchmark")
    assert out == ["event: close\ndata: READY\n\n"]


async def test_the_state_is_read_before_the_logs_so_the_last_lines_are_sent(monkeypatch):
    # The worker wrote its last line and then set READY. A poll that sees READY reads the logs
    # after, so it gets that line, sends it, and closes only on a later poll with nothing new.
    polls = [("READY", "execute_benchmark", [log(1, "Summary: 1794 tok/s")]),
             ("READY", "execute_benchmark", [])]
    out, reads = await run(monkeypatch, polls, ready_after="execute_benchmark")
    assert "Summary: 1794 tok/s" in out[0] and out[-1].startswith("event: close")
    assert reads[:3] == ["state", "trigger", "logs"]


@pytest.mark.parametrize("line, masked", [
    ("ssh to 10.6.12.26", "ssh to 10.6.x.x"),
    ("Connecting to 10.6.12.26...", "Connecting to 10.6.x.x..."),
    ("reached 10.6.12.26.", "reached 10.6.x.x."),
    ("10.6.12.26:22 refused", "10.6.x.x:22 refused"),
    ("node 10.6.12.26, then 192.168.1.4.", "node 10.6.x.x, then 192.168.x.x."),
    ("vllm 0.11.0.1.2 is a version, not an address", "vllm 0.11.0.1.2 is a version, not an address"),
])
def test_node_ips_are_masked_even_before_a_period(line, masked):
    assert sse._mask_ip(line) == masked


async def test_a_quiet_first_poll_after_a_reconnect_does_not_resend_the_last_event(monkeypatch):
    # The browser reconnects with Last-Event-ID = the timestamp of row 1, which it already has.
    # The install is idle, so nothing new comes for a few polls: row 1 must not be sent again.
    row1 = log(1, "pulling image")

    def rows(sql):  # what the database returns: row 1 matches ">=" its own timestamp, not ">"
        return [row1] if ">=" in sql else []
    polls = [("INSTALLING", None, rows)] * 4 + [("FAILED", None, [])]
    out, _ = await run(monkeypatch, polls, last_event_id=row1.logged_at.isoformat())
    assert out == ["event: close\ndata: FAILED\n\n"]


async def test_a_multi_line_log_line_stays_one_event_and_cannot_forge_fields(monkeypatch):
    # An exception message with newlines, one of them looking like an SSE field.
    line = "ERROR: ssh failed\nevent: close\r\nid: 1999-01-01\rcaused by: timeout"
    out, _ = await run(monkeypatch, [("RUNNING", None, [log(1, line)]), ("FAILED", None, [])])
    event = out[0]
    assert event == ("id: %s\ndata: ERROR: ssh failed\ndata: event: close\ndata: id: 1999-01-01\n"
                     "data: caused by: timeout\n\n") % log(1, "").logged_at.isoformat()
    fields = [ln.split(":", 1)[0] for ln in event.strip("\n").split("\n")]
    assert fields == ["id", "data", "data", "data", "data"]    # nothing but data after the id


async def test_a_deleted_workload_closes_the_stream(monkeypatch):
    out, reads = await run(monkeypatch, [(None, None, [])], ready_after="execute_benchmark")
    assert out == ["event: close\ndata: DELETED\n\n"] and reads == ["state"]
