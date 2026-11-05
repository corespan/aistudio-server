"""The shared contract: plan (a task graph), the registry's kinds and stream events.

GET /contract serves the JSON Schema of these models for the panel.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Names other files refer to, and that appear in gateway routes (/mcp/<connector>) and tool names.
NAME = r"^[a-z][a-z0-9-]{0,39}$"


class Task(BaseModel):
    id: str = Field(description="Short id, unique in the plan: t1, t2, ...")
    agent: str = Field(description="The harness that runs this task")
    task: str = Field(description="What this agent must find out, in one or two sentences")
    needs: list[str] = Field(default_factory=list,
                             description="Ids of tasks whose output this task reads")


class Plan(BaseModel):
    goal: str = Field(description="The user's request, restated in one sentence")
    tasks: list[Task]


class Budgets(BaseModel):
    steps: int = 8          # tool rounds for one agent
    tokens: int = 60_000    # claude-code: weighted tokens for the step (docs/agent-runner.md#usage-and-limits)
    seconds: int = 300


class ClaudeSpec(BaseModel):
    """How a claude-code agent runs Claude Code in its workspace."""
    model_config = ConfigDict(extra="forbid")
    # Claude Code permission rules, such as Read or "Bash(git diff *)": its only tools, with the
    # agent's MCP tools (a chat agent may have only those).
    allowed_tools: list[str] = Field(default_factory=list, max_length=30)
    max_turns: int = Field(30, ge=1, le=200)
    # What the step starts in: none, an empty folder (no clone); shallow, the repo's latest commit;
    # full, the whole history (the coding agents: diffs and merge bases need it).
    workspace: Literal["none", "shallow", "full"] = "full"


class Harness(BaseModel):
    """An agent the runner runs (packs/*/agents/*.yaml).

    engine langgraph: the runner's tool loop on vLLM, over the caller's MCP tools.
    engine claude-code: Claude Code in a workspace sandbox (sandbox-mcp), on an anthropic model
    file; never offered to the planner, only started by name and by workflows. Its ``tools`` (exact
    connector/tool names) reach Claude Code as MCP tools, through agentgateway-sandbox.
    """
    model_config = ConfigDict(extra="forbid")
    kind: Literal["agent"] = "agent"
    name: str = Field(pattern=NAME)
    purpose: str
    engine: Literal["langgraph", "claude-code"] = "langgraph"
    model: str = "worker"   # logical model name; claude-code: a model file's name
    # connector/tool, fnmatch patterns allowed in the tool part; intersected with the caller's tools
    tools: list[str] = []
    claude: ClaudeSpec | None = None
    budgets: Budgets = Budgets()

    @model_validator(mode="after")
    def _engine(self) -> "Harness":
        if self.engine == "claude-code" and self.claude is None:
            raise ValueError("a claude-code agent has a claude: block")
        if self.engine == "claude-code" and any("*" in t or "?" in t or "[" in t for t in self.tools):
            raise ValueError("a claude-code agent names each MCP tool exactly: the sandbox gateway allows only those")
        if self.engine == "claude-code" and not 1 <= len(self.claude.allowed_tools) + len(self.tools) <= 30:
            raise ValueError("a claude-code agent has 1 to 30 tools, MCP ones included")
        if self.engine == "langgraph" and (self.claude is not None or not self.tools):
            raise ValueError("a langgraph agent lists tools and has no claude: block")
        return self


class Connector(BaseModel):
    """An MCP server (packs/*/connectors/*.yaml). The runner reaches it at <gateway>/mcp/<name>."""
    model_config = ConfigDict(extra="forbid")
    kind: Literal["connector"]
    name: str = Field(pattern=NAME)
    upstream: str = Field(description="The server itself; agentgateway's route points here")
    description: str = ""
    # ponytail: CEL rules per connector until the policy kind (step 3) writes them from roles.
    rules: list[str] = Field(min_length=1, description="agentgateway mcpAuthorization: who may call which tool")
    browser: bool = Field(False, description="MCP clients outside: served on the gateway's HTTPS port too, "
                                             "with OAuth discovery and CORS for any origin")
    audience: str | None = Field(None, description="The token audience its server checks (default mcp:<name>)")


class Model(BaseModel):
    """A model agents name by logical name (packs/*/models/*.yaml).

    ponytail: Anthropic only. Every other logical name is the vLLM model behind /llm; a vllm
    provider joins when a second vLLM model does.
    """
    model_config = ConfigDict(extra="forbid")
    kind: Literal["model"]
    name: str = Field(pattern=NAME)
    provider: Literal["anthropic"] = Field(description="Served at agentgateway :8051/llm/anthropic, for sandboxes")
    model: str = Field(description="The provider's model id; the runner always passes it (--model)")
    # A subscription is one person's: the Agent SDK terms don't allow sharing it. Anyone else
    # needs an API key on the route first (docs/agent-runner.md#the-anthropic-route).
    # Written into a CEL rule, so plain names only.
    users: list[Annotated[str, Field(pattern=r"^[A-Za-z0-9._@-]+$")]] = Field(
        min_length=1, description="JWT subs the gateway lets through")


class Revise(BaseModel):
    """After a review step: send its findings back to ``step`` while changes are asked for."""
    model_config = ConfigDict(extra="forbid")
    step: str
    rounds: int = Field(2, ge=1, le=5)


class WorkflowStep(BaseModel):
    """One step: a claude-code agent in a mode, the workflow's own command, a gate, or a publish
    step (after a gate: the draft PR, the PR review, or a push to the PR's own agent/ branch).

    Gates: ``write`` shows the result before anything is written; ``triage`` shows findings and
    you drop the ones not to work on."""
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=NAME)
    agent: str | None = None
    mode: Literal["plan", "plan-review", "code", "review", "learn", "hunt", "fix", "address"] | None = None
    run: str | None = Field(None, max_length=2000, description="A fixed command, never from a model")
    gate: Literal["write", "triage"] | None = None
    publish: Literal["pr", "review", "push"] | None = None
    needs: list[str] = []
    revise: Revise | None = None

    @model_validator(mode="after")
    def _one_kind(self) -> "WorkflowStep":
        if sum(x is not None for x in (self.agent, self.run, self.gate, self.publish)) != 1:
            raise ValueError(f"step {self.id}: exactly one of agent, run, gate or publish")
        if (self.agent is None) != (self.mode is None):
            raise ValueError(f"step {self.id}: an agent step has a mode, other steps don't")
        if self.revise and self.mode not in ("plan-review", "review"):
            raise ValueError(f"step {self.id}: only a review step can send work back")
        return self


class Workflow(BaseModel):
    """A fixed sequence of steps on one repo (packs/*/workflows/*.yaml). No planner: an injected
    instruction has nothing to re-plan. It ends at a gate for your approval."""
    model_config = ConfigDict(extra="forbid")
    kind: Literal["workflow"]
    name: str = Field(pattern=NAME)
    purpose: str
    input: Literal["task", "pr"] = Field(description="task: a change to make; pr: a pull request number")
    users: list[str] = Field(min_length=1, description="JWT subs that may start it")
    repo: str = Field(description="owner/name, one of platform.yaml's repos")
    ref: str = Field("master", description="Where the workspace starts; for a PR, its base")
    max_usd: float = Field(10.0, gt=0, description="Cap on Claude Code's reported cost for one run")
    # GitHub logins asked to review the draft PR (a run may add more).
    reviewers: list[Annotated[str, Field(pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")]] = []
    # pr-followup: GitHub logins whose PR comments are work to do. Anyone else's are ignored.
    trusted: list[Annotated[str, Field(pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")]] = []
    steps: list[WorkflowStep] = Field(min_length=1)


class Pack(BaseModel):
    """packs/<name>/pack.yaml: a bundle of files of the kinds above."""
    model_config = ConfigDict(extra="forbid")
    kind: Literal["pack"]
    name: str = Field(pattern=NAME)
    version: str
    description: str = ""
    requires: list[str] = Field(default_factory=list, description="Packs this one uses files from")


class Platform(BaseModel):
    """platform.yaml: one per install."""
    model_config = ConfigDict(extra="forbid")
    name: str
    packs: list[str]
    # GitHub repos (owner/name) workspaces may clone, read-only, through agentgateway-sandbox.
    repos: list[Annotated[str, Field(pattern=r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")]] = []


class WorkflowRequest(BaseModel):
    task: str | None = Field(default=None, min_length=1, max_length=4000, description="For input: task")
    pr: int | None = Field(default=None, ge=1, description="For input: pr")
    branch: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,79}$",
                               description="The new branch's name, under agent/ (default: the run id)")
    reviewers: list[Annotated[str, Field(pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")]] = Field(
        default_factory=list, max_length=10, description="GitHub logins to ask for review, besides the workflow's")

    @field_validator("branch")
    @classmethod
    def _ref(cls, b: str | None) -> str | None:
        # git's own ref rules the pattern can't say: no "..", "//", trailing "/" or ".", or ".lock".
        if b and (".." in b or "//" in b or b.endswith(("/", ".", ".lock"))):
            raise ValueError("not a valid branch name")
        return b


class Approval(BaseModel):
    drop: list[int] = Field(default_factory=list, description="code-review: findings not to post, by number")


class Rejection(BaseModel):
    reason: str = Field("", max_length=1000)


class PanelContext(BaseModel):
    """Where the user is in the panel, sent with a turn so "this run" questions work. The agents
    get it as data, never as instructions."""
    model_config = ConfigDict(extra="forbid")
    page: str | None = Field(default=None, max_length=200, description="The panel page, e.g. benchmarks")
    run_id: str | None = Field(default=None, max_length=200, description="The selected run or workload id")
    node: str | None = Field(default=None, max_length=200, description="The selected node")
    model: str | None = Field(default=None, max_length=200, description="The selected model")


class RunRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    harness: str | None = Field(default=None, description="Skip planning: one task for this harness")
    conversation_id: str | None = Field(default=None, pattern=r"^conv-[0-9a-f]{16}$",
                                        description="A turn in this conversation: its earlier turns go to the agents")
    context: PanelContext | None = None


class ConversationRequest(BaseModel):
    title: str = Field(default="", max_length=200)


# Stream events. Each carries "type"; task events also carry "task" and "agent".
EVENTS = ("run", "plan", "task_queued", "agent_started", "step_started", "step_result",
          "agent_done", "message", "error", "done")


def json_schema() -> dict:
    return {"plan": Plan.model_json_schema(), "harness": Harness.model_json_schema(),
            "connector": Connector.model_json_schema(), "model": Model.model_json_schema(), "workflow": Workflow.model_json_schema(), "pack": Pack.model_json_schema(),
            "platform": Platform.model_json_schema(),
            "run_request": RunRequest.model_json_schema(), "conversation_request": ConversationRequest.model_json_schema(), "events": list(EVENTS)}
