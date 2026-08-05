#!/usr/bin/env python3
"""
Seed TinyLlama-1.1B benchmark results from 2× NVIDIA T4 GPU node 10.6.12.23.

6 rows total:
  TinyLlama-1.1B-Chat, TP=1 (1 GPU), concurrencies 4/8/16
  TinyLlama-1.1B-Chat, TP=2 (2 GPUs), concurrencies 4/8/16

Usage:
    docker compose exec api python scripts/seed_t4_results.py
"""

import sys
from datetime import datetime, timedelta, timezone

try:
    import httpx
except ImportError:
    print("httpx not found — run inside the container via 'docker compose exec api ...'")
    sys.exit(1)

API_URL     = "http://localhost:8001"
NODE_IP     = "10.6.12.23"
GPU_TYPE    = "t4"
GPU_MODEL   = "NVIDIA T4"
SERVER_NAME = "Dell Inc."


def post(run_id: str, sub_run_index: int, payload: dict) -> None:
    payload["run_id"] = run_id
    payload["sub_run_index"] = sub_run_index
    resp = httpx.post(f"{API_URL}/api/v1/metrics", json=payload, timeout=10)
    mark = "OK" if resp.status_code in (200, 202) else f"FAIL({resp.status_code})"
    print(f"  [{mark}] {run_id}[{sub_run_index}]")
    if resp.status_code not in (200, 202):
        print(f"         {resp.text[:120]}")


def seed() -> None:
    print("\nSeeding T4 TinyLlama benchmark results...\n")

    # 1-GPU and 2-GPU runs kept on separate days so the leaderboard shows
    # distinct entries rather than two runs on the same date.
    base_tp1 = datetime(2025, 10, 3, 10, 0, 0, tzinfo=timezone.utc)
    base_tp2 = datetime(2025, 10, 12, 10, 0, 0, tzinfo=timezone.utc)

    # sub_idx, c,  duration, tpt,     ttft,    tpot,  e2el,      p99,     otp,    rtp,  tin,    tout
    TP1 = [
        (0,  4, 144.81, 565.41,  356.9,   13.81, 14479.89,  493.3,  282.84, 0.28, 40920,  40960),
        (1,  8, 173.56, 941.61,  619.37,  16.36, 17353.15,  960.65, 471.99, 0.46, 81509,  81920),
        (2, 16, 221.4,  1477.74, 1055.53, 20.61, 22134.7,  1806.69, 740.01, 0.72, 163336, 163840),
    ]
    TP2 = [
        (0,  4, 118.71, 689.74,  325.07, 11.29, 11869.65,  415.97, 345.04, 0.34, 40920,  40960),
        (1,  8, 140.7,  1161.52, 532.2,  13.23, 14067.75,  799.98, 582.22, 0.57, 81509,  81920),
        (2, 16, 182.34, 1794.32, 924.08, 16.92, 18228.2,  1559.83, 898.54, 0.88, 163336, 163840),
    ]

    for tp, gpu_count, base, data in [(1, 1, base_tp1, TP1), (2, 2, base_tp2, TP2)]:
        run_id = f"t4-tinyllama-1b-tp{tp}"
        for sub, c, dur, tpt, ttft, tpot, e2el, p99, otp, rtp, tin, tout in data:
            post(run_id, sub, {
                "timestamp": (base + timedelta(seconds=dur)).isoformat(),
                "workload": {"name": "tinyllama-1.1b-chat", "type": "llm"},
                "status": "success",
                "gpu_type": GPU_TYPE,
                "node_ip": NODE_IP,
                "metrics": {
                    "total_token_throughput": tpt,
                    "mean_ttft_ms": ttft,
                    "mean_tpot_ms": tpot,
                    "mean_e2el_ms": e2el,
                    "p99_ttft_ms": p99,
                    "output_throughput": otp,
                    "request_throughput": rtp,
                    "total_input_tokens": tin,
                    "total_output_tokens": tout,
                    "benchmark_tool": "vLLM benchmark",
                    "server_name": SERVER_NAME,
                    "parallelism": {
                        "tensor_parallel_size": tp,
                        "pipeline_parallel_size": 1,
                    },
                },
                "config": {
                    "concurrency": c,
                    "precision": "fp16",
                    "input_tokens": 1023,
                    "output_tokens": 1024,
                    "gpu_count": gpu_count,
                    "gpu_model": GPU_MODEL,
                    "pipeline_version": "vllm-openai:v0.14.1",
                    "started_at": base.isoformat(),
                    "notes": f"TinyLlama-1.1B-Chat TP={tp}, {gpu_count}× T4",
                },
            })


def verify() -> None:
    print("\n--- Verification ---\n")
    for tp in (1, 2):
        run_id = f"t4-tinyllama-1b-tp{tp}"
        r = httpx.get(f"{API_URL}/api/v1/benchmarks/{run_id}", timeout=10)
        if r.status_code == 200:
            n = len(r.json().get("sub_runs", []))
            print(f"  {run_id}: {n}/3 sub-runs")
        else:
            print(f"  {run_id}: HTTP {r.status_code}")
    print()


if __name__ == "__main__":
    seed()
    verify()
