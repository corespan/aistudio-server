# Model and Dataset Licences

**The Apache-2.0 licence on this repository covers CoreSpan AI's source code only.
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
| `sharegpt` | `gs://aistudio-datasets/sharegpt.json` | **Unknown** | ⚠️ Unresolved |

### sharegpt.json — unresolved provenance

The ShareGPT corpora in circulation are user-submitted transcripts of ChatGPT
conversations, scraped from the sharegpt.com site. There is no single authoritative
release and no consistent licence across the copies that exist. The widely-mirrored
`anon8231489123/ShareGPT_Vicuna_unfiltered` is tagged Apache-2.0 on the Hub, but
the uploader was not in a position to grant that: the underlying transcripts are
model outputs subject to the originating provider's terms, contributed by users who
did not license them for redistribution.

This cannot be cleared by inspecting the file. It requires knowing which copy was
downloaded and from where.

**Required action before the next release** — one of:

1. **Trace it.** Identify the exact upstream source of the copy in
   `gs://aistudio-datasets/`, record it in this file, and assess that source's
   terms. Document the provenance chain in the bucket alongside the file.
2. **Replace it.** Swap in a dataset with a clean licence. For sampling prompt and
   response length distributions in a throughput benchmark, any corpus with
   realistic turn lengths works — the specific content is not load-bearing.
   Candidates: `Open-Orca/OpenOrca` (MIT), `databricks/databricks-dolly-15k`
   (CC-BY-SA-3.0), or a synthetic generator seeded from the target length
   distribution.
3. **Drop it.** Remove the `sharegpt` entry from `catalog.json` and the
   `benchmarks/` scripts.

Option 2 is the cheapest path to a defensible position and removes a dependency on
a third-party bucket at the same time. Option 1 is only worth attempting if
benchmark comparability against previously published ShareGPT numbers matters.

**Note on comparability:** ShareGPT is the conventional prompt source for vLLM
throughput benchmarks, so switching datasets makes new numbers non-comparable to
previously published ones and to third-party results. If published numbers already
exist, version them and state the dataset explicitly.

---

## Re-verifying

Gate status and licences change. Re-run before every release:

```bash
python3 scripts/check_model_access.py
```

This is wired into CI as a non-blocking weekly job — see
`.github/workflows/compliance.yml`.
