#!/usr/bin/env bash
# =============================================================================
# Benchmark: Qwen2.5-32B-Instruct — 4× NVIDIA RTX 5090, TP=4, BF16
# Hardware:  PRU server, node 10.6.5.158
# Run ID:    rtx5090-tp4-bf16-v1
# Date:      2026-05-13
# Tool:      ApacheBench (200 requests, concurrency=20)
# Results:   ~860 tok/s  |  e2el p50=21713ms  |  0.84 req/s
# =============================================================================

set -euo pipefail

MODEL="Qwen/Qwen2.5-32B-Instruct"
TP=4
PRECISION="bfloat16"
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
  --name vllm-rtx5090-qwen25-32b-tp4 \
  --gpus '"device=0,1,2,3"' \
  --ipc=host \
  -p ${PORT}:${PORT} \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  "$CONTAINER" \
  --model "$MODEL" \
  --tensor-parallel-size $TP \
  --dtype $PRECISION \
  --max-model-len 4096 \
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
# total_token_throughput  ~860    tok/s
# per_gpu_throughput      ~215    tok/s/GPU
# mean_e2el_ms            ~23870  ms
# p50_e2el_ms             ~21713  ms
# p95_e2el_ms             ~24521  ms
# requests_per_second     ~0.84

docker stop vllm-rtx5090-qwen25-32b-tp4 && docker rm vllm-rtx5090-qwen25-32b-tp4
