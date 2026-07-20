#!/usr/bin/env python3
"""
Seed real benchmark results from 4× NVIDIA GeForce RTX 5090 runs.

Two runs captured on node 10.6.5.158 with Qwen2.5-32B-Instruct:
  1. TP=4, BF16, no chunked prefill   → ~860 tok/s
  2. PP=4, FP8, chunked prefill        → ~5,345 tok/s

Usage:
    docker compose exec api python scripts/seed_rtx5090_results.py

Idempotent: safe to run multiple times (upserts on run_id + sub_run_index).
"""

import sys
from datetime import datetime, timedelta, timezone

try:
    import httpx
except ImportError:
    print("httpx not found — run this inside the container via 'docker compose exec api ...'")
    sys.exit(1)

API_URL = "http://localhost:8001"
NODE_IP  = "10.6.5.158"
GPU_TYPE = "rtx5090"
GPU_MODEL = "NVIDIA GeForce RTX 5090"
MODEL    = "qwen2.5-32b-instruct"

# ApacheBench prompt used in both runs (≈14 input tokens)
INPUT_TOKENS  = 14
OUTPUT_TOKENS = 1024


def post(run_id: str, payload: dict) -> None:
    resp = httpx.post(f"{API_URL}/api/v1/metrics", json=payload, timeout=10)
    if resp.status_code in (200, 202):
        print(f"  [OK]   {run_id}")
    else:
        print(f"  [FAIL] {run_id}  HTTP {resp.status_code}: {resp.text[:120]}")


def seed() -> None:
    print("\nSeeding 4× RTX 5090 benchmark results...\n")

    # ── Run 1: TP=4, BF16 ────────────────────────────────────────────────────
    # Source: table-1.tsv + ApacheBench output
    # ApacheBench: 200 requests, concurrency=20, Time taken=238.709s
    # Total throughput ~860 tok/s, mean E2EL 23,870ms
    started_tp4 = datetime(2026, 7, 9, 10, 0, 0, tzinfo=timezone.utc)
    completed_tp4 = started_tp4 + timedelta(seconds=238.709)

    post(
        "rtx5090-tp4-bf16-v1",
        {
            "run_id": "rtx5090-tp4-bf16-v1",
            "timestamp": completed_tp4.isoformat(),
            "workload": {"name": MODEL, "type": "llm"},
            "status": "success",
            "gpu_type": GPU_TYPE,
            "node_ip": NODE_IP,
            "metrics": {
                "total_token_throughput": 860.0,
                "mean_ttft_ms": None,
                "mean_tpot_ms": None,
                "mean_e2el_ms": 23870.0,
                "p50_e2el_ms": 21713.0,
                "p95_e2el_ms": 24521.0,
                "max_e2el_ms": 26726.0,
                "requests_per_second": 0.84,
                "total_requests": 200,
                "successful_requests": 200,
                "failed_requests": 0,
                "per_gpu_throughput_tok_s": 215.0,
                "benchmark_tool": "ApacheBench",
                "parallelism": {
                    "tensor_parallel_size": 4,
                    "pipeline_parallel_size": 1,
                },
                "chunked_prefill": False,
            },
            "config": {
                "concurrency": 20,
                "precision": "bf16",
                "input_tokens": INPUT_TOKENS,
                "output_tokens": OUTPUT_TOKENS,
                "gpu_count": 4,
                "gpu_model": GPU_MODEL,
                "pipeline_version": "vllm-openai-latest",
                "started_at": started_tp4.isoformat(),
                "notes": "Tensor Parallel 4, BF16, no chunked prefill",
            },
        },
    )

    # ── Run 2: PP=4, FP8, chunked prefill ────────────────────────────────────
    # Source: table.tsv + 4x5090_FP8_PP_Analysis.pdf
    # 200 requests, concurrency=20, duration ≈ 38.3s (200 / 5.22 req/s)
    # --enable-chunked-prefill --max-num-batched-tokens 65536
    started_pp4 = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)
    completed_pp4 = started_pp4 + timedelta(seconds=38.3)

    post(
        "rtx5090-pp4-fp8-v1",
        {
            "run_id": "rtx5090-pp4-fp8-v1",
            "timestamp": completed_pp4.isoformat(),
            "workload": {"name": MODEL, "type": "llm"},
            "status": "success",
            "gpu_type": GPU_TYPE,
            "node_ip": NODE_IP,
            "metrics": {
                "total_token_throughput": 5345.0,
                "mean_ttft_ms": None,
                "mean_tpot_ms": None,
                "mean_e2el_ms": 3830.0,
                "p50_e2el_ms": 127.0,
                "p95_e2el_ms": 7000.0,
                "max_e2el_ms": 30100.0,
                "requests_per_second": 5.22,
                "total_requests": 200,
                "successful_requests": 200,
                "failed_requests": 0,
                "per_gpu_throughput_tok_s": 1336.0,
                "benchmark_tool": "ApacheBench",
                "parallelism": {
                    "tensor_parallel_size": 1,
                    "pipeline_parallel_size": 4,
                },
                "chunked_prefill": True,
                "max_num_batched_tokens": 65536,
            },
            "config": {
                "concurrency": 20,
                "precision": "fp8",
                "input_tokens": INPUT_TOKENS,
                "output_tokens": OUTPUT_TOKENS,
                "gpu_count": 4,
                "gpu_model": GPU_MODEL,
                "pipeline_version": "vllm-openai-latest",
                "started_at": started_pp4.isoformat(),
                "notes": "Pipeline Parallel 4, FP8, chunked prefill, max_num_batched_tokens=65536",
            },
        },
    )


def verify() -> None:
    print("\n--- Verification ---\n")
    for run_id in ("rtx5090-tp4-bf16-v1", "rtx5090-pp4-fp8-v1"):
        r = httpx.get(f"{API_URL}/api/v1/benchmarks/{run_id}", timeout=10)
        if r.status_code == 200:
            d = r.json()
            sub = d.get("sub_runs", [{}])[0]
            print(
                f"  {run_id}: {sub.get('total_token_throughput', '?')} tok/s  "
                f"| e2el={sub.get('mean_e2el_ms', '?')}ms  "
                f"| status={sub.get('status', '?')}"
            )
        else:
            print(f"  {run_id}: HTTP {r.status_code}")
    print()


if __name__ == "__main__":
    seed()
    verify()
