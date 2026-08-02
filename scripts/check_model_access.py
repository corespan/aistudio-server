#!/usr/bin/env python3
"""Check gate status and licence for every model referenced by this repo.

Reads model IDs from catalog.json and benchmarks/**/*.sh, queries the Hugging Face
API for each, and reports which are gated and under what licence.

Usage:
    python3 scripts/check_model_access.py
    python3 scripts/check_model_access.py --token $HF_TOKEN   # also test access

Exit codes:
    0  all models resolved
    1  one or more models could not be resolved, or --token was supplied and
       access to a gated model was denied
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
CATALOG = ROOT / "catalog.json"
BENCHMARKS = ROOT / "benchmarks"

MODEL_RE = re.compile(r'MODEL="([^"]+)"')


def collect_models() -> dict[str, list[str]]:
    """Map model id -> list of places it is referenced."""
    found: dict[str, list[str]] = {}

    if CATALOG.exists():
        catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
        for entry in catalog.get("supported_models", []):
            repo = entry.get("hf_repo")
            if repo:
                found.setdefault(repo, []).append("catalog.json")

    for script in sorted(BENCHMARKS.rglob("*.sh")):
        for model in MODEL_RE.findall(script.read_text(encoding="utf-8")):
            found.setdefault(model, []).append(str(script.relative_to(ROOT)))

    return found


def query(model: str, token: str | None) -> dict:
    request = urllib.request.Request(f"https://huggingface.co/api/models/{model}")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        return {"_error": f"HTTP {exc.code}"}
    except (urllib.error.URLError, TimeoutError) as exc:
        return {"_error": str(exc)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--token",
        default=None,
        help="Hugging Face token; if given, gated models are checked for actual access",
    )
    args = parser.parse_args()

    models = collect_models()
    if not models:
        print("No models found in catalog.json or benchmarks/.")
        return 1

    failures = 0
    gated = []

    print(f"{'MODEL':<52} {'GATED':<10} LICENCE")
    print("-" * 90)
    for model, sources in sorted(models.items()):
        data = query(model, args.token)
        if "_error" in data:
            print(f"{model:<52} {'?':<10} lookup failed: {data['_error']}")
            failures += 1
            continue

        gate = data.get("gated")
        licence = (data.get("cardData") or {}).get("license", "unstated")
        gate_str = "no" if gate in (False, None) else str(gate)
        print(f"{model:<52} {gate_str:<10} {licence}")

        if gate not in (False, None):
            gated.append((model, sources))
            if args.token:
                # A token without granted access still resolves the model card but
                # is refused the weights. Probe a real file to test that.
                probe = urllib.request.Request(
                    f"https://huggingface.co/{model}/resolve/main/config.json"
                )
                probe.add_header("Authorization", f"Bearer {args.token}")
                try:
                    urllib.request.urlopen(probe, timeout=30).read(1)
                except urllib.error.HTTPError as exc:
                    print(f"{'':<52} -> ACCESS DENIED with supplied token (HTTP {exc.code})")
                    failures += 1
                except (urllib.error.URLError, TimeoutError) as exc:
                    print(f"{'':<52} -> access probe failed: {exc}")

    if gated:
        print("\nGated models require HF_TOKEN on the GPU node. Referenced from:")
        for model, sources in gated:
            print(f"  {model}")
            for source in dict.fromkeys(sources):
                print(f"    - {source}")
        print("\nSee MODEL-LICENSES.md.")

    if not args.token:
        print("\nRun again with --token $HF_TOKEN to verify the token actually has access.")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
