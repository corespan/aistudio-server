# API Reference

Base URL: `http://localhost:8002` (local) or your deployment URL.

Interactive docs (Swagger UI): `GET /docs`

All request and response bodies are JSON. All timestamps are UTC ISO-8601.

---

## Health

### `GET /health`

Liveness and readiness probe. Returns database connectivity status.

**Response**
```json
{
  "status": "healthy",
  "database": "ok"
}
```
`status` is `"healthy"` when the database is reachable, `"degraded"` otherwise.

---

## Benchmarks

### `POST /api/v1/benchmarks/start`

Start a new LLM benchmark run. The server SSHes into each node and runs the workload container asynchronously.

**Request body**
```json
{
  "model_name": "TinyLlama/TinyLlama-1.1B-Chat-v1.0",
  "node_ips": ["10.0.0.12"],
  "config": {
    "precision": "fp16",
    "concurrency": 4,
    "input_tokens": 512,
    "output_tokens": 128,
    "gpu_count": 1,
    "max_model_len": 2048,
    "dataset_path": "/home/ubuntu/datasets/sharegpt.json"
  }
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `model_name` | string | ✓ | HuggingFace repo ID, e.g. `meta-llama/Meta-Llama-3-8B-Instruct` |
| `node_ips` | string[] | ✓ | GPU node IP addresses to run on |
| `config` | object | ✓ | Benchmark parameters — see table below |

**Config fields**

| Field | Default | Description |
|-------|---------|-------------|
| `precision` | `"fp16"` | Model precision: `fp16`, `bf16`, `fp8` |
| `concurrency` | `4` | Number of concurrent requests |
| `input_tokens` | `512` | Prompt length in tokens |
| `output_tokens` | `256` | Generated tokens per request |
| `gpu_count` | `1` | Number of GPUs (tensor parallelism) |
| `max_model_len` | _(model default)_ | Max sequence length override |
| `dataset_path` | _(required)_ | Absolute path to a ShareGPT-format JSON file on the GPU node |

**Response `200`**
```json
{
  "status": "queued",
  "task_id": "wl-20260810-a1b2c3",
  "message": "Benchmark started"
}
```

---

### `GET /api/v1/benchmarks/{task_id}/status`

Poll the status of a running or completed benchmark.

**Response `200`**
```json
{
  "task_id": "wl-20260810-a1b2c3",
  "state": "running",
  "model_name": "TinyLlama/TinyLlama-1.1B-Chat-v1.0",
  "node_ips": ["10.0.x.x"],
  "created_at": "2026-08-10T10:00:00Z"
}
```

`state` values: `queued` → `running` → `completed` | `failed`

**Response `404`** — task_id not found.

---

### `GET /api/v1/benchmarks/{task_id}/logs/stream`

Server-Sent Events (SSE) stream of all log lines for a run. Supports `Last-Event-ID` for browser reconnects — only logs after the last received event are sent.

**Headers**

```
Accept: text/event-stream
```

**Event format**
```
id: 142
data: [10:01:23] Starting vLLM server on port 9123...

id: 143
data: BENCH_RESULT:{"concurrency":4,"throughput_tok_s":1234.5,...}
```

The stream ends with a `data: [DONE]` sentinel when the run completes or fails.

---

### `GET /api/v1/benchmarks`

Leaderboard — paginated list of completed benchmark results.

**Query parameters**

| Parameter | Type | Description |
|-----------|------|-------------|
| `model` | string | Filter by model name |
| `gpu_type` | string | Filter by GPU type |
| `server_name` | string | Filter by server name |
| `precision` | string | Filter by precision |
| `concurrency` | int | Filter by concurrency level |
| `date` | string | Filter by date (YYYY-MM-DD) |

**Response `200`** — array of benchmark result objects. Key fields:

```json
[
  {
    "run_id": "wl-20260810-a1b2c3",
    "sub_run_index": 0,
    "workload_type": "LLMInference",
    "model_name": "TinyLlama/TinyLlama-1.1B-Chat-v1.0",
    "gpu_type": "T4",
    "gpu_count": 1,
    "precision": "fp16",
    "concurrency": 4,
    "input_tokens": 512,
    "output_tokens": 128,
    "total_token_throughput": 1234.5,
    "per_gpu_throughput_tok_s": 1234.5,
    "mean_ttft_ms": 45.2,
    "mean_tpot_ms": 12.1,
    "mean_e2el_ms": 520.3,
    "status": "completed",
    "started_at": "2026-08-10T10:01:00Z",
    "completed_at": "2026-08-10T10:12:00Z",
    "duration_seconds": 660.0
  }
]
```

> Node IPs in responses have the last two octets masked (`10.6.x.x`) for privacy.

---

### `GET /api/v1/benchmarks/{run_id}`

Full detail for a single run, including all sub-runs (one per concurrency level) and the raw metrics blob.

---

### `GET /api/v1/benchmarks/compare`

Compare two runs side by side.

**Query parameters:** `run_id_a`, `run_id_b`

---

### `DELETE /api/v1/benchmarks/{run_id}`

Delete a single benchmark run and all its sub-runs.

### `DELETE /api/v1/benchmarks/bulk`

Delete multiple runs by ID. **Body:** `{"run_ids": ["wl-...", "wl-..."]}`

### `DELETE /api/v1/benchmarks/all`

Delete all benchmark data. Irreversible.

---

## System

### `GET /api/v1/models/config`

Returns the default vLLM configuration for a given model. Used by the UI wizard after the user selects a model. Falls back to generic defaults for unknown models.

**Query parameter:** `model` (string, required) — HuggingFace repo ID

**Response `200`**
```json
{
  "precision": "fp16",
  "concurrency": 4,
  "input_tokens": 512,
  "output_tokens": 128,
  "max_model_len": 2048,
  "gpu_count": 1,
  "dataset_path": "",
  "gated": false,
  "license": "Apache-2.0",
  "license_url": "https://huggingface.co/TinyLlama/TinyLlama-1.1B-Chat-v1.0",
  "hf_repo": "TinyLlama/TinyLlama-1.1B-Chat-v1.0"
}
```

---

### `GET /api/v1/workload-types`

Returns the list of supported workload types, seeded from `catalog.json`.

**Response `200`**
```json
[
  {
    "id": "uuid",
    "name": "LLMInference",
    "display_name": "LLM Inference (vLLM)",
    "description": "Benchmark LLM inference throughput and latency using vLLM."
  }
]
```

---

## Reference Dropdowns

These endpoints return distinct values from completed benchmark results, used to populate UI filter dropdowns.

| Endpoint | Returns |
|----------|---------|
| `GET /api/v1/models` | Distinct model names |
| `GET /api/v1/gpu-types` | Distinct GPU types |
| `GET /api/v1/servers` | Distinct server names |
| `GET /api/v1/nodes` | Distinct node IPs (masked) |
| `GET /api/v1/concurrencies` | Distinct concurrency levels |

All accept an optional `date` query parameter to filter by run date.

---

## Jupyter

### `POST /api/v1/jupyter/launch`

Launch a Jupyter Lab instance on a GPU node.

**Request body**
```json
{
  "node_ip": "10.0.0.12",
  "gpu_type": "T4"
}
```

**Response `200`**
```json
{
  "task_id": "jup-20260810-xyz",
  "url": "http://10.0.0.12:7008/lab"
}
```

When `NGINX_ENABLED=true`, the `url` uses the public `PROXY_BASE_URL` with a path-based route.

### `GET /api/v1/jupyter/instances`

List all active Jupyter instances.

### `GET /api/v1/jupyter/instances/{task_id}/status`

Status of a specific Jupyter instance.

### `GET /api/v1/jupyter/instances/{task_id}/logs/stream`

SSE log stream for Jupyter launch (same format as benchmark logs).

### `GET /api/v1/jupyter/instances/{task_id}/health`

Checks if the Jupyter server is responding on the node.

### `DELETE /api/v1/jupyter/instances/{task_id}`

Stop and remove a Jupyter instance.

---

## Metrics Ingestion (Legacy)

### `POST /api/v1/metrics`

Legacy ingestion endpoint for external runners (Kubeflow, CLI scripts). Accepts the old metrics payload format and maps it into the benchmark results table.

```json
{
  "run_id": "my-run-001",
  "timestamp": "2026-08-10T10:00:00Z",
  "workload": { "name": "llama3-8b", "type": "llm" },
  "metrics": { "throughput_tok_s": 1234.5, "ttft_ms": 45.2 },
  "status": "completed",
  "gpu_type": "A100",
  "node_ip": "10.0.0.12",
  "config": { "concurrency": 8 }
}
```

**Response `202 Accepted`**
