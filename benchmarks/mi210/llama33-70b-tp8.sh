#!/usr/bin/env bash
# =============================================================================
# Benchmark: Llama-3.3-70B-Instruct — 8× AMD Instinct MI210, TP=8
# Hardware:  SuperMicro server, node 10.6.12.11
# Run ID:    mi210-llama33-70b-tp8
# Date:      2026-02-05
# Results:   c=4 → 130 tok/s  |  c=8 → 251 tok/s  |  c=16 → 410 tok/s
# =============================================================================

set -euo pipefail

MODEL="meta-llama/Llama-3.3-70B-Instruct"
TP=8
PRECISION="bfloat16"
CONTAINER="rocm/vllm:latest"

# ── Step 1: Start vLLM inference server ──────────────────────────────────────
docker run -d \
  --name vllm-mi210-llama33-70b-tp8 \
  --device=/dev/kfd \
  --device=/dev/dri \
  --group-add video \
  --ipc=host \
  -p 8000:8000 \
  -e HIP_VISIBLE_DEVICES=0,1,2,3,4,5,6,7 \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  "$CONTAINER" \
  --model "$MODEL" \
  --tensor-parallel-size $TP \
  --dtype $PRECISION \
  --max-model-len 4096 \
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
# Concurrency  4  →  total_token_throughput ~130  tok/s  |  TTFT ~2810ms
# Concurrency  8  →  total_token_throughput ~251  tok/s  |  TTFT ~2838ms
# Concurrency 16  →  total_token_throughput ~410  tok/s  |  TTFT ~4163ms

docker stop vllm-mi210-llama33-70b-tp8 && docker rm vllm-mi210-llama33-70b-tp8
