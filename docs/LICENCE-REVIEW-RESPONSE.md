# Response to the licence review of 31 July 2026

**Repo:** `corespan/aistudio-server`
**Review date:** 31 July 2026 · 10 findings · 2 blocker, 2 high, 4 medium, 2 low
**Response date:** 2 August 2026
**Author:** ManojDev

---

## Summary

Every finding in the review is accepted. Nothing was disputed on the facts — all
ten reproduce against the repository.

Seven are fixed in code. Two need a decision from product before they can be
closed. One is external and needs a trademark search.

| | Count | Findings |
| --- | --- | --- |
| ✅ Fixed | 7 | 1, 2, 4, 5, 7, 8, 9 |
| 🟡 Partially fixed — needs input | 2 | 3, 6 |
| 🔵 Open — needs a decision outside engineering | 1 | 10 |

The two blockers are addressed differently. Finding 1 is closed outright.
Finding 6 — the largest exposure — now has the tooling and the process, but the
scan itself needs registry credentials and the CUDA question needs counsel. That
is the one item that genuinely cannot be closed from a keyboard.

**Recommended sequencing:** ship findings 1, 4, 5, 7, 8, 9 immediately — they are
done and carry no product risk. Cut a corrected release (finding 2) the same
week. Run the image scan (finding 6) as soon as registry credentials are
available; treat the CUDA EULA answer as the gate on the next customer image
handover. Findings 3 and 10 are product decisions on their own clock.

---

## The two facts the review left open

The review flagged two open questions. Both are now answered, and both change a
severity.

**Do we distribute prebuilt images?** Yes. We build the workload images locally
and publish them to GCP with public access, and we plan to open-source the code
that builds them. This means notice and redistribution obligations attach to
CoreSpan for those images. Finding 8's conditional escalation is triggered:
treat it as a blocker for anything we ship prebuilt, not just as a repo hygiene
item.

**Are the workload images actually public?** They are intended to be, and the
plan is to open-source their build code. The review's probe on 31 July returned
`UNAUTHORIZED`, so either the public binding was applied after the review, or it
is not applied to every tag. **This needs verifying before we make the claim in
the README** — see finding 3 below. The README has been updated to state that
the images are public and to tell users to report a failed anonymous pull as a
bug, which is only honest if the binding actually holds.

---

## Finding-by-finding

### 1. LICENSE file isn't the Apache-2.0 licence — ✅ Fixed

*Blocker · Licence*

**Verdict:** Accepted, reproduced. The file was 740 bytes of source-file header
boilerplate. Sections 3 (patent grant) and 5 (contributions) were absent.

**Fix:** Replaced with the full 11,344-byte Apache-2.0 text, copyright line
filled in at the appendix. GitHub's licence detection and enterprise scanners
(FOSSA, Black Duck, Snyk) will now classify it `Apache-2.0` instead of
`NOASSERTION`.

**Preventing recurrence:** `make compliance` and CI both fail if `LICENSE` drops
below 10 KB or is missing any of sections 2, 3, 5, 6, or the terms terminator.
On pushes to `master`, CI additionally queries the GitHub licence API and fails
if it does not return `Apache-2.0` — this catches the exact failure mode the
review found, which is that the file can look right to a human and still fail
machine detection.

**Why this one mattered most commercially:** it was silently failing corporate
procurement review. A prospect's licence scanner rejects the repo before anyone
at CoreSpan hears about it.

---

### 2. Release is empty, circular, and unverifiable — ✅ Fixed

*High · Supply chain*

**Verdict:** Accepted. The v1.0.0 asset was 1,340 bytes containing only the two
install scripts. It contained no source, so the v1.0.0 artifact did not contain
v1.0.0. The tag was lightweight and unsigned; `install.sh` checked out a mutable
tag name; no checksums were published.

**Fix:**

- `install.sh` and `install.ps1` now pin a **commit SHA**, and verify `HEAD`
  matches it before building anything. A mismatch aborts the install.
- Both scripts check the release tag's signature when one is present and report
  the result.
- [`RELEASE.md`](../RELEASE.md) documents the corrected process: annotated
  signed tags, no hand-built tarball, GitHub's auto-generated archives plus a
  signed `SHA256SUMS`.

**Still to do (process, not code):** generate a CoreSpan signing key, publish the
public key, and re-cut v1.0.0 following `RELEASE.md`. The existing release should
be edited to remove the stub tarball.

---

### 3. "Open source" label vs. private runtime gate — 🟡 Needs verification

*Medium · Framing*

**Verdict:** Accepted as stated at the time of review. **Direction chosen:** open
the workload images rather than soften the wording.

This is the right call. Reframing would have been cheaper but would have left us
describing a project nobody outside CoreSpan can run, which is the underlying
problem the finding names. Making the images genuinely pullable resolves the
framing question by making the framing true.

**Fix so far:** the README's Workload Images section now documents anonymous
access, gives users a `docker pull` command to verify it themselves, and tells
them to file a bug if the pull returns `UNAUTHORIZED`. It also demotes
`~/gcr.json` from a blanket node requirement to something only needed for private
or pre-release tags.

**Blocking item — please confirm before this ships:**

```bash
# Must succeed with no gcloud auth, in a clean shell:
docker logout us-docker.pkg.dev
docker pull us-docker.pkg.dev/aimlworkbench/workbench-registry/services/workloads/llminference:2.3.1-nvidia
docker pull us-docker.pkg.dev/aimlworkbench/workbench-registry/services/workloads/jupyternotebook:2.0.0-nvidia
docker pull us-docker.pkg.dev/aimlworkbench/workbench-registry/services/workloads/benchmark-client:2.3.1-nvidia
```

The review's probe returned 403 on 31 July. If any of these still fails, grant
`roles/artifactregistry.reader` to `allUsers` on the repository, or the README
claim is worse than the original framing — an unverifiable promise rather than
an overstated one.

**Also worth doing, given the plan to open-source the image build code:** publish
the Dockerfiles in this repo or a sibling repo. Public images plus public build
recipes closes the finding completely and removes the need for anyone to trust
our registry.

---

### 4. No NOTICE file — ✅ Fixed

*Low · Convention*

**Verdict:** Accepted. Optional under Apache-2.0, conventional, cheap.

**Fix:** Added [`NOTICE`](../NOTICE) with the copyright line, a pointer to the
third-party inventory, the three reciprocal dependencies named explicitly, a
model-licensing disclaimer, and a trademark statement. CI fails if it is deleted.

---

### 5. No disclaimer that AI models carry their own licences — ✅ Fixed

*High · Licence*

**Verdict:** Accepted. Two of four catalogued models are gated behind Meta's
manual approval, the benchmark scripts add two more, and there was no `HF_TOKEN`
path anywhere in the codebase — so gated models could only ever have worked from
a pre-warmed cache.

**Fix:**

- [`MODEL-LICENSES.md`](../MODEL-LICENSES.md) — per-model licences, gate status,
  how to request access, and a plain statement that our Apache grant does not
  cover model weights. Includes the Llama Community Licence provisions that
  actually bite: the "Built with Llama" attribution requirement, the naming rule
  for derivatives, the Acceptable Use Policy, and the 700M MAU threshold.
- `catalog.json` now carries `license`, `gated` and `license_url` per model, so
  the API can surface it and the UI can warn before a run.
- **`HF_TOKEN` is now plumbed through.** `manifest_builder.py` forwards it from
  the GPU node's environment into the workload container. Deliberately from the
  *node*, not the server: the credential never transits the API and is never
  written into a run manifest. If unset, the flag collapses and behaviour is
  unchanged.

  The token is read from `~/.aistudio/env` on the node, **not** `~/.bashrc`. An
  independent review of this change caught that the obvious instruction —
  append `export HF_TOKEN=...` to `~/.bashrc` — silently does not work.
  Debian and Ubuntu's stock `~/.bashrc` returns early for non-interactive
  shells, and every command the worker runs over SSH is non-interactive. The
  variable would be set when an operator logs in by hand and unset for every
  benchmark, which is about the worst possible way to discover a problem. The
  dedicated file is sourced explicitly. `make check-node-env NODE=<host>`
  exercises the same non-interactive path the worker uses, so the check cannot
  pass while the real thing fails.
- `scripts/check_model_access.py` reports gate status and licence for every model
  in `catalog.json` and `benchmarks/`, and with `--token` verifies the token
  actually has access rather than just resolving the model card. Wired into CI as
  a weekly non-blocking job, because gate status changes without warning.

**Unresolved and needs a decision — `sharegpt.json`.** Its provenance cannot be
cleared by inspecting the file. The ShareGPT corpora in circulation are scraped
ChatGPT transcripts; the widely-mirrored copy is tagged Apache-2.0 by an uploader
who was not in a position to grant it.

Three options, in `MODEL-LICENSES.md`. **Recommendation: replace it.** For
sampling prompt and response length distributions in a throughput benchmark the
specific content is not load-bearing — `Open-Orca/OpenOrca` (MIT) or
`databricks/databricks-dolly-15k` (CC-BY-SA-3.0) would do the same job with a
clean licence, and it drops a dependency on a third-party bucket.

The cost is real and product should weigh it: ShareGPT is the conventional prompt
source for vLLM throughput benchmarks, so switching makes new numbers
non-comparable to previously published ones and to third-party results. If we
have published ShareGPT-based numbers, version them and state the dataset
explicitly rather than quietly re-baselining.

---

### 6. Workload images have no third-party licence inventory — 🟡 Tooling ready, scan pending

*Blocker · Licence · largest exposure*

**Verdict:** Accepted, and the answer to the open question makes it worse than
stated. We do distribute these images, so the obligations attach to CoreSpan.

**Fix so far:** `scripts/generate_sbom.sh` scans each image from `catalog.json`
with syft, resolves the immutable digest first (an SBOM against a mutable tag is
not evidence of anything), and emits SPDX JSON plus a roll-up
`sbom/REPORT.md` that flags GPL/AGPL/proprietary/NOASSERTION entries by name.
`make sbom` runs it. A CI job exists but is disabled pending workload-identity
federation credentials.

**What cannot be done from a keyboard, and who needs to do it:**

1. **Run the scan.** Needs registry pull credentials. Engineering, ~1 day once
   credentials exist. The review's estimate of an afternoon is about right.
2. **Read the NVIDIA CUDA EULA.** If our images derive from an NVIDIA base, the
   CUDA runtime components are proprietary and their redistribution terms govern
   handing the image to third parties. Whether "publicly pullable from our
   registry" falls inside NVIDIA's redistribution grant is a legal question, not
   a scanner question. **Counsel, and it should start now** — it is the long pole
   and it gates the open-sourcing plan in finding 3.
3. **Decide on the base OS source offer.** The Debian/Ubuntu layer carries GPL
   packages with a written-offer obligation that downstream redistributors must
   pass along. Either publish an offer or point at the upstream archives.
   Engineering + counsel, low effort once decided.

**Note the interaction with finding 3.** Making the images public increases
distribution, which makes the CUDA question more pressing rather than less. The
two decisions should be taken together, not sequentially.

---

### 7. Dependency versions aren't pinned — ✅ Fixed

*Medium · Supply chain*

**Verdict:** Accepted. Every entry used `>=`, and the benchmark scripts pinned
`:latest`.

**Fix:**

- `requirements.in` holds the intended ranges; `requirements.txt` is now the
  fully pinned resolution (52 packages); `requirements.lock.txt` adds hashes.
  `make deps-lock` regenerates both.
- `benchmarks/versions.env` centralises container versions, supports digest pins,
  and every benchmark script sources it. Each run now prints its own provenance —
  image, host, date, git SHA — and warns loudly when pinned by mutable tag rather
  than digest, so nobody publishes an unreproducible number without seeing it.
- CI fails on any unpinned requirement, and recompiles `requirements.in` to check
  `requirements.txt` has not drifted.

**Left as a TODO for the team:** the actual digests in `benchmarks/versions.env`
need resolving with `docker buildx imagetools inspect`. I pinned tags and left
the digest fields empty with the warning path active, rather than inventing
digests I could not verify.

**Worth flagging to product:** the review calls the reproducibility angle a "side
effect", but for a benchmarking product it is arguably the more serious half. We
publish performance numbers. If we cannot say which vLLM build produced them, the
numbers are not defensible against a competitor who can.

---

### 8. No inventory of the repo's open-source libraries — ✅ Fixed

*Medium → blocker · Licence*

**Verdict:** Accepted, and escalated to blocker by the answer above — we do ship
prebuilt artifacts, so LGPL §4 and MPL §3 obligations do attach.

**Fix:** [`THIRD-PARTY-NOTICES.md`](../THIRD-PARTY-NOTICES.md), generated by
`scripts/generate_third_party_notices.py` from the pinned set and verified fresh
by CI. Confirms the review's three findings and adds a per-package compliance
position:

| Package | Licence | Position |
| --- | --- | --- |
| `paramiko` 5.0.0 | LGPL-2.1 | Unmodified, dynamically imported. Ship licence text + pinned version. |
| `psycopg2-binary` 2.9.12 | LGPL-3.0-or-later (OpenSSL exception) | Same analysis. Note the wheel also embeds libpq and OpenSSL. |
| `certifi` 2026.7.22 | MPL-2.0 | File-level copyleft, unmodified. Do not patch the CA bundle without re-reading §3.3. |

The other 49 packages are MIT/BSD/Apache/PSF with no surviving obligations.

---

### 9. Fonts load from Google's servers — ✅ Fixed

*Medium · Privacy*

**Verdict:** Accepted, and there was a second instance the review did not name:
`demo-ui/index.html` also loaded Chart.js from jsdelivr. Same two problems.

**Fix:** Both self-hosted. `scripts/vendor_frontend_assets.sh` pulls Inter,
JetBrains Mono and Chart.js from npm, copies their licence texts alongside,
generates the `@font-face` CSS, and writes `demo-ui/vendor/NOTICE`. Latin subset
and only the weights the page uses — 460 KB total. The OFL-1.1 notice obligation
the review anticipated is handled by the same script that creates it.

**The 460 KB of binaries are gitignored, not committed.** They are fetched once
at setup time — `make setup` runs the vendoring step, as do both installers.
This keeps the git history clean at the cost of one network round trip during
installation.

Worth being precise about what that does and does not solve, because it is a
weaker position than committing the files:

- *GDPR:* fully resolved. The disclosure was visitor IPs going to Google on
  every page load. Fetching from npm once, on the operator's own machine, at
  install time, discloses nothing about visitors.
- *Air-gap:* resolved for the GPU nodes, which is where isolation actually
  bites — they never serve the dashboard. Not resolved if the **server** host is
  itself air-gapped, in which case setup cannot fetch and `demo-ui/vendor/` has
  to be copied across from a connected machine. The script says so when it
  fails.

The licence texts, the generated CSS and the NOTICE **are** committed. Those are
compliance artifacts; they have to be present in the repository itself rather
than produced by a build step someone might skip. Only the binaries are ignored.

Degradation is deliberate and quiet where it can be: every `font-family`
declaration already falls back to a system stack, so a missing `.woff2` costs
appearance and nothing else. A missing Chart.js cannot degrade — the page shows
a banner naming the command to fix it, rather than throwing a bare
`ReferenceError`.

CI fails on any third-party origin in `demo-ui/`, verifies the committed licence
texts match a clean regeneration, and checks that every font `fonts.css`
references is actually produced by the declared versions — so a weight can never
ship as a CSS rule with no file behind it.

**The air-gap half deserves more weight than "medium" suggests.** GPU clusters
are the target deployment and are routinely isolated. A CDN reference there is
not a slow load, it is a dashboard that renders unstyled with no charts. This was
a functional bug in the primary deployment environment that happened to also be a
GDPR problem.

---

### 10. "AI Studio" name collision — 🔵 Open, needs a product decision

*Low · Branding*

**Verdict:** Accepted. Apache-2.0 §6 grants no trademark rights, and the name
collides with both Google AI Studio and Azure AI Studio.

**No code fix.** The README now carries a trademark statement, and `NOTICE`
records that CoreSpan marks are ours and others' are theirs. That is the limit of
what engineering can do.

**For product to weigh:**

- Both collisions are with hyperscalers in the *same* product category — AI
  tooling. That is the fact pattern trademark examiners and opposing counsel care
  about, and it is worse than a collision in an unrelated field.
- Neither Google nor Microsoft appears to hold a registration on the bare phrase
  "AI Studio", which is likely descriptive and hard to register. That cuts both
  ways: it is why we probably will not be sued, and also why we cannot protect
  the name ourselves.
- The practical cost is not litigation, it is search. "AI Studio benchmarking"
  competes against two of the largest marketing budgets in the industry.
- The cost of renaming rises with every customer, doc page and published
  benchmark. Cheapest it will ever be is now.

Suggested next step: a clearance search before the name sticks. Low urgency
legally, higher urgency commercially.

---

## What CI now enforces

Each of these exists because something in the review got past a human review
once. The point is that it cannot happen quietly a second time.

| Check | Catches |
| --- | --- |
| LICENSE size + required sections | Finding 1 recurring |
| GitHub licence API returns `Apache-2.0` | Finding 1's actual failure mode — machine detection, not human reading |
| `NOTICE`, `THIRD-PARTY-NOTICES.md`, `MODEL-LICENSES.md`, `vendor/NOTICE` present | Findings 4, 5, 8, 9 |
| No `>=` in `requirements.txt` | Finding 7 |
| `requirements.txt` matches a fresh compile of `requirements.in` | Silent drift |
| `THIRD-PARTY-NOTICES.md` matches the pinned set | Inventory going stale |
| No third-party origin in `demo-ui/` | Finding 9 |
| Vendored assets match a clean regeneration | Tampering or version drift |
| Model gate status (weekly, non-blocking) | Finding 5 changing upstream |
| Workload image SBOMs (disabled — needs credentials) | Finding 6 |

---

## Verification pass

Every change was re-reviewed independently against the original findings before
this document was finalised. That pass found two real bugs in the remediation
itself, both now fixed:

- **`HF_TOKEN` via `~/.bashrc` could never have worked** — the fix for finding 5
  would have shipped looking correct and failing in production. Detail under
  finding 5 above.
- **`generate_sbom.sh` resolved the wrong tag for `benchmark-client`.** It is not
  a `workload_type`, so it fell through to a default and would have scanned a
  tag that does not exist. The script now maps it explicitly to its paired
  `llminference` version and hard-fails on any image it cannot resolve, rather
  than defaulting.

Four smaller issues were also corrected:

- The "no unpinned dependency" guard in both `make compliance` and CI used a
  package-name character class that did not account for extras, so
  `uvicorn[standard]>=0.30.0` — a line that exists verbatim in
  `requirements.in`, and therefore the most likely thing to be pasted in by
  mistake — would have passed. Rewritten and tested against that exact case.
- The font vendoring pinned floating majors (`@fontsource/inter@5`), which would
  have made the CI reproducibility check fail on unrelated PRs whenever
  upstream published a 5.x. Now exact.
- The install-time integrity check was tautological: verifying `HEAD == $COMMIT`
  after `git checkout $COMMIT` proves nothing, since git resolves commits by
  content address. Both installers now check whether the **tag** still points at
  the pinned commit, which is the actual attack a mutable tag permits and the
  one thing git will not warn about.
- The `HF_TOKEN` docker flag interpolated the secret into a shell string, which
  word-splits on a token containing whitespace. Switched to docker's passthrough
  form (`-e HF_TOKEN` with no value), which reads from the client environment —
  no quoting hazard, and the secret no longer appears in the command text at
  all.

---

## Open items

| # | Item | Owner | Blocks |
| --- | --- | --- | --- |
| 6 | Read the NVIDIA CUDA EULA redistribution terms | Counsel | Open-sourcing the images; any customer image handover |
| 6 | Run `make sbom` against all three images | Engineering | Needs registry credentials |
| 6 | Decide on the Debian/Ubuntu GPL source offer | Counsel + Eng | — |
| 3 | Verify anonymous `docker pull` succeeds on all three images | Engineering | The README claim shipping |
| 3 | Publish the image build Dockerfiles | Engineering + Product | Closing finding 3 completely |
| 5 | Decide: trace, replace, or drop `sharegpt.json` | Product + Eng | Publishing benchmark numbers that use it |
| 2 | Create signing key, re-cut v1.0.0 per `RELEASE.md` | Engineering | — |
| 2 | Re-pin `COMMIT` in both installers to the full post-remediation SHA | Engineering | Installers currently point at the pre-fix tree |
| 7 | Resolve and pin container digests in `versions.env` | Engineering | Reproducible published numbers |
| 10 | Trademark clearance search on "AI Studio" | Product + Counsel | — |

### Known limitations, deliberately left

- **The new `catalog.json` licence keys are inert.** `license`, `gated` and
  `license_url` are written but nothing reads them — `/models` is served from a
  separate hardcoded `_MODEL_CONFIGS` dict in `app/catalog.py`. Model licence
  disclosure is therefore documentation-only right now. Surfacing a gate warning
  in the API and UI before a run starts is the natural follow-up, and would turn
  finding 5's fix from a document into a guardrail. Left out of this change to
  keep it reviewable.
- **Container digests in `benchmarks/versions.env` are empty.** Tags are pinned
  and every run prints a visible warning that it is not digest-pinned, but the
  digests themselves need resolving against the registry. I did not invent
  values I could not verify.
- **The installer `COMMIT` is a 7-char abbreviation of the pre-remediation
  commit.** It has to be, until these changes are committed. `RELEASE.md` step 3
  covers updating it, and it must be done before anyone runs the one-line
  installer.

---

## Files added or changed

**Added**

```
NOTICE                                    Copyright, attribution, model + trademark disclaimers
THIRD-PARTY-NOTICES.md                    52-package inventory, 3 reciprocal licences analysed
MODEL-LICENSES.md                         Per-model terms, gate status, sharegpt provenance
RELEASE.md                                Corrected release process
docs/LICENCE-REVIEW-RESPONSE.md           This document
requirements.in                           Intended version ranges
requirements.lock.txt                     Hashed lockfile
benchmarks/versions.env                   Central container pins + run provenance
demo-ui/vendor/                           Self-hosted fonts + Chart.js + licences (460 KB)
scripts/generate_third_party_notices.py   Inventory generator, with --check for CI
scripts/generate_sbom.sh                  Workload image SBOMs via syft
scripts/check_model_access.py             Model gate + licence reporter
scripts/vendor_frontend_assets.sh         Frontend asset vendoring
.github/workflows/compliance.yml          CI enforcement
```

**Changed**

```
LICENSE                    740 B boilerplate → 11,344 B full Apache-2.0
requirements.txt           12 ranges → 52 pinned packages
install.sh, install.ps1    Tag checkout → SHA pin + integrity verification
benchmarks/*/*.sh          :latest → central pins + provenance banner (10 files)
demo-ui/index.html         Google Fonts + jsdelivr → local vendor/
catalog.json               Added per-model licence/gated/url; dataset licence status
app/services/manifest_builder.py   HF_TOKEN forwarding for gated models
.env.example               HF_TOKEN documentation
README.md                  Licensing section, image access, framing corrections
Makefile                   compliance, deps-lock, third-party, vendor-assets, sbom, check-models
.gitignore                 node_modules, IDE dirs; note that sbom/ is committed
```

---

## A note on the review itself

It is accurate. Every claim reproduces, and the "check it yourself" commands work
as written. The severity ordering is right with one caveat: finding 9's air-gap
consequence is understated at medium, since it breaks the dashboard outright in
the deployment environment we actually target.

Worth passing back: the reviewer explicitly scoped findings 6 and 8 to what is
visible from outside, and noted that an internal audit would not be visible to
them. If any image licence work has already been done internally, it should be
published — it would close most of finding 6 immediately.
