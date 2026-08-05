# Model and Dataset Licences

**The Apache-2.0 licence on this repository covers Corespan Systems, Inc's source code only.
It does not cover model weights or datasets.**

AIStudio Server downloads and runs models published by third parties. Each carries
its own licence, and several are *gated* — the publisher requires you to request
access and accept their terms before you can download the weights. Accepting those
terms is between you and the publisher. CoreSpan is not a party to it and cannot
grant, sublicense, or transfer access on your behalf.

If you benchmark a model and publish the numbers, the model's licence may also
constrain how you attribute and name the results. The Llama Community Licences in
particular impose naming and attribution conditions on outputs and derivatives.

---

## Gated models — access token required

These models cannot be downloaded anonymously. You must request access on Hugging
Face under an account that has been approved by the publisher, then supply that
account's token.

| Model | Licence | Gate |
| --- | --- | --- |
| `meta-llama/Meta-Llama-3-8B-Instruct` | Llama 3 Community Licence | Manual approval |
| `meta-llama/Meta-Llama-3-70B-Instruct` | Llama 3 Community Licence | Manual approval |
| `meta-llama/Meta-Llama-3.1-70B-Instruct` | Llama 3.1 Community Licence | Manual approval |
| `meta-llama/Llama-3.3-70B-Instruct` | Llama 3.3 Community Licence | Manual approval |

### Supplying a token

The token lives on the **GPU node**, not on the server. The worker sources it
there and forwards it into the workload container at run time, so it is never
persisted to the database or written into a run manifest.

On each GPU node:

```bash
mkdir -p ~/.aistudio
echo "HF_TOKEN=hf_xxxxxxxxxxxxxxxxxxxx" > ~/.aistudio/env
chmod 600 ~/.aistudio/env
```

> **Do not put this in `~/.bashrc`.** Debian and Ubuntu ship a `~/.bashrc` whose
> first statement returns early for non-interactive shells:
>
> ```bash
> case $- in
>     *i*) ;;
>       *) return;;
> esac
> ```
>
> Every command this server runs over SSH is non-interactive, so an `export`
> appended to `~/.bashrc` sits below that `return` and never executes. The
> variable would be set when you log in by hand and unset for every benchmark —
> which makes the failure look like a token problem rather than a shell problem.
> `~/.aistudio/env` is sourced explicitly by the worker and has no such issue.

Verify before your first gated run:

```bash
make check-node-env NODE=<gpu-node>            # exercises the same path the worker uses
python3 scripts/check_model_access.py          # checks every model in catalog.json
```

If `HF_TOKEN` is unset, gated models will only run when their weights are already
present in the mounted cache (`MODEL_STORAGE_MODE=local` or a pre-warmed
`~/.cache/huggingface`). A fresh download will fail with a 401 from the Hub.

### What the Llama Community Licences require

Not legal advice — read the licence text linked from each model card. The
provisions that most often surprise people:

- **Attribution.** Derivative models must include "Llama" at the start of their
  name, and you must display "Built with Llama" prominently.
- **Acceptable Use Policy.** Incorporated by reference into the licence.
- **Scale threshold.** If your products had more than 700 million monthly active
  users on the licence's effective date, Meta must grant you a separate licence.
- **Redistribution.** If you pass the weights on, the licence text and a specific
  attribution notice must travel with them.

---

## Ungated models

Downloadable anonymously. Still governed by their own licences, not by ours.

| Model | Licence | Note |
| --- | --- | --- |
| `mistralai/Mistral-7B-Instruct-v0.3` | Apache-2.0 | Hub page may ask for contact details before download |
| `Qwen/Qwen2.5-7B-Instruct` | Apache-2.0 | |
| `Qwen/Qwen2.5-32B-Instruct` | Apache-2.0 | |
| `TinyLlama/TinyLlama-1.1B-Chat-v1.0` | Apache-2.0 | |
| `deepseek-ai/DeepSeek-R1-Distill-Llama-70B` | MIT (see below) | **Mixed lineage** |

### DeepSeek-R1-Distill-Llama-70B — mixed lineage

DeepSeek publishes this under MIT, but it is a distillation of
`meta-llama/Llama-3.3-70B-Instruct`. The Llama 3.3 Community Licence treats
distilled outputs as derivative works, so the Llama attribution and Acceptable
Use terms plausibly travel with the distilled weights regardless of DeepSeek's MIT
grant. The two grants are not obviously reconcilable.

**Position:** treat this model as subject to *both* MIT and the Llama 3.3
Community Licence, and apply the stricter of the two. Escalate to counsel before
publishing benchmark results that name it in marketing material.

---

## Datasets

| Dataset | Source | Licence | Status |
| --- | --- | --- | --- |
| `humaneval` | OpenAI HumanEval | MIT | Clear |
| user-supplied | specified via `dataset_path` in benchmark config | operator's responsibility | N/A |

### Dataset policy

AIStudio Server does not bundle, download, or distribute any benchmark dataset.
The operator supplies a dataset file path via the `dataset_path` field in the
benchmark configuration (UI or API). The file must already exist on the GPU node.

This means the licence of the dataset is entirely the operator's responsibility.
Common choices for vLLM throughput benchmarks:

| Dataset | Licence | Notes |
| --- | --- | --- |
| ShareGPT (any copy) | Contested — see below | Industry-standard for comparability |
| `Open-Orca/OpenOrca` | MIT | Clean licence; realistic turn lengths |
| `databricks/databricks-dolly-15k` | CC-BY-SA-3.0 | Clean; requires attribution |

**ShareGPT note:** The widely-used `anon8231489123/ShareGPT_Vicuna_unfiltered`
copy is tagged Apache-2.0 on HuggingFace, but the provenance is contested —
the transcripts are ChatGPT outputs and user messages that were not licensed for
redistribution by the original contributors. If you use ShareGPT and publish
benchmark results, state the exact source URL and version in your methodology.

---

## Re-verifying

Gate status and licences change. Re-run before every release:

```bash
python3 scripts/check_model_access.py
```

This is wired into CI as a non-blocking weekly job — see
`.github/workflows/compliance.yml`.
