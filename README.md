# aistudio-server

Open-source LLM benchmarking backend. Orchestrates inference benchmarks on your GPU nodes, stores results in PostgreSQL, and serves a REST + SSE API for any frontend.

## Quick Start

```bash
git clone https://github.com/corespan/aistudio-server.git
cd aistudio-server
cp .env.example .env        # edit SSH_KEY_PATH and MODEL_STORAGE_MODE if needed
make setup                   # starts all services, runs migrations, seeds catalog
```

That's it. API is at **http://localhost:8001/docs** (Swagger UI).

### Alternative Setup (Without `make`)
If you don't have the `make` utility installed, copy the `.env` file and execute the underlying Docker commands manually:

* **Ubuntu / WSL (Linux):**
  ```bash
  cp .env.example .env
  docker compose up --build -d
  docker compose exec api alembic upgrade head
  docker compose exec api python -m app.services.catalog_seeder
  ```

* **Windows (PowerShell):**
  ```powershell
  copy .env.example .env
  docker compose up --build -d
  docker compose exec api alembic upgrade head
  docker compose exec api python -m app.services.catalog_seeder
  ```

### Installing `make` in WSL/Ubuntu
If you are using WSL/Ubuntu and want to utilize `make` shortcuts, install it with:
```bash
sudo apt update && sudo apt install -y make
```

## Verifying Your Setup
You can verify if the server is running and database ingestion is working by sending a dummy metrics payload using the included `test_metric.json` file.

* **Ubuntu / WSL (Linux):**
  ```bash
  curl -X POST http://localhost:8001/api/v1/metrics \
    -H "Content-Type: application/json" \
    -d @test_metric.json
  ```

* **Windows (PowerShell):**
  *(Note: You must use `curl.exe` in PowerShell to bypass the default `Invoke-WebRequest` alias, and use a backtick ` ` ` for multi-line commands).*
  ```powershell
  curl.exe -X POST http://localhost:8001/api/v1/metrics `
    -H "Content-Type: application/json" `
    -d @test_metric.json
  ```

If successful, you will receive a response: `{"status":"success","run_id":"newfile-1234","message":"Ingested successfully into PostgreSQL."}`.

## Run a Benchmark

```bash
curl -X POST http://localhost:8001/api/v1/benchmarks/start \
  -H "Content-Type: application/json" \
  -d '{
    "model_name": "llama3-8b-instruct",
    "node_ips": ["YOUR_GPU_NODE_IP"],
    "config": {"concurrency": 4, "input_tokens": 512, "output_tokens": 512}
  }'
```

Response: `{"status": "success", "task_id": "wl-20260603-a3f9bc", ...}`

Then poll status and stream logs:

```bash
# Poll status
curl http://localhost:8001/api/v1/benchmarks/wl-20260603-a3f9bc/status

# Stream live logs (SSE)
curl -N http://localhost:8001/api/v1/benchmarks/wl-20260603-a3f9bc/logs/stream
```

## What's Included

| Component | Purpose |
|-----------|---------|
| **FastAPI server** | REST API + SSE log streaming |
| **Celery worker** | SSHs into GPU nodes, runs benchmarks |
| **PostgreSQL** | All state, metrics, audit log |
| **RabbitMQ** | Task broker for Celery |

## Pre-Built Workload Images (GCR)

No need to build anything. The Celery worker pulls these automatically:

| Image | Path |
|-------|------|
| LLM Inference (vLLM) | `gcr.io/aistudio-oss/llm-inference:latest` |
| Benchmark Client | `gcr.io/aistudio-oss/benchmark-client:latest` |

## Supported Models

| Model | HuggingFace Repo | Min GPU Memory |
|-------|-----------------|----------------|
| Llama 3 8B Instruct | `meta-llama/Meta-Llama-3-8B-Instruct` | 16 GB |
| Mistral 7B Instruct v0.3 | `mistralai/Mistral-7B-Instruct-v0.3` | 16 GB |

## Configuration

All config is via `.env` file. Key variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `WORKLOAD_REGISTRY` | `gcr.io/aistudio-oss` | Where workload images are pulled from |
| `MODEL_STORAGE_MODE` | `huggingface` | `huggingface`, `local`, or `gcs` |
| `SSH_KEY_PATH` | `~/.ssh/id_rsa` | Path to SSH private key for GPU node access |
| `SSH_DEFAULT_USER` | `ubuntu` | SSH username on GPU nodes |

See `.env.example` for the full list.

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check (K8s readiness probe) |
| GET | `/api/v1/workload-types` | List supported workload types |
| POST | `/api/v1/benchmarks/start` | Start a new benchmark |
| GET | `/api/v1/benchmarks/{task_id}/status` | Poll workload state |
| GET | `/api/v1/benchmarks/{task_id}/logs/stream` | SSE live log stream |
| POST | `/api/v1/metrics` | Ingest benchmark results (upsert) |
| GET | `/api/v1/benchmarks` | Leaderboard with filters |
| GET | `/api/v1/benchmarks/{run_id}` | Single run detail |
| GET | `/api/v1/benchmarks/compare` | Side-by-side comparison |
| GET | `/api/v1/models` | Distinct model names |
| GET | `/api/v1/gpu-types` | Distinct GPU types |
| GET | `/api/v1/concurrencies` | Distinct concurrency levels |
| GET | `/api/v1/summary` | Dashboard aggregate stats |

Full interactive docs: **http://localhost:8001/docs**

## Using with Your Own UI

This server works standalone — no specific UI required. Point any frontend at the API:

```
REACT_APP_API_URL=http://localhost:8001
```

See `api_contract/API_CONTRACT.md` for the full API contract with response shapes.

## Using with Your Own Backend

If you want to use the `aistudio-ui` (separate repo) with your own backend, your backend must implement the endpoints documented in `api_contract/API_CONTRACT.md`.

## Make Commands

```bash
make up        # Start all services
make down      # Stop all services
make logs      # Tail API + worker logs
make migrate   # Run Alembic migrations
make seed      # Seed workload_types from catalog.json
make setup     # up + migrate + seed (first-time setup)
make test      # Run tests
make spec      # Export OpenAPI spec to api_contract/openapi.json
make shell     # Python shell inside the API container
make benchmark # Example curl to start a benchmark
```

## License

Apache 2.0 — see [LICENSE](./LICENSE)
