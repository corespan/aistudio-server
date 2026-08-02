#!/usr/bin/env bash
# =============================================================================
# Benchmark: Llama-3.1-70B-Instruct — 2× NVIDIA H100 NVL, TP=2
# Hardware:  PRU server, node 10.6.91.204
# Run ID:    h100-llama3-70b-c256
# Date:      2026-01-15
# Dataset:   Shared prefix, input=8092, output=256
# Results:   concurrency=256 → 2255 tok/s  |  TTFT 11429ms
# =============================================================================

set -euo pipefail

MODEL="meta-llama/Meta-Llama-3.1-70B-Instruct"
TP=2
PRECISION="float16"
# Container version is pinned centrally so this run is reproducible.
# shellcheck source=../versions.env
source "$(dirname "${BASH_SOURCE[0]}")/../versions.env"
CONTAINER="$CONTAINER_NVIDIA"
bench_provenance "$CONTAINER"

# ── Step 1: Start vLLM inference server ──────────────────────────────────────
docker run -d \
  --name vllm-h100-llama31-70b-tp2 \
  --gpus '"device=0,1"' \
  --ipc=host \
  -p 8000:8000 \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  "$CONTAINER" \
  --model "$MODEL" \
  --tensor-parallel-size $TP \
  --dtype $PRECISION \
  --max-model-len 8192 \
  --port 8000

echo "Waiting for server to be ready..."
until curl -sf http://localhost:8000/health; do sleep 5; done
echo "Server ready."

# ── Step 2: Run benchmark ─────────────────────────────────────────────────────
echo ""
echo "=== Concurrency 256 ==="
python benchmarks/benchmark_serving.py \
  --backend vllm \
  --base-url http://localhost:8000 \
  --model "$MODEL" \
  --dataset-name sharegpt \
  --num-prompts 2559 \
  --request-rate 256 \
  --max-input-len 8092 \
  --max-output-len 256

# ── Expected results ──────────────────────────────────────────────────────────
# total_token_throughput  ~2255  tok/s
# mean_ttft_ms            ~11429 ms
# mean_tpot_ms            ~171   ms
# mean_e2el_ms            ~55079 ms
# avg_gpu_power_watts     ~631   W
# avg_gpu_utilization     ~96.8%

docker stop vllm-h100-llama31-70b-tp2 && docker rm vllm-h100-llama31-70b-tp2
