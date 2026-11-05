"""Live runs: drive the run graph, fan its events out to SSE streams, enforce the run caps.

A run is live: when no stream has been attached for STREAM_GRACE_SECONDS it is cancelled.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import time
from dataclasses import dataclass, field

import structlog
from langgraph.types import Command

from app import graph as run_graph, harness as harnesses, tracing
from app.config import settings

log = structlog.get_logger()


@dataclass
class Run:
    id: str
    sub: str
    events: list[dict] = field(default_factory=list)
    streams: set[asyncio.Queue] = field(default_factory=set)
    detached_at: float = field(default_factory=time.monotonic)
    task: asyncio.Task | None = None
    status: str = "running"
    workflow: str | None = None
    started: float = field(default_factory=time.monotonic)
    user: str = ""          # the username, for allow-lists; sub owns the run
    conversation_id: str | None = None


class TooManyRuns(Exception):
    pass


class NotWaiting(Exception):
    """The run isn't at a gate."""


class RunManager:
    def __init__(self, graph, runlog):
        self.graph = graph
        self.runlog = runlog
        self.runs: dict[str, Run] = {}

    def active(self, sub: str) -> int:
        return sum(1 for r in self.runs.values() if r.sub == sub and r.status == "running")

    async def start(self, ident, message: str, harness: str | None, conversation_id: str | None = None,
                    context: dict | None = None, history: list | None = None) -> Run:
        """A run; in a conversation, ``history`` is its earlier turns, for the agents as data."""
        return await self._start(ident, message, {"message": message, "harness": harness, "replans": 0,
                                                  "history": history or [], "context": context or {}},
                                 conversation_id=conversation_id)

    async def start_workflow(self, ident, name: str, message: str, inputs: dict) -> Run:
        """One workflow at a time per user, and a few starts an hour: it's one subscription.
        ponytail: counted in memory, so a restart forgets the hour; the usage windows don't."""
        mine = [r for r in self.runs.values() if r.sub == ident.sub and r.workflow]
        if sum(r.status == "running" for r in mine) >= settings.workflows_at_once:
            raise TooManyRuns(f"at most {settings.workflows_at_once} workflow at a time per user")
        if sum(time.monotonic() - r.started < 3600 for r in mine) >= settings.workflow_starts_per_hour:
            raise TooManyRuns(f"at most {settings.workflow_starts_per_hour} workflow starts an hour per user")
        return await self._start(ident, message, {"message": message, "workflow": name, "inputs": inputs},
                                 workflow=name)

    async def _start(self, ident, message: str, inputs: dict, workflow: str | None = None,
                     conversation_id: str | None = None) -> Run:
        if self.active(ident.sub) >= settings.max_runs_per_user:
            raise TooManyRuns(f"at most {settings.max_runs_per_user} active runs per user")
        run = Run(id="run-" + secrets.token_hex(8), sub=ident.sub, workflow=workflow, user=ident.user,
                  conversation_id=conversation_id)
        await self.runlog.start(run.id, ident.sub, message, conversation_id, inputs.get("context") or None)
        self._launch(run, ident.token, inputs)
        return run

    async def resume(self, ident, run_id: str) -> Run:
        """Continue a run this process doesn't hold (after a restart) from its last checkpoint."""
        if self.active(ident.sub) >= settings.max_runs_per_user:
            raise TooManyRuns(f"at most {settings.max_runs_per_user} active runs per user")
        run = Run(id=run_id, sub=ident.sub, events=await self.runlog.events(run_id), user=ident.user)
        self._launch(run, ident.token, None)
        return run

    async def decide(self, ident, run_id: str, decision: dict) -> Run | None:
        """Resume a run stopped at its gate with your decision. None: no such run of yours."""
        run = self.runs.get(run_id)
        if run is None:
            row = await self.runlog.get(run_id)
            if row is None or row["sub"] != ident.sub:
                return None
            run = Run(id=run_id, sub=ident.sub, events=await self.runlog.events(run_id), status=row["status"],
                      user=ident.user)
        if run.sub != ident.sub:
            return None
        if run.status != "awaiting_approval":
            raise NotWaiting(f"the run is {run.status}, not waiting for approval")
        run.status = "running"
        self._launch(run, ident.token, Command(resume={**decision, "by": ident.user}))
        return run

    def _launch(self, run: Run, token: str, inputs) -> None:
        self.runs[run.id] = run
        run_graph.RUNTIME[run.id] = run_graph.Runtime(token=token, sub=run.sub, user=run.user,
                                                      conversation=run.conversation_id)
        run.task = asyncio.create_task(self._drive(run, inputs))
        asyncio.create_task(self._watch(run))

    def refresh_token(self, run: Run, token: str) -> None:
        """The newest token the caller sent; every later MCP call uses it."""
        rt = run_graph.RUNTIME.get(run.id)
        if rt:
            rt.token = token

    async def _emit(self, run: Run, event: dict) -> None:
        event = {"seq": len(run.events) + 1, **event}
        run.events.append(event)
        for q in run.streams:
            q.put_nowait(event)
        try:
            await self.runlog.event(run.id, event)
        except Exception as exc:  # noqa: BLE001 - the log must never break a run
            log.warning("runlog.event_failed", run_id=run.id, error=str(exc))

    async def _drive(self, run: Run, inputs) -> None:
        # The root span encloses the whole run, so every LangGraph span nests under it.
        with tracing.root(run.id, run.sub, inputs.get("message") if isinstance(inputs, dict) else None) as finish:
            await self._drive_traced(run, inputs, finish)

    async def _drive_traced(self, run: Run, inputs, finish) -> None:
        config = {"configurable": {"thread_id": run.id}, **tracing.run_config(run.id, run.sub)}
        status, answer = "failed", None
        if isinstance(inputs, dict):
            await self._emit(run, {"type": "run", "run_id": run.id})
        try:
            async with asyncio.timeout(_max_seconds(inputs)):
                async for event in self.graph.astream(inputs, config, stream_mode="custom"):
                    await self._emit(run, event)
            snap = await self.graph.aget_state(config)
            values = snap.values
            # Stopped at a gate (interrupt): not over, waiting for POST /runs/{id}/approve or /reject.
            status = "awaiting_approval" if snap.next else values.get("status", "failed")
            answer = values.get("answer")
        except asyncio.CancelledError:
            status = "cancelled"
        except TimeoutError:
            status = "timeout"
            await self._emit(run, {"type": "error", "detail": f"the run took over {_max_seconds(inputs)}s"})
        except Exception as exc:  # noqa: BLE001 - reported to the caller, logged in full
            log.exception("run.failed", run_id=run.id)
            await self._emit(run, {"type": "error", "detail": f"{type(exc).__name__}: {exc}"})
        finally:
            finish(answer, status)
            run.status = status
            run_graph.RUNTIME.pop(run.id, None)
            await self._emit(run, {"type": "done", "status": status})
            try:
                await self.runlog.finish(run.id, status, answer)
            except Exception as exc:  # noqa: BLE001
                log.warning("runlog.finish_failed", run_id=run.id, error=str(exc))
            # ponytail: finished runs stay in memory for reattach until the process restarts;
            # the run log has them for good.

    async def _watch(self, run: Run) -> None:
        """Cancel the run once no stream has been attached for the grace period."""
        while run.status == "running":
            await asyncio.sleep(1)
            if not run.streams and time.monotonic() - run.detached_at > settings.stream_grace_seconds:
                log.info("run.detached", run_id=run.id)
                self.cancel(run)
                return

    def cancel(self, run: Run) -> None:
        if run.task and not run.task.done():
            run.task.cancel()

    async def stream(self, run: Run, after: int = 0):
        """SSE lines: every event after ``after``, then live ones until the run is done."""
        q: asyncio.Queue = asyncio.Queue()
        run.streams.add(q)
        try:
            sent = after
            for event in list(run.events):
                if event["seq"] > sent:
                    sent = event["seq"]
                    yield _sse(event)
            if run.status != "running" and run.events and run.events[-1]["type"] == "done":
                return
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), settings.stream_ping_seconds)
                except TimeoutError:
                    # A Claude step can be quiet for minutes; a comment keeps proxies from closing the stream.
                    yield ": ping\n\n"
                    continue
                if event["seq"] <= sent:
                    continue
                sent = event["seq"]
                yield _sse(event)
                if event["type"] == "done":
                    return
        finally:
            run.streams.discard(q)
            run.detached_at = time.monotonic()


def _max_seconds(inputs) -> int:
    """claude-code runs and workflows get their own wall clock. ponytail: a resumed run (no
    inputs) gets the default."""
    if not isinstance(inputs, dict) or inputs.get("workflow"):     # a decision resumes a workflow
        return settings.claude_run_max_seconds
    name = inputs.get("harness")
    h = harnesses.load().get(name) if name else None
    return settings.claude_run_max_seconds if h and h.engine == "claude-code" else settings.run_max_seconds


def _sse(event: dict) -> str:
    return f"id: {event['seq']}\nevent: {event['type']}\ndata: {json.dumps(event, default=str)}\n\n"
