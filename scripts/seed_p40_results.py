#!/usr/bin/env python3
"""
Seed NVIDIA P40 ResNet50 training results.

2 rows:
  Single node — 2× P40, 15 epochs ImageNet, 2240.37s → ~8,577 samples/s
  Multi  node — 2+2 P40 (4 GPUs total), 15 epochs, 1491.42s → ~12,889 samples/s

total_token_throughput = (1,281,167 × 15 epochs) / total_seconds

Note: node IPs not recorded for these runs — stored as "unknown".

Usage:
    docker compose exec api python scripts/seed_p40_results.py
"""

import sys
from datetime import datetime, timedelta, timezone

try:
    import httpx
except ImportError:
    print("httpx not found — run inside the container via 'docker compose exec api ...'")
    sys.exit(1)

API_URL   = "http://localhost:8001"
GPU_TYPE  = "p40"
GPU_MODEL = "NVIDIA Tesla P40"

IMAGENET_IMAGES  = 1_281_167
EPOCHS           = 15
TOTAL_IMAGES     = IMAGENET_IMAGES * EPOCHS   # 19,217,505


def post(run_id: str, payload: dict) -> None:
    payload["run_id"] = run_id
    payload["sub_run_index"] = 0
    resp = httpx.post(f"{API_URL}/api/v1/metrics", json=payload, timeout=10)
    mark = "OK" if resp.status_code in (200, 202) else f"FAIL({resp.status_code})"
    print(f"  [{mark}] {run_id}")
    if resp.status_code not in (200, 202):
        print(f"         {resp.text[:120]}")


def seed() -> None:
    print("\nSeeding P40 ResNet50 training results...\n")

    # ── Single node — 2× P40 ──────────────────────────────────────────────────
    duration_1node = 2240.37
    started_1node  = datetime(2025, 6, 1, 9, 0, 0, tzinfo=timezone.utc)
    tpt_1node = round(TOTAL_IMAGES / duration_1node, 1)   # 8576.7

    post("p40-resnet50-training-1node", {
        "timestamp": (started_1node + timedelta(seconds=duration_1node)).isoformat(),
        "workload": {"name": "resnet50", "type": "cv"},
        "status": "success",
        "gpu_type": GPU_TYPE,
        "node_ip": "unknown",
        "metrics": {
            "total_token_throughput": tpt_1node,   # samples/s
            "mean_ttft_ms": None,
            "mean_tpot_ms": None,
            "mean_e2el_ms": None,
            "num_epochs": EPOCHS,
            "dataset_size": IMAGENET_IMAGES,
            "epoch_time_s": round(duration_1node / EPOCHS, 2),
            "avg_gpu_utilization_gpu0_pct": 86.39,
            "avg_gpu_utilization_gpu1_pct": 83.81,
            "benchmark_tool": "PyTorch distributed training",
        },
        "config": {
            "concurrency": 0,
            "precision": "fp32",
            "input_tokens": 0,
            "output_tokens": 0,
            "gpu_count": 2,
            "gpu_model": GPU_MODEL,
            "pipeline_version": "pytorch-resnet-2.4.2",
            "started_at": started_1node.isoformat(),
            "notes": "ResNet50 training, single node 2× P40, 15 epochs ImageNet",
        },
    })

    # ── Multi-node — 2+2 P40 (4 GPUs total) ──────────────────────────────────
    duration_2node = 1491.42
    started_2node  = datetime(2025, 6, 2, 9, 0, 0, tzinfo=timezone.utc)
    tpt_2node = round(TOTAL_IMAGES / duration_2node, 1)   # 12888.7

    post("p40-resnet50-training-2node", {
        "timestamp": (started_2node + timedelta(seconds=duration_2node)).isoformat(),
        "workload": {"name": "resnet50", "type": "cv"},
        "status": "success",
        "gpu_type": GPU_TYPE,
        "node_ip": "unknown",
        "metrics": {
            "total_token_throughput": tpt_2node,
            "mean_ttft_ms": None,
            "mean_tpot_ms": None,
            "mean_e2el_ms": None,
            "num_epochs": EPOCHS,
            "dataset_size": IMAGENET_IMAGES,
            "epoch_time_s": round(duration_2node / EPOCHS, 2),
            "node0_gpu0_utilization_pct": 77.89,
            "node0_gpu1_utilization_pct": 79.944,
            "node1_gpu0_utilization_pct": 62.22,
            "node1_gpu1_utilization_pct": 62.59,
            "benchmark_tool": "PyTorch distributed training",
        },
        "config": {
            "concurrency": 0,
            "precision": "fp32",
            "input_tokens": 0,
            "output_tokens": 0,
            "gpu_count": 4,
            "gpu_model": GPU_MODEL,
            "pipeline_version": "pytorch-resnet-2.4.2",
            "started_at": started_2node.isoformat(),
            "notes": "ResNet50 training, multi-node 2+2 P40 (4 GPUs), 15 epochs ImageNet",
        },
    })


def verify() -> None:
    print("\n--- Verification ---\n")
    for run_id in ("p40-resnet50-training-1node", "p40-resnet50-training-2node"):
        r = httpx.get(f"{API_URL}/api/v1/benchmarks/{run_id}", timeout=10)
        if r.status_code == 200:
            sub = r.json().get("sub_runs", [{}])[0]
            print(f"  {run_id}: throughput={sub.get('total_token_throughput')} status={sub.get('status')}")
        else:
            print(f"  {run_id}: HTTP {r.status_code}")
    print()


if __name__ == "__main__":
    seed()
    verify()
