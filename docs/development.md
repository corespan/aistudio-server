# Development

This page covers local development setup, running tests, and the contribution workflow.

---

## Prerequisites

| Tool | Version |
|------|---------|
| Python | 3.11+ |
| Docker Desktop | 24+ |
| PostgreSQL | 15+ (or run via docker-compose) |
| RabbitMQ | 3.13+ (or run via docker-compose) |

---

## Local Setup

### 1. Clone and install dependencies

```bash
git clone https://github.com/corespan/aistudio-server.git
cd aistudio-server

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Start infrastructure services

The easiest way is to start only PostgreSQL and RabbitMQ from docker-compose:

```bash
docker compose up -d postgres rabbitmq
```

Or run a full local PostgreSQL:
```bash
docker run -d --name aistudio-pg \
  -e POSTGRES_USER=aistudio \
  -e POSTGRES_PASSWORD=aistudio \
  -e POSTGRES_DB=aistudio \
  -p 5432:5432 \
  postgres:15-alpine
```

### 3. Configure environment

```bash
cp .env.example .env
```

For local dev, update `POSTGRES_HOST` and `RABBITMQ_URL`:
```env
POSTGRES_HOST=localhost
RABBITMQ_URL=localhost
```

### 4. Run migrations and seed the catalog

```bash
alembic upgrade head
python -m app.services.catalog_seeder
```

### 5. Start the API and worker

```bash
# Terminal 1 — FastAPI
uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload

# Terminal 2 — Celery worker
celery -A app.worker:celery_app worker --loglevel=info --concurrency=2 -Q celery
```

The API is available at **http://localhost:8001** and **http://localhost:8001/docs**.

---

## Running Tests

### AIStudio tests (79 tests)

Tests live in `AIStudio/tests/` and use a dedicated `aistudio_test` database that is created and dropped automatically each run.

**Run all tests:**
```bash
cd AIStudio
pytest tests/ -v --log-cli-level=INFO
```

**Run a specific test class:**
```bash
pytest tests/test_services.py::TestStateMachine -v
```

**Run by keyword:**
```bash
pytest tests/test_services.py -k "ingest or state_machine" -v
```

**Inside the docker-compose container:**
```bash
docker compose exec api pytest tests/ -v --log-cli-level=INFO
```

### Test coverage

| # | Class | What it validates |
|---|-------|------------------|
| 1 | `TestHealthCheck` | `GET /health` — 200, shape, db=ok |
| 2 | `TestWorkloadTypes` | `GET /api/v1/workload-types` |
| 3 | `TestMetricsIngest` | `POST /api/v1/metrics` — 202, persistence, idempotent upsert |
| 4–5 | `TestBenchmarkStartAndStatus` | Start endpoint + status polling |
| 6 | `TestLogStreaming` | SSE `/logs/stream` — 404 when no task, emits all log lines |
| 7–9 | `TestResults` | Leaderboard, filters, single result, compare |
| 10 | `TestSummary` | Aggregate counts and success-rate maths |
| 11 | `TestReferenceDropdowns` | `/models`, `/gpu-types`, `/concurrencies` |
| 12 | `TestStateMachine` | Valid/invalid transitions, terminal states, audit trail |
| 13 | `TestManifestBuilder` | Shell script content — image selection, volume mounts |
| 14 | `TestDependencyInstaller` | SSH-mocked install — pull, fallback, NFS/GCS |
| 15 | `TestConfig` | Computed URL properties, storage defaults |

---

## Deployment Smoke Test

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

## Code Style

This project uses **ruff** for linting and formatting.

```bash
# Check
ruff check .

# Fix
ruff check --fix .

# Format
ruff format .
```

Configuration is in `pyproject.toml` (or `ruff.toml` if present). CI will fail on linting errors.

---

## Database Migrations

Migrations are managed with **Alembic**.

```bash
# Create a new migration after changing a model
alembic revision --autogenerate -m "describe your change"

# Apply migrations
alembic upgrade head

# Roll back one step
alembic downgrade -1
```

Migration files live in `alembic/versions/`. Always review auto-generated migrations before committing — autogenerate can miss or misidentify changes.

---

## Makefile Targets

| Target | Description |
|--------|-------------|
| `make setup` | Full first-time setup (vendor assets, build, migrate, seed) |
| `make check-licenses` | Run licence compliance checks |
| `make check-node-env NODE=<ip>` | Verify a GPU node is correctly configured |
| `make sbom` | Generate SBOM for all workload images |

Run `make help` to see all available targets.

---

## Project Structure

```
aistudio-server/
├── app/
│   ├── main.py              # FastAPI application factory
│   ├── config.py            # Settings (pydantic-settings)
│   ├── worker.py            # Celery app + benchmark task
│   ├── catalog.py           # catalog.json loader
│   ├── database.py          # SQLAlchemy async session
│   ├── models/              # SQLAlchemy ORM models
│   ├── routers/             # FastAPI route handlers
│   ├── schemas/             # Pydantic request/response schemas
│   └── services/
│       ├── manifest_builder.py   # Builds docker run shell commands
│       ├── ssh_executor.py       # SSH connection + command execution
│       └── catalog_seeder.py     # Seeds workload types from catalog.json
├── AIStudio/tests/          # pytest test suite
├── alembic/                 # Database migrations
├── scripts/                 # Utility scripts
├── sbom/                    # Generated SBOMs (committed at release)
├── catalog.json             # Model and workload catalog
├── docker-compose.yml       # Local development stack
├── .env.example             # Environment variable template
└── docs/                    # This documentation
```

---

## Contribution Workflow

1. Fork the repo and create a feature branch from `master`
2. Make your changes with tests
3. Run the full test suite: `pytest tests/ -v`
4. Run lint: `ruff check .`
5. Run licence compliance: `make check-licenses`
6. Open a pull request — describe what changed and why

For larger changes (new workload types, schema changes, new API endpoints), open an issue first to align on the approach. See [CONTRIBUTING.md](../CONTRIBUTING.md) for full details.
