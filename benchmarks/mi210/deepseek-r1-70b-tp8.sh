#!/usr/bin/env bash
# =============================================================================
# Benchmark: DeepSeek-R1-Distill-Llama-70B — 8× AMD Instinct MI210, TP=8
# Hardware:  SuperMicro server, node 10.6.12.11
# Run ID:    mi210-deepseek-r1-70b-tp8
# Date:      2026-02-05
# Dataset:   Open Orca (avg ~200 input tokens, 256 output tokens)
# Results:   c=32 → 757 tok/s  |  c=64 → 1365 tok/s  |  c=128 → 2112 tok/s
# =============================================================================

set -euo pipefail

MODEL="deepseek-ai/DeepSeek-R1-Distill-Llama-70B"
TP=8
PRECISION="bfloat16"
CONTAINER="rocm/vllm:latest"

# ── Step 1: Start vLLM inference server ──────────────────────────────────────
docker run -d \
  --name vllm-mi210-deepseek-r1-tp8 \
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
  --max-model-len 2048 \
  --port 8000

echo "Waiting for server to be ready..."
until curl -sf http://localhost:8000/health; do sleep 5; done
echo "Server ready."

# ── Step 2: Run benchmark (Open Orca dataset) ─────────────────────────────────
# Download Open Orca dataset if not present
if [ ! -f "openorca.jsonl" ]; then
  echo "Downloading Open Orca sample dataset..."
  wget -q https://huggingface.co/datasets/Open-Orca/OpenOrca/resolve/main/1M-GPT4-Augmented.parquet \
    -O openorca.parquet
  python3 -c "
import pandas as pd, json
df = pd.read_parquet('openorca.parquet').head(5000)
with open('openorca.jsonl','w') as f:
    for _, r in df.iterrows():
        f.write(json.dumps({'prompt': r['system_prompt'] + ' ' + r['question'], 'completion': r['response']}) + '\n')
"
fi

for CONCURRENCY in 32 64 128; do
  echo ""
  echo "=== Concurrency $CONCURRENCY ==="
  python -m vllm.entrypoints.openai.cli_client benchmark_serving \
    --backend vllm \
    --base-url http://localhost:8000 \
    --model "$MODEL" \
    --dataset-name random \
    --random-input-len 200 \
    --random-output-len 256 \
    --num-prompts 500 \
    --request-rate "$CONCURRENCY"
done

# ── Expected results ──────────────────────────────────────────────────────────
# Concurrency  32  →  total_token_throughput ~757   tok/s  |  TTFT ~847ms
# Concurrency  64  →  total_token_throughput ~1365  tok/s  |  TTFT ~403ms
# Concurrency 128  →  total_token_throughput ~2112  tok/s  |  TTFT ~626ms

docker stop vllm-mi210-deepseek-r1-tp8 && docker rm vllm-mi210-deepseek-r1-tp8
