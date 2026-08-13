# Models

AIStudio Server maintains a `catalog.json` at the repository root that defines which models are available to benchmark. This page explains how the catalog works and how to add new models.

---

## How the Catalog Works

`catalog.json` is the single source of truth for:
- Which models appear in the benchmark wizard UI
- The default vLLM configuration for each model (precision, concurrency, GPU count, etc.)
- Whether a model is gated (requires a HuggingFace token)
- License metadata surfaced in the UI

On startup (`make setup` or `catalog_seeder`), the server reads `catalog.json` and seeds the `workload_types` table in PostgreSQL. Model configuration is served directly from the file at runtime via `GET /api/v1/models/config`.

---

## Supported Models

### Gated models — HuggingFace token required

Access must be requested on HuggingFace and approved by the publisher. See [gpu-nodes.md](./gpu-nodes.md#huggingface-token-for-gated-models) for token setup.

| Model | Licence | Min GPU memory |
|-------|---------|----------------|
| `meta-llama/Meta-Llama-3-8B-Instruct` | Llama 3 Community | 16 GB |
| `meta-llama/Meta-Llama-3-70B-Instruct` | Llama 3 Community | 80 GB (4× GPU) |
| `meta-llama/Meta-Llama-3.1-70B-Instruct` | Llama 3.1 Community | 80 GB (4× GPU) |
| `meta-llama/Llama-3.3-70B-Instruct` | Llama 3.3 Community | 80 GB (4× GPU) |

### Ungated models — downloadable anonymously

| Model | Licence | Min GPU memory |
|-------|---------|----------------|
| `TinyLlama/TinyLlama-1.1B-Chat-v1.0` | Apache-2.0 | 4 GB |
| `mistralai/Mistral-7B-Instruct-v0.3` | Apache-2.0 | 16 GB |
| `Qwen/Qwen2.5-7B-Instruct` | Apache-2.0 | 16 GB |
| `Qwen/Qwen2.5-32B-Instruct` | Apache-2.0 | 40 GB (2× GPU) |
| `deepseek-ai/DeepSeek-R1-Distill-Llama-70B` | MIT + Llama 3.3 (see below) | 80 GB (4× GPU) |

**DeepSeek-R1-Distill-Llama-70B note:** Published under MIT but distilled from `Llama-3.3-70B-Instruct`. The Llama 3.3 Community Licence treats distilled outputs as derivative works, so Llama terms likely travel with it. Treat as subject to both MIT and Llama 3.3. Escalate to counsel before using in marketing material.

See [MODEL-LICENSES.md](../MODEL-LICENSES.md) for full per-model licence details.

---

## Adding a New Model

Open `catalog.json` and add an entry to `supported_models`:

```json
{
  "model_id": "my-model-id",
  "display_name": "My Model 7B",
  "hf_repo": "org/model-name-on-huggingface",
  "license": "Apache-2.0",
  "gated": false,
  "license_url": "https://huggingface.co/org/model-name-on-huggingface",
  "min_gpu_memory_gb": 16,
  "default_config": {
    "precision": "fp16",
    "gpu_count": 1,
    "max_model_len": 8192,
    "concurrency": 4,
    "input_tokens": 512,
    "output_tokens": 512
  }
}
```

| Field | Description |
|-------|-------------|
| `model_id` | Unique slug used internally. Lowercase, hyphens. |
| `display_name` | Human-readable name shown in the UI |
| `hf_repo` | Exact HuggingFace repo ID (used for the `docker run --model` arg) |
| `license` | SPDX licence identifier, e.g. `Apache-2.0`, `MIT` |
| `gated` | `true` if HuggingFace requires approval before downloading |
| `license_url` | Link to the model card or licence text |
| `min_gpu_memory_gb` | Minimum GPU VRAM in GB |
| `default_config` | Default benchmark parameters — shown pre-filled in the UI |

After editing `catalog.json`, re-seed the database so the new model appears in the UI:

```bash
docker compose exec api python -m app.services.catalog_seeder
```

Verify the model access is available (useful to run before attempting a benchmark):
```bash
python3 scripts/check_model_access.py
```

---

## Model Configuration

When the UI wizard reaches the "Configure" step, it calls `GET /api/v1/models/config?model=<hf_repo>`. The server returns the model's `default_config` from `catalog.json`, merged with licence metadata. The user can edit any field before starting the run.

For models not in `catalog.json`, the server falls back to these defaults:

```json
{
  "precision": "fp16",
  "concurrency": 4,
  "input_tokens": 512,
  "output_tokens": 256,
  "max_model_len": 4096,
  "tensor_parallel_size": 1,
  "pipeline_parallel_size": 1,
  "batch_size": 32,
  "dataset_path": ""
}
```

---

## Llama Community Licence — Key Obligations

Not legal advice — read the full licence text linked from each model card. Provisions that most often surprise people:

- **Attribution.** Derivative models must include "Llama" at the start of their name. Outputs must display "Built with Llama" prominently.
- **Acceptable Use Policy.** Incorporated by reference. Review before deploying in production.
- **Scale threshold.** Over 700M monthly active users requires a separate Meta licence.
- **Redistribution.** If you pass the weights on, the licence text and a specific attribution notice must travel with them.

---

## Re-verifying Gate Status and Licences

Model gate status and licence terms change without notice. Run before every release:

```bash
python3 scripts/check_model_access.py
```

This is also wired into CI as a non-blocking weekly job — see `.github/workflows/compliance.yml`.
