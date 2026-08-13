# Quickstart

Get AIStudio Server running and complete your first LLM benchmark in under 10 minutes.

## Prerequisites

| Tool | Version | Install |
|------|---------|---------|
| Docker Desktop | 24+ | https://docs.docker.com/get-docker/ |
| `make` | any | Pre-installed on Linux/Mac. Windows: use WSL |
| A GPU node | NVIDIA, any size | Must be reachable over SSH from the server |

> **Windows users:** Run all `make` and shell commands in a WSL terminal (`Win+R` → `wsl`), not PowerShell or CMD.

---

## 1. Clone and configure

```bash
git clone https://github.com/corespan/aistudio-server.git
cd aistudio-server
cp .env.example .env
```

Open `.env` and set at minimum:

```env
SSH_KEY_PATH=~/.ssh/id_rsa       # path to the private key that can SSH into your GPU node
SSH_DEFAULT_USER=ubuntu           # SSH user on the GPU node
```

Everything else works with defaults for local development. See [configuration.md](./configuration.md) for the full reference.

---

## 2. Start the server

```bash
make setup
```

`make setup` does four things in one shot:
1. Vendors the demo-UI frontend assets (fonts + Chart.js, served locally — no CDN calls)
2. Builds and starts all Docker containers (API, Celery worker, PostgreSQL, RabbitMQ, Nginx)
3. Runs database migrations (`alembic upgrade head`)
4. Seeds the workload catalog (`python -m app.services.catalog_seeder`)

**Without `make` (Linux/WSL):**
```bash
./scripts/vendor_frontend_assets.sh --if-missing
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

Interactive API docs are available at **http://localhost:8002/docs**.

---

## 3. Prepare your GPU node

The server SSHes into the GPU node and runs Docker commands there. The node needs:

**a) Docker installed and running:**
```bash
# On the GPU node
docker --version
```

**b) The server's SSH public key authorised:**
```bash
# On your local machine
ssh-copy-id -i ~/.ssh/id_rsa.pub <ssh-user>@<gpu-node-ip>

# Verify
ssh <ssh-user>@<gpu-node-ip> docker ps
```

**c) A results directory:**
```bash
# On the GPU node — must match NODE_RESULTS_PATH in .env (default: /results)
sudo mkdir -p /results && sudo chown $USER:$USER /results
```

**d) A dataset file** (required for LLM benchmarks):
```bash
# On the GPU node — download a ShareGPT-format JSON dataset
# Example using OpenOrca (MIT licence):
wget -O /home/$USER/datasets/dataset.json \
  https://huggingface.co/datasets/Open-Orca/OpenOrca/resolve/main/1M-GPT4-Augmented.parquet
# Or place any ShareGPT-format JSON at a path of your choice
```

Verify the node is reachable:
```bash
make check-node-env NODE=<gpu-node-ip>
```

---

## 4. Run your first benchmark

Using the API directly:

```bash
curl -X POST http://localhost:8002/api/v1/benchmarks/start \
  -H "Content-Type: application/json" \
  -d '{
    "model_name": "TinyLlama/TinyLlama-1.1B-Chat-v1.0",
    "node_ips": ["<gpu-node-ip>"],
    "config": {
      "precision": "fp16",
      "concurrency": 4,
      "input_tokens": 512,
      "output_tokens": 128,
      "gpu_count": 1,
      "dataset_path": "/home/ubuntu/datasets/dataset.json"
    }
  }'
# → {"status":"queued","task_id":"wl-20260810-a1b2c3","message":"..."}
```

Poll for status:
```bash
curl http://localhost:8002/api/v1/benchmarks/wl-20260810-a1b2c3/status
```

Stream live logs:
```bash
curl -N http://localhost:8002/api/v1/benchmarks/wl-20260810-a1b2c3/logs/stream
```

View results in the leaderboard:
```bash
curl http://localhost:8002/api/v1/benchmarks
```

---

## 5. Open the demo UI

The demo UI is served at **http://localhost:3000** and connects to the local API automatically. It provides a benchmark wizard, live log streaming, and the results leaderboard.

---

## Next steps

- [gpu-nodes.md](./gpu-nodes.md) — SSH setup, HF token for gated models, troubleshooting
- [models.md](./models.md) — Adding models, gated vs ungated, catalog.json
- [configuration.md](./configuration.md) — Full environment variable reference
- [api.md](./api.md) — REST API reference
