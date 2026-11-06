# Agent runner

`agent-runner` turns a question from a chat panel into a small graph of tasks, runs an agent for
each task (independent ones at the same time), and writes one answer from their reports. It is
built on [LangGraph](https://github.com/langchain-ai/langgraph) and runs in the `mcp` compose
profile beside agentgateway and the MCP server.

```
panel ──Keycloak token──> agentgateway :8050 /assistant/ (HTTPS) ──> agent-runner :8060 ──same token──> agentgateway :8052 ──> /mcp/aistudio  (platform tools)
  │                                                                       │                            (inside only)    ├──> /mcp/sandbox   (sandbox_run, workspaces)
  └─ logs in to Keycloak                                                  │                                             ├──> /llm           (vLLM)
     (realm aistudio)                                                     │                                             └──> /github        (after approval)
                                                                          └── Postgres: agent_runner database (checkpoints, run log, conversations, usage)
```

It holds no keys. Every tool call and every LLM call carries the caller's own Keycloak token
through agentgateway, so no agent can do more than the caller could, however many run at once.
agentgateway's `:8052` listener is for the runner inside the compose network and is never
published; `:8050` is the HTTPS door for MCP clients outside ([mcp-server.md](mcp-server.md)).

A browser panel reaches the runner at `https://<MCP_PUBLIC_HOST>:8050/assistant/` (the same
paths as below, behind that prefix): HTTPS from the gateway's certificate, the token passed on
untouched for the runner to check, CORS answered by the runner (`CORS_ORIGINS`), and the event
stream forwarded as it comes. `:8060` stays for local tools and `curl` on the host. Every
endpoint, ready to import into Postman, Insomnia or Bruno:
[agent-runner.postman_collection.json](agent-runner.postman_collection.json).

Each agent opens one MCP session per connector in the registry (see [Packs](#packs)), at
`GATEWAY_URL/mcp/<connector>`, and calls each tool on the connector that listed it. A connector
that is down is logged and left out, so its tools aren't offered; the run goes on with the rest.
`sandbox_run` is described in [sandbox.md](sandbox.md).

## A run

1. **Plan.** The `planner` model splits the request into tasks, each for one harness, with
   `needs` for tasks that read another task's result. A request may name a harness instead,
   which skips planning.
2. **Validate.** Every rule is checked in code: known harnesses only, at most `MAX_TASKS`
   tasks, unique ids, and `needs` pointing only at earlier tasks (so there is no cycle). A
   rejected plan goes back to the planner with the reason, at most twice.
3. **Run.** Tasks whose inputs are ready run in parallel, at most `MAX_PARALLEL_AGENTS` at
   once. Each agent has its own context, its own MCP session and only its harness's tools.
   A task's input from another task is passed as capped, fenced data, never as instructions.
   A failed task is retried once; tasks that need it are skipped.
4. **Summarize.** The `summarizer` model writes the answer from the reports. With one task,
   that task's answer is the answer.

LangGraph checkpoints the run after every step. If the runner restarts mid-run, reattaching
(`GET /runs/{id}`) resumes from the last finished task.

This build reads only: the runner binds a tool only when the MCP server marks it read-only, so
`start_workload`, `start_jupyter` and `stop_workload` are never offered to an agent. Writes come
with plan approval in the next phase.

## Packs

Everything the runner works with is declared in files under `agent-runner/platform/`, not in
code. `platform.yaml` lists the packs this install enables; each pack is a folder of files, each
with a `kind`:

```
agent-runner/platform/
  platform.yaml                     name, and the packs enabled: [core, aistudio, devflow]
  registry.lock.json                generated: every enabled file, checked, with one hash
  packs/
    core/      pack.yaml · connectors/sandbox.yaml
    aistudio/  pack.yaml · connectors/aistudio.yaml · agents/analyst.yaml, assistant.yaml, diagnostics.yaml
    devflow/   pack.yaml · models/claude-coder.yaml
```

| Kind | A file declares | Example |
|---|---|---|
| `pack` | `pack.yaml`: name, version, and the packs it `requires` | `packs/aistudio/pack.yaml` |
| `connector` | An MCP server: `upstream`, the role `rules` for its tools, and `browser` (CORS for browser clients) | `packs/core/connectors/sandbox.yaml` |
| `agent` | An agent the runner runs: purpose, model, tools, budget (the harness) | `packs/aistudio/agents/analyst.yaml` |
| `model` | A logical model name: `provider` (today only `anthropic`), the provider's `model` id, and the `users` (JWT subs) allowed to use it | `packs/devflow/models/claude-coder.yaml` |

After changing any file under `platform/`, render:

```bash
make registry
```

It checks every file, then writes these files. None of them is edited by hand:

| File | Holds | Read by |
|---|---|---|
| `agent-runner/platform/registry.lock.json` | Every enabled file, with one hash | The runner, at start; it logs the hash, and `GET /healthz` shows it |
| `gateway/config.yaml` | The `/llm` route, and one `/mcp/<name>` route per connector with its JWT check and rules | agentgateway (restart it after a render) |
| `gateway/sandbox.yaml` | `/llm/anthropic` on `:8051`, when a pack has an `anthropic` model | agentgateway-sandbox (restart it after a render) |
| `gateway/audiences.json` | `aistudio-mcp` (the gateway and aistudio-mcp), `agent-runner`, and each connector's audience (`mcp:<name>` unless it sets `audience`) | `gateway/kc.py setup` (`make mcp-up`): an audience mapper for each, so every Keycloak token carries them all |

A new connector is one file: `make registry`, restart agentgateway and agent-runner, and mint new
tokens if its server checks its own audience. The aistudio connector's rules come from the MCP
server's manifest ([mcp-server.md](mcp-server.md#roles)).

`make runner-test` (and CI) fails when any of the three files is out of date, when a file has an unknown
kind or field, when two files declare the same name, when a pack needs one that isn't enabled,
or when an agent lists a tool on a connector no enabled pack has. More kinds (skills,
remote agents, sandbox profiles, policy) join the registry as they are built.

Tool names are `connector/tool` in agent files, with `fnmatch` patterns allowed in the tool part
(`aistudio/get_*`). The model sees the tool's own name; only when two connectors offer the same
name does it see `connector__tool`.

## Panel tools, writes and the Claude planner

**Panel tools** (`platform/panel-tools.json`, `panel/<name>` in an agent's `tools`) change what
the user sees in the AI Studio panel: navigate, filter, show or compare runs, open logs, prefill a
form, open a delete confirmation, show a health notice, download the table as CSV. The browser
runs them: the runner streams a `panel` event (`tool`, `args`) and tells the model only that it
was sent. A Claude agent calls them on a local stdio MCP server, `panel`, that sandbox-mcp starts
in its workspace (`sandbox-mcp/app/panel_server.py`); it only acknowledges. The panel keeps a copy
of the file, runs only the tools in it, and checks each call's arguments against its schema.
Browser-only tools (`click_element`, `set_style`, `fill_input`, `list_fields`) are never offered.

**Writes** (`writes:` in an agent file: exact `connector/tool` names). The agent never gets the
tool itself: it gets `request_<tool>`, which only records the call (at most 5 a run). After the
answer the runner streams an `approval` event (`actions`: `id`, `tool`, `args`, `agent`) and the
run stops, `awaiting_approval`. `POST /runs/{id}/approve` resumes it with the approver's token and
the runner makes each call itself, so the gateway checks that person's role again; `/reject` ends
it with nothing done. Only the caller who started the run can decide. Assistant and
assistant-claude may ask for `start_workload`, `start_jupyter`, `stop_jupyter` and
`delete_run_results`. Writes count toward a Claude agent's 30 tools; they never reach the sandbox
gateway's routes.

**The planner without vLLM.** A run with no agent named is planned by an LLM. With
`PLANNER_ENGINE=auto` (the default), a caller listed on `planner-claude`'s model gets Claude
instead: `planner-claude` (no tools) writes the plan as JSON over the Claude chat agents, and the
summary when there is more than one task. Everyone else gets the LLM (`/llm`, 503 without
`LLM_HOST`). `PLANNER_ENGINE=llm` turns the Claude planner off, `claude` forces it. A user's Claude
steps run one at a time: sandbox-mcp allows one workspace step per user.

## Harnesses

A harness is an agent file in a pack (`kind: agent`): its purpose (the planner reads it), its
tools and its budget. An agent's tools are the caller's tools (agentgateway filters `tools/list`
by role) that its harness lists, and only those. A harness left with no tools for a caller is not
offered to them.

| Harness | For | Tools |
|---|---|---|
| `analyst` | Benchmark numbers: find, compare, explain, compute | `list_benchmarks`, `get_benchmark`, `summary`, `throughput_compare`, `workload_history`, `platform_catalog`, `model_config`, `sandbox_run` |
| `diagnostics` | Why something failed or is slow | task status and logs, service health, queue, the host probes (admin only), GPU node probes |
| `assistant` | Short lookups | a few of each |

A viewer gets all three, but the host and node probes are admin-only, so a viewer's
`diagnostics` has only the platform reads (task status and logs, workload history, service
health).

## Claude Code agents

An agent file with `engine: claude-code` runs Claude Code in a workspace sandbox
([sandbox.md](sandbox.md#workspaces)) instead of the runner's tool loop. Claude Code brings its own
edit, search and shell tools and reads the repo's `CLAUDE.md`. The `devflow` pack has two:

| Agent | Claude Code tools | Turns | Step budget |
|---|---|---|---|
| `coder` | Read, Edit, Write, Glob, Grep, `Bash(git *)`, `Bash(make *)`, `Bash(python -m pytest *)` | 40 | 600k weighted tokens, 900 s |
| `reviewer` | Read, Glob, Grep, `Bash(git diff *)`, `Bash(git log *)`, `Bash(git show *)` | 15 | 250k weighted tokens, 600 s |

```yaml
kind: agent
name: coder
engine: claude-code
purpose: ...
model: claude-coder          # a model file: provider anthropic, model claude-opus-5-5, users [rsingh]
claude:
  allowed_tools: [Read, Edit, ...]
  max_turns: 40
budgets: { tokens: 600000, seconds: 900 }
```

A step opens a fresh workspace on the platform's first repo at `master`, runs Claude Code with
the task, takes the diff and closes the workspace (until workflows keep one across steps). The
answer is Claude Code's reply, the changed files and the diff. Nothing leaves the sandbox: no
commit, no push.

They are never offered to the planner. A run names one (`{"harness": "coder"}`), and only a
`users` entry of its model file may (403 for anyone else: it's that person's subscription). The
caller also needs the operator or admin role, which the workspace tools require. A failed step
is not retried, since it spends the subscription.

Each line of Claude Code's stream becomes a run event (`step_started` / `step_result` per tool
call), a usage row per turn, and a span: `agent:coder` with a generation per turn and a tool
span per call ([Traces](#traces)).

### Chat agents on Claude

The `aistudio` pack has the three chat agents again on Claude Code, for a subscription's own user
while there is no API key or vLLM: `analyst-claude`, `assistant-claude` and `diagnostics-claude`.
Each has its langgraph twin's purpose and platform tools and the model file `claude-chat`
(Claude Sonnet 5.5, `users: [rsingh]`). The analyst also has `Bash(python3 *)` for statistics,
and diagnostics the repo's Read, Glob and Grep, to read the code a log line points at. The panel sends `{"harness": "assistant-claude", ...}` with a turn;
`GET /harnesses` lists them, with `engine: claude-code`, only for that user.

```yaml
kind: agent
name: assistant-claude
engine: claude-code
model: claude-chat
tools: [aistudio/summary, aistudio/check_service_health, ...]   # exact names, no wildcards
claude:
  allowed_tools: []          # Claude Code's own tools: none, only the platform tools above
  workspace: none            # none: an empty folder | shallow: the latest commit | full (default)
  max_turns: 10
budgets: { tokens: 200000, seconds: 180 }
```

A claude-code agent's `tools` reach Claude Code as MCP tools (`mcp__aistudio__summary`): the
step's MCP config names only its connectors, at `agentgateway-sandbox:8051/mcp/<name>` with the
caller's token (`--strict-mcp-config`, so a `.mcp.json` in the repo adds nothing). That route
allows only the tools some claude-code agent names, only for the subscription's users, and still
the connector's role rules; aistudio-mcp checks the token again. So nothing else running in a
workspace, such as a repo's tests in a coding workflow, can reach more than the chat agents can,
and `start_workload` or `delete_run_results` not at all.

The question goes to one agent, with the conversation and panel context, and its reply is the
answer: no planner, which runs on vLLM. Turns count against the same Claude limits as the
workflows ([Usage and limits](#usage-and-limits)).

An agent that can't edit (no Edit, Write, MultiEdit or NotebookEdit) costs one container per turn:

- Its workspace is what `claude.workspace` asks for. `none` opens no container at all, and
  `shallow` clones only the latest commit. The coding agents keep `full`, since diffs and merge
  bases need the history.
- The tree a step leaves isn't saved (`keep: false`), so there's no snapshot to pack, and no
  diff step.
- In a conversation, the workspace stays open for the next turn of the same agent, and only the
  first turn opens one. It's kept in the runner's memory: after a restart, or once sandbox-mcp's
  TTL (`SANDBOX_WORKSPACE_TTL_HOURS`, 24) has deleted it, the next turn opens a new one. Without a
  conversation, it's closed after the turn.

## Workflows

A workflow (`kind: workflow`, `packs/*/workflows/*.yaml`) is a fixed sequence of steps on one
workspace, ending at a gate for your approval. There is no planner, so an instruction injected
through repo or PR text has nothing to re-plan. The `devflow` pack has two:

| Workflow | Input | Steps |
|---|---|---|
| `code-change` | `{"task": "..."}` | plan → plan-review (up to 2 rounds back to plan) → code → check → review (up to 2 rounds back to code) → **approve** → learn → draft PR |
| `code-review` | `{"pr": 12}` | check → review → **approve** → post the review |
| `bug-hunt` | `{"task": "app/routers/"}` (the scope, or "anything") | hunt (read-only, may run the tests) → **triage** (you drop findings) → fix → check → review (up to 2 rounds back to fix) → **approve** → learn → draft PR |
| `pr-followup` | `{"pr": 9}` (one of the agent's own PRs) | address (new comments from `trusted` logins, failed checks) → check → review (up to 2 rounds back) → **approve** → push to the same branch, and a PR comment |

`code-change` and `bug-hunt` also take `"branch": "fix-status"` (the PR's new branch becomes
`agent/fix-status`; default `agent/<run-id>`) and `"reviewers": ["login", ...]` (asked for review on
the draft PR, with the workflow file's own `reviewers`).

```yaml
kind: workflow
name: code-change
input: task
users: [rsingh]                      # who may start it (your subscription)
repo: corespan/aistudio-server       # one of platform.yaml's repos
ref: master
max_usd: 10                          # Claude Code's reported cost for the run
steps:
  - { id: plan, agent: coder, mode: plan }
  - { id: plan-review, agent: reviewer, mode: plan-review, needs: [plan], revise: { step: plan, rounds: 2 } }
  - { id: code, agent: coder, mode: code, needs: [plan] }
  - { id: check, run: "with-postgres env POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=5432 python -m pytest tests/ -q -p no:cacheprovider" }
  - { id: review, agent: reviewer, mode: review, needs: [code, check], revise: { step: code, rounds: 2 } }
  - { id: approve, gate: write }
```

- **Agent steps** run a claude-code agent in a mode. `plan` gets only the agent's read-only tools
  (Read, Glob, Grep, git), and its answer is the plan. `code` is told the check command and may
  run it. `plan-review` and `review` answer with a JSON verdict (`approve` or
  `changes_requested`, a summary, findings with severity, file and line); an answer that isn't
  one fails the run.
- **`run` steps** are the workflow's own command, never a model's. It runs with no token, and
  the tree it leaves isn't kept.
- **`revise`** sends a review's findings, fenced as data, back to an earlier step while changes
  are asked for, at most `rounds` times. A failed check in the review's `needs` counts as
  changes asked, whatever the reviewer said. When the rounds run out, the run goes on to the gate
  and you decide with the findings in view.
- **`needs`** passes earlier steps' outputs into the prompt, fenced as data.
- **The gate** stops the run with status `awaiting_approval` and a message with the check, the
  reviews, the revision rounds, the numbered findings and the diff ([Approval and
  GitHub](#approval-and-github)). A workspace nobody decides on expires after 24 hours. A failed
  run closes its workspace.

Each step is one pass of the run graph, so it is checkpointed: a restart resumes at the next
step. `make registry` checks every workflow: its agents are claude-code agents, `needs` and
`revise` point at earlier steps, and its repo is one the sandbox gateway serves.

```bash
curl -N http://localhost:8060/workflows/code-change/runs -H "Authorization: Bearer $T"   -H 'content-type: application/json' -d '{"task": "app/database.py has no module docstring; add one."}'
```

Only a workflow's `users` see it or may start it (404 for anyone else); one runs at a time per
user, at most 6 starts an hour (429 beyond), and a run has `CLAUDE_RUN_MAX_SECONDS` in all. Before
each agent step the run's reported cost is checked against `max_usd`: past it, the run stops and
a `limit_events` row says so. Cost is known only when a step ends, so `max_usd` can't stop a step
in the middle; the token limits do that.

## Approval and GitHub

Nothing in a workflow writes outside its sandbox before you approve, and only its `publish` step
writes then. A run stopped at its gate waits (LangGraph `interrupt()`, checkpointed: a restart
keeps it waiting) until its owner decides:

```bash
curl -N -X POST http://localhost:8060/runs/$RUN/approve -H "Authorization: Bearer $T" \
  -H 'content-type: application/json' -d '{}'                  # code-review: {"drop": [1, 3]}
curl -N -X POST http://localhost:8060/runs/$RUN/reject -H "Authorization: Bearer $T" \
  -H 'content-type: application/json' -d '{"reason": "not now"}'
```

Both stream the rest of the run. Anyone else gets 404; a run not at its gate, 409.

- **Reject**: the workspace is closed. Nothing was written, and nothing is.
- **Approve, code-change**: the `learn` step runs. The coder may edit only `CLAUDE.md` and
  `.agents/knowledge/`, to write down what it had to discover about the repo. Then a draft PR on
  a new branch `agent/<run-id>`, into the workflow's `ref`, with two commits. The first is
  exactly the tree the gate showed you (the snapshot it recorded), whatever ran after. The
  second is only the changes under `CLAUDE.md` and `.agents/knowledge/` since then, so you
  review what it learned like code; it is left out when there are none. The PR body has the
  task, the reviews, the check and who approved.
- **Approve, code-review**: the review is posted as a COMMENT, never an approval or a request
  for changes (those stay with people), without the findings you listed in `drop` (numbered as
  the gate message shows them). Findings with a file and line go inline; if GitHub refuses a
  line (not in the diff), they go into the review's body. The workspace is opened at the PR's
  own base commit from GitHub, so merged PRs review correctly too.
- **The triage gate (bug-hunt)**: the hunter's findings, numbered. Approve with `drop` for the
  ones not to fix; the rest are the fix step's task. Dropping all of them, or rejecting, ends the
  run with nothing written. Then the usual fix, check and review, and the approval gate with the
  diff before any PR.
- **Reviewers**: after the draft PR opens, the workflow's `reviewers` and the run's are asked
  for review. If GitHub refuses (yourself, someone without access), the PR stays open and the
  answer says so.
- **Approve, pr-followup**: the change you saw becomes one commit on top of the PR's head, and its
  `agent/` branch moves forward to it, never forced: if someone pushed meanwhile, GitHub refuses
  and the run waits at the gate again. Then a comment on the PR says what was done.

**pr-followup** reads a PR's comments since its last commit from the logins in the workflow's
`trusted` list only. Anyone else's comments are untrusted text and are ignored (the PR comment
says how many). It also reads the failed checks on the PR's head, with their annotations. It
works only on open PRs from an `agent/` branch of the same repo, and with nothing to address it
ends at once, before opening a workspace. Start it yourself after commenting: GitHub can't send
webhooks into the test host's network, so nothing triggers it.

A publish that fails (a token without write access, a branch that moved) doesn't throw the
approved change away: the run goes back to its gate with the error and its workspace, and
approving again retries only the publish.

**How it writes.** The runner, never a sandbox: it reads the approved files from the
snapshot (`workspace_files`, no container), then calls GitHub's REST API through agentgateway's
`/github` route with the caller's token. The route checks the token, that its username is one of the
workflows' `users`, and the exact call, on the workflows' repos only:

| | |
|---|---|
| Read | a PR, its review comments and conversation, a commit, the check runs on a commit and their annotations |
| Create | blobs, trees, commits, new branches, a PR, a review, review requests, a PR comment |
| Update | a branch under `agent/` only (pr-followup's push; the runner sends `force: false`) |

Then it adds `GITHUB_PAT` from its own environment. Creating a branch that exists fails, and only
`agent/` branches can be updated, so `master` can't be written through it. Nothing can be merged
or deleted, and a model never sees the token.

`GITHUB_PAT` goes in `.env` on the host: a fine-grained token for this repository only, with
Contents and Pull requests read and write (and Metadata read, which GitHub adds). The PRs and
reviews appear under your account. Branch protection on `master` is still worth having.

## Usage and limits

Every Claude turn goes in the `usage` table (agent_runner database): sub, run, task, agent,
model, message id, input, output, cache reads and writes, and weighted tokens. When a step ends,
one more row, `kind = adjust`, holds Claude Code's own final totals minus the turns' sum, with
its reported cost (an API-price equivalent; on a subscription you aren't billed it). Claude
Code's stream undercounts output, so this row makes a step's rows add up to exactly what Claude
Code reported. The table holds counts only, no prompt text, and is kept.

**Weighted tokens** = input + cache writes + output + 0.1 × cache reads. Claude Code re-reads a
large cached prefix every turn; counting it in full would trip limits for no real cost.

| Limit | Default | Enforced | When hit |
|---|---|---|---|
| Turns per step | agent file `max_turns` | Claude Code `--max-turns` | the step ends with what it has |
| Weighted tokens per step | agent file `budgets.tokens` | sandbox-mcp, on every stream line | the container is killed mid-run; nothing is kept; `budget_exceeded` |
| Per run | `CLAUDE_RUN_MAX_WEIGHTED` 2,000,000 | runner | the next step is refused |
| Per user, rolling 5 h / 24 h | `CLAUDE_WINDOW_5H_WEIGHTED` 4,000,000 / `CLAUDE_DAY_WEIGHTED` 12,000,000 | runner, from the table | new runs get 429 saying when the window frees; a running step is stopped at the cap |
| Wall clock | agent file `budgets.seconds`; `CLAUDE_RUN_MAX_SECONDS` 2700 per run | sandbox-mcp and runner | the container is killed |
| Steps at once | `CLAUDE_STEPS_AT_ONCE` 1 | runner semaphore, and sandbox-mcp's one per user | the next step waits |
| Requests to Anthropic | 60 a minute | agentgateway-sandbox | 429 to Claude Code, which retries |

Before each step the runner sets its cap to the smallest of the step's own, what the run has
left and what both windows have left, and sandbox-mcp stops the step there. Every hit writes a
`limit_events` row (limit, value, cap, `refused` or `stopped`).

```bash
curl http://localhost:8060/usage?by=day -H "Authorization: Bearer $T"     # or by=run, by=agent; since=<ISO time>
```

`GET /usage` returns the caller's totals since `since` (default 7 days) and, per window, what is
used, left, and when its oldest usage drops out.

## Setup

The chat needs a model: vLLM (step 1) for everyone, or, with neither vLLM nor an API key, your
own Claude subscription for you alone (step 1b).

1. Point the gateway at an OpenAI-compatible LLM in `.env`:

   ```bash
   LLM_HOST=10.0.0.20:8000          # host:port of the server (vLLM, for example)
   LLM_MODEL=Qwen/Qwen2.5-32B-Instruct
   LLM_API_KEY=                     # the server's own key, if it has one
   ```

   The model needs tool calling (vLLM: `--enable-auto-tool-choice` and the model's
   `--tool-call-parser`) and JSON-schema output for the plan.

1b. Or the Claude chat agents ([Chat agents on Claude](#chat-agents-on-claude)):
   - Run `claude setup-token` on your own machine.
   - Put the token in `.env` yourself as `CLAUDE_CODE_OAUTH_TOKEN=`, with no line breaks: `make mcp-up` checks.
   - Make sure your Keycloak username is in `users` of
     `agent-runner/platform/packs/aistudio/models/claude-chat.yaml`, then run `make registry`.
   - Give yourself the operator or admin role (the workspace tools need it).
   - The panel sends `"harness": "assistant-claude"` (or `analyst-claude`, `diagnostics-claude`)
     with each turn. `make mcp-up` also builds the workspace image they run in.

2. Start the profile: `make mcp-up`. The runner creates its `agent_runner` database on first
   start.

3. Users and roles are Keycloak's: `make mcp-user NAME=alice ROLE=viewer`. For curl and
   scripts, `make -s mcp-token ROLE=viewer CLIENT=panel` logs in a `mcp-check-viewer` user
   through the panel's client and prints its token (an hour; without `CLIENT=panel`, the MCP
   client's 10 minutes). Set `PANEL_REDIRECT_URIS` in `.env` to the panel's own URL before
   `make mcp-up`, so Keycloak sends its users back there.

## The LLM route

agentgateway serves `/llm` (see `gateway/config.yaml`). It checks the caller's JWT on every
call, replaces it with `LLM_API_KEY`, so user tokens never reach the LLM server, and maps every
logical model name (`planner`, `worker`, `summarizer`) to `LLM_MODEL`. With `LLM_HOST` unset it
answers 503, and the MCP route is unaffected.

## The Anthropic route

For Claude Code running in a workspace sandbox (the coding workflow). A second agentgateway
container, `agentgateway-sandbox`, serves only `/llm/anthropic` on `:8051`. Workspaces join
`sandbox-egress`, an internal Docker network with no route out, and this container is the only
other thing on it, at the fixed address `172.30.51.10` (gVisor can't use Docker's DNS, so
workspaces get `--add-host agentgateway-sandbox:172.30.51.10`). It's a separate container, not a
second listener on the main gateway, because agentgateway can't bind a listener to one network:
a sandbox could otherwise reach `/mcp/*` and `/llm` with its JWT. It also keeps the token out of
the main gateway. Nothing is published on the host.

A workspace runs Claude Code with `ANTHROPIC_BASE_URL=http://agentgateway-sandbox:8051/llm/anthropic` and
the caller's JWT as `ANTHROPIC_AUTH_TOKEN`. The route:

- checks the Keycloak token, and that its username (`preferred_username`) is one of the `users`
  of an `anthropic` model file;
- allows 60 requests a minute (429 after that; Claude Code retries), a backstop under the
  runner's own limits;
- replaces the JWT with `CLAUDE_CODE_OAUTH_TOKEN`, so no container ever holds the real token.

`CLAUDE_CODE_OAUTH_TOKEN` comes from `claude setup-token` and goes in `.env` on the host, on one
line. `make mcp-up` refuses a value with whitespace in it, which is what a paste across a
terminal line wrap leaves behind (Anthropic answers 401 and Claude Code says "Not logged in").

**It is one person's subscription.** The Agent SDK terms don't allow sharing a subscription's
login or limits with other people, so `users` lists only its owner. Before anyone else runs
it, replace the header swap in `_anthropic()` (`agent-runner/app/registry.py`) with an Anthropic
API key. The token also works only for Claude Code: a plain Messages API call with it is
refused, so the planner and summarizer stay on `/llm`.

Always pass the model file's `model` as `--model`: behind a gateway Claude Code can't tell it is
on a subscription and picks a different default.

## API

All calls need `Authorization: Bearer <token>`. CORS allows any origin: auth is a bearer token,
never a cookie.

| Call | Does |
|---|---|
| `POST /runs` | `{"message": "...", "harness": null, "conversation_id": null, "context": null}`: starts a run and returns its event stream |
| `POST /conversations` | `{"title": "..."}`: a new chat for the panel ([Conversations](#conversations)) |
| `GET /conversations` | The caller's conversations, newest first, with their turn counts |
| `GET /conversations/{id}` | A conversation's turns: message, answer, status, panel context |
| `DELETE /conversations/{id}` | Hides it from the caller for good (204); its runs stay as the audit log |
| `GET /runs/{id}` | Reattach: replays the events (after `Last-Event-ID`), then streams live ones |
| `DELETE /runs/{id}` | Cancel the run and every agent in it |
| `GET /harnesses` | The harnesses this caller can use, with their tools |
| `GET /usage` | The caller's Claude usage and what is left in the windows ([Usage and limits](#usage-and-limits)) |
| `GET /workflows` | The workflows this caller may start |
| `POST /workflows/{name}/runs` | `{"task": "..."}` or `{"pr": n}`: starts a workflow and returns its event stream ([Workflows](#workflows)) |
| `POST /runs/{id}/approve` | `{"drop": [n, ...]}` (code-review): a workflow at its gate goes on and publishes ([Approval and GitHub](#approval-and-github)) |
| `POST /runs/{id}/reject` | `{"reason": "..."}`: a workflow at its gate ends; nothing is written |
| `GET /contract` | JSON Schema of the plan, harness and request, and the event names |
| `GET /healthz` | Liveness |

A run belongs to its caller: someone else's run id answers 404. A caller has at most
`MAX_RUNS_PER_USER` runs active at once (429 beyond).

### Conversations

The panel's chat. A conversation is a list of runs: each `POST /runs` with its
`conversation_id` is one turn, and streams like any run. The new turn's agents get the
conversation's last `CONVERSATION_TURNS` (6) finished turns, newest kept when they're longer
than `CONVERSATION_CHARS` (6000), after the request and fenced as data: the earlier answers came
from tool output, so they are no more trusted than it. With a planner, it's told to use them only
to resolve references ("this run", "the second one") and to put the specifics in the tasks.

`context` says where the user is in the panel: `{"page", "run_id", "node", "model"}`, all
optional, nothing else accepted. It goes to the agents the same way, as data, so "is this run
slower than last week's?" works without the user typing the id.

A conversation is its owner's alone: anyone else gets 404 for it, can't add a turn to it, and
doesn't see it listed. Deleting it hides it from its owner for good. The runs and their events
stay, because they are the audit log of what the agents did. History is kept until then; a
retention period, if one is needed, is a cleanup job on `conversations.deleted_at` and run age.

### Who the caller is

Keycloak, realm `aistudio` (the same login as the MCP door, [mcp-server.md](mcp-server.md)),
issues every token. The panel logs its users in through the `aistudio-panel` client (code + PKCE,
redirects from `PANEL_REDIRECT_URIS`), whose tokens last an hour: a coding workflow runs up to 45
minutes on the token it started with, and approving resumes it with your fresh one. MCP clients'
tokens last 10 minutes.

The runner checks the token against Keycloak's keys (`JWKS_URL`, cached), its issuer
(`JWT_ISSUER`, `https://<MCP_PUBLIC_HOST>:8050/realms/aistudio`, as clients see it) and its
audience `agent-runner`. It passes the same token to agentgateway on every call, so the caller's
role (`roles.aistudio`, from the user's `aistudio_role` attribute) decides which tools any agent
can reach. `kc.py setup` puts every audience in `gateway/audiences.json` into every token.

Two names for a caller: `sub`, Keycloak's user id, owns runs, conversations, workspaces and
usage; the username (`preferred_username`) is what allow-lists name, such as a model's or a
workflow's `users` and the gateway's rules for the Claude subscription and the PAT. Users
can't change their username (`kc.py setup` keeps `editUsernameAllowed` off), and nobody can
register themselves.

Events (server-sent; each has `seq`, `type`, and task events also `task` and `agent`):
`run`, `plan`, `task_queued`, `agent_started`, `step_started` (a tool call), `step_result`,
`agent_done`, `panel` (a panel tool for the browser), `approval` (writes waiting for Approve),
`message` (the answer), `error`, `done` (`status`: ok, failed, cancelled, timeout,
awaiting_approval).

```bash
T=$(make -s mcp-token ROLE=viewer CLIENT=panel)
curl -N http://localhost:8060/runs -H "Authorization: Bearer $T" \
  -H 'content-type: application/json' \
  -d '{"message": "Which model had the best throughput on T4, and are the services healthy?"}'
```

Runs are live: one with no stream attached for `STREAM_GRACE_SECONDS` is cancelled. A quiet
stream gets a `: ping` comment every `STREAM_PING_SECONDS` (15), so proxies keep it open while
a Claude step works.

## Limits

| Setting | Default | |
|---|---|---|
| `MAX_PARALLEL_AGENTS` | 2 | Agents running at once in one run |
| `MAX_RUNS_PER_USER` | 2 | Active runs per caller |
| `MAX_LLM_INFLIGHT` | 4 | LLM calls in flight per runner; set from the server's measured throughput |
| `MAX_TASKS` / `MAX_REPLANS` | 6 / 2 | Plan size; rejected plans retried |
| `RUN_MAX_STEPS` / `RUN_MAX_TOKENS` / `RUN_MAX_SECONDS` | 40 / 200000 / 600 | Budget per run, shared by its agents |
| `STREAM_GRACE_SECONDS` | 30 | A run with no stream for this long is cancelled |
| `TOOL_RESULT_CHARS` / `TASK_OUTPUT_CHARS` | 12000 / 6000 | Caps on text reaching a model |
| `LLM_MAX_TOKENS` | 2048 | Output cap per LLM call, so a looping model can't run to its context limit |
| `LLM_REASONING_EFFORT` | `none` | Sent to the model; `none` turns off thinking on Qwen3-style models (a tool round in seconds, not a minute) |

Each harness has its own step, token and time budget as well. When an agent has less than one
LLM call's worth of time left (`LLM_TIMEOUT_SECONDS`), it stops calling tools and answers with
what it has, as it does when its steps run out.

## Traces

With Langfuse configured, every run is one trace: the planner call, each agent as a span named
`agent:<harness>`, and inside it every model call (prompt, tool schemas, tokens) and tool round.
The session is the run id, so a run resumed after a restart lands in the same session, and the
user is the caller's `sub`.

```bash
LANGFUSE_HOST=http://<langfuse-host>:<port>
LANGFUSE_PUBLIC_KEY=pk-lf-...      # Langfuse: Settings -> API keys
LANGFUSE_SECRET_KEY=sk-lf-...
```

Put them in `.env` (never in git), then `docker compose --profile mcp up -d agent-runner`. The
startup log says which case you are in: `tracing.ready`, `tracing.unreachable` (the network
path), `tracing.auth_failed` (the keys) or `tracing.disabled`. Tracing is fail-open: a Langfuse
that is down or unreachable costs a run nothing.

A claude-code agent's span holds a generation per Claude turn (model, input, output, cache reads
and writes) and a tool span per tool call, then a last generation, `reported totals`, with the
adjust row's tokens and Claude Code's reported cost. Turns carry cost 0, so Langfuse adds no
estimate of its own: the trace's usage and cost equal the usage table's.

The runner pins Langfuse SDK v4, which works with self-hosted Langfuse 3.63 and later. SDK v5
needs a 4.59+ server. Traces hold the same things as the run log: prompts, tool output and the
caller's `sub`.

## Safety

- **Tool output is data.** The MCP server fences every result; the runner fences task outputs
  it passes between agents. Prompts tell every model to treat both as data, and an injected
  instruction in a tool result can't change the plan: the plan is fixed before any tool runs.
- **Tools only shrink.** Caller's role (agentgateway) ∩ harness list ∩ read-only.
- **Repeats and spin.** A repeated call is refused with a nudge; three rounds of repeats, or
  the step budget, make the agent answer with what it has.
- **No keys.** The container runs as a non-root user, read-only, with no capabilities, and is
  mounted nothing: it fetches Keycloak's public keys to check tokens.

## Not yet

- **Writes with approval**, beyond each action: plan approval before any agent runs, and an
  `Approval-Id` in the MCP server's audit log.
- **Per-task scheduling and the reviewer**: ready tasks start in waves today, so a task waits
  for its whole wave, not just its inputs. A Postgres task table (and a reviewer agent that
  checks joined results) comes with that.
- **Token refresh**: there is no proxy here, so a run lasts as long as the caller's token;
  reattaching with a fresh token switches the run to it.
