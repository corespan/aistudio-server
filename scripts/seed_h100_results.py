#!/usr/bin/env python3
"""
Seed real benchmark results from 2× NVIDIA H100 NVL runs on node 10.6.91.204.

Five runs:
  1. LLM  — Llama3.1-70B, concurrency=128  → 2545 tok/s
  2. LLM  — Llama3.1-70B, concurrency=256  → 2255 tok/s
  3. ResNet-50 Inference                    → 2591 samples/s
  4. ResNet-18 Inference                    → 1899 samples/s
  5. ResNet-18 Training                     →  409 samples/s (ImageNet 160GB)

Note on ResNet runs: total_token_throughput stores samples/s (not LLM tokens).
input_tokens / output_tokens / concurrency are 0 — not applicable for image workloads.
model_name distinguishes the model (resnet50, resnet18, llama3.1-70b-instruct).
inference vs training is encoded in run_id. All ResNet-specific metrics (tflops,
accuracy, power, p95, correct/incorrect counts) live in the JSONB metrics blob.

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
    print("\nSeeding 2× H100 NVL benchmark results...\n")

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
        },
        "config": {
            "concurrency": 128,
            "precision": "fp16",
            "input_tokens": 8092,
            "output_tokens": 256,
            "gpu_count": GPU_COUNT,
            "gpu_model": GPU_MODEL,
            "pipeline_version": "vllm-openai-latest",
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
        },
        "config": {
            "concurrency": 256,
            "precision": "fp16",
            "input_tokens": 8092,
            "output_tokens": 256,
            "gpu_count": GPU_COUNT,
            "gpu_model": GPU_MODEL,
            "pipeline_version": "vllm-openai-latest",
            "started_at": started_c256.isoformat(),
            "notes": "Context length 8092, 2× H100 NVL, tensor parallel 2",
        },
    })

    # ── 3. ResNet-50 Inference ────────────────────────────────────────────────
    # total_token_throughput stores samples/s (2591). input/output/concurrency = 0.
    # Dataset: images-large (50000 images), Duration: 19s
    started_r50 = datetime(2026, 1, 9, 8, 55, 23, tzinfo=timezone.utc)
    completed_r50 = started_r50 + timedelta(seconds=19)

    post("h100-resnet50-inference", {
        "run_id": "h100-resnet50-inference",
        "timestamp": completed_r50.isoformat(),
        "workload": {"name": "resnet50", "type": "cv"},
        "status": "success",
        "gpu_type": GPU_TYPE,
        "node_ip": NODE_IP,
        "metrics": {
            "total_token_throughput": 2591.0,   # samples/s
            "mean_ttft_ms": None,
            "mean_tpot_ms": None,
            "mean_e2el_ms": None,
            "tflops": 10.85,
            "accuracy": 76.15,
            "p95_latency_s": 0.19,
            "p99_latency_s": 0.19,
            "correct": 38076,
            "incorrect": 11924,
            "processed_requests": 50000,
            "dataset_size": 50000,
            "avg_gpu_power_watts": 192.07,
            "avg_gpu_utilization_percent": 7.69,
            "benchmark_tool": "Drut Workbench",
        },
        "config": {
            "concurrency": 0,
            "precision": "fp32",
            "input_tokens": 0,
            "output_tokens": 0,
            "gpu_count": GPU_COUNT,
            "gpu_model": GPU_MODEL,
            "pipeline_version": "pytorch-resnet-latest",
            "started_at": started_r50.isoformat(),
            "notes": "ResNet-50 image inference, dataset=images-large (50k)",
        },
    })

    # ── 4. ResNet-18 Inference ────────────────────────────────────────────────
    # Dataset: images-large (50000 images), Duration: 26s
    started_r18_inf = datetime(2026, 1, 9, 8, 32, 51, tzinfo=timezone.utc)
    completed_r18_inf = started_r18_inf + timedelta(seconds=26)

    post("h100-resnet18-inference", {
        "run_id": "h100-resnet18-inference",
        "timestamp": completed_r18_inf.isoformat(),
        "workload": {"name": "resnet18", "type": "cv"},
        "status": "success",
        "gpu_type": GPU_TYPE,
        "node_ip": NODE_IP,
        "metrics": {
            "total_token_throughput": 1899.0,   # samples/s
            "mean_ttft_ms": None,
            "mean_tpot_ms": None,
            "mean_e2el_ms": None,
            "tflops": 3.49,
            "accuracy": 69.76,
            "p95_latency_s": 0.41,
            "p99_latency_s": 0.41,
            "correct": 34881,
            "incorrect": 15119,
            "processed_requests": 50000,
            "dataset_size": 50000,
            "avg_gpu_power_watts": 99.23,
            "avg_gpu_utilization_percent": 6.87,
            "benchmark_tool": "Drut Workbench",
        },
        "config": {
            "concurrency": 0,
            "precision": "fp32",
            "input_tokens": 0,
            "output_tokens": 0,
            "gpu_count": GPU_COUNT,
            "gpu_model": GPU_MODEL,
            "pipeline_version": "pytorch-resnet-latest",
            "started_at": started_r18_inf.isoformat(),
            "notes": "ResNet-18 image inference, dataset=images-large (50k)",
        },
    })

    # ── 5. ResNet-18 Training ─────────────────────────────────────────────────
    # Dataset: imagenet-160gb (1281167 samples), Duration: 52m10s = 3130.646s
    # samples/s = 1281167 / 3130.646 ≈ 409
    started_r18_train = datetime(2026, 1, 9, 10, 40, 54, tzinfo=timezone.utc)
    completed_r18_train = started_r18_train + timedelta(seconds=3130.646)

    post("h100-resnet18-training", {
        "run_id": "h100-resnet18-training",
        "timestamp": completed_r18_train.isoformat(),
        "workload": {"name": "resnet18", "type": "cv"},
        "status": "success",
        "gpu_type": GPU_TYPE,
        "node_ip": NODE_IP,
        "metrics": {
            "total_token_throughput": 409.0,    # samples/s (1281167 / 3130.646)
            "mean_ttft_ms": None,
            "mean_tpot_ms": None,
            "mean_e2el_ms": None,
            "tflops": 2.24,
            "train_acc_at1_best": 12.0559,
            "train_acc_at5_best": 27.5965,
            "train_loss_latest": 4.8708,
            "train_acc_at1_latest": 12.0559,
            "train_acc_at5_latest": 27.5965,
            "epoch_time_s": 3130.646,
            "num_epochs": 1,
            "specificity": 0.999,
            "avg_gpu_power_watts": 102.01,
            "avg_gpu_utilization_percent": 49.09,
            "power_efficiency_score": 0.9845,
            "dataset_size": 1281167,
            "benchmark_tool": "Drut Workbench",
        },
        "config": {
            "concurrency": 0,
            "precision": "fp32",
            "input_tokens": 0,
            "output_tokens": 0,
            "gpu_count": GPU_COUNT,
            "gpu_model": GPU_MODEL,
            "pipeline_version": "pytorch-resnet-latest",
            "started_at": started_r18_train.isoformat(),
            "notes": "ResNet-18 training, ImageNet-160GB (1.28M samples), 1 epoch",
        },
    })


def verify() -> None:
    print("\n--- Verification ---\n")
    runs = [
        "h100-llama3-70b-c128",
        "h100-llama3-70b-c256",
        "h100-resnet50-inference",
        "h100-resnet18-inference",
        "h100-resnet18-training",
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
