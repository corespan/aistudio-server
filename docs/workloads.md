# Workloads

AIStudio Server supports two workload types. Each runs as a Docker container on the GPU node, managed by the Celery worker over SSH.

---

## LLM Inference (`LLMInference`)

**Image:** `us-docker.pkg.dev/aimlworkbench/aistudio/llminference:1.0.0-nvidia`
**Source:** [aistudio-workloads/llm-inference](https://github.com/corespan/aistudio-workloads/tree/main/llm-inference)

Benchmarks LLM inference throughput and latency using vLLM. The container owns the entire workflow — it starts its own vLLM server, sweeps the requested concurrency levels, collects metrics, and writes results.

### What it measures

| Metric | Description |
|--------|-------------|
| `total_token_throughput` | Total output tokens per second across all concurrent requests |
| `per_gpu_throughput_tok_s` | `total_token_throughput ÷ gpu_count` — for cross-GPU comparison |
| `mean_ttft_ms` | Mean time to first token (ms) |
| `mean_tpot_ms` | Mean time per output token (ms) |
| `mean_e2el_ms` | Mean end-to-end latency per request (ms) |

### How it works

1. The Celery worker calls `ManifestBuilder.build_llm_benchmark_command()` to produce a `docker run` shell command.
2. The command is executed on the GPU node via SSH.
3. Inside the container, `benchmark.py`:
   - Starts a vLLM server on port `9123` (internal to the container)
   - Runs `vllm bench serve` for each concurrency level
   - Prints `BENCH_RESULT:{json}` lines to stdout for each level
   - Writes `benchmark_result.json` and `summary.json` to `/results/<run_id>/`
4. The worker parses the `BENCH_RESULT:` lines and inserts a `BenchmarkResult` row per concurrency level.

### Volume mounts

| Host path | Container path | Purpose |
|-----------|----------------|---------|
| `~/.cache/huggingface` | `/root/.cache/huggingface` | Model weight cache — shared across runs |
| `NODE_RESULTS_PATH/<run_id>` | `/results/<run_id>` | Benchmark output persistence |
| `dataset_path` | `dataset_path` (same path) | User-supplied dataset file |

### Dataset requirement

The benchmark requires a ShareGPT-format JSON file. No dataset is bundled or downloaded automatically. The operator provides an absolute path via `dataset_path` in the benchmark config.

```json
"config": {
  "dataset_path": "/home/ubuntu/datasets/sharegpt.json"
}
```

The path is bind-mounted into the container at the same location — `benchmark.py` reads it directly.

### Configuration parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `precision` | `fp16` | Model precision |
| `concurrency` | `4` | Number of simultaneous requests |
| `input_tokens` | `512` | Prompt length |
| `output_tokens` | `256` | Generated tokens per request |
| `gpu_count` | `1` | Number of GPUs (tensor parallelism) |
| `max_model_len` | _(model default)_ | Max sequence length override |
| `dataset_path` | _(required)_ | Absolute path to dataset on GPU node |

---

## Jupyter Notebook (`JupyterNotebook`)

**Image:** `us-docker.pkg.dev/aimlworkbench/aistudio/jupyternotebook:1.0.0-nvidia`
**Source:** [aistudio-workloads/jupyter-notebook](https://github.com/corespan/aistudio-workloads/tree/main/jupyter-notebook)

Launches a JupyterLab environment on the GPU node with pre-installed GPU profiling utilities and MLPerf microbenchmarks.

### What's included

- JupyterLab with GPU access (`--gpus all`)
- `jupyter-ai` — AI-assisted coding inside notebooks
- PyTorch + torchvision + CUDA
- vLLM for in-notebook inference experiments
- onnxruntime-gpu, accelerate, datasets, pycocotools (MLPerf dependencies)

### How it works

1. The Celery worker calls `ManifestBuilder.build_jupyter_command()`.
2. The container starts in detached mode (`-d`) — unlike benchmarks, it runs until explicitly stopped.
3. `script.sh` copies notebooks to `/data/<workload_id>/` and starts JupyterLab from that directory.
4. The worker polls the Jupyter API endpoint until it responds (up to 5 minutes).
5. The UI shows the Jupyter URL once the health check passes.

### URL and proxy

When `NGINX_ENABLED=false` (default), the Jupyter URL is the GPU node's direct IP and port. This exposes the node's internal IP to the client.

When `NGINX_ENABLED=true`, the server writes an nginx location config for the instance and the URL becomes a public path-based route:
```
{PROXY_BASE_URL}/jupyter/{gpu_type}/{task_id}/lab
```
The nginx container auto-reloads when new location configs are written (via `inotifywait`) — no manual restart needed.

### Volume mounts

| Host path | Container path | Purpose |
|-----------|----------------|---------|
| `NODE_JUPYTER_DATA_PATH` | `/data` | Notebook storage — persists across container restarts |

### Stopping a Jupyter instance

```bash
DELETE /api/v1/jupyter/instances/{task_id}
```

Or from the UI — click the delete button on the instance row.

### AI Assistant Setup

JupyterLab ships with `jupyter-ai` built in — write, execute, and visualize code with LLM assistance inside the notebook.

**Prerequisites:**
- GPUs with CUDA compute capability ≥ 7
- Docker installed with the NVIDIA Container Toolkit

**1. Run a vLLM inference server (or point at one already running):**

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

Replace `<NUMBER_OF_GPUS>` with the number of GPUs available on the machine.

**2. Point JupyterLab at it:**

1. Open the **Settings** tab in JupyterLab.
2. Go to **AI Settings** → click **Add Secret**.
3. Set **Secret Name** to `HOSTED_VLLM_API_BASE` and **Value** to your vLLM server URL: `http://<MACHINE_IP>:8000/v1/`
4. Update the **Chat Model** to `hosted_vllm/Qwen/Qwen2.5-7B-Instruct-1M`.

The AI assistant is now ready to use inside notebooks.

---

## Adding a New Workload Type

New workload types require changes in both repositories:

**aistudio-workloads** — create a new directory with:
- `Dockerfile` — base image + dependencies
- `script.sh` — entrypoint
- `requirements.txt`
- `version.py`

**aistudio-server** — three changes:
1. Add the workload type to `catalog.json` under `workload_types`
2. Add a `build_<workload>_command()` method to `ManifestBuilder`
3. Add a route and Celery task to handle the new type

Re-seed the catalog after updating `catalog.json`:
```bash
docker compose exec api python -m app.services.catalog_seeder
```

See [CONTRIBUTING.md](../CONTRIBUTING.md) for the full contribution workflow.
