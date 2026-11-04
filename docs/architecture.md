# Architecture

AIStudio Server is a Python backend that orchestrates GPU workloads on remote nodes over SSH. It does not run workloads itself — it builds shell commands, executes them remotely, and collects results.

---

## Service Overview

```
┌─────────────────────────────────────────────────────┐
│                    Client (UI / API)                 │
└──────────────────────┬──────────────────────────────┘
                       │ HTTP / SSE
┌──────────────────────▼──────────────────────────────┐
│              FastAPI (api container)                 │
│  POST /benchmarks/start → enqueues Celery task       │
│  GET  /benchmarks/{id}/logs/stream → SSE log tail    │
│  GET  /benchmarks → leaderboard results              │
└──────────┬───────────────────────┬──────────────────┘
           │ PostgreSQL            │ RabbitMQ
           │ (state + results)     │ (task queue)
┌──────────▼──────────────────────▼──────────────────┐
│           Celery Worker (worker container)           │
│  execute_benchmark() — the main orchestration task   │
│    1. SSHExecutor.run_command(manifest_builder cmd)  │
│    2. Streams stdout/stderr → TaskLog rows           │
│    3. Parses BENCH_RESULT:{json} → BenchmarkResult   │
└──────────────────────┬──────────────────────────────┘
                       │ SSH
┌──────────────────────▼──────────────────────────────┐
│                  GPU Node (remote)                   │
│  docker run ... llminference:1.0.0-nvidia            │
│    → benchmark.py starts vLLM server                 │
│    → sweeps concurrency levels                       │
│    → prints BENCH_RESULT:{json} to stdout            │
│    → writes /results/<run_id>/ on the node           │
└─────────────────────────────────────────────────────┘
```

---

## Components

### FastAPI (`api` container)

Entry point: `app/main.py`. Handles all HTTP traffic:

- **Benchmark routes** (`app/routers/benchmarks.py`) — start, status, log streaming, leaderboard CRUD
- **Jupyter routes** (`app/routers/jupyter.py`) — launch, status, health, delete
- **System routes** (`app/routers/system.py`) — health check, model config, workload types
- **Results routes** (`app/routers/results.py`) — leaderboard filters, compare, distinct values
- **Ingest routes** (`app/routers/ingest.py`) — legacy `POST /api/v1/metrics` for external runners
- **GPU specs routes** (`app/routers/gpu_specs.py`) — GPU hardware metadata

The API is stateless — all persistent state lives in PostgreSQL.

### Celery Worker (`worker` container)

Entry point: `app/worker.py`. Picks up tasks from RabbitMQ and runs the benchmark orchestration:

1. Validates the request and reads workload config from PostgreSQL
2. Calls `ManifestBuilder.build_llm_benchmark_command()` to assemble the `docker run` shell command
3. Runs it on the GPU node via `SSHExecutor`
4. Tails stdout/stderr, writing each line as a `TaskLog` row in PostgreSQL (the SSE endpoint reads these)
5. Parses `BENCH_RESULT:{json}` lines from stdout into `BenchmarkResult` rows

### ManifestBuilder (`app/services/manifest_builder.py`)

Builds the exact shell string executed on the GPU node. Responsibilities:

- Sources `~/.aistudio/env` on the node (loads `HF_TOKEN` for gated models — see [gpu-nodes.md](./gpu-nodes.md))
- Constructs `docker run` with the correct GPU flags, volume mounts, env vars, and `benchmark.py` arguments
- Bind-mounts the user-supplied dataset file at the same path inside the container
- Bind-mounts `~/.cache/huggingface` so models are cached across runs

### SSHExecutor

Opens an SSH connection to the GPU node using the key at `SSH_KEY_PATH`, runs the command, and yields stdout/stderr lines as they arrive. Non-interactive shell — `~/.bashrc` is not sourced (see [gpu-nodes.md](./gpu-nodes.md) for why this matters).

### PostgreSQL

Stores all persistent state. Key tables:

| Table | Purpose |
|-------|---------|
| `workloads` | One row per benchmark run — state machine, config, node IPs |
| `nodes` | GPU nodes used per run |
| `benchmark_results` | Parsed metric rows (one per concurrency level per run) |
| `task_logs` | Streamed log lines, read by the SSE endpoint |
| `workload_types` | Catalog of supported workload types, seeded from `catalog.json` |

### RabbitMQ

Message broker for Celery. The API enqueues tasks; the worker picks them up. No custom exchanges — uses Celery's default queue (`celery`).

### Nginx (`nginx` container)

Optional reverse proxy for Jupyter instances. When `NGINX_ENABLED=true`, each Jupyter session gets a path-based public URL (`{PROXY_BASE_URL}/jupyter/{gpu_type}/{task_id}/lab`). The worker writes an nginx location config file; `inotifywait` inside the nginx container auto-reloads on changes — no manual reload needed.

### AI Assistant backend (`agent-runner`, compose profile `mcp`)

The service the in-product AI Assistant Panel talks to (corespan/aistudio-app#5,
corespan/aistudio-server#8). It replaces the earlier idea of a stateless chat proxy that returns
UI actions: it runs agents that look things up and answer, and changes nothing.

```
panel ──Keycloak token──> agent-runner :8060 ──same token──> agentgateway :8052 (inside) ──> /mcp/aistudio, /mcp/sandbox
                  │  LangGraph: plan → agents (parallel) → answer                       └──> /llm (vLLM)
                  └─ Postgres: checkpoints, run log, conversations
```

Users log in to Keycloak (realm `aistudio`, client `aistudio-panel`), the same login as the MCP
door; their role is the `roles.aistudio` claim.

- **Agents**, read-only: `analyst` (benchmark results, comparisons), `assistant` (quick status
  and health), `diagnostics` (failures and slowdowns). Each is a file in
  `agent-runner/platform/packs/`, with its purpose, tools and limits.
- **Read-only is enforced in code**: agents are given only tools the MCP server marks read-only,
  so `start_workload`, `start_jupyter` and `stop_workload` are never offered. agentgateway also
  filters every tool by the caller's own role.
- **The panel's API**: start a turn and stream it over SSE (tool progress, then the answer),
  conversations with history, and the panel context with each turn ("this run").
- **Limits** per run and per agent: tool rounds, tokens, time. When one runs out, the agent
  answers with what it has.
- **Audit**: every run and every event (each tool call with its arguments and result) is in the
  run log, with the caller's `sub`; traces go to Langfuse when it is configured.
- **The LLM** is any OpenAI-compatible server (vLLM) behind agentgateway's `/llm` route:
  `LLM_HOST` and `LLM_MODEL` in `.env`.

Details, settings and the API: [agent-runner.md](agent-runner.md). The same service also runs
the coding workflows (Claude Code in sandboxes, draft PRs after approval), which are a separate,
single-user track: [agent-runner.md#workflows](agent-runner.md#workflows).

---

## Workload Lifecycle

```
POST /benchmarks/start
  → Workload row created (state: queued)
  → Celery task dispatched

Worker picks up task
  → state: running
  → SSHExecutor runs docker pull (if needed)
  → SSHExecutor runs benchmark.py in container
  → Logs stream to TaskLog rows
  → BENCH_RESULT lines parsed → BenchmarkResult rows
  → state: completed (or failed)

Client polls GET /benchmarks/{id}/status
Client streams GET /benchmarks/{id}/logs/stream (SSE)
Client reads GET /benchmarks → leaderboard
```

---

## Key Design Decisions

**No agent on GPU nodes.** The server pushes work via SSH; there is no persistent daemon on the GPU node. Any node reachable over SSH with Docker installed is a valid target.

**BENCH_RESULT protocol.** `benchmark.py` prints `BENCH_RESULT:{json}` lines to stdout. The worker reads these from the SSH stream and inserts them as `BenchmarkResult` rows. This keeps the workload image decoupled from the server's database.

**SSE log streaming.** Logs are stored as `TaskLog` rows, not tailed from a live SSH stream. The SSE endpoint polls the database every 500ms, supporting browser reconnects via `Last-Event-ID`.

**User-supplied datasets.** No dataset is bundled or downloaded by the server. The operator places a ShareGPT-format JSON file on the GPU node and provides its absolute path in `dataset_path`. The path is bind-mounted into the container at the same location.
