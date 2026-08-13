# aistudio-server

[![CI](https://github.com/corespan/aistudio-server/actions/workflows/ci.yml/badge.svg)](https://github.com/corespan/aistudio-server/actions/workflows/ci.yml)

Open-source LLM benchmarking and workload orchestration backend. SSHes into GPU nodes, runs vLLM benchmarks or launches Jupyter Lab, streams live logs via SSE, and stores results in PostgreSQL with a full leaderboard API.

**Licence:** Apache-2.0 for this repository's source. The workload container
images are publicly pullable — see [Workload Images](#workload-images). The AI
models this tool benchmarks are **not** covered by our licence and several
require you to request access from their publisher — see
[MODEL-LICENSES.md](./MODEL-LICENSES.md).

---

## Documentation

| Guide | Go here for |
|-------|-------------|
| [docs/quickstart.md](./docs/quickstart.md) | Installing and running your first benchmark in under 10 minutes |
| [docs/gpu-nodes.md](./docs/gpu-nodes.md) | Preparing a GPU node: SSH keys, Docker, datasets, HuggingFace tokens, troubleshooting |
| [docs/architecture.md](./docs/architecture.md) | How the API, Celery worker, SSH executor, and database fit together |
| [docs/api.md](./docs/api.md) | Full REST API reference — every endpoint, request/response shape |
| [docs/configuration.md](./docs/configuration.md) | Full `.env` variable reference, including the Nginx reverse proxy |
| [docs/workloads.md](./docs/workloads.md) | LLM inference & Jupyter workload internals, AI-assistant setup, adding new workload types |
| [docs/models.md](./docs/models.md) | `catalog.json`, adding models, gated vs. ungated, licence obligations |
| [docs/development.md](./docs/development.md) | Local dev setup, running tests, linting, contribution workflow |

---

## One-line Install

**Linux / Mac:**
```bash
curl -sL https://raw.githubusercontent.com/corespan/aistudio-server/master/install.sh | bash
```

**Windows (PowerShell):**
```powershell
Invoke-WebRequest -Uri https://raw.githubusercontent.com/corespan/aistudio-server/master/install.ps1 -OutFile install.ps1; .\install.ps1
```

---

## Quick Start

Requires Docker Desktop and `make` (Windows: run these in WSL, not PowerShell/CMD).

```bash
git clone https://github.com/corespan/aistudio-server.git
cd aistudio-server
cp .env.example .env
make setup
```

`make setup` vendors the demo-ui frontend assets, builds containers, runs DB
migrations, and seeds the workload catalog in one shot.

```bash
curl http://localhost:8002/health
# → {"status":"healthy","database":"ok"}
```

Interactive API docs: **http://localhost:8002/docs**. Demo UI: **http://localhost:3000**.

For the full walkthrough — including running without `make`, preparing a GPU
node, and starting your first benchmark — see
[docs/quickstart.md](./docs/quickstart.md) and
[docs/gpu-nodes.md](./docs/gpu-nodes.md).

---

## Make Commands

```bash
make setup     # First-time setup: build + migrate + seed
make up        # Start services (no rebuild)
make down      # Stop services
make logs      # Tail API + worker logs
make migrate   # Run Alembic migrations only
make seed      # Re-seed workload_types from catalog.json
make test      # Run the test suite
make shell     # Python shell inside the API container
make spec      # Export OpenAPI spec to openapi.json
```

Licence compliance — see [Licensing](#licensing):

```bash
make compliance     # Run every check CI runs: licence files, pins, inventory, CDN refs
make deps-lock      # Re-pin requirements.txt from requirements.in (after editing .in)
make third-party    # Regenerate THIRD-PARTY-NOTICES.md from the pinned set
make vendor-assets  # Re-vendor demo-ui fonts and Chart.js from npm
make sbom           # Inventory the workload images (needs syft + registry access)
make check-models   # Report gate status and licence for every model referenced
make check-node-env NODE=<host>   # Confirm HF_TOKEN reaches a GPU node
```

---

## Services and Ports

| Container | Internal | External | Purpose |
|-----------|----------|----------|---------|
| `api` | 8001 | **8002** | FastAPI REST + SSE |
| `postgres` | 5432 | **5433** | PostgreSQL |
| `rabbitmq` (AMQP) | 5672 | **5672** | Celery broker |
| `rabbitmq` (UI) | 15672 | **15672** | RabbitMQ admin |
| `pgadmin` | 80 | **5050** | DB admin |
| `demo-ui` | 80 | **3000** | Benchmark dashboard |
| `worker` | — | — | No port; connects outbound only |

Architecture, component responsibilities, and the workload lifecycle are covered in [docs/architecture.md](./docs/architecture.md).

---

## Workload Images

No local builds on the GPU node — images are pulled automatically on each run.

| Image | Used for |
|-------|---------|
| `llminference:2.3.1-nvidia` | vLLM inference server — also used as the benchmark client via `docker exec` |
| `jupyternotebook:2.2.0-nvidia` | Jupyter Lab (supports `--ServerApp.base_url` for nginx subpath proxy) |

Hosted in GCP Artifact Registry at `us-docker.pkg.dev/aimlworkbench/aistudio/`.
The images are public — `docker pull us-docker.pkg.dev/aimlworkbench/aistudio/llminference:2.3.1-nvidia`
should succeed without `gcloud auth`. If it returns `UNAUTHORIZED`, that's a bug —
please [open an issue](https://github.com/corespan/aistudio-server/issues).

These are prebuilt artifacts Corespan Systems, Inc distributes, so third-party
notice and redistribution obligations attach to us rather than to the projects
whose code they contain (vLLM, PyTorch, Jupyter, CUDA/ROCm userspace, Debian
base layer, and several hundred Python wheels). Generate the inventory with
`make sbom` (requires [syft](https://github.com/anchore/syft) and pull access) —
output lands in `sbom/`, with `sbom/REPORT.md` flagging anything that needs a
licence decision.

To update an image tag, change it in `catalog.json` and run `make seed` — no
code change needed. See [docs/workloads.md](./docs/workloads.md) for what each
image runs and how.

---

## Licensing

### This repository

Apache-2.0. See [LICENSE](./LICENSE) for the full text and [NOTICE](./NOTICE) for
the copyright and attribution summary.

Apache-2.0 Section 6 grants no trademark rights. "CoreSpan" and the CoreSpan logo
are trademarks of Corespan Systems, Inc.

### Everything else

The Apache-2.0 grant covers CoreSpan's own source. It does not extend to the
software and weights this tool pulls in at run time. Three separate inventories
cover those:

| What | Where | Covers |
| --- | --- | --- |
| Python dependencies | [THIRD-PARTY-NOTICES.md](./THIRD-PARTY-NOTICES.md) | Every package in `requirements.txt`, with the three reciprocal licences called out |
| Models and datasets | [MODEL-LICENSES.md](./MODEL-LICENSES.md) | Per-model terms, which models are gated, how to obtain access |
| Workload images | `sbom/` — run `make sbom` | Contents of the distributed containers, including the CUDA EULA question |
| Frontend assets | [demo-ui/vendor/NOTICE](./demo-ui/vendor/NOTICE) | Self-hosted fonts (OFL-1.1) and Chart.js (MIT) |

**If you plan to run gated models** — all the Meta Llama entries in
`catalog.json` — read [MODEL-LICENSES.md](./MODEL-LICENSES.md) first. You must
request access from the publisher and set `HF_TOKEN` on the GPU node. CoreSpan
cannot grant that access on your behalf. Check which models in your catalog are
gated:

```bash
python3 scripts/check_model_access.py
```

### Verifying compliance yourself

```bash
make compliance        # licence files, pinned deps, inventory freshness, no CDN refs
make sbom              # workload image inventory (needs syft + registry access)
make check-models      # gate status and licence for every model referenced
```

CI runs the same checks on every push — see
[.github/workflows/compliance.yml](./.github/workflows/compliance.yml).

### Reporting a licence problem

Open an issue, or email <legal@corespan.ai> if it concerns a third party's
rights. We will respond within five working days.
