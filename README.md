# aistudio-server

Open-source LLM benchmarking and workload orchestration backend. SSHes into GPU nodes, runs vLLM benchmarks or launches Jupyter Lab, streams live logs via SSE, and stores results in PostgreSQL with a full leaderboard API.

**Licence:** Apache-2.0 for this repository's source. The workload container
images are publicly pullable — see [Workload Images](#workload-images). The AI
models this tool benchmarks are **not** covered by our licence and several
require you to request access from their publisher — see
[MODEL-LICENSES.md](./MODEL-LICENSES.md).

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

## Prerequisites

| Tool | Install |
|------|---------|
| Docker Desktop | https://docs.docker.com/get-docker/ |
| `make` (Linux / Mac) | Usually pre-installed — check: `make --version` |
| `make` (Windows) | Use WSL: `sudo apt update && sudo apt install make` |

> **Windows:** Run all `make` commands in a WSL terminal (`Win+R` → `wsl`), not PowerShell or CMD.

---

## Quick Start

```bash
git clone https://github.com/corespan/aistudio-server.git
cd aistudio-server
cp .env.example .env
make setup
```

`make setup` vendors the demo-ui frontend assets, builds containers, runs DB
migrations, and seeds the workload catalog in one shot.

**Without `make` (Linux / WSL):**
```bash
./scripts/vendor_frontend_assets.sh --if-missing   # fonts + Chart.js, one time
docker compose up --build -d
docker compose exec api alembic upgrade head
docker compose exec api python -m app.services.catalog_seeder
```

> **Why the vendoring step:** the dashboard's fonts and Chart.js are served from
> this repo rather than a CDN — CDN assets fail outright in air-gapped clusters,
> and loading fonts from Google discloses visitor IPs. The binaries are
> gitignored to keep them out of the history, so they are fetched once at setup
> instead of on every page load. Skipping the step costs you charts and custom
> fonts; nothing else. See [Licensing](#licensing).

**Without `make` (Windows PowerShell):**
```powershell
copy .env.example .env
docker compose up --build -d
docker compose exec api alembic upgrade head
docker compose exec api python -m app.services.catalog_seeder
```

Verify the server is up:
```bash
curl http://localhost:8002/health
# → {"status":"healthy","database":"ok"}
```

Interactive API docs: **http://localhost:8002/docs**

---

## GPU Node Setup

Each GPU machine must be prepared once before it can receive benchmark jobs.

### 1. Generate an SSH key (skip if you already have one)

**Linux / WSL:**
```bash
ssh-keygen -t rsa -f ~/.ssh/id_rsa
# Press Enter twice for no passphrase
```

**Windows (PowerShell):**
```powershell
ssh-keygen -t rsa -f $env:USERPROFILE\.ssh\id_rsa
# Press Enter twice for no passphrase
```

### 2. Copy the public key to the node

**Linux / WSL:**
```bash
ssh-copy-id -i ~/.ssh/id_rsa.pub drut@<node-ip>
# Enter the node's password once — last time you'll need it
```

**Windows (PowerShell — `ssh-copy-id` not available):**
```powershell
type $env:USERPROFILE\.ssh\id_rsa.pub | ssh drut@<node-ip> "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && chmod 700 ~/.ssh"
```

### 3. Test passwordless SSH

**Linux / WSL:**
```bash
ssh -i ~/.ssh/id_rsa drut@<node-ip> "echo OK"
# Should print: OK   (no password prompt)
```

**Windows (PowerShell):**
```powershell
ssh -i $env:USERPROFILE\.ssh\id_rsa drut@<node-ip> "echo OK"
# Should print: OK   (no password prompt)
```

If you see `Permission denied`, fix permissions on the node:
```bash
chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys
```

### 4. Configure `.env`

```dotenv
SSH_KEY_PATH=/root/.ssh/id_rsa   # container-side path (bind-mounted from host)
SSH_DEFAULT_USER=drut
```

### Node requirements

| Requirement | How to check |
|-------------|-------------|
| Docker installed and runnable as the SSH user | `docker ps` |
| NVIDIA Container Toolkit | `docker run --gpus all --rm nvidia/cuda:12.0-base nvidia-smi` |
| `~/.aistudio/env` with `HF_TOKEN` — only for gated models | `make check-node-env NODE=<host>` |
| `~/gcr.json` — GCP key, only for private image tags | `ls ~/gcr.json` |
| Port 8000 open (inbound) | vLLM benchmark server |
| Port 8899 open (inbound) | Jupyter Lab |
| Sufficient VRAM | TinyLlama: ~3 GB · Llama-3-8B: ~16 GB · Llama-3-70B: 4×A100 |

---

## Running Benchmarks

### Start a benchmark

```bash
curl -X POST http://localhost:8002/api/v1/benchmarks/start \
  -H "Content-Type: application/json" \
  -d '{
    "model_name": "TinyLlama/TinyLlama-1.1B-Chat-v1.0",
    "node_ips": ["10.6.12.26"],
    "config": {
      "concurrency": 4,
      "input_tokens": 512,
      "output_tokens": 128,
      "precision": "fp16",
      "max_model_len": 2048,
      "gpu_count": 1
    }
  }'
# → {"status":"success","task_id":"wl-20260708-a3f9bc"}
```

### Check status

```bash
curl http://localhost:8002/api/v1/benchmarks/wl-20260708-a3f9bc/status
# → {"task_id":"...","state":"RUNNING","error_message":null}
```

States: `CREATED → VALIDATING → VALIDATED → INSTALLING → READY → RUNNING → READY` (or `FAILED`)

### Stream live logs

```bash
curl -N http://localhost:8002/api/v1/benchmarks/wl-20260708-a3f9bc/logs/stream
# Streams node validation → dependency install → benchmark output in real time
```

### Launch Jupyter Lab

```bash
curl -X POST http://localhost:8002/api/v1/jupyter/launch \
  -H "Content-Type: application/json" \
  -d '{"node_ip": "10.6.12.26"}'
# → {"status":"success","task_id":"jup-20260708-dc4f70"}

# Poll until READY, then open the URL in your browser
curl http://localhost:8002/api/v1/jupyter/jup-20260708-dc4f70/status
# Without nginx proxy: {"state":"READY","jupyter_url":"http://10.6.12.26:8899/lab"}
# With nginx proxy:    {"state":"READY","jupyter_url":"http://corespan.ddnsgeek.com/jupyter/T4/jup-20260708-dc4f70/lab"}
```

### Jupyter Lab with AI Assistant

JupyterLab with built-in AI assistance — write, execute, and visualize code with LLMs for faster experimentation and code writing.

### Prerequisites

- **GPUs** with CUDA compute capability ≥ 7
- **Docker** installed with Nvidia Container Toolkit

### 1. Run a vLLM Inference Server or use a already running one.

```bash
sudo docker run --restart=always --gpus all \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  --network host -p 8000:8000 --ipc=host \
  vllm/<vllm_docker_image> \
  --model <model_name> \
  --dtype half \
  --gpu-memory-utilization 0.95 \
  --trust-remote-code \
  --tensor-parallel-size <NUMBER_OF_GPUS> \
  --max-model-len 131072 \
  --max-num-seqs 8 \
  --host 0.0.0.0
```

Replace `<NUMBER_OF_GPUS>` with the number of GPUs available on your machine.

### 2. Integrate with JupyterLab

1. Open the **Settings** tab in JupyterLab.
2. Go to **AI Settings** → click **Add Secret**.
3. Set **Secret Name** to `HOSTED_VLLM_API_BASE` and **Value** to your vLLM server URL:
   ```
   http://<MACHINE_IP>:8000/v1/
   ```
4. Update the **Chat Model** to:
   ```
   hosted_vllm/Qwen/Qwen2.5-7B-Instruct-1M
   ```

The AI assistant is now ready to use inside your notebooks.

### View results

```bash
# Full leaderboard
curl http://localhost:8002/api/v1/benchmarks

# Filter by model or GPU type
curl "http://localhost:8002/api/v1/benchmarks?model=tinyllama%2Ftinyllama-1.1b-chat-v1.0"
curl "http://localhost:8002/api/v1/benchmarks?gpu_type=p40"

# Compare two runs
curl "http://localhost:8002/api/v1/benchmarks/compare?run_a=wl-20260708-a3f9bc&run_b=wl-20260708-x9k2mn"
```

On the GPU node, each run creates an identifiable container — use `docker ps` or `docker logs vllm-wl-20260708-a3f9bc` to inspect a live run.

---

## Testing

Checklist to verify a fresh deployment is working end to end.

**1. API health**
```bash
curl http://localhost:8002/health
# Expected: {"status":"healthy","database":"ok"}
```

**2. Workload catalog seeded**
```bash
curl http://localhost:8002/api/v1/workload-types
# Expected: JSON array with at least one entry
```

**3. Models endpoint populated**
```bash
curl http://localhost:8002/api/v1/models
# Expected: ["tinyllama/tinyllama-1.1b-chat-v1.0","llama3-8b-instruct",...]
```

**4. SSH reaches the node**
```bash
ssh -i ~/.ssh/id_rsa drut@10.6.12.26 "echo OK"
# Expected: OK   (no password prompt)
```

**5. Node validation passes**
```bash
curl -X POST http://localhost:8002/api/v1/benchmarks/start \
  -H "Content-Type: application/json" \
  -d '{"model_name":"tinyllama/tinyllama-1.1b-chat-v1.0","node_ips":["10.6.12.26"],"config":{}}'
# Note the task_id, then:
curl -N http://localhost:8002/api/v1/benchmarks/<task_id>/logs/stream
# Expected in stream: "✓ Node 10.6.12.26 validated."
```

**6. Metrics ingest**
```bash
curl -X POST http://localhost:8002/api/v1/metrics \
  -H "Content-Type: application/json" \
  -d '{
    "run_id": "test-run-001",
    "model_name": "tinyllama/tinyllama-1.1b-chat-v1.0",
    "node_ips": ["10.6.12.26"],
    "gpu_type": "p40",
    "gpu_count": 1,
    "precision": "fp16",
    "input_tokens": 512,
    "output_tokens": 128,
    "concurrency": 4,
    "status": "success",
    "total_token_throughput": 320.5,
    "mean_ttft_ms": 45.2,
    "mean_e2el_ms": 980.0
  }'
# Expected: {"status":"success","run_id":"test-run-001","message":"..."}

curl http://localhost:8002/api/v1/benchmarks
# Expected: test-run-001 appears in results
```

**7. Test suite**
```bash
make test
# or: docker compose exec api pytest tests/ -v
# Expected: 79 tests passed
```

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

---

## Architecture

```
Browser / UI (localhost:3000)
        │  REST + SSE
FastAPI API (localhost:8002)
        │  Celery task dispatch
RabbitMQ  ←→  Celery Worker
                    │  Paramiko SSH
               GPU Node(s)
                └─ docker run vllm-<run_id> / jupyter-<run_id>
                    │  results POSTed to /api/v1/metrics
               PostgreSQL (localhost:5433)
```

| Component | Purpose |
|-----------|---------|
| **FastAPI** | REST API + SSE log streaming |
| **Celery worker** | SSHes into GPU nodes, runs benchmarks / launches Jupyter |
| **ManifestBuilder** | Builds shell commands executed on remote nodes |
| **SSHExecutor** | Runs remote commands, streams stdout into TaskLog |
| **PostgreSQL** | Workloads, tasks, task logs, benchmark results |
| **RabbitMQ** | Task broker for Celery |

---

## Workload Images

No local builds on the GPU node — images are pulled automatically on each run.

| Image | Used for |
|-------|---------|
| `llminference:2.3.1-nvidia` | vLLM benchmark server |
| `jupyternotebook:2.2.0-nvidia` | Jupyter Lab (supports `--ServerApp.base_url` for nginx subpath proxy) |
| `benchmark-client:2.3.1-nvidia` | Load generator that drives the inference server |

Hosted in GCP Artifact Registry at
`us-docker.pkg.dev/aimlworkbench/workbench-registry/services/workloads/`.

### Anonymous access

The images are public. Verify before setting up a node:

```bash
docker pull us-docker.pkg.dev/aimlworkbench/workbench-registry/services/workloads/llminference:2.3.1-nvidia
```

If that succeeds without `gcloud auth`, no credential is needed. The `~/gcr.json`
service-account key described under [Node requirements](#node-requirements) is
only required for private or pre-release tags.

> If the pull returns `UNAUTHORIZED`, the registry's public access binding has
> been lost. That is a bug — please
> [open an issue](https://github.com/corespan/aistudio-server/issues). Public
> pullability is a supported property of this project, not a convenience.

### What is in them

These images are prebuilt artifacts that CoreSpan distributes, so third-party
notice and redistribution obligations attach to us rather than to the projects
whose code they contain. Each contains vLLM, PyTorch, Jupyter, several hundred
Python wheels, the CUDA or ROCm userspace, and a Debian base layer.

Generate the inventory with `make sbom` (requires
[syft](https://github.com/anchore/syft) and pull access). Output lands in
`sbom/`, with `sbom/REPORT.md` flagging anything that needs a licence decision.

### Updating an image tag

Change it in `catalog.json` and run `make seed` — no code change needed. Then
re-run `make sbom` so the inventory still describes what users actually pull.

---

## Nginx Reverse Proxy (optional)

By default, `jupyter_url` in the launch response is a direct `http://<node-ip>:<port>/lab` link. Enabling the nginx proxy routes all Jupyter sessions through a single public domain (`corespan.ddnsgeek.com`) so GPU node IPs are never exposed to users.

**URL format with proxy enabled:**
```
http://corespan.ddnsgeek.com/jupyter/<GPU_TYPE>/<task_id>/lab
```

### How it works

When `NGINX_ENABLED=true` the worker:
1. Writes a location block to `NGINX_CONF_DIR/jupyter-<task_id>.conf` on the nginx host.
2. Passes `JUPYTER_BASE_URL=/jupyter/<GPU_TYPE>/<task_id>/` to the container so JupyterLab serves assets from the correct subpath.
3. Reloads nginx via `NGINX_RELOAD_CMD`.

Location files are deleted automatically when a session ends.

### One-time server setup (master node)

Run these once on the machine running nginx (`10.6.12.15`):

```bash
# Install nginx if not present
sudo apt install -y nginx

# Create the directory for per-instance location files
sudo mkdir -p /etc/nginx/jupyter-locations

# Create the main server block (only needed once)
sudo tee /etc/nginx/conf.d/aistudio-jupyter.conf > /dev/null << 'EOF'
server {
    listen 80;
    listen [::]:80;
    server_name corespan.ddnsgeek.com;
    include /etc/nginx/jupyter-locations/*.conf;
}
EOF

# Remove the default nginx site to avoid server_name conflicts
sudo rm -f /etc/nginx/sites-enabled/default

# Test and reload
sudo nginx -t && sudo nginx -s reload
```

### `.env` settings to enable

```dotenv
NGINX_ENABLED=true
PROXY_BASE_URL=http://corespan.ddnsgeek.com

# Path that is bind-mounted from the nginx host into the worker container
NGINX_CONF_DIR=/etc/nginx/jupyter-locations

# If nginx is on the same host as the worker
NGINX_RELOAD_CMD=nginx -s reload

# If nginx is on a separate machine (e.g. master node at 10.6.12.15)
NGINX_RELOAD_CMD=ssh -i /root/.ssh/id_rsa -o StrictHostKeyChecking=no drut@10.6.12.15 sudo nginx -s reload
```

Also update `docker-compose.yml` to bind-mount the nginx locations directory into the worker container:

```yaml
worker:
  volumes:
    - /etc/nginx/jupyter-locations:/etc/nginx/jupyter-locations
    - ~/.ssh:/root/.ssh:ro
```

---

## Configuration (`.env`)

Copy `.env.example` to `.env` and fill in the values below.

| Variable | Default | Description |
|----------|---------|-------------|
| `SSH_KEY_PATH` | `/root/.ssh/id_rsa` | Container-side path to SSH private key |
| `SSH_DEFAULT_USER` | `drut` | SSH username on GPU nodes |
| `GCP_REGISTRY_URL` | `us-docker.pkg.dev` | Artifact Registry hostname |
| `GCP_PROJECT_ID` | `aimlworkbench` | GCP project ID |
| `GCP_REPOSITORY` | `workbench-registry` | Artifact Registry repo |
| `GCP_IMAGE_PATH` | `services/workloads` | Path prefix inside the repo |
| `WORKLOAD_IMAGE_TAG` | `2.3.1-nvidia` | Fallback image tag (overridden by `catalog.json`) |
| `JUPYTER_IMAGE_TAG` | `2.2.0-nvidia` | Tag for the jupyternotebook image |
| `MODEL_STORAGE_MODE` | `huggingface` | `huggingface`, `local`, or `gcs` |
| `MODEL_LOCAL_PATH` | `/home/ubuntu/models` | Used when `MODEL_STORAGE_MODE=local` |
| `NGINX_ENABLED` | `false` | Set to `true` to route Jupyter through nginx (hides GPU node IPs) |
| `PROXY_BASE_URL` | *(empty)* | Public domain with scheme and no trailing slash — e.g. `http://corespan.ddnsgeek.com`. Required when `NGINX_ENABLED=true`. |
| `NGINX_CONF_DIR` | `/etc/nginx/jupyter-locations` | Directory where the worker writes per-instance location files. Must be bind-mounted in `docker-compose.yml`. |
| `NGINX_RELOAD_CMD` | `nginx -s reload` | Command to reload nginx after a config is written. When nginx runs on a separate host, use an SSH command — e.g. `ssh -i /root/.ssh/id_rsa -o StrictHostKeyChecking=no user@master-node sudo nginx -s reload`. |

---

## API Endpoints

### Benchmark Orchestration
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/benchmarks/start` | Start a benchmark — returns `task_id` |
| GET | `/api/v1/benchmarks/{task_id}/status` | Poll workload state |
| GET | `/api/v1/benchmarks/{task_id}/logs/stream` | SSE live log stream |

### Jupyter
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/jupyter/launch` | Launch Jupyter Lab — returns `task_id` |
| GET | `/api/v1/jupyter/{task_id}/status` | Poll state; returns `jupyter_url` when READY |
| GET | `/api/v1/jupyter/{task_id}/logs/stream` | SSE live log stream |

### Leaderboard
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/benchmarks` | Filterable leaderboard (`model`, `gpu_type`, `node_ip`, `precision`, `concurrency`, `input_tokens`, `output_tokens`, `status`, `date`) |
| GET | `/api/v1/benchmarks/{run_id}` | Full detail for one run |
| GET | `/api/v1/benchmarks/compare?run_a=&run_b=` | Side-by-side comparison |

### Delete
| Method | Path | Description |
|--------|------|-------------|
| DELETE | `/api/v1/benchmarks/{run_id}` | Delete a single run |
| DELETE | `/api/v1/benchmarks/bulk` | Delete multiple — body: `{"run_ids":[...]}` |
| DELETE | `/api/v1/benchmarks/filter` | Delete by filter — at least one filter required |
| DELETE | `/api/v1/benchmarks/all?confirm=true` | Wipe all results |

### Ingest
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/metrics` | Ingest a benchmark result (upsert by `run_id`) |

### Reference
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/models` | Catalog models + historical model names |
| GET | `/api/v1/gpu-types` | Distinct GPU types from past runs |
| GET | `/api/v1/nodes` | Distinct node IPs from past runs |
| GET | `/api/v1/concurrencies` | Distinct concurrency levels |
| GET | `/api/v1/models/config` | Recommended vLLM config for a model |

### System
| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Readiness probe — checks DB connectivity |
| GET | `/api/v1/workload-types` | List supported workload types |

Full interactive docs: **http://localhost:8002/docs**

---

## Licensing

### This repository

Apache-2.0. See [LICENSE](./LICENSE) for the full text and [NOTICE](./NOTICE) for
the copyright and attribution summary.

Apache-2.0 Section 6 grants no trademark rights. "CoreSpan" and the CoreSpan logo
are trademarks of CoreSpan AI.

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
cannot grant that access on your behalf.

Check which models in your catalog are gated:

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
