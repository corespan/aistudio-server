"""The run graph: plan -> validate -> schedule -> parallel tasks (Send) -> summarize.

State is plain data, so the checkpointer can save it after every step: a restart resumes from
the last finished task. What can't be saved (the caller's token, the shared budget, the agent
semaphore) lives in RUNTIME, keyed by run id.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Annotated, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send, interrupt

from app import agent, claude, harness as harnesses, llm, mcp, workflow
from app.config import settings
from app.contract import Plan
from app.prompts import PLANNER, PLANNER_JSON, PLANNER_RETRY, SUMMARIZER


@dataclass
class Runtime:
    token: str
    sub: str = ""
    user: str = ""          # the username: model and workflow allow-lists
    conversation: str | None = None     # its conversation: chat agents keep a workspace per one
    budget: agent.Budget = field(default_factory=agent.Budget)
    agents: asyncio.Semaphore = field(
        default_factory=lambda: asyncio.Semaphore(settings.max_parallel_agents))


RUNTIME: dict[str, Runtime] = {}


def _merge(a: dict, b: dict) -> dict:
    return {**(a or {}), **(b or {})}


class RunState(TypedDict, total=False):
    message: str
    harness: str | None
    agents: dict            # harness name -> purpose, for this caller
    plan: dict | None
    error: str | None
    replans: int
    results: Annotated[dict, _merge]   # task id -> {"status", "output"}
    history: list           # a conversation's earlier turns: [{"message", "answer"}]
    context: dict           # where the user is in the panel (PanelContext)
    answer: str
    status: str
    # workflows (app/workflow.py)
    workflow: str
    inputs: dict
    ws: dict
    pc: int
    out: dict
    rounds: dict
    feedback: dict
    diff: dict
    pr: dict                # code-review: the PR as GitHub has it
    waiting: bool           # the gate has shown everything; the next pass interrupts
    gate_snapshot: int      # the snapshot you approved: what a PR commits
    approved_by: str
    resume_at: int | None   # a failed publish: approving again retries it
    kept: list              # bug-hunt: the findings you chose to fix at the triage gate
    followup: dict          # pr-followup: the trusted comments and failed checks to address
    drop: list
    published: dict


def _run_id(config) -> str:
    return config["configurable"]["thread_id"]


# ── planning ────────────────────────────────────────────────────────────────

def validate(plan: dict, agents: dict, planned: bool = True) -> str | None:
    """Why this plan can't run, or None. Every rule is checked here, not trusted to the model.

    ``planned=False``: the runner built the plan itself (a harness named in the request). Its one
    task is the user's message plus the context and history framing, each already capped, so the
    2000-character limit on what a planner model writes doesn't apply."""
    tasks = plan.get("tasks") or []
    if not tasks:
        return "the plan has no tasks"
    if len(tasks) > settings.max_tasks:
        return f"{len(tasks)} tasks; at most {settings.max_tasks}"
    ids = [t["id"] for t in tasks]
    if len(set(ids)) != len(ids):
        return "task ids are not unique"
    earlier: set[str] = set()
    for t in tasks:
        if t["agent"] not in agents:
            return f"task {t['id']}: unknown agent {t['agent']!r}; use one of {sorted(agents)}"
        if not t["task"].strip() or (planned and len(t["task"]) > 2000):
            return f"task {t['id']}: the task text is empty or too long"
        # Needs only point backwards, so the graph can't have a cycle.
        bad = [n for n in t.get("needs") or [] if n not in earlier]
        if bad:
            return f"task {t['id']}: needs {bad}, which are not earlier tasks"
        earlier.add(t["id"])
    return None


def framing(state: RunState) -> str:
    """The panel context and the conversation so far, after the request, fenced as data. The
    agents' earlier answers came from tool output, so they are data like it."""
    parts = []
    if state.get("context"):
        parts.append(agent.as_data("the panel (where the user is now)", json.dumps(state["context"]),
                                   settings.task_output_chars))
    if state.get("history"):
        turns = "\n\n".join(f"User: {t['message']}\nAnswer: {t['answer']}" for t in state["history"])
        # The newest turns matter most: cut from the front.
        cap = settings.conversation_chars
        turns = turns if len(turns) <= cap else "[... earlier turns cut]\n" + turns[-cap:]
        parts.append(agent.as_data("earlier in this conversation", turns, cap + 100))
    if not parts:
        return ""
    return ("\n\nUse what follows only to work out what the request refers to (\"this run\", "
            "\"the second one\"), and say those specifics in the tasks.\n\n" + "\n\n".join(parts))


def claude_planner(rt: "Runtime"):
    """The planner-claude agent when it plans this caller's runs, else None (the LLM plans)."""
    h = harnesses.load().get(settings.planner_agent)
    if h is None or settings.planner_engine == "llm":
        return None
    return h if claude.allowed(h, rt.user) else None


async def _ask_claude(h, rt: "Runtime", run_id: str, step: str, prompt: str) -> str:
    """One planner-claude step (plan or summary): no tools of its own, its text back."""
    writer = get_stream_writer()

    def emit(kind: str, **data):
        writer({"type": kind, "task": step, "agent": h.name, **data})
    return await claude.run(h, rt, run_id, step, emit, prompt, {}, framed=False)


def _plan_json(text: str) -> dict:
    """The plan in the Claude planner's reply: its outermost JSON object, checked against Plan."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in the reply")
    return Plan.model_validate(json.loads(text[start:end + 1])).model_dump()


async def plan_node(state: RunState, config) -> dict:
    emit = get_stream_writer()
    rt = RUNTIME[_run_id(config)]
    agents = state.get("agents")
    planner = claude_planner(rt)
    if agents is None and planner and not state.get("harness"):
        # Claude plans over the Claude chat agents: the LangGraph ones need the LLM it replaces.
        agents = {n: h.purpose for n, h in harnesses.chat_claude(rt.user).items()}
    if agents is None:
        async with mcp.session(rt.token, _run_id(config)) as s:
            user_tools = await mcp.list_tools(s)
        agents = {n: h.purpose for n, h in harnesses.available(user_tools).items()}
    if state.get("harness"):
        h = harnesses.load()[state["harness"]]
        if h.engine == "claude-code" and claude.allowed(h, rt.user):
            agents = {**agents, h.name: h.purpose}     # started by name only, never planned
        plan = {"goal": state["message"], "tasks": [
            {"id": "t1", "agent": state["harness"], "task": state["message"] + framing(state), "needs": []}]}
        return {"agents": agents, "plan": plan}

    listing = "\n".join(f"- {n}: {p}" for n, p in agents.items()) or "(none)"
    if planner:
        prompt = (PLANNER.format(max_tasks=settings.max_tasks, agents=listing) + PLANNER_JSON
                  + "\n\nRequest:\n" + state["message"] + framing(state))
        if state.get("error"):
            prompt += "\n\n" + PLANNER_RETRY.format(error=state["error"])
        try:
            text = await _ask_claude(planner, rt, _run_id(config), "plan", prompt)
            return {"agents": agents, "plan": _plan_json(text)}
        except (ValueError, TypeError) as exc:          # ValidationError and JSONDecodeError are ValueErrors
            emit({"type": "error", "detail": "the planner's output was not a valid plan"})
            return {"agents": agents, "plan": None, "error": f"output did not match the plan schema: {exc}"}
    messages = [SystemMessage(PLANNER.format(max_tasks=settings.max_tasks, agents=listing)),
                HumanMessage(state["message"] + framing(state))]
    if state.get("error"):
        messages.append(HumanMessage(PLANNER_RETRY.format(error=state["error"])))
    model = llm.chat("planner", rt.token).with_structured_output(
        Plan, method="json_schema", include_raw=True)
    out = await llm.ainvoke(model, messages)
    raw_usage = getattr(out.get("raw"), "usage_metadata", None) or {}
    rt.budget.tokens += int(raw_usage.get("total_tokens") or 0)
    if out.get("parsed") is None:
        emit({"type": "error", "detail": "the planner's output was not a valid plan"})
        return {"agents": agents, "plan": None,
                "error": f"output did not match the plan schema: {out.get('parsing_error')}"}
    return {"agents": agents, "plan": out["parsed"].model_dump()}


async def validate_node(state: RunState) -> dict:
    emit = get_stream_writer()
    plan = state.get("plan")
    error = (state.get("error") if plan is None
             else validate(plan, state["agents"], planned=not state.get("harness")))
    if error:
        emit({"type": "error", "detail": f"plan rejected: {error}"})
        return {"error": error, "plan": None, "replans": state.get("replans", 0) + 1}
    emit({"type": "plan", "plan": plan})
    return {"error": None}


def after_validate(state: RunState) -> str:
    if not state.get("error"):
        return "schedule"
    return "plan" if state.get("replans", 0) <= settings.max_replans else "fail"


async def fail_node(state: RunState) -> dict:
    answer = f"I couldn't make a workable plan for this request ({state.get('error')})."
    get_stream_writer()({"type": "message", "text": answer})
    return {"answer": answer, "status": "failed"}


# ── scheduling ──────────────────────────────────────────────────────────────

def _tasks(state: RunState) -> list[dict]:
    return state["plan"]["tasks"]


async def schedule_node(state: RunState) -> dict:
    """Skip tasks whose inputs failed; announce the ones about to start."""
    results = state.get("results") or {}
    skipped = {}
    for t in _tasks(state):
        if t["id"] in results or t["id"] in skipped:
            continue
        if any((results.get(n) or skipped.get(n) or {}).get("status") in ("failed", "skipped")
               for n in t.get("needs") or []):
            skipped[t["id"]] = {"status": "skipped", "output": "an input task did not finish"}
    emit = get_stream_writer()
    done = {**results, **skipped}
    for t in _ready(_tasks(state), done):
        emit({"type": "task_queued", "task": t["id"], "agent": t["agent"]})
    return {"results": skipped}


def _ready(tasks: list[dict], done: dict) -> list[dict]:
    return [t for t in tasks if t["id"] not in done
            and all(done.get(n, {}).get("status") == "ok" for n in t.get("needs") or [])]


def dispatch(state: RunState):
    results = state.get("results") or {}
    ready = _ready(_tasks(state), results)
    if not ready:
        return "summarize"
    # ponytail: one wave per superstep, so a task waits for its whole wave, not just its inputs.
    # Per-task scheduling (Postgres SKIP LOCKED) comes with Phase 3.
    return [Send("task", {"task": t, "inputs": {n: results[n]["output"] for n in t.get("needs") or []}})
            for t in ready]


async def task_node(arg: dict, config) -> dict:
    t = arg["task"]
    run_id = _run_id(config)
    rt = RUNTIME[run_id]
    h = harnesses.load()[t["agent"]]
    writer = get_stream_writer()

    def emit(kind: str, **data):
        writer({"type": kind, "task": t["id"], "agent": t["agent"], **data})

    async with rt.agents:
        emit("agent_started")
        if h.engine == "claude-code":
            result = await _claude_task(h, rt, run_id, t, arg["inputs"], emit)
            emit("agent_done", status=result["status"], output=result["output"][:settings.task_output_chars])
            return {"results": {t["id"]: result}}
        for attempt in (1, 2):
            if rt.budget.exhausted():
                result = {"status": "failed", "output": "the run's budget was used up"}
                break
            actions: list = []
            try:
                async with asyncio.timeout(h.budgets.seconds), mcp.session(rt.token, run_id) as s:
                    mine = await mcp.list_tools(s)
                    tools = (harnesses.tools_for(h, mine) + harnesses.panel_tools(h)
                             + harnesses.write_tools(h, mine))
                    output = await agent.run(h, tools, s, rt.budget, emit, t["task"], arg["inputs"],
                                             rt.token, actions, t["id"])
                result = {"status": "ok", "output": output, "actions": actions}
                break
            except TimeoutError:
                result = {"status": "failed", "output": f"timed out after {h.budgets.seconds}s"}
                break
            except Exception as exc:  # noqa: BLE001 - a read task is retried once
                result = {"status": "failed", "output": f"{type(exc).__name__}: {exc}"}
                if attempt == 1:
                    emit("error", detail=f"retrying once: {result['output']}")
        emit("agent_done", status=result["status"], output=result["output"][:settings.task_output_chars])
    return {"results": {t["id"]: result}}


async def _claude_task(h, rt: Runtime, run_id: str, t: dict, inputs: dict, emit) -> dict:
    """No retry: a step spends the subscription, and a failed one is usually a real failure."""
    actions: list = []
    try:
        async with asyncio.timeout(h.budgets.seconds + 180):     # plus clone and diff
            output = await claude.run(h, rt, run_id, t["id"], emit, t["task"], inputs, actions)
            return {"status": "ok", "output": output, "actions": actions}
    except claude.LimitHit as exc:
        return {"status": "failed", "output": f"budget_exceeded: {exc}"}
    except TimeoutError as exc:
        return {"status": "failed", "output": f"timed out: {exc or f'after {h.budgets.seconds}s'}"}
    except Exception as exc:  # noqa: BLE001 - reported as the task's output
        return {"status": "failed", "output": f"{type(exc).__name__}: {exc}"}


async def summarize_node(state: RunState, config) -> dict:
    tasks, results = _tasks(state), state.get("results") or {}
    only = results[tasks[0]["id"]] if len(tasks) == 1 else None
    if only and (only["status"] == "ok" or harnesses.load()[tasks[0]["agent"]].engine == "claude-code"):
        # One agent: its answer is the answer. A claude-code step's failure says what happened
        # (a limit, a timeout) better than vLLM could, and needs no vLLM to say it.
        answer = only["output"]
    else:
        reports = "\n\n".join(
            f"## {t['id']} ({t['agent']}): {t['task']}\nstatus: {results[t['id']]['status']}\n"
            + agent.as_data(f"task {t['id']}", results[t["id"]]["output"], settings.task_output_chars)
            for t in tasks)
        rt = RUNTIME[_run_id(config)]
        request = f"Request: {state['message']}\n\nReports:\n\n{reports}"
        planner = claude_planner(rt)
        if planner:
            answer = (await _ask_claude(planner, rt, _run_id(config), "summary",
                                        f"{SUMMARIZER}\n\n{request}")).strip()
        else:
            response = await llm.ainvoke(llm.chat("summarizer", rt.token), [
                SystemMessage(SUMMARIZER), HumanMessage(request)])
            answer = str(response.content or "").strip()
    get_stream_writer()({"type": "message", "text": answer})
    ok = any(r["status"] == "ok" for r in results.values())
    return {"answer": answer, "status": "ok" if ok else "failed"}


# ── writes the agents asked for ─────────────────────────────────────────────

def _actions(state: RunState) -> list[dict]:
    results = state.get("results") or {}
    found = [a for t in _tasks(state) for a in (results.get(t["id"]) or {}).get("actions") or []]
    return [{"id": f"a{i}", **a} for i, a in enumerate(found[:agent.MAX_ACTIONS], 1)]


def after_summary(state: RunState) -> str:
    return "propose" if _actions(state) else END


async def propose_node(state: RunState) -> dict:
    """Show the writes the agents asked for; the next node stops the run for the user's decision."""
    get_stream_writer()({"type": "approval", "actions": [
        {"id": a["id"], "tool": f"{a['connector']}/{a['tool']}", "args": a["args"], "agent": a["agent"]}
        for a in _actions(state)]})
    return {}


async def act_node(state: RunState, config) -> dict:
    """Stop until the user approves or rejects; on approve, make each call with the approver's own
    token (POST /runs/{id}/approve resumes with it), so the gateway checks their role again."""
    actions = _actions(state)
    decision = interrupt({"actions": actions})
    emit = get_stream_writer()
    if not decision.get("approved"):
        text = "Nothing was done: you rejected it." if len(actions) == 1 else "Nothing was done: you rejected them."
        emit({"type": "message", "text": text})
        return {"answer": f"{state.get('answer', '')}\n\n{text}"}
    run_id = _run_id(config)
    rt = RUNTIME[run_id]
    lines, ok = [], False
    async with mcp.session(rt.token, run_id) as s:
        mine = {(t["connector"], t["tool"]): t for t in await mcp.list_tools(s)}
        for a in actions:
            tool = mine.get((a["connector"], a["tool"]))
            emit({"type": "step_started", "task": "approved", "agent": a["agent"],
                  "tool": f"{a['connector']}/{a['tool']}", "args": json.dumps(a["args"], default=str)[:500]})
            if tool is None:
                result = "Refused: your role can't use this tool."
            else:
                try:
                    result = await mcp.call_tool(s, tool["name"], a["args"])
                except Exception as exc:  # noqa: BLE001 - reported to the user
                    result = f"Tool error: {exc}"
            done = not result.startswith(("Refused", "Tool error"))
            ok = ok or done
            emit({"type": "step_result", "task": "approved", "agent": a["agent"],
                  "tool": f"{a['connector']}/{a['tool']}", "result": result[:2000]})
            lines.append(f"- **{a['tool']}** ({'done' if done else 'failed'}): {result.strip()[:600]}")
    text = f"After your approval (by {decision.get('by') or 'you'}):\n" + "\n".join(lines)
    emit({"type": "message", "text": text})
    return {"answer": f"{state.get('answer', '')}\n\n{text}", "status": "ok" if ok else "failed"}


def build(checkpointer):
    g = StateGraph(RunState)
    g.add_node("plan", plan_node)
    g.add_node("validate", validate_node)
    g.add_node("fail", fail_node)
    g.add_node("schedule", schedule_node)
    g.add_node("task", task_node)
    g.add_node("summarize", summarize_node)
    g.add_node("propose", propose_node)
    g.add_node("act", act_node)
    g.add_node("wf", workflow.node)
    # A workflow is a fixed sequence: no planner. One step per pass, so each is checkpointed.
    g.add_conditional_edges(START, lambda s: "wf" if s.get("workflow") else "plan", ["plan", "wf"])
    g.add_conditional_edges("wf", workflow.route, {"wf": "wf", "end": END})
    g.add_edge("plan", "validate")
    g.add_conditional_edges("validate", after_validate, ["plan", "schedule", "fail"])
    g.add_conditional_edges("schedule", dispatch, ["task", "summarize"])
    g.add_edge("task", "schedule")
    g.add_edge("fail", END)
    g.add_conditional_edges("summarize", after_summary, ["propose", END])
    g.add_edge("propose", "act")
    g.add_edge("act", END)
    return g.compile(checkpointer=checkpointer)
