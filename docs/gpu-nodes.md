# GPU Node Setup

AIStudio Server connects to GPU nodes over SSH and runs Docker commands on them. This page covers everything needed to prepare a node.

---

## Requirements

| Requirement | Notes |
|-------------|-------|
| NVIDIA GPU | Any size — T4, A100, H100, RTX series |
| Ubuntu 20.04+ or Debian 11+ | Other distros work but are untested |
| Docker 24+ with NVIDIA Container Toolkit | See below |
| SSH access from the server | Key-based, no password |
| Internet access (optional) | Only needed to download model weights |

---

## 1. Install Docker and NVIDIA Container Toolkit

```bash
# Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker

# NVIDIA Container Toolkit
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | \
  sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -sL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
  sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
  sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt update && sudo apt install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker

# Verify
docker run --rm --gpus all nvidia/cuda:12.1-base-ubuntu22.04 nvidia-smi
```

---

## 2. Authorize the server's SSH key

The server connects using the private key at `SSH_KEY_PATH` (default: `~/.ssh/id_rsa`). Copy the corresponding public key to the node:

```bash
# From your local machine / server host
ssh-copy-id -i ~/.ssh/id_rsa.pub <ssh-user>@<gpu-node-ip>

# Verify — should run without a password prompt and show docker output
ssh <ssh-user>@<gpu-node-ip> docker ps
```

If you don't have an existing key pair:
```bash
ssh-keygen -t ed25519 -C "aistudio-server"
# Then copy the public key as above
```

---

## 3. Create required directories

```bash
# Results directory — must match NODE_RESULTS_PATH in .env (default: /results)
sudo mkdir -p /results && sudo chown $USER:$USER /results

# Jupyter data directory — must match NODE_JUPYTER_DATA_PATH in .env (default: /data)
sudo mkdir -p /data && sudo chown $USER:$USER /data

# HuggingFace model cache (created automatically on first run, but can pre-create)
mkdir -p ~/.cache/huggingface
```

---

## 4. Place a dataset file

LLM benchmarks require a ShareGPT-format JSON file on the GPU node. The path you provide in `dataset_path` is bind-mounted into the benchmark container at the same path.

```bash
mkdir -p ~/datasets

# Option A: OpenOrca (MIT licence — recommended)
# Download from HuggingFace and convert to ShareGPT format, or use a pre-converted copy

# Option B: Dolly (CC-BY-SA-3.0)
wget -O ~/datasets/dolly.json \
  https://huggingface.co/datasets/databricks/databricks-dolly-15k/resolve/main/databricks-dolly-15k.jsonl

# Option C: ShareGPT (contested provenance — see MODEL-LICENSES.md)
# Place a local copy at e.g. ~/datasets/sharegpt.json
```

When starting a benchmark, set `dataset_path` to the absolute path:
```json
"config": {
  "dataset_path": "/home/ubuntu/datasets/sharegpt.json"
}
```

---

## 5. HuggingFace token (for gated models)

Several models in `catalog.json` (all Meta Llama variants) require you to request access on HuggingFace and accept their licence. Downloads fail with HTTP 401 without an approved token — unless the weights are already in `~/.cache/huggingface`.

**The token goes on the GPU node, not in the server's `.env`.** The Celery worker sources it from the node and forwards it into the workload container at run time. It is never stored in the database or written into a run manifest.

```bash
# On the GPU node
mkdir -p ~/.aistudio
echo "HF_TOKEN=hf_xxxxxxxxxxxxxxxxxxxx" > ~/.aistudio/env
chmod 600 ~/.aistudio/env
```

> **Do not use `~/.bashrc`.** Ubuntu and Debian ship a `~/.bashrc` that starts with an early `return` for non-interactive shells:
> ```bash
> case $- in
>     *i*) ;;
>       *) return;;
> esac
> ```
> Every command the server runs over SSH is non-interactive, so anything appended to `~/.bashrc` sits below that `return` and never runs. The token appears set when you log in interactively and unset for every benchmark — `~/.aistudio/env` is sourced explicitly by the worker and avoids this entirely.

Verify the token is being picked up correctly:
```bash
# From the server machine
make check-node-env NODE=<gpu-node-ip>
```

Check which models require a token:
```bash
python3 scripts/check_model_access.py
```

---

## 6. Verify the node

Run the full pre-flight check from the server:
```bash
make check-node-env NODE=<gpu-node-ip>
```

This checks:
- SSH connectivity
- Docker is running and the GPU is accessible
- `~/.aistudio/env` is present (if HF_TOKEN is configured)
- Required directories exist

---

## Troubleshooting

**`docker: command not found` on the node**
The SSH session doesn't inherit the same `PATH` as an interactive login. Check if Docker is installed:
```bash
ssh <user>@<node> which docker
```
If Docker is at a non-standard path, symlink it: `sudo ln -s /usr/local/bin/docker /usr/bin/docker`

**Benchmark fails with `HF_TOKEN` not set for a gated model**
The token is unset in the SSH session. Verify `~/.aistudio/env` exists and is readable:
```bash
ssh <user>@<node> 'cat ~/.aistudio/env'
```
Do not use `~/.bashrc` — see above.

**`permission denied` writing to `/results`**
The directory exists but the SSH user doesn't own it:
```bash
ssh <user>@<node> 'sudo chown $USER:$USER /results'
```

**Container image pull fails**
The GCR registry is public. If the node is in an air-gapped environment, pull the image manually on a connected machine and transfer it:
```bash
docker save us-docker.pkg.dev/aimlworkbench/aistudio/llminference:1.0.0-nvidia | gzip > llminference.tar.gz
scp llminference.tar.gz <user>@<node>:~/
ssh <user>@<node> 'docker load < ~/llminference.tar.gz'
```
