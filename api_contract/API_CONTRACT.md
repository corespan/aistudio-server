# AIStudio Server — API Contract

This document defines the API contract that any compatible frontend (including `aistudio-ui`) must follow. If you're building your own UI or integrating with an existing one, implement against these endpoints.

## Base URL

```
http://<server-host>:8001
```

## Authentication

None (v1). API is unauthenticated. Deploy behind a reverse proxy with auth if needed.

## Endpoints

### GET /api/v1/workload-types

Returns available workload types for the model selection dropdown.

**Response:**
```json
[
  {
    "id": "uuid",
    "name": "LLMInference",
    "display_name": "LLM Inference (vLLM)",
    "description": "..."
  }
]
```

### POST /api/v1/benchmarks/start

Start a new benchmark run.

**Request:**
```json
{
  "model_name": "llama3-8b-instruct",
  "node_ips": ["10.0.0.5"],
  "config": {
    "concurrency": 4,
    "input_tokens": 512,
    "output_tokens": 512,
    "gpu_count": 1
  }
}
```

**Response (201):**
```json
{
  "status": "success",
  "task_id": "wl-20260603-a3f9bc",
  "message": "Benchmark workload created and dispatched to Celery."
}
```

### GET /api/v1/benchmarks/{task_id}/status

Poll workload progress. Call every 2 seconds.

**Response:**
```json
{
  "task_id": "wl-20260603-a3f9bc",
  "state": "RUNNING",
  "error_message": null,
  "updated_at": "2026-06-03T12:34:56Z"
}
```

**States:** CREATED → VALIDATING → VALIDATED → INSTALLING → READY → RUNNING → READY (done) or FAILED

### GET /api/v1/benchmarks/{task_id}/logs/stream

Server-Sent Events stream of live terminal output.

**Content-Type:** `text/event-stream`

**SSE format:**
```
data: Checking Docker...

data: Pulling workload image...

data: Running benchmark: concurrency=4

event: close
data: stream closed
```

The `close` event signals the stream is done. Reconnect if the connection drops.

### POST /api/v1/metrics

Ingest benchmark results. Idempotent (upsert on `run_id` + `sub_run_index`).

**Request:**
```json
{
  "run_id": "wl-20260603-a3f9bc",
  "timestamp": "2026-06-03T12:45:00Z",
  "workload": {"name": "llama3-8b-instruct", "type": "llm"},
  "metrics": {
    "total_token_throughput": 1250.5,
    "mean_ttft_ms": 45.2,
    "mean_tpot_ms": 12.3,
    "mean_e2el_ms": 890.1
  },
  "status": "success",
  "gpu_type": "a100",
  "node_ip": "10.0.0.5",
  "config": {
    "concurrency": 4,
    "precision": "fp16",
    "input_tokens": 512,
    "output_tokens": 512,
    "gpu_count": 1,
    "pipeline_version": "vllm-0.4.2"
  }
}
```

**Response (202):**
```json
{"status": "success", "run_id": "wl-20260603-a3f9bc", "message": "Ingested successfully."}
```

### GET /api/v1/benchmarks

Leaderboard with filters. All query params are optional.

**Query params:** `model`, `gpu_type`, `node_ip`, `concurrency`, `status`, `date` (YYYY-MM-DD), `limit`

**Response:** Array of `BenchmarkResult` objects.

### GET /api/v1/benchmarks/{run_id}

Full detail for a single run (with all sub-runs if it's a sweep).

### GET /api/v1/benchmarks/compare?run_a=X&run_b=Y

Side-by-side comparison of two runs.

### GET /api/v1/models

Distinct model names for dropdown. Optional `date` filter.

### GET /api/v1/gpu-types

Distinct GPU types for dropdown. Optional `date` filter.

### GET /api/v1/summary

Aggregate stats. Optional `date` filter. Defaults to last 30 days.

**Response:**
```json
{
  "total_runs": 42,
  "successful_runs": 39,
  "success_rate": 92.86,
  "avg_throughput": 1180.5,
  "date": "last_30d"
}
```
