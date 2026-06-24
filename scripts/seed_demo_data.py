#!/usr/bin/env python3
"""
Seed the database with realistic demo benchmark results.

Usage:
    docker compose exec api python scripts/seed_demo_data.py
"""

import json
import random
import sys
from datetime import datetime, timedelta

try:
    import httpx
    API_URL = "http://localhost:8001"
except ImportError:
    print("httpx not found. Run: docker compose exec api python scripts/seed_demo_data.py")
    sys.exit(1)


def seed_workload_types():
    try:
        from app.services.catalog_seeder import seed_catalog
        seed_catalog()
        print("[OK] Workload types seeded")
    except Exception as e:
        print("[SKIP] %s" % e)


def post_metric(run_id, model, gpu_type, gpu_count, concurrency, precision,
                input_tokens, output_tokens, node_ip, throughput, ttft, tpot, e2el,
                status="success", extra_metrics=None):
    now = datetime.utcnow()
    started = now - timedelta(seconds=random.randint(60, 300))

    payload = {
        "run_id": run_id,
        "timestamp": now.isoformat() + "Z",
        "workload": {"name": model, "type": "llm"},
        "metrics": {
            "total_token_throughput": throughput,
            "mean_ttft_ms": ttft,
            "mean_tpot_ms": tpot,
            "mean_e2el_ms": e2el,
            "p50_ttft_ms": round(ttft * 0.85, 1),
            "p99_ttft_ms": round(ttft * 2.1, 1),
            "p50_tpot_ms": round(tpot * 0.9, 1),
            "p99_tpot_ms": round(tpot * 1.8, 1),
            "successful_requests": concurrency * 10,
            "failed_requests": 0 if status == "success" else concurrency,
            **(extra_metrics or {}),
        },
        "status": status,
        "gpu_type": gpu_type,
        "node_ip": node_ip,
        "config": {
            "concurrency": concurrency,
            "precision": precision,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "gpu_count": gpu_count,
            "gpu_model": "NVIDIA %s-80GB" % gpu_type.upper(),
            "pipeline_version": "vllm-0.5.0",
            "started_at": started.isoformat() + "Z",
        },
    }

    resp = httpx.post("%s/api/v1/metrics" % API_URL, json=payload, timeout=10)
    mark = "OK" if resp.status_code in (200, 202) else "FAIL(%d)" % resp.status_code
    print("  [%s] %s | %s on %s c=%d: %.0f tok/s" % (mark, run_id, model, gpu_type, concurrency, throughput))


def seed_demo_results():
    print("\nSeeding 12 demo benchmark results...\n")

    # Llama3-8B on A100 — scaling concurrency
    post_metric("demo-llama3-a100-c1",  "llama3-8b-instruct", "a100", 1, 1,  "fp16", 512, 512, "10.0.0.5", 285.3,  42.1, 14.2, 920.5)
    post_metric("demo-llama3-a100-c4",  "llama3-8b-instruct", "a100", 1, 4,  "fp16", 512, 512, "10.0.0.5", 1120.7, 48.3, 15.1, 980.2)
    post_metric("demo-llama3-a100-c8",  "llama3-8b-instruct", "a100", 1, 8,  "fp16", 512, 512, "10.0.0.5", 1850.2, 55.7, 16.8, 1050.3)
    post_metric("demo-llama3-a100-c16", "llama3-8b-instruct", "a100", 1, 16, "fp16", 512, 512, "10.0.0.5", 2400.1, 72.4, 19.2, 1320.8)

    # Llama3-8B on H100
    post_metric("demo-llama3-h100-c4", "llama3-8b-instruct", "h100", 1, 4, "fp16", 512, 512, "10.0.0.6", 1680.5, 28.1, 9.4,  650.2)
    post_metric("demo-llama3-h100-c8", "llama3-8b-instruct", "h100", 1, 8, "fp16", 512, 512, "10.0.0.6", 2850.3, 32.5, 10.8, 720.1)

    # Mistral-7B on A100
    post_metric("demo-mistral-a100-c4", "mistral-7b-instruct", "a100", 1, 4, "fp16", 512, 512, "10.0.0.5", 1250.8, 38.9, 12.6, 880.4)
    post_metric("demo-mistral-a100-c8", "mistral-7b-instruct", "a100", 1, 8, "fp16", 512, 512, "10.0.0.5", 2100.4, 45.2, 14.1, 950.7)

    # INT4 quantized
    post_metric("demo-llama3-a100-int4", "llama3-8b-instruct", "a100", 1, 8, "int4", 512, 512, "10.0.0.5", 3200.6, 35.2, 8.9, 620.3)

    # T4 budget GPU
    post_metric("demo-llama3-t4-c1", "llama3-8b-instruct", "t4", 1, 1, "int4", 256, 256, "10.0.0.7", 85.2, 120.5, 45.8, 2800.1)

    # Multi-GPU
    post_metric("demo-llama3-2gpu", "llama3-8b-instruct", "a100", 2, 8, "fp16", 1024, 1024, "10.0.0.5", 3800.9, 40.1, 11.2, 780.5)

    # Failed run
    post_metric("demo-fail", "llama3-8b-instruct", "a100", 1, 32, "fp16", 2048, 2048, "10.0.0.5", 0, 0, 0, 0,
                status="failed", extra_metrics={"error": "CUDA OOM at concurrency=32"})


def verify():
    print("\n--- Verification ---\n")

    r = httpx.get("%s/api/v1/summary" % API_URL, timeout=10).json()
    print("Summary: %d runs, %.0f%% success, avg %.0f tok/s" % (r["total_runs"], r["success_rate"], r["avg_throughput"]))

    r = httpx.get("%s/api/v1/models" % API_URL, timeout=10).json()
    print("Models:  %s" % r)

    r = httpx.get("%s/api/v1/gpu-types" % API_URL, timeout=10).json()
    print("GPUs:    %s" % r)

    # Verify JSONB storage
    r = httpx.get("%s/api/v1/benchmarks/demo-llama3-a100-c8" % API_URL, timeout=10).json()
    if r.get("sub_runs"):
        m = r["sub_runs"][0].get("metrics", {})
        print("JSONB:   %d keys stored (p99_ttft=%.1f, p99_tpot=%.1f)" % (len(m), m.get("p99_ttft_ms", 0), m.get("p99_tpot_ms", 0)))

    print("\nDone! API: http://localhost:8002/docs")


if __name__ == "__main__":
    seed_workload_types()
    seed_demo_results()
    verify()
