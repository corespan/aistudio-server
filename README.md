# aistudio-server

Open-source LLM benchmarking and workload orchestration backend. SSHes into GPU nodes, runs vLLM benchmarks or launches Jupyter Lab, streams live logs via SSE, and stores results in PostgreSQL with a full leaderboard API.

## Quick Start

```bash
git clone https://github.com/corespan/aistudio-server.git
cd aistudio-server
cp .env.example .env        # edit SSH_KEY_PATH, SSH_DEFAULT_USER, GCP credentials
make setup                  # starts all services, runs migrations, seeds catalog
```

Or without `make`:

```bash
docker compose up --build -d
docker compose exec api alembic upgrade head
docker compose exec api python -m app.services.catalog_seeder
```

**Windows (PowerShell):**
```powershell
copy .env.example .env
docker compose up --build -d
docker compose exec api alembic upgrade head
docker compose exec api python -m app.services.catalog_seeder
```

Services start at:
| Service | URL |
|---------|-----|
| API (FastAPI / Swagger) | http://localhost:8002/docs |
| Dashboard UI | http://localhost:3000 |
| RabbitMQ management | http://localhost:15672 |
| pgAdmin | http://localhost:5050 |

> **Port note:** uvicorn listens on 8001 inside the container; docker-compose maps it to **8002** on your host.

---

## GPU Node Setup (prerequisite)

The Celery worker SSHes into GPU nodes to run workloads. Before you can start a benchmark or launch Jupyter, each target node must accept passwordless SSH from the machine running this server.

### Step 1 — Generate an SSH key (skip if you already have one)

```bash
ssh-keygen -t ed25519 -C "aistudio-server" -f ~/.ssh/aistudio_id
# Press Enter twice to leave the passphrase empty
```

This creates two files: `~/.ssh/aistudio_id` (private key) and `~/.ssh/aistudio_id.pub` (public key).

### Step 2 — Copy your public key to the GPU node

```bash
ssh-copy-id -i ~/.ssh/aistudio_id.pub drut@<node-ip>
# e.g.
ssh-copy-id -i ~/.ssh/aistudio_id.pub drut@10.6.12.22
```

It will prompt for the node's password once, then append your public key to `~/.ssh/authorized_keys` on the node. After this, all subsequent SSH connections use the key and require no password.

**Windows (no ssh-copy-id):** run this instead:

```powershell
type $env:USERPROFILE\.ssh\aistudio_id.pub | ssh drut@10.6.12.22 "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
```

### Step 3 — Test the connection

```bash
ssh -i ~/.ssh/aistudio_id drut@10.6.12.22 "echo OK"
# Should print: OK   (no password prompt)
```

If you see `Permission denied`, check that `~/.ssh/authorized_keys` on the node has the correct permissions (`chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys`).

### Step 4 — Point the server at your key

In your `.env`:

```
SSH_KEY_PATH=/root/.ssh/aistudio_id
SSH_DEFAULT_USER=drut
```

The key file is bind-mounted into the `api` and `worker` containers via `docker-compose.yml`. The path in `.env` must be the **container-side** path (i.e. wherever you mount it), not your host path.

### Step 5 — Verify the node passes validation

```bash
curl -X POST http://localhost:8002/api/v1/benchmarks/start \
  -H "Content-Type: application/json" \
  -d '{"model_name":"TinyLlama/TinyLlama-1.1B-Chat-v1.0","node_ips":["10.6.12.22"],"config":{}}'
# Watch logs — validate_node step should pass
```

### Node requirements

| Requirement | Notes |
|-------------|-------|
| Docker installed | `docker --version` must work as the SSH user |
| `nvidia-docker2` / NVIDIA Container Toolkit | Required for `--gpus all` flag |
| `gcr.json` at `~/gcr.json` | GCP service account key for pulling images from Artifact Registry |
| Port 8899 open (inbound) | For Jupyter Lab access from your browser |
| Port 8000 open (inbound) | For vLLM benchmark server |
| Sufficient VRAM | Depends on the model; TinyLlama needs ~3 GB, Llama-3 70B needs 4×A100 |

---

## Ports Reference

| Container | Internal port | External port (your machine) | Purpose |
|-----------|--------------|------------------------------|---------|
| `api` | 8001 | **8002** | FastAPI REST + SSE |
| `postgres` | 5432 | **5433** | PostgreSQL |
| `rabbitmq` (AMQP) | 5672 | **5672** | Celery broker |
| `rabbitmq` (UI) | 15672 | **15672** | RabbitMQ admin |
| `pgadmin` | 80 | **5050** | DB admin |
| `demo-ui` | 80 | **3000** | Benchmark dashboard |
| `worker` | — | — | No port; connects outbound |

On GPU nodes (launched via SSH):

| Workload | Host → Container |
|----------|-----------------|
| vLLM benchmark server | `8000:8000` |
| Jupyter Lab | `8899:7008` |

---

## Workloads

### 1 — Run a Benchmark

```bash
curl -X POST http://localhost:8002/api/v1/benchmarks/start \
  -H "Content-Type: application/json" \
  -d '{
    "model_name": "TinyLlama/TinyLlama-1.1B-Chat-v1.0",
    "node_ips": ["10.6.12.22"],
    "config": {
      "concurrency": 4,
      "input_tokens": 512,
      "output_tokens": 128,
      "precision": "fp16",
      "max_model_len": 2048,
      "gpu_count": 1
    }
  }'
# → {"status":"success","task_id":"wl-20260619-a3f9bc"}
```

Poll status and stream logs:

```bash
curl http://localhost:8002/api/v1/benchmarks/wl-20260619-a3f9bc/status
curl -N http://localhost:8002/api/v1/benchmarks/wl-20260619-a3f9bc/logs/stream
```

Celery chain: `validate_node → install_dependencies → execute_benchmark`  
States: `CREATED → VALIDATING → VALIDATED → INSTALLING → READY → RUNNING → READY` (or `FAILED`)

### 2 — Launch Jupyter Lab

```bash
curl -X POST http://localhost:8002/api/v1/jupyter/launch \
  -H "Content-Type: application/json" \
  -d '{"node_ip": "10.6.12.22"}'
# → {"status":"success","task_id":"jup-20260619-dc4f70"}
```

Poll until READY, then open the returned `jupyter_url` in your browser:

```bash
curl http://localhost:8002/api/v1/jupyter/jup-20260619-dc4f70/status
# → {"state":"READY","jupyter_url":"http://10.6.12.22:8899/lab"}
```

Celery chain: `validate_node → launch_jupyter`  
States: `CREATED → VALIDATING → VALIDATED → INSTALLING → READY` (or `FAILED`)

The **Dashboard UI** (`localhost:3000`) handles both workloads under a **Workloads** tab with sub-buttons "Benchmark Run" and "Launch Jupyter". Jupyter opens automatically in a new tab when ready.

---

## Verifying Ingest

Send a test metrics payload:

```bash
# Linux / WSL
curl -X POST http://localhost:8002/api/v1/metrics \
  -H "Content-Type: application/json" \
  -d @test_metric.json

# Windows PowerShell
curl.exe -X POST http://localhost:8002/api/v1/metrics `
  -H "Content-Type: application/json" `
  -d @test_metric.json
```

Expected: `{"status":"success","run_id":"...","message":"Ingested successfully..."}`

---

## Architecture

```
Browser / UI (localhost:3000 — nginx)
        │  REST + SSE
FastAPI API (localhost:8002)
        │  Celery chain dispatch
RabbitMQ broker  ←→  Celery Worker
                          │  Paramiko SSH
                     GPU Node(s)
                      └─ docker run vllm / jupyternotebook
                          │  results / logs
                     PostgreSQL (localhost:5433)
```

| Component | Purpose |
|-----------|---------|
| **FastAPI** | REST API + SSE log streaming |
| **Celery worker** | SSHes into GPU nodes, runs benchmarks / launches Jupyter |
| **Paramiko** | SSH client used inside the Celery worker |
| **ManifestBuilder** | Builds the bash command strings executed on remote nodes |
| **SSHExecutor** | Runs remote commands, streams stdout line-by-line into `TaskLog` |
| **PostgreSQL** | All state, metrics, workload events, task logs |
| **RabbitMQ** | Task broker for Celery |

---

## Workload Images (GCP Artifact Registry)

No local builds needed — the worker pulls these automatically on each run.

Image tags are seeded from `catalog.json` into the `workload_types` table at startup.
To update a tag, change it in `catalog.json` and re-run `make seed` — no code change needed:

| Image | GCR path | Used for |
|-------|----------|---------|
| LLM Inference | `us-docker.pkg.dev/aimlworkbench/workbench-registry/services/workloads/llminference:2.3.1-nvidia` | vLLM benchmark server |
| Jupyter Notebook | `us-docker.pkg.dev/aimlworkbench/workbench-registry/services/workloads/jupyternotebook:1.1.1-nvidia` | Jupyter Lab on GPU node |

The worker uses `--entrypoint python3` (llminference) or `--entrypoint bash` (jupyternotebook) to bypass the image's default `script.sh`, which requires an internal Nexus/aiAgent setup. Model weights are pulled from HuggingFace on first run and cached at `~/.cache/huggingface` on the node.

---

## Configuration (`.env`)

| Variable | Default | Description |
|----------|---------|-------------|
| `GCP_REGISTRY_URL` | `us-docker.pkg.dev` | Artifact Registry hostname |
| `GCP_PROJECT_ID` | `aimlworkbench` | GCP project ID |
| `GCP_REPOSITORY` | `workbench-registry` | Artifact Registry repo |
| `GCP_IMAGE_PATH` | `services/workloads` | Path prefix inside the repo |
| `WORKLOAD_IMAGE_TAG` | `2.3.1-nvidia` | Fallback image tag (overridden by `catalog.json`) |
| `JUPYTER_IMAGE_TAG` | `1.1.1-nvidia` | Tag for the jupyternotebook image |
| `MODEL_STORAGE_MODE` | `huggingface` | `huggingface`, `local`, or `gcs` |
| `MODEL_LOCAL_PATH` | `/home/ubuntu/models` | Used when `MODEL_STORAGE_MODE=local` |
| `SSH_KEY_PATH` | `~/.ssh/id_rsa` | Path to SSH private key (mounted into containers) |
| `SSH_DEFAULT_USER` | `drut` | SSH username on GPU nodes |

---

## API Endpoints

### Benchmark Orchestration
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/benchmarks/start` | Start a benchmark (returns `task_id`) |
| GET | `/api/v1/benchmarks/{task_id}/status` | Poll workload state |
| GET | `/api/v1/benchmarks/{task_id}/logs/stream` | SSE live log stream |

### Jupyter Workload
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/jupyter/launch` | Launch Jupyter Lab on a node (returns `task_id`) |
| GET | `/api/v1/jupyter/{task_id}/status` | Poll state; returns `jupyter_url` when READY |
| GET | `/api/v1/jupyter/{task_id}/logs/stream` | SSE live log stream |

### Leaderboard
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/benchmarks` | Filterable leaderboard (`model`, `gpu_type`, `node_ip`, `precision`, `concurrency`, `input_tokens`, `output_tokens`, `status`, `date`) |
| GET | `/api/v1/benchmarks/{run_id}` | Full detail for one run (all sub-runs) |
| GET | `/api/v1/benchmarks/compare?run_a=&run_b=` | Side-by-side comparison |

### Delete
| Method | Path | Description |
|--------|------|-------------|
| DELETE | `/api/v1/benchmarks/{run_id}` | Delete a single run |
| DELETE | `/api/v1/benchmarks/bulk` | Delete multiple runs — body: `{"run_ids":[...]}` |
| DELETE | `/api/v1/benchmarks/filter` | Delete by filter (`?gpu_type=p40`, `?model=...`, `?date=...`, etc.) — at least one filter required |
| DELETE | `/api/v1/benchmarks/all?confirm=true` | Wipe all results — requires `confirm=true` |

### Ingest (external push)
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/metrics` | Ingest a completed benchmark result (upsert) |

### Reference / Dropdowns
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/models` | Distinct model names |
| GET | `/api/v1/gpu-types` | Distinct GPU types |
| GET | `/api/v1/nodes` | Distinct node IPs |
| GET | `/api/v1/concurrencies` | Distinct concurrency levels |
| GET | `/api/v1/precisions` | Distinct precision values |
| GET | `/api/v1/input-tokens` | Distinct ISL values |
| GET | `/api/v1/output-tokens` | Distinct OSL values |

### System
| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Readiness probe — checks DB connectivity |
| GET | `/api/v1/workload-types` | List supported workload types |
| GET | `/api/v1/models/config` | Recommended vLLM config for a model |

Full interactive docs: **http://localhost:8002/docs**

---

## Make Commands

```bash
make up        # Start all services
make down      # Stop all services
make logs      # Tail API + worker logs
make migrate   # Run Alembic migrations
make seed      # Seed workload_types from catalog.json
make setup     # up + migrate + seed (first-time setup)
make test      # Run tests
make spec      # Export OpenAPI spec
make shell     # Python shell inside the API container
make benchmark # Example curl to start a benchmark
```

---

## License

Apache 2.0 — see [LICENSE](./LICENSE)
