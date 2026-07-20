#!/usr/bin/env python3
"""
Seed LLM benchmark results from AMD MI210 runs on node 10.6.12.11.

21 rows total:
  LLM — Llama-3.1-70B TP=4, 4 GPUs, c=4/8/16      [screenshots, full metrics]
  LLM — Llama-3.1-70B TP=8, 8 GPUs, c=4/8/16      [screenshots, full metrics]
  LLM — DeepSeek-R1-Distill-Llama-70B TP=8, c=32/64/128  [Open Orca, screenshots]
  LLM — Llama-3.1-70B TP=8, c=32/64/128            [Open Orca, screenshots]
  LLM — Llama-3.1-70B TP=8, 8 GPUs, c=4/8/16      [Benchmarks page, partial]
  LLM — Llama-3.1-70B TP=4, 4 GPUs, c=4/8/16      [Benchmarks page, partial]
  LLM — Llama-3.3-70B-Instruct TP=8, 8 GPUs, c=4/8/16  [Benchmarks page, partial]

Usage:
    docker compose exec api python scripts/seed_mi210_results.py
"""

import sys
from datetime import datetime, timedelta, timezone

try:
    import httpx
except ImportError:
    print("httpx not found — run inside the container via 'docker compose exec api ...'")
    sys.exit(1)

API_URL   = "http://localhost:8001"
NODE_IP   = "10.6.12.11"
GPU_TYPE  = "mi210"
GPU_MODEL = "AMD Instinct MI210"


def post(run_id: str, sub_run_index: int, payload: dict) -> None:
    payload["run_id"] = run_id
    payload["sub_run_index"] = sub_run_index
    resp = httpx.post(f"{API_URL}/api/v1/metrics", json=payload, timeout=10)
    mark = "OK" if resp.status_code in (200, 202) else f"FAIL({resp.status_code})"
    print(f"  [{mark}] {run_id}[{sub_run_index}]")
    if resp.status_code not in (200, 202):
        print(f"         {resp.text[:120]}")


def _ts(base: datetime, duration_s: float) -> str:
    return (base + timedelta(seconds=duration_s)).isoformat()


def _llm(model, concurrency, duration, tpt, ttft, tpot, e2el, p99, otp, rtp,
         tin, tout, gpu_count, base, notes, in_tok=1023, out_tok=1024):
    return {
        "timestamp": _ts(base, duration),
        "workload": {"name": model, "type": "llm"},
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
        },
        "config": {
            "concurrency": concurrency,
            "precision": "bf16",
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "gpu_count": gpu_count,
            "gpu_model": GPU_MODEL,
            "pipeline_version": "vllm-rocm-latest",
            "started_at": base.isoformat(),
            "notes": notes,
        },
    }


def seed() -> None:
    print("\nSeeding AMD MI210 benchmark results...\n")

    # ── Llama-3.1-70B TP=4 (4 GPUs) — screenshots, wl1060run6, 2025-07-18 ──
    base = datetime(2025, 7, 18, 10, 0, 0, tzinfo=timezone.utc)
    notes = "Llama-3.1-70B TP=4, 4× MI210, Random dataset"
    rows = [
        # sub_idx, c,  dur,     tpt,    ttft,    tpot,  e2el,      p99,     otp,    rtp,  tin,    tout
        (0,  4,  789.49, 103.71, 823.92,  76.36, 78938.53, 1282.39,  51.88, 0.05, 40920,  40960),
        (1,  8,  821.47, 199.35, 868.33,  79.43, 82127.67, 3344.99,  99.72, 0.10, 81840,  81920),
        (2, 16,  893.22, 366.51, 1250.03, 86.06, 89289.67, 6682.81, 183.43, 0.18, 163539, 163840),
    ]
    for sub, c, dur, tpt, ttft, tpot, e2el, p99, otp, rtp, tin, tout in rows:
        post("mi210-llama31-70b-tp4", sub,
             _llm("llama3.1-70b-instruct", c, dur, tpt, ttft, tpot, e2el, p99, otp, rtp, tin, tout, 4, base, notes))

    # ── Llama-3.1-70B TP=8 (8 GPUs) — screenshots, wl1062run7, 2025-07-18 ──
    base = datetime(2025, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
    notes = "Llama-3.1-70B TP=8, 8× MI210, Random dataset"
    rows = [
        (0,  4,  673.54, 121.57, 1221.06, 64.64, 67343.95, 2680.97,  60.81, 0.06, 40920,  40960),
        (1,  8,  636.81, 257.16, 1103.57, 61.15, 63644.55, 4479.08, 128.64, 0.13, 81840,  81920),
        (2, 16,  696.12, 470.29, 1687.9,  66.37, 69580.84, 8675.51, 235.36, 0.23, 163539, 163840),
    ]
    for sub, c, dur, tpt, ttft, tpot, e2el, p99, otp, rtp, tin, tout in rows:
        post("mi210-llama31-70b-tp8", sub,
             _llm("llama3.1-70b-instruct", c, dur, tpt, ttft, tpot, e2el, p99, otp, rtp, tin, tout, 8, base, notes))

    # ── DeepSeek-R1-Distill-Llama-70B TP=8 — Open Orca, wl1002run1, 2025-07-28 ──
    base = datetime(2025, 7, 28, 10, 0, 0, tzinfo=timezone.utc)
    notes = "DeepSeek-R1-Distill-Llama-70B TP=8, 8× MI210, Open Orca dataset"
    rows = [
        # sub, c,   dur,    tpt,     ttft,   tpot,   e2el,      p99,    otp,    rtp,  tin,    tout,  in_tok, out_tok
        (0,  32, 189.07,  757.77,  847.67,  70.78, 18997.61, 4543.39,  433.27, 1.69, 61353,  81920,  192, 256),
        (1,  64, 211.92, 1365.4,   403.46,  81.49, 21184.22,  812.0,   773.1,  3.02, 125522, 163840, 196, 256),
        (2, 128, 276.02, 2112.75,  626.83, 105.74, 27590.49,  914.5,  1187.17, 4.64, 255477, 327680, 200, 256),
    ]
    for sub, c, dur, tpt, ttft, tpot, e2el, p99, otp, rtp, tin, tout, in_tok, out_tok in rows:
        post("mi210-deepseek-r1-70b-tp8", sub,
             _llm("deepseek-r1-distill-llama-70b", c, dur, tpt, ttft, tpot, e2el, p99, otp, rtp,
                  tin, tout, 8, base, notes, in_tok, out_tok))

    # ── Llama-3.1-70B TP=8 — Open Orca, wl1003run5, 2025-07-28 ──
    base = datetime(2025, 7, 28, 14, 0, 0, tzinfo=timezone.utc)
    notes = "Llama-3.1-70B TP=8, 8× MI210, Open Orca dataset"
    for sub, c, dur, tpt, ttft, tpot, e2el, p99, otp, rtp, tin, tout, in_tok, out_tok in rows:
        post("mi210-llama31-70b-tp8-openorca", sub,
             _llm("llama3.1-70b-instruct", c, dur, tpt, ttft, tpot, e2el, p99, otp, rtp,
                  tin, tout, 8, base, notes, in_tok, out_tok))

    # ── Llama-3.1-70B TP=8, 8 GPUs — Benchmarks page (partial), 2025-07-18 ──
    # No e2el / p99 available from this source.
    base = datetime(2025, 7, 18, 16, 0, 0, tzinfo=timezone.utc)
    bench8 = [
        # sub, c,  dur,  tpt,    ttft,  tpot,  otp,    tin,    tout
        (0,  4, 621,    131.78, 2817,   57.98, 65.92,  40920,  40960),
        (1,  8, 640,    255.70, 2781,   59.87, 127.91, 81840,  81920),
        (2, 16, 787,    415.73, 4136,   72.91, 208.06, 163539, 163840),
    ]
    for sub, c, dur, tpt, ttft, tpot, otp, tin, tout in bench8:
        post("mi210-llama31-70b-tp8-bench", sub, {
            "timestamp": _ts(base, dur),
            "workload": {"name": "llama3.1-70b-instruct", "type": "llm"},
            "status": "success", "gpu_type": GPU_TYPE, "node_ip": NODE_IP,
            "metrics": {
                "total_token_throughput": tpt, "mean_ttft_ms": ttft,
                "mean_tpot_ms": tpot, "mean_e2el_ms": None, "p99_ttft_ms": None,
                "output_throughput": otp, "total_input_tokens": tin,
                "total_output_tokens": tout, "benchmark_tool": "vLLM benchmark",
            },
            "config": {
                "concurrency": c, "precision": "bf16", "input_tokens": 1023,
                "output_tokens": 1024, "gpu_count": 8, "gpu_model": GPU_MODEL,
                "pipeline_version": "vllm-rocm-latest", "started_at": base.isoformat(),
                "notes": "Llama-3.1-70B TP=8, 8× MI210 — Benchmarks page summary",
            },
        })

    # ── Llama-3.1-70B TP=4, 4 GPUs — Benchmarks page (partial), 2025-07-18 ──
    base = datetime(2025, 7, 18, 20, 0, 0, tzinfo=timezone.utc)
    bench4 = [
        (0,  4, 766,    106.77, 777,  74.19, 53.41,  40920,  40960),
        (1,  8, 796.51, 205.6,  814,  77.04, 102.85, 81840,  81920),
        (2, 16, 870.65, 376.01, 1173, 83.93, 188.18, 163539, 163840),
    ]
    for sub, c, dur, tpt, ttft, tpot, otp, tin, tout in bench4:
        post("mi210-llama31-70b-tp4-bench", sub, {
            "timestamp": _ts(base, dur),
            "workload": {"name": "llama3.1-70b-instruct", "type": "llm"},
            "status": "success", "gpu_type": GPU_TYPE, "node_ip": NODE_IP,
            "metrics": {
                "total_token_throughput": tpt, "mean_ttft_ms": ttft,
                "mean_tpot_ms": tpot, "mean_e2el_ms": None, "p99_ttft_ms": None,
                "output_throughput": otp, "total_input_tokens": tin,
                "total_output_tokens": tout, "benchmark_tool": "vLLM benchmark",
            },
            "config": {
                "concurrency": c, "precision": "bf16", "input_tokens": 1023,
                "output_tokens": 1024, "gpu_count": 4, "gpu_model": GPU_MODEL,
                "pipeline_version": "vllm-rocm-latest", "started_at": base.isoformat(),
                "notes": "Llama-3.1-70B TP=4, 4× MI210 — Benchmarks page summary",
            },
        })

    # ── Llama-3.3-70B-Instruct TP=8 (assumed), 8 GPUs — Benchmarks page ──
    base = datetime(2025, 7, 18, 22, 0, 0, tzinfo=timezone.utc)
    l33 = [
        (0,  4, 629.00, 130.17, 2810.49, 58.73, 65.12,  40920,  40960),
        (1,  8, 652.14, 251.11, 2838.55, 60.96, 125.62, 81840,  81920),
        (2, 16, 797.36, 410.58, 4163.85, 73.85, 205.48, 163539, 163840),
    ]
    for sub, c, dur, tpt, ttft, tpot, otp, tin, tout in l33:
        post("mi210-llama33-70b-tp8", sub, {
            "timestamp": _ts(base, dur),
            "workload": {"name": "llama3.3-70b-instruct", "type": "llm"},
            "status": "success", "gpu_type": GPU_TYPE, "node_ip": NODE_IP,
            "metrics": {
                "total_token_throughput": tpt, "mean_ttft_ms": ttft,
                "mean_tpot_ms": tpot, "mean_e2el_ms": None, "p99_ttft_ms": None,
                "output_throughput": otp, "total_input_tokens": tin,
                "total_output_tokens": tout, "benchmark_tool": "vLLM benchmark",
            },
            "config": {
                "concurrency": c, "precision": "bf16", "input_tokens": 1023,
                "output_tokens": 1024, "gpu_count": 8, "gpu_model": GPU_MODEL,
                "pipeline_version": "vllm-rocm-latest", "started_at": base.isoformat(),
                "notes": "Llama-3.3-70B-Instruct TP=8 (assumed), 8× MI210",
            },
        })



def verify() -> None:
    print("\n--- Verification ---\n")
    runs = [
        ("mi210-llama31-70b-tp4", 3),
        ("mi210-llama31-70b-tp8", 3),
        ("mi210-deepseek-r1-70b-tp8", 3),
        ("mi210-llama31-70b-tp8-openorca", 3),
        ("mi210-llama31-70b-tp8-bench", 3),
        ("mi210-llama31-70b-tp4-bench", 3),
        ("mi210-llama33-70b-tp8", 3),
    ]
    for run_id, expected in runs:
        r = httpx.get(f"{API_URL}/api/v1/benchmarks/{run_id}", timeout=10)
        if r.status_code == 200:
            n = len(r.json().get("sub_runs", []))
            print(f"  {run_id}: {n}/{expected} sub-runs")
        else:
            print(f"  {run_id}: HTTP {r.status_code}")
    print()


if __name__ == "__main__":
    seed()
    verify()
