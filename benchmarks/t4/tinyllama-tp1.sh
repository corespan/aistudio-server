#!/usr/bin/env bash
# =============================================================================
# Benchmark: TinyLlama-1.1B-Chat — 1× NVIDIA T4, TP=1
# Hardware:  Dell Inc. server, node 10.6.12.23
# Run ID:    t4-tinyllama-1b-tp1
# Date:      2025-10-03
# Results:   c=4 → 565 tok/s  |  c=8 → 941 tok/s  |  c=16 → 1477 tok/s
# =============================================================================

set -euo pipefail

MODEL="TinyLlama/TinyLlama-1.1B-Chat-v1.0"
TP=1
PRECISION="float16"
# Container version is pinned centrally so this run is reproducible.
# shellcheck source=../versions.env
source "$(dirname "${BASH_SOURCE[0]}")/../versions.env"
CONTAINER="$CONTAINER_NVIDIA"
bench_provenance "$CONTAINER"

# ── Step 1: Start vLLM inference server ──────────────────────────────────────
docker run -d \
  --name vllm-t4-tinyllama-tp1 \
  --gpus '"device=0"' \
  --ipc=host \
  -p 8000:8000 \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  "$CONTAINER" \
  --model "$MODEL" \
  --tensor-parallel-size $TP \
  --dtype $PRECISION \
  --max-model-len 2048 \
  --port 8000

echo "Waiting for server to be ready..."
until curl -sf http://localhost:8000/health; do sleep 5; done
echo "Server ready."

# ── Step 2: Run benchmark ─────────────────────────────────────────────────────
for CONCURRENCY in 4 8 16; do
  echo ""
  echo "=== Concurrency $CONCURRENCY ==="
  python -m vllm.entrypoints.openai.cli_client benchmark_serving \
    --backend vllm \
    --base-url http://localhost:8000 \
    --model "$MODEL" \
    --dataset-name random \
    --random-input-len 1023 \
    --random-output-len 1024 \
    --num-prompts 80 \
    --request-rate "$CONCURRENCY"
done

# ── Expected results ──────────────────────────────────────────────────────────
# Concurrency  4  →  total_token_throughput ~565   tok/s  |  TTFT ~357ms
# Concurrency  8  →  total_token_throughput ~941   tok/s  |  TTFT ~619ms
# Concurrency 16  →  total_token_throughput ~1477  tok/s  |  TTFT ~1055ms

docker stop vllm-t4-tinyllama-tp1 && docker rm vllm-t4-tinyllama-tp1
