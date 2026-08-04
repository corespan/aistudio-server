#!/usr/bin/env python3
"""
Seed real benchmark results from 2× NVIDIA H100 NVL runs on node 10.6.91.204.

Two runs:
  1. LLM  — Llama3.1-70B, concurrency=128  → 2545 tok/s
  2. LLM  — Llama3.1-70B, concurrency=256  → 2255 tok/s

Usage:
    docker compose exec api python scripts/seed_h100_results.py

Idempotent — safe to run multiple times.
"""

import sys
from datetime import datetime, timedelta, timezone

try:
    import httpx
except ImportError:
    print("httpx not found — run inside the container via 'docker compose exec api ...'")
    sys.exit(1)

API_URL   = "http://localhost:8001"
NODE_IP   = "10.6.91.204"
GPU_TYPE  = "h100"
GPU_MODEL = "NVIDIA H100 NVL"
GPU_COUNT = 2


def post(run_id: str, payload: dict) -> None:
    resp = httpx.post(f"{API_URL}/api/v1/metrics", json=payload, timeout=10)
    mark = "OK" if resp.status_code in (200, 202) else f"FAIL({resp.status_code})"
    print(f"  [{mark}] {run_id}")
    if resp.status_code not in (200, 202):
        print(f"         {resp.text[:120]}")


def seed() -> None:
    print("\nSeeding 2× H100 NVL LLM benchmark results...\n")

    # ── 1. LLM — Llama3.1-70B, concurrency=128 ───────────────────────────────
    # Context length: 8092 tokens. ~256 output tokens/req (327680 total / ~1276 reqs).
    # Full vLLM metrics available: TTFT, TPOT, E2EL.
    started_c128 = datetime(2026, 1, 15, 9, 0, 0, tzinfo=timezone.utc)
    completed_c128 = started_c128 + timedelta(seconds=243.93)

    post("h100-llama3-70b-c128", {
        "run_id": "h100-llama3-70b-c128",
        "timestamp": completed_c128.isoformat(),
        "workload": {"name": "llama3.1-70b-instruct", "type": "llm"},
        "status": "success",
        "gpu_type": GPU_TYPE,
        "node_ip": NODE_IP,
        "metrics": {
            "total_token_throughput": 2545.07,
            "mean_ttft_ms": 4084.57,
            "mean_tpot_ms": 79.55,
            "mean_e2el_ms": 24370.31,
            "p99_ttft_ms": 9346.7,
            "output_throughput": 1343.35,
            "request_throughput": 5.23,
            "total_input_tokens": 293134,
            "total_output_tokens": 327680,
            "avg_gpu_power_watts": 664.76,
            "gpu_power_efficiency_score": 3.7251,
            "avg_gpu_utilization_percent": 97.3,
            "benchmark_tool": "vLLM benchmark",
            "server_name": "PRU",
            "parallelism": {"tensor_parallel_size": 2, "pipeline_parallel_size": 1},
        },
        "config": {
            "concurrency": 128,
            "precision": "fp16",
            "input_tokens": 8092,
            "output_tokens": 256,
            "gpu_count": GPU_COUNT,
            "gpu_model": GPU_MODEL,
            "pipeline_version": "vllm-openai:v0.14.1",
            "started_at": started_c128.isoformat(),
            "notes": "Context length 8092, 2× H100 NVL, tensor parallel 2",
        },
    })

    # ── 2. LLM — Llama3.1-70B, concurrency=256 ───────────────────────────────
    # ~256 output tokens/req (655360 total / ~2559 reqs).
    started_c256 = datetime(2026, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
    completed_c256 = started_c256 + timedelta(seconds=557.46)

    post("h100-llama3-70b-c256", {
        "run_id": "h100-llama3-70b-c256",
        "timestamp": completed_c256.isoformat(),
        "workload": {"name": "llama3.1-70b-instruct", "type": "llm"},
        "status": "success",
        "gpu_type": GPU_TYPE,
        "node_ip": NODE_IP,
        "metrics": {
            "total_token_throughput": 2255.98,
            "mean_ttft_ms": 11429.78,
            "mean_tpot_ms": 171.17,
            "mean_e2el_ms": 55079.05,
            "p99_ttft_ms": 28714.76,
            "output_throughput": 1175.63,
            "request_throughput": 4.59,
            "total_input_tokens": 602246,
            "total_output_tokens": 655360,
            "avg_gpu_power_watts": 631.09,
            "gpu_power_efficiency_score": 3.4595,
            "avg_gpu_utilization_percent": 96.78,
            "benchmark_tool": "vLLM benchmark",
            "server_name": "PRU",
            "parallelism": {"tensor_parallel_size": 2, "pipeline_parallel_size": 1},
        },
        "config": {
            "concurrency": 256,
            "precision": "fp16",
            "input_tokens": 8092,
            "output_tokens": 256,
            "gpu_count": GPU_COUNT,
            "gpu_model": GPU_MODEL,
            "pipeline_version": "vllm-openai:v0.14.1",
            "started_at": started_c256.isoformat(),
            "notes": "Context length 8092, 2× H100 NVL, tensor parallel 2",
        },
    })



def verify() -> None:
    print("\n--- Verification ---\n")
    runs = [
        "h100-llama3-70b-c128",
        "h100-llama3-70b-c256",
    ]
    for run_id in runs:
        r = httpx.get(f"{API_URL}/api/v1/benchmarks/{run_id}", timeout=10)
        if r.status_code == 200:
            sub = r.json().get("sub_runs", [{}])[0]
            print(
                f"  {run_id}: throughput={sub.get('total_token_throughput')}  "
                f"status={sub.get('status')}"
            )
        else:
            print(f"  {run_id}: HTTP {r.status_code}")
    print()


if __name__ == "__main__":
    seed()
    verify()
