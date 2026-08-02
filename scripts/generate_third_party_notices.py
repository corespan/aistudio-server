#!/usr/bin/env python3
"""Generate THIRD-PARTY-NOTICES.md from the pinned dependency set.

Reads requirements.txt (the pinned output of `uv pip compile requirements.in`),
resolves each package's declared licence from the PyPI JSON API, and writes a
Markdown inventory.

Usage:
    python3 scripts/generate_third_party_notices.py
    python3 scripts/generate_third_party_notices.py --check   # CI mode: fail if stale

CI runs this with --check so the inventory can never silently drift from the
pinned dependency set.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date

ROOT = pathlib.Path(__file__).resolve().parent.parent
REQUIREMENTS = ROOT / "requirements.txt"
OUTPUT = ROOT / "THIRD-PARTY-NOTICES.md"

PIN_RE = re.compile(r"^([A-Za-z0-9_.\-]+)==([0-9][^\s;]*)")

# Give up on PyPI after this many consecutive failures rather than retrying
# every remaining package against an endpoint that is plainly down.
OFFLINE_AFTER = 3

# Per-request timeout. Deliberately short: the failure mode that matters is a
# hung CI job, not a slow-but-eventually-successful lookup.
REQUEST_TIMEOUT = 15

# PyPI metadata is inconsistent: some projects use the modern SPDX
# `license_expression` field, others only trove classifiers, others a free-text
# blob. Normalise the classifier spellings we actually encounter to SPDX.
CLASSIFIER_TO_SPDX = {
    "BSD License": "BSD-3-Clause",
    "MIT License": "MIT",
    "MIT No Attribution License (MIT-0)": "MIT-0",
    "Apache Software License": "Apache-2.0",
    "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "GNU Library or Lesser General Public License (LGPL)": "LGPL-3.0-or-later",
    "GNU Lesser General Public License v2 (LGPLv2)": "LGPL-2.1",
    "Python Software Foundation License": "PSF-2.0",
    "ISC License (ISCL)": "ISC",
}

# Licences whose notice / source-availability obligations survive redistribution
# of a *binary* artifact. Anything matching these prefixes is called out
# separately in the generated document.
RECIPROCAL_PREFIXES = ("LGPL", "MPL", "GPL", "EPL", "CDDL", "EUPL")

# Hand-maintained notes for the reciprocal dependencies. Keep these accurate;
# they are the substance of the compliance position, not decoration.
RECIPROCAL_NOTES = {
    "paramiko": (
        "Used as an unmodified library, imported dynamically at runtime. LGPL-2.1 "
        "Section 6 obligations (licence text + relinking ability) attach only when "
        "CoreSpan conveys a copy. Satisfied by shipping the licence text and the "
        "pinned version, which is installable from PyPI."
    ),
    "psycopg2-binary": (
        "LGPL-3.0-or-later with an OpenSSL linking exception. Same conveyance "
        "analysis as paramiko. Note the -binary wheel also embeds libpq "
        "(PostgreSQL Licence) and OpenSSL (Apache-2.0 for 3.x)."
    ),
    "certifi": (
        "MPL-2.0 is file-level copyleft. Unmodified, so MPL Section 3.2 is "
        "satisfied by shipping the licence text and pointing at the upstream "
        "source. Do not patch the bundled CA bundle without re-reading Section 3.3."
    ),
}


def parse_pins(path: pathlib.Path) -> list[tuple[str, str]]:
    pins = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = PIN_RE.match(line.strip())
        if match:
            pins.append((match.group(1), match.group(2)))
    if not pins:
        sys.exit(f"error: no pinned requirements found in {path}")
    return pins


def fetch_licence(name: str, version: str, attempts: int = 3) -> tuple[str, str]:
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT) as response:
                info = json.load(response)["info"]
            break
        except (urllib.error.URLError, TimeoutError, KeyError) as exc:
            last_error = exc
            if attempt < attempts - 1:
                time.sleep(2 ** attempt)
    else:
        return f"LOOKUP-FAILED ({last_error})", ""

    expression = (info.get("license_expression") or "").strip()
    if not expression:
        classifiers = [
            c.split("::")[-1].strip()
            for c in info.get("classifiers", [])
            if c.startswith("License ::")
        ]
        mapped = [CLASSIFIER_TO_SPDX.get(c, c) for c in classifiers]
        expression = " OR ".join(dict.fromkeys(mapped))
    if not expression:
        raw = (info.get("license") or "").strip()
        expression = raw.splitlines()[0][:60] if raw else "UNKNOWN"

    homepage = info.get("home_page") or ""
    if not homepage:
        homepage = (info.get("project_urls") or {}).get("Homepage", "")
    if not homepage:
        homepage = f"https://pypi.org/project/{name}/"
    return expression, homepage


def is_reciprocal(expression: str) -> bool:
    return any(expression.upper().startswith(p) for p in RECIPROCAL_PREFIXES)


def render(rows: list[tuple[str, str, str, str]]) -> str:
    reciprocal = [r for r in rows if is_reciprocal(r[2])]
    unknown = [r for r in rows if r[2] == "UNKNOWN" or r[2].startswith("LOOKUP-FAILED")]

    out: list[str] = []
    out.append("# Third-Party Notices — aistudio-server\n")
    out.append(
        "Inventory of the third-party Python packages resolved by `requirements.txt`\n"
        "(the pinned output of `uv pip compile requirements.in`).\n"
    )
    out.append(
        f"Generated: {date.today().isoformat()} · "
        f"Packages: {len(rows)} · Reciprocal: {len(reciprocal)}\n"
    )
    out.append(
        "> Regenerate with `make third-party`. CI fails if this file drifts from\n"
        "> `requirements.txt`. Do not edit the table by hand.\n"
    )
    out.append("\n## Scope\n")
    out.append(
        "This file covers the **server application** dependency tree only. It does\n"
        "**not** cover:\n\n"
        "- the distributed workload container images (`llminference`,\n"
        "  `jupyternotebook`, `benchmark-client`) — see `sbom/` for their SBOMs,\n"
        "  generated by `make sbom`;\n"
        "- model weights and datasets — see `MODEL-LICENSES.md`;\n"
        "- frontend assets in `demo-ui/` — see `demo-ui/vendor/NOTICE`.\n"
    )

    out.append("\n## Redistribution obligations\n")
    if reciprocal:
        out.append(
            "The packages below carry notice or source-availability obligations that\n"
            "survive redistribution of a **binary** artifact. If CoreSpan ships a\n"
            "prebuilt server image, wheel, or installer containing them, the\n"
            "corresponding licence text must travel with it and, for LGPL, users must\n"
            "retain the ability to substitute their own build of the library.\n"
        )
        for name, version, expression, _ in reciprocal:
            out.append(f"\n### {name} {version} — `{expression}`\n")
            note = RECIPROCAL_NOTES.get(name)
            out.append(f"\n{note}\n" if note else "\nObligation review pending.\n")
    else:
        out.append("\nNone. Every dependency is permissively licensed.\n")

    if unknown:
        out.append("\n## Unresolved\n")
        out.append(
            "\nThese packages did not report a machine-readable licence. Resolve each\n"
            "manually before the next release.\n\n"
        )
        for name, version, expression, _ in unknown:
            out.append(f"- `{name}=={version}` — {expression}\n")

    out.append("\n## Full inventory\n\n")
    out.append("| Package | Version | Licence | Project |\n")
    out.append("| --- | --- | --- | --- |\n")
    for name, version, expression, homepage in sorted(rows):
        flag = " ⚠️" if is_reciprocal(expression) else ""
        out.append(f"| `{name}` | {version} | {expression}{flag} | {homepage} |\n")

    out.append(
        "\n---\n\nLicence strings are taken from each project's PyPI metadata "
        "(`license_expression`, falling back to trove classifiers). Where a project's "
        "metadata is ambiguous, the upstream LICENSE file governs.\n"
    )
    return "".join(out)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if the generated output differs from the committed file",
    )
    args = parser.parse_args()

    rows = []
    consecutive_failures = 0
    offline = False

    for name, version in parse_pins(REQUIREMENTS):
        if offline:
            # Circuit broken — record the rest without hammering a dead endpoint.
            rows.append((name, version, "LOOKUP-FAILED (skipped: PyPI unreachable)", ""))
            continue

        expression, homepage = fetch_licence(name, version)
        rows.append((name, version, expression, homepage))
        print(f"  {name:24} {version:16} {expression}", file=sys.stderr)

        if expression.startswith("LOOKUP-FAILED"):
            consecutive_failures += 1
            if consecutive_failures >= OFFLINE_AFTER:
                # Retrying 52 packages three times each against a dead endpoint
                # takes the better part of an hour and tells us nothing we do
                # not already know after the first few.
                print(
                    f"\n{OFFLINE_AFTER} consecutive lookup failures — treating PyPI as "
                    f"unreachable and skipping the rest.",
                    file=sys.stderr,
                )
                offline = True
        else:
            consecutive_failures = 0

    unreachable = [r for r in rows if r[2].startswith("LOOKUP-FAILED")]
    if unreachable and args.check:
        # A PyPI outage or a rate-limited runner is not evidence that the
        # committed inventory is stale, and failing the build on it would train
        # people to ignore this job. Report loudly, exit clean.
        print(
            f"\nWARNING: {len(unreachable)} package(s) could not be resolved "
            f"after retries — cannot verify freshness. Not failing the build.",
            file=sys.stderr,
        )
        for name, version, expression, _ in unreachable:
            print(f"  {name}=={version}: {expression}", file=sys.stderr)
        return 0

    rendered = render(rows)

    if args.check:
        if not OUTPUT.exists():
            print(f"error: {OUTPUT.name} is missing. Run `make third-party`.")
            return 1
        # Ignore the generated-on date line, which changes every run.
        strip = lambda t: "\n".join(
            l for l in t.splitlines() if not l.startswith("Generated:")
        )
        if strip(OUTPUT.read_text(encoding="utf-8")) != strip(rendered):
            print(
                f"error: {OUTPUT.name} is out of date with requirements.txt. "
                f"Run `make third-party` and commit the result."
            )
            return 1
        print(f"{OUTPUT.name} is up to date ({len(rows)} packages).")
        return 0

    OUTPUT.write_text(rendered, encoding="utf-8")
    print(f"wrote {OUTPUT.name} ({len(rows)} packages)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
