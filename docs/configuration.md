# Configuration

All configuration is read from environment variables. For local development, copy `.env.example` to `.env` — docker-compose loads it automatically.

```bash
cp .env.example .env
```

---

## PostgreSQL

| Variable | Default | Description |
|----------|---------|-------------|
| `POSTGRES_HOST` | `postgres` | Hostname of the PostgreSQL server. Use `localhost` for local dev without docker-compose. |
| `POSTGRES_PORT` | `5432` | PostgreSQL port |
| `POSTGRES_USERNAME` | `aistudio` | Database user |
| `POSTGRES_PASSWORD` | `aistudio` | Database password — change for production |
| `POSTGRES_DATABASE` | `aistudio` | Database name |

---

## RabbitMQ / Celery

| Variable | Default | Description |
|----------|---------|-------------|
| `RABBITMQ_URL` | `rabbitmq` | Hostname of the RabbitMQ broker |
| `RABBITMQ_PORT` | `5672` | AMQP port |
| `RABBITMQ_USERNAME` | `aistudio` | RabbitMQ user |
| `RABBITMQ_PASSWORD` | `aistudio` | RabbitMQ password — change for production |

---

## Workload Images

The workload container images are pulled from Google Artifact Registry (`us-docker.pkg.dev/aimlworkbench/aistudio`). This registry has public read access — no authentication is needed on GPU nodes.

| Variable | Default | Description |
|----------|---------|-------------|
| `GCP_REGISTRY_URL` | `us-docker.pkg.dev` | Registry hostname |
| `GCP_PROJECT_ID` | `aimlworkbench` | GCP project ID |
| `GCP_REPOSITORY` | `aistudio` | Artifact Registry repository name |
| `WORKLOAD_IMAGE_TAG` | `1.0.0-nvidia` | Tag for the `llminference` image |
| `JUPYTER_IMAGE_TAG` | `1.0.0-nvidia` | Tag for the `jupyternotebook` image |

Full image paths:
```
us-docker.pkg.dev/aimlworkbench/aistudio/llminference:1.0.0-nvidia
us-docker.pkg.dev/aimlworkbench/aistudio/jupyternotebook:1.0.0-nvidia
```

---

## Model Storage

Controls how the workload container accesses model weights on the GPU node.

| Variable | Default | Description |
|----------|---------|-------------|
| `MODEL_STORAGE_MODE` | `huggingface` | Storage backend: `huggingface`, `local`, or `gcs` |
| `MODEL_LOCAL_PATH` | `/home/ubuntu/models` | Local model directory (used when `MODEL_STORAGE_MODE=local`) |
| `MODEL_GCS_BUCKET` | _(empty)_ | GCS bucket URI, e.g. `gs://my-bucket` (used when `MODEL_STORAGE_MODE=gcs`) |

**Mode details:**

- `huggingface` (default) — mounts `~/.cache/huggingface` from the GPU node into the container. Models are downloaded on first run and cached for subsequent runs.
- `local` — mounts `MODEL_LOCAL_PATH` from the node as `/models` inside the container. Use when weights are pre-downloaded and stored locally.
- `gcs` — passes `GCS_BUCKET` as an env var to the container. The workload image must support GCS model loading.

---

## GPU Node Paths

These paths are on the **GPU node**, not on the server.

| Variable | Default | Description |
|----------|---------|-------------|
| `NODE_RESULTS_PATH` | `/results` | Where benchmark output is written on the GPU node. Each run creates a subdirectory `/results/<run_id>/` containing `benchmark_result.json`, `summary.json`, and `logs/`. |
| `NODE_JUPYTER_DATA_PATH` | `/data` | Where Jupyter notebooks are stored on the GPU node. Each session creates `/data/<workload_id>/`. Any writable directory works — does not need shared storage. |

---

## SSH

The server connects to GPU nodes over SSH using a private key. The key must be mounted into the `api` and `worker` containers (done automatically by docker-compose when using the default `SSH_KEY_PATH`).

| Variable | Default | Description |
|----------|---------|-------------|
| `SSH_KEY_PATH` | `~/.ssh/id_rsa` | Path to the private SSH key on the host machine |
| `SSH_DEFAULT_USER` | `ubuntu` | Default SSH username on GPU nodes |

> **Important:** SSH commands run in a non-interactive shell. Do not rely on `~/.bashrc` for environment variables on GPU nodes — they are not sourced. Use `~/.aistudio/env` instead. See [gpu-nodes.md](./gpu-nodes.md).

---

## Nginx Reverse Proxy

When enabled, the Nginx container proxies Jupyter instances via path-based routing, hiding GPU node IPs from clients and providing public URLs.

| Variable | Default | Description |
|----------|---------|-------------|
| `NGINX_ENABLED` | `false` | Set to `true` to enable the reverse proxy |
| `PROXY_BASE_URL` | `https://your-domain.com:8443` | Public base URL. Jupyter URLs become `{PROXY_BASE_URL}/jupyter/{gpu_type}/{task_id}/lab` |
| `NGINX_CONF_DIR` | `/etc/nginx/jupyter-locations` | Directory where the worker writes per-instance location configs. Shared via Docker volume with the nginx container. |
| `NGINX_RELOAD_CMD` | `true` | Command to reload nginx after config changes. Defaults to `true` (no-op) because the nginx container uses `inotifywait` for auto-reload. |

By default, `jupyter_url` in the launch response is a direct `http://<node-ip>:<port>/lab` link. Enabling the proxy routes all Jupyter sessions through a single public domain instead, so GPU node IPs are never exposed to users. URL format with the proxy enabled: `{PROXY_BASE_URL}/jupyter/<GPU_TYPE>/<task_id>/lab`.

### One-time server setup (master node)

Run these once on the machine that will run nginx:

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
    server_name your-domain.example.com;
    include /etc/nginx/jupyter-locations/*.conf;
}
EOF

# Remove the default nginx site to avoid server_name conflicts
sudo rm -f /etc/nginx/sites-enabled/default

# Test and reload
sudo nginx -t && sudo nginx -s reload
```

Also update `docker-compose.yml` to bind-mount the nginx locations directory into the worker container:

```yaml
worker:
  volumes:
    - /etc/nginx/jupyter-locations:/etc/nginx/jupyter-locations
    - ~/.ssh:/root/.ssh:ro
```

When `NGINX_ENABLED=true` the worker writes a location block to `NGINX_CONF_DIR/jupyter-<task_id>.conf` on the nginx host, passes `JUPYTER_BASE_URL=/jupyter/<GPU_TYPE>/<task_id>/` to the container so JupyterLab serves assets from the correct subpath, and reloads nginx via `NGINX_RELOAD_CMD`. Location files are deleted automatically when a session ends.

---

## Server

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `8001` | Port the FastAPI server listens on inside the container. The docker-compose default maps this to `8002` on the host. |

---

## HuggingFace Token

The HF token is **not** configured in `.env`. It belongs on the GPU node, not on the server — it is forwarded into the workload container at runtime and never stored in the database or a run manifest.

See [gpu-nodes.md](./gpu-nodes.md#huggingface-token-for-gated-models) for setup instructions.
