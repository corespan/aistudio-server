# aistudio-server

Open-source LLM benchmarking and workload orchestration backend. SSHes into GPU nodes, runs vLLM benchmarks or launches Jupyter Lab, streams live logs via SSE, and stores results in PostgreSQL with a full leaderboard API.

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

`make setup` builds containers, runs DB migrations, and seeds the workload catalog in one shot.

**Without `make` (Linux / WSL):**
```bash
docker compose up --build -d
docker compose exec api alembic upgrade head
docker compose exec api python -m app.services.catalog_seeder
```

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
| `~/gcr.json` — GCP service account key | `ls ~/gcr.json` |
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

## Workload Images (GCP Artifact Registry)

No local builds on the GPU node — images are pulled automatically on each run.

| Image | Used for |
|-------|---------|
| `llminference:2.3.1-nvidia` | vLLM benchmark server |
| `jupyternotebook:2.2.0-nvidia` | Jupyter Lab (supports `--ServerApp.base_url` for nginx subpath proxy) |

To update an image tag: change it in `catalog.json` and run `make seed` — no code change needed.

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

> **Network prerequisites:** pfSense (or your router) must forward external port 80 → `10.6.12.15:80`. Internal clients must resolve `corespan.ddnsgeek.com` → `10.6.12.15` via split DNS; without it, requests loop through the WAN IP (NAT hairpin) and time out.

### Internal team access — hosts file (required once per machine)

`corespan.ddnsgeek.com` has split DNS on pfSense: the internal DNS server (`10.1.18.21`) returns `10.6.12.15`, but public DNS servers (8.8.8.8 etc.) return the WAN IP. The VPN does **not** automatically push pfSense as the DNS server to clients. If your machine falls back to public DNS, `corespan.ddnsgeek.com` resolves to the WAN IP, pfSense drops the connection, and you see `ERR_EMPTY_RESPONSE` in the browser.

**Every team member on the VPN must add this once:**

**Windows — run Notepad as Administrator:**
1. Open `C:\Windows\System32\drivers\etc\hosts`
2. Add this line at the bottom:
   ```
   10.6.12.15 corespan.ddnsgeek.com
   ```
3. Save, then flush DNS in PowerShell:
   ```powershell
   ipconfig /flushdns
   ```

**Linux / macOS:**
```bash
echo "10.6.12.15 corespan.ddnsgeek.com" | sudo tee -a /etc/hosts
```

After this, `corespan.ddnsgeek.com` always resolves to `10.6.12.15` regardless of which DNS server is active — Jupyter Lab URLs will work consistently every time.

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

## License

Apache 2.0 — see [LICENSE](./LICENSE)
