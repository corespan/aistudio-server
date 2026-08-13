"""
app/catalog.py — Model catalog for AIStudio (open-source edition).

Loads model definitions from catalog.json at startup — single source of truth.
Shared by the system router (/models/config) and results router (/models).

catalog.json carries the authoritative per-model data:
  - default vLLM config  (precision, gpu_count, max_model_len, …)
  - licence metadata     (gated, license, license_url)
  - HuggingFace repo     (hf_repo — the full path passed to vLLM --model)

_MODEL_CONFIGS  — keyed by model_id, values are the vLLM runtime config.
                  Used by /api/v1/models/config and the /models list.
_MODEL_INFO     — keyed by model_id, values are licence + access metadata.
                  Use this to surface gate warnings in the API or UI.
_DEFAULT_CONFIG — fallback for models not in catalog.json.
"""

import json
import os

_CATALOG_PATH = os.path.join(os.path.dirname(__file__), "..", "catalog.json")


def _load_catalog() -> dict:
    path = os.path.abspath(_CATALOG_PATH)
    with open(path) as f:
        return json.load(f)


def _build_model_configs(catalog: dict) -> dict:
    """
    Build the vLLM runtime config dict from catalog.json supported_models.

    Only returns keys that ManifestBuilder.build_llm_benchmark_command() /
    worker.py actually read when starting a run — precision, input_tokens,
    output_tokens, batch_size, pipeline_parallel_size, and tensor_parallel_size
    (a duplicate of gpu_count) were previously returned here but never
    forwarded to the benchmark container, so they were dropped to avoid
    the UI collecting/sending config the backend silently ignores.

    dataset_path has no catalog-level default — it's a path on the target
    GPU node's filesystem, supplied by the user per run — so it's always
    returned as "" here; the UI must require the user to fill it in before
    submitting, and /api/v1/benchmarks/start rejects a blank value.
    """
    configs = {}
    for model in catalog.get("supported_models", []):
        model_id = model["model_id"].lower()
        dc = model.get("default_config", {})
        configs[model_id] = {
            "gpu_count":            dc.get("gpu_count", 1),
            "concurrency":          dc.get("concurrency", 4),
            "max_model_len":        dc.get("max_model_len", 4096),
            "enable_optimizations": dc.get("enable_optimizations", False),
            "dataset_path":         "",
        }
    return configs


def _build_model_info(catalog: dict) -> dict:
    """
    Build the licence/access metadata dict from catalog.json supported_models.

    Keys: display_name, hf_repo, gated, license, license_url, min_gpu_memory_gb.
    Use this to surface a gate warning in the API or UI before a run starts.
    """
    info = {}
    for model in catalog.get("supported_models", []):
        model_id = model["model_id"].lower()
        info[model_id] = {
            "display_name":      model.get("display_name", model_id),
            "hf_repo":           model.get("hf_repo", ""),
            "gated":             model.get("gated", False),
            "license":           model.get("license", ""),
            "license_url":       model.get("license_url", ""),
            "min_gpu_memory_gb": model.get("min_gpu_memory_gb", 0),
        }
    return info


_catalog      = _load_catalog()
_MODEL_CONFIGS: dict = _build_model_configs(_catalog)
_MODEL_INFO:   dict  = _build_model_info(_catalog)

_DEFAULT_CONFIG: dict = {
    "gpu_count":             1,
    "concurrency":           4,
    "max_model_len":         4096,
    "enable_optimizations":  False,
    "dataset_path":          "",
}
