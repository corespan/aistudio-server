#!/usr/bin/env bash
# =============================================================================
# Benchmark: Qwen2.5-32B-Instruct — 4× NVIDIA RTX 5090, PP=4, FP8 + chunked prefill
# Hardware:  PRU server, node 10.6.5.158
# Run ID:    rtx5090-pp4-fp8-v1
# Date:      2026-05-13
# Tool:      ApacheBench (200 requests, concurrency=20)
# Results:   ~5345 tok/s  |  e2el p50=127ms  |  5.22 req/s
# Note:      chunked prefill dramatically improves throughput on RTX 5090.
#            Use PP=4 instead of TP=4 for this config.
# =============================================================================

set -euo pipefail

MODEL="Qwen/Qwen2.5-32B-Instruct"
PP=4
PRECISION="fp8"
# Container version is pinned centrally so this run is reproducible.
# shellcheck source=../versions.env
source "$(dirname "${BASH_SOURCE[0]}")/../versions.env"
CONTAINER="$CONTAINER_NVIDIA"
bench_provenance "$CONTAINER"
PORT=8000
CONCURRENCY=20
TOTAL_REQUESTS=200

# ── Step 1: Start vLLM inference server ──────────────────────────────────────
docker run -d \
  --name vllm-rtx5090-qwen25-32b-pp4-fp8 \
  --gpus '"device=0,1,2,3"' \
  --ipc=host \
  -p ${PORT}:${PORT} \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  "$CONTAINER" \
  --model "$MODEL" \
  --pipeline-parallel-size $PP \
  --dtype $PRECISION \
  --max-model-len 4096 \
  --enable-chunked-prefill \
  --max-num-batched-tokens 65536 \
  --port $PORT

echo "Waiting for server to be ready..."
until curl -sf http://localhost:${PORT}/health; do sleep 5; done
echo "Server ready."

# ── Step 2: Prepare a sample prompt file ─────────────────────────────────────
cat > /tmp/prompt.json <<'EOF'
{"model": "Qwen/Qwen2.5-32B-Instruct", "messages": [{"role": "user", "content": "Write a short paragraph about artificial intelligence."}], "max_tokens": 1024}
EOF

# ── Step 3: Run ApacheBench ───────────────────────────────────────────────────
echo ""
echo "=== ApacheBench: concurrency=$CONCURRENCY, requests=$TOTAL_REQUESTS ==="
ab \
  -n $TOTAL_REQUESTS \
  -c $CONCURRENCY \
  -T "application/json" \
  -p /tmp/prompt.json \
  http://localhost:${PORT}/v1/chat/completions

# ── Expected results ──────────────────────────────────────────────────────────
# total_token_throughput  ~5345   tok/s
# per_gpu_throughput      ~1336   tok/s/GPU
# mean_e2el_ms            ~3830   ms
# p50_e2el_ms             ~127    ms
# p95_e2el_ms             ~7000   ms
# requests_per_second     ~5.22
#
# Key levers: --enable-chunked-prefill + --max-num-batched-tokens 65536
# Try increasing max-num-batched-tokens for further gains.

docker stop vllm-rtx5090-qwen25-32b-pp4-fp8 && docker rm vllm-rtx5090-qwen25-32b-pp4-fp8
