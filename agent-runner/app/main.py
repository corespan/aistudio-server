"""agent-runner HTTP API: start a run and stream it, reattach, cancel, list harnesses."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Literal

import structlog
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from langgraph.checkpoint.memory import InMemorySaver

from app import claude, graph as run_graph, harness as harnesses, mcp, registry, tracing, usage
from app.auth import Identity, caller
from app.config import settings
from app.contract import Approval, ConversationRequest, Rejection, RunRequest, WorkflowRequest, json_schema
from app.runlog import RunLog
from app.runs import NotWaiting, RunManager, TooManyRuns

log = structlog.get_logger()


async def _ensure_database(url: str) -> None:
    """Create the agent_runner database on first start (the compose Postgres volume predates it)."""
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict
    name = conninfo_to_dict(url)["dbname"]
    async with await psycopg.AsyncConnection.connect(url, dbname="postgres", autocommit=True) as c:
        cur = await c.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
        if await cur.fetchone() is None:
            await c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
            log.info("db.created", database=name)


@asynccontextmanager
async def lifespan(app: FastAPI):
    reg = registry.load()       # a missing or hand-edited lock file stops the start here
    log.info("registry.loaded", hash=reg.hash, agents=sorted(reg.agents), connectors=sorted(reg.connectors))
    await tracing.check()
    try:
        async with _state(app):
            yield
    finally:
        tracing.shutdown()


@asynccontextmanager
async def _state(app: FastAPI):
    if settings.database_url:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg.rows import dict_row
        from psycopg_pool import AsyncConnectionPool
        await _ensure_database(settings.database_url)
        async with AsyncConnectionPool(
                settings.database_url, open=False, max_size=10,
                kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row}) as pool:
            saver = AsyncPostgresSaver(pool)
            await saver.setup()
            runlog = RunLog(pool)
            await runlog.setup()
            usage.store = usage.Usage(pool)
            await usage.store.setup()
            app.state.runs = RunManager(run_graph.build(saver), runlog)
            yield
    else:
        log.warning("db.none", detail="DATABASE_URL unset: runs are kept in memory only")
        app.state.runs = RunManager(run_graph.build(InMemorySaver()), RunLog())
        yield


app = FastAPI(title="agent-runner", lifespan=lifespan)
# Browser panels on other origins. Auth is a bearer token, never a cookie, so no credentials.
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=False,
                   allow_methods=["GET", "POST", "DELETE"],
                   allow_headers=["authorization", "content-type", "last-event-id"])


def _stream(runs: RunManager, run, after: int = 0) -> StreamingResponse:
    return StreamingResponse(runs.stream(run, after), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _runs() -> RunManager:
    return app.state.runs


@app.post("/runs")
async def start_run(body: RunRequest, who: Identity = Depends(caller)):
    if body.harness and body.harness not in harnesses.load():
        raise HTTPException(422, f"unknown harness {body.harness!r}")
    h = harnesses.load().get(body.harness or "")
    if h is not None and h.engine == "claude-code":
        if not claude.allowed(h, who.user):
            raise HTTPException(403, f"{h.name} runs on a Claude subscription that isn't yours")
        await _windows_left(who.sub)
    history = []
    if body.conversation_id:
        if await _runs().runlog.conversation(who.sub, body.conversation_id) is None:
            raise HTTPException(404, "no such conversation")
        history = await _runs().runlog.history(body.conversation_id, settings.conversation_turns)
    context = body.context.model_dump(exclude_none=True) if body.context else None
    try:
        run = await _runs().start(who, body.message, body.harness, body.conversation_id, context, history)
    except TooManyRuns as exc:
        raise HTTPException(429, str(exc)) from None
    return _stream(_runs(), run)


@app.post("/conversations", status_code=201)
async def create_conversation(body: ConversationRequest = ConversationRequest(), who: Identity = Depends(caller)):
    """A new chat for the panel. Each POST /runs with its id is a turn."""
    return await _runs().runlog.create_conversation(who.sub, body.title)


@app.get("/conversations")
async def list_conversations(who: Identity = Depends(caller)):
    """The caller's conversations, newest first."""
    return await _runs().runlog.conversations(who.sub)


@app.get("/conversations/{conv_id}")
async def get_conversation(conv_id: str, who: Identity = Depends(caller)):
    """A conversation's turns: each message, its answer, status and panel context."""
    conv = await _runs().runlog.conversation(who.sub, conv_id)
    if conv is None:
        raise HTTPException(404, "no such conversation")
    return conv


@app.delete("/conversations/{conv_id}", status_code=204)
async def delete_conversation(conv_id: str, who: Identity = Depends(caller)):
    """Hidden from its owner for good. Its runs and events stay, as the audit log."""
    if not await _runs().runlog.delete_conversation(who.sub, conv_id):
        raise HTTPException(404, "no such conversation")


@app.get("/runs/{run_id}")
async def reattach(run_id: str, who: Identity = Depends(caller),
                   last_event_id: int = Header(default=0)):
    """Reattach to a run's stream. A run this process lost in a restart resumes from its checkpoint."""
    runs = _runs()
    run = runs.runs.get(run_id)
    if run is None:
        row = await runs.runlog.get(run_id)
        if row is None or row["sub"] != who.sub:
            raise HTTPException(404, "no such run")
        if row["status"] != "running":
            raise HTTPException(410, f"run ended: {row['status']}")
        try:
            run = await runs.resume(who, run_id)
        except TooManyRuns as exc:
            raise HTTPException(429, str(exc)) from None
    elif run.sub != who.sub:
        raise HTTPException(404, "no such run")   # someone else's run doesn't exist for this caller
    runs.refresh_token(run, who.token)
    return _stream(runs, run, last_event_id)


@app.delete("/runs/{run_id}", status_code=202)
async def cancel_run(run_id: str, who: Identity = Depends(caller)):
    run = _runs().runs.get(run_id)
    if run is None or run.sub != who.sub:
        raise HTTPException(404, "no such run")
    _runs().cancel(run)
    return {"run_id": run_id, "status": "cancelling"}


async def _windows_left(sub: str) -> None:
    """429 while a Claude usage window is used up, saying when it frees up."""
    for name, w in (await usage.store.windows(sub)).items():
        if w["left"] <= 0:
            raise HTTPException(429, f"the {name} Claude usage limit is used up "
                                     f"({w['used']} of {w['cap']}); it frees up from {w['frees_from']}")


@app.get("/workflows")
async def list_workflows(who: Identity = Depends(caller)):
    """The workflows this caller may start."""
    return [{"name": w.name, "purpose": w.purpose, "input": w.input, "repo": w.repo,
             "steps": [s.id for s in w.steps]}
            for w in registry.load().workflows.values() if who.user in w.users]


@app.post("/workflows/{name}/runs")
async def start_workflow(name: str, body: WorkflowRequest, who: Identity = Depends(caller)):
    """Start a workflow; returns its event stream, like POST /runs. It ends at the approval gate."""
    wf = registry.load().workflows.get(name)
    if wf is None or who.user not in wf.users:
        raise HTTPException(404, "no such workflow")      # someone else's doesn't exist for this caller
    given = getattr(body, wf.input)
    if given is None:
        raise HTTPException(422, f"{name} takes {wf.input!r}")
    await _windows_left(who.sub)
    message = body.task if wf.input == "task" else f"{wf.name}: pull request #{body.pr} on {wf.repo}"
    try:
        run = await _runs().start_workflow(who, name, message, {wf.input: given, "branch": body.branch,
                                                                "reviewers": body.reviewers})
    except TooManyRuns as exc:
        raise HTTPException(429, str(exc)) from None
    return _stream(_runs(), run)


async def _decide(run_id: str, who: Identity, decision: dict):
    try:
        run = await _runs().decide(who, run_id, decision)
    except NotWaiting as exc:
        raise HTTPException(409, str(exc)) from None
    if run is None:
        raise HTTPException(404, "no such run")
    return _stream(_runs(), run, len(run.events))


@app.post("/runs/{run_id}/approve")
async def approve(run_id: str, body: Approval = Approval(), who: Identity = Depends(caller)):
    """Let a workflow stopped at its gate go on: for code-change, the learn step and a draft PR;
    for code-review, the review is posted without the findings in ``drop``. Streams the rest."""
    return await _decide(run_id, who, {"approved": True, "drop": body.drop})


@app.post("/runs/{run_id}/reject")
async def reject(run_id: str, body: Rejection = Rejection(), who: Identity = Depends(caller)):
    """End a workflow at its gate: its workspace is closed and nothing is written."""
    return await _decide(run_id, who, {"approved": False, "reason": body.reason})


@app.get("/usage")
async def get_usage(since: datetime | None = None, by: Literal["day", "run", "agent"] = "day",
                    who: Identity = Depends(caller)):
    """The caller's Claude usage: totals since ``since`` (default 7 days) by day, run or agent,
    and what is left in the 5-hour and daily windows."""
    since = since or datetime.now(UTC) - timedelta(days=7)
    if since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    return {"since": since.isoformat(), "by": by, "totals": await usage.store.report(who.sub, since, by),
            "windows": await usage.store.windows(who.sub)}


@app.get("/harnesses")
async def list_harnesses(who: Identity = Depends(caller)):
    """The harnesses this caller can start: those left with a tool after the caller's role, and
    the claude-code chat agents on a subscription that is the caller's."""
    async with mcp.session(who.token, "harnesses") as s:
        tools = await mcp.list_tools(s)
    mine = {n: h for n, h in harnesses.load().items()
            if h.engine == "claude-code" and h.tools and claude.allowed(h, who.user) and harnesses.tools_for(h, tools)}
    return [{"name": n, "purpose": h.purpose, "engine": h.engine,
             "tools": [t["name"] for t in harnesses.tools_for(h, tools)]}
            for n, h in {**harnesses.available(tools), **mine}.items()]


@app.get("/contract")
async def contract():
    return json_schema()


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "registry": registry.load().hash, "harnesses": sorted(harnesses.load())}
