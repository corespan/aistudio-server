#!/usr/bin/env bash
# =============================================================================
# Generate SBOMs for the distributed workload container images
# =============================================================================
#
# WHY THIS EXISTS
#
# CoreSpan builds llminference, jupyternotebook and benchmark-client, then
# conveys them to node operators. Distributing a binary artifact is what
# triggers third-party notice and redistribution obligations — they attach to
# whoever hands the image over, which is us, not to the upstream projects that
# wrote the code inside it.
#
# Each image contains, at minimum: vLLM (Apache-2.0), PyTorch (BSD-3), Jupyter
# (BSD-3), several hundred Python wheels, the CUDA or ROCm userspace, and the
# base OS package set. Most of that is permissive and unremarkable. Two things
# are not, and are called out by name in the report this script produces:
#
#   NVIDIA CUDA runtime  Proprietary EULA, not open source. It permits
#                        redistribution of specified runtime components subject
#                        to conditions — including that you not reverse
#                        engineer, and that end users be bound by terms at least
#                        as protective. Whether our distribution model satisfies
#                        this is a question for counsel, not for a scanner. The
#                        scanner's job is to establish whether CUDA is in the
#                        image at all and which components.
#
#   Debian/Ubuntu base   GPL and LGPL system packages carry a written-offer or
#                        accompanying-source obligation (GPLv2 §3, GPLv3 §6).
#                        Canonical and Debian satisfy this for their own
#                        archives, but the offer must be passed along by
#                        downstream redistributors.
#
# Usage:
#   ./scripts/generate_sbom.sh                 # all images from catalog.json
#   ./scripts/generate_sbom.sh llminference    # one image
#
# Requires: syft (https://github.com/anchore/syft), docker, and registry
# credentials with pull access:
#   gcloud auth configure-docker us-docker.pkg.dev
#
# Output: sbom/<image>-<tag>.spdx.json  (full machine-readable inventory)
#         sbom/<image>-<tag>.txt        (human-readable licence summary)
#         sbom/REPORT.md                (roll-up, with flagged licences)
# =============================================================================

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/sbom"
CATALOG="$ROOT/catalog.json"

if ! command -v syft >/dev/null 2>&1; then
  cat >&2 <<'EOF'
ERROR: syft is not installed.

  curl -sSfL https://raw.githubusercontent.com/anchore/syft/main/install.sh \
    | sh -s -- -b /usr/local/bin

Or use the container form, which needs no install:
  docker run --rm -v /var/run/docker.sock:/var/run/docker.sock \
    anchore/syft:latest <image> -o spdx-json
EOF
  exit 1
fi

mkdir -p "$OUT"

# Licence identifiers that must not pass unreviewed. Strong copyleft in a
# distributed image is a genuine question; proprietary EULAs are a genuine
# question. Everything else is noise.
FLAG_PATTERN='GPL-2|GPL-3|AGPL|SSPL|BUSL|CC-BY-NC|Commons-Clause|NVIDIA|Proprietary|UNKNOWN|NOASSERTION'

# Read image list and tags from catalog.json so this can never describe a
# different set of images than the server actually launches.
mapfile -t IMAGES < <(python3 - "$CATALOG" <<'PY'
import json, sys

catalog = json.load(open(sys.argv[1]))

# workload_types carries the tag for the images the server launches directly.
tags = {w["name"].lower(): w["image_tag"] for w in catalog.get("workload_types", [])}

# benchmark-client is not a workload type — it is the load generator that
# llminference is benchmarked with, and the two are built and versioned as a
# pair. Everything else must resolve from workload_types or this is a
# configuration error, not something to paper over with a default.
PAIRED_WITH = {"benchmark-client": "llminference"}

rows, missing = [], []
for ref in catalog.get("images", {}).values():
    short = ref.rsplit("/", 1)[-1]
    key = PAIRED_WITH.get(short, short)
    tag = tags.get(key)
    if tag is None:
        missing.append(short)
        continue
    rows.append(f"{short}\t{ref}\t{tag}")

if missing:
    # Silently defaulting here would scan a tag that does not exist, and the
    # resulting SBOM would either fail or describe the wrong image.
    print(
        "ERROR: no tag resolvable for: %s\n"
        "Add it to workload_types in catalog.json, or map it in PAIRED_WITH "
        "in scripts/generate_sbom.sh." % ", ".join(missing),
        file=sys.stderr,
    )
    sys.exit(1)

print("\n".join(rows))
PY
) || exit 1

if [ "$#" -gt 0 ]; then
  FILTER="$1"
else
  FILTER=""
fi

SCANNED=0
for row in "${IMAGES[@]}"; do
  IFS=$'\t' read -r short ref tag <<< "$row"
  [ -n "$FILTER" ] && [ "$short" != "$FILTER" ] && continue

  image="${ref}:${tag}"
  echo "=== $image"

  # Resolve the digest. An SBOM against a mutable tag describes whatever
  # happened to be there at scan time, which is not evidence of anything.
  if digest=$(docker buildx imagetools inspect "$image" --format '{{.Manifest.Digest}}' 2>/dev/null); then
    echo "    digest: $digest"
    target="${ref}@${digest}"
  else
    echo "    WARNING: could not resolve digest — scanning mutable tag" >&2
    digest="unresolved"
    target="$image"
  fi

  syft "$target" -o "spdx-json=$OUT/${short}-${tag}.spdx.json" \
                 -o "table=$OUT/${short}-${tag}.txt" \
                 --quiet

  echo "$digest" > "$OUT/${short}-${tag}.digest"
  SCANNED=$((SCANNED + 1))
done

if [ "$SCANNED" -eq 0 ]; then
  echo "No images matched '${FILTER}'." >&2
  exit 1
fi

echo
echo "Building roll-up report ..."
python3 - "$OUT" "$FLAG_PATTERN" <<'PY'
import glob, json, os, re, sys
from collections import Counter
from datetime import date

out, flag_pattern = sys.argv[1], sys.argv[2]
flag_re = re.compile(flag_pattern, re.I)

lines = [
    "# Workload Image SBOM Report\n\n",
    f"Generated: {date.today().isoformat()}\n\n",
    "Machine-readable SPDX documents sit alongside this file. Regenerate with\n"
    "`make sbom`.\n\n",
    "## Why this report exists\n\n",
    "CoreSpan distributes these images to node operators. Third-party notice and\n"
    "redistribution obligations attach at the point of distribution, so they\n"
    "attach to us. This is the inventory of what we are handing over.\n\n",
]

any_flagged = False
for path in sorted(glob.glob(os.path.join(out, "*.spdx.json"))):
    doc = json.load(open(path))
    packages = doc.get("packages", [])
    name = os.path.basename(path).replace(".spdx.json", "")

    digest_file = os.path.join(out, name + ".digest")
    digest = open(digest_file).read().strip() if os.path.exists(digest_file) else "unknown"

    counts = Counter()
    flagged = []
    for pkg in packages:
        lic = pkg.get("licenseConcluded") or pkg.get("licenseDeclared") or "NOASSERTION"
        counts[lic] += 1
        if flag_re.search(lic) or flag_re.search(pkg.get("name", "")):
            flagged.append((pkg.get("name", "?"), pkg.get("versionInfo", "?"), lic))

    lines.append(f"## {name}\n\n")
    lines.append(f"- Digest: `{digest}`\n")
    lines.append(f"- Packages: {len(packages)}\n")
    lines.append(f"- Distinct licence strings: {len(counts)}\n\n")

    if flagged:
        any_flagged = True
        lines.append(f"### Requires review ({len(flagged)})\n\n")
        lines.append("| Package | Version | Licence |\n| --- | --- | --- |\n")
        for pkg_name, version, lic in sorted(set(flagged)):
            lines.append(f"| `{pkg_name}` | {version} | {lic} |\n")
        lines.append("\n")
    else:
        lines.append("No packages matched the review pattern.\n\n")

    lines.append("<details><summary>Licence distribution</summary>\n\n")
    lines.append("| Licence | Count |\n| --- | --- |\n")
    for lic, count in counts.most_common():
        lines.append(f"| {lic} | {count} |\n")
    lines.append("\n</details>\n\n")

lines.append("## Standing questions for counsel\n\n")
lines.append(
    "1. **NVIDIA CUDA EULA.** If an image derives from an NVIDIA base image, the\n"
    "   CUDA runtime components are governed by a proprietary EULA with conditions\n"
    "   on redistribution to third parties. Confirm our distribution model — "
    "conveying prebuilt images to external node operators — falls within the\n"
    "   permitted redistribution grant.\n\n"
    "2. **Base OS source offer.** The Debian/Ubuntu layer contains GPL packages.\n"
    "   Downstream redistributors must pass along a written offer for source.\n"
    "   Decide whether to publish one or to point at the upstream archives.\n\n"
    "3. **Strong copyleft in the Python layer.** Anything flagged GPL/AGPL above\n"
    "   needs a per-package call on whether it is linked, invoked, or merely\n"
    "   present.\n"
)

report = os.path.join(out, "REPORT.md")
open(report, "w").write("".join(lines))
print(f"wrote {report}")
if any_flagged:
    print("\nPackages requiring review were found. See sbom/REPORT.md.")
PY

echo
echo "Done. $SCANNED image(s) scanned into sbom/"
