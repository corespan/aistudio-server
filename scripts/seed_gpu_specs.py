#!/usr/bin/env python3
"""
Seed the gpu_specs table with the hardware catalog.

Tier ranking (1 = most powerful):
  1  H100 NVL          — datacenter flagship, Hopper
  2  RTX 5090          — consumer flagship, Blackwell
  3  A100 SXM4-80GB    — datacenter, Ampere
  4  MI300X            — AMD datacenter flagship, CDNA3
  5  MI210             — AMD workstation, CDNA2
  6  T4                — inference-optimised, Turing
  7  P100              — older datacenter, Pascal
  8  P40               — older workstation, Pascal

Idempotent — ON CONFLICT DO UPDATE on gpu_type slug.

Usage:
    docker compose exec api python scripts/seed_gpu_specs.py
"""

import sys

try:
    import httpx
except ImportError:
    print("httpx not found — run inside the container: docker compose exec api ...")
    sys.exit(1)

API_URL = "http://localhost:8001"

GPU_SPECS = [
    {
        "gpu_type":    "h100",
        "display_name": "NVIDIA H100 NVL",
        "vendor":      "nvidia",
        "arch":        "hopper",
        "vram_gb":     94,
        "tdp_watts":   400,
        "tier_rank":   1,
        "fp16_tflops": 1979.0,
        "fp8_tflops":  3958.0,
    },
    {
        "gpu_type":    "rtx5090",
        "display_name": "NVIDIA GeForce RTX 5090",
        "vendor":      "nvidia",
        "arch":        "blackwell",
        "vram_gb":     32,
        "tdp_watts":   575,
        "tier_rank":   2,
        "fp16_tflops": 838.0,
        "fp8_tflops":  1676.0,
    },
    {
        "gpu_type":    "a100",
        "display_name": "NVIDIA A100 SXM4-80GB",
        "vendor":      "nvidia",
        "arch":        "ampere",
        "vram_gb":     80,
        "tdp_watts":   400,
        "tier_rank":   3,
        "fp16_tflops": 312.0,
        "fp8_tflops":  None,
    },
    {
        "gpu_type":    "mi300x",
        "display_name": "AMD Instinct MI300X",
        "vendor":      "amd",
        "arch":        "cdna3",
        "vram_gb":     192,
        "tdp_watts":   750,
        "tier_rank":   4,
        "fp16_tflops": 1307.0,
        "fp8_tflops":  2614.0,
    },
    {
        "gpu_type":    "mi210",
        "display_name": "AMD Instinct MI210",
        "vendor":      "amd",
        "arch":        "cdna2",
        "vram_gb":     64,
        "tdp_watts":   300,
        "tier_rank":   5,
        "fp16_tflops": 181.0,
        "fp8_tflops":  None,
    },
    {
        "gpu_type":    "t4",
        "display_name": "NVIDIA Tesla T4",
        "vendor":      "nvidia",
        "arch":        "turing",
        "vram_gb":     16,
        "tdp_watts":   70,
        "tier_rank":   6,
        "fp16_tflops": 65.0,
        "fp8_tflops":  None,
    },
    {
        "gpu_type":    "p100",
        "display_name": "NVIDIA Tesla P100 SXM2",
        "vendor":      "nvidia",
        "arch":        "pascal",
        "vram_gb":     16,
        "tdp_watts":   300,
        "tier_rank":   7,
        "fp16_tflops": 18.7,
        "fp8_tflops":  None,
    },
    {
        "gpu_type":    "p40",
        "display_name": "NVIDIA Tesla P40",
        "vendor":      "nvidia",
        "arch":        "pascal",
        "vram_gb":     24,
        "tdp_watts":   250,
        "tier_rank":   8,
        "fp16_tflops": None,
        "fp8_tflops":  None,
    },
]


def seed() -> None:
    print("\nSeeding GPU specs catalog...\n")
    ok = True
    for spec in GPU_SPECS:
        resp = httpx.post(f"{API_URL}/api/v1/gpu-specs", json=spec, timeout=10)
        mark = "OK" if resp.status_code in (200, 201) else f"FAIL({resp.status_code})"
        if resp.status_code not in (200, 201):
            ok = False
        print(f"  [{mark}] tier={spec['tier_rank']}  {spec['gpu_type']:12s}  {spec['display_name']}")
        if resp.status_code not in (200, 201):
            print(f"         {resp.text[:120]}")
    print()
    return ok


def verify() -> None:
    print("--- Verification (ordered by tier) ---\n")
    r = httpx.get(f"{API_URL}/api/v1/gpu-specs", timeout=10)
    if r.status_code == 200:
        for s in r.json():
            fp16 = f"{s['fp16_tflops']} TF" if s['fp16_tflops'] else "—"
            print(f"  tier={s['tier_rank']}  {s['gpu_type']:12s}  {s['vram_gb']:4d}GB  {fp16:12s}  {s['display_name']}")
    else:
        print(f"  HTTP {r.status_code}: {r.text[:120]}")
    print()


if __name__ == "__main__":
    seed()
    verify()
