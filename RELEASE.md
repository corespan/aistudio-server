# Release process

## What was wrong with v1.0.0

The `ai-studio-server-1.0.0-1-opensource` release shipped a 1,340-byte tarball
containing only `install.sh` and `install.ps1` — byte-identical to the copies
already in the repo. It contained no source. The scripts fetch the code from git
at run time, so **the v1.0.0 artifact did not contain v1.0.0**. Nothing was
archived: if the repository disappeared, the release would install nothing.

GitHub already attaches complete `.tar.gz` and `.zip` source archives to every
tagged release, for free, on the same page. Those do contain LICENSE and every
file. The manual upload was strictly worse than the thing sitting next to it.

Compounding it, the install chain had no integrity verification at any step:

- the tag was lightweight (`type=commit`) and unsigned,
- `install.sh` checked out a **tag name**, which is mutable — anyone with write
  access can repoint it and every subsequent install silently gets different
  code,
- no checksum file was published, so nothing downstream could detect the change.

## What changed

- `install.sh` and `install.ps1` pin a **commit SHA**, not a tag name. A SHA
  cannot be repointed.
- Tags are **annotated and GPG-signed**.
- Release assets are limited to GitHub's auto-generated source archives plus a
  signed `SHA256SUMS`.
- The install scripts verify the checkout resolves to the expected SHA before
  running anything.

---

## Cutting a release

### 1. Pre-flight

```bash
make compliance          # licence files, pinned deps, third-party inventory
make sbom                # workload image inventory — needs registry credentials
python3 scripts/check_model_access.py
```

All three must be clean. `make sbom` output goes into `sbom/` and is committed
with the release, so there is a record of what the images contained at that
version.

### 2. Tag — annotated and signed

```bash
VERSION=1.1.0
git tag -a -s "v${VERSION}" -m "AIStudio Server ${VERSION}"
git push origin "v${VERSION}"
```

`-a` makes it annotated (`type=tag`, carries author and date), `-s` signs it.
Both matter: a lightweight tag carries no provenance and `git tag -v` has
nothing to check.

Verify before pushing:

```bash
git tag -v "v${VERSION}"     # must print "Good signature"
git cat-file -t "v${VERSION}" # must print "tag", not "commit"
```

If you have no signing key:

```bash
gpg --full-generate-key
git config --global user.signingkey <KEY_ID>
git config --global tag.gpgsign true
gpg --armor --export <KEY_ID>   # add to GitHub → Settings → SSH and GPG keys
```

### 3. Pin the install scripts to the release SHA

```bash
SHA=$(git rev-parse "v${VERSION}^{commit}")
sed -i "s/^COMMIT=.*/COMMIT=\"${SHA}\"/" install.sh
sed -i "s/^\$Commit = .*/\$Commit = \"${SHA}\"/" install.ps1
git commit -am "Pin installers to v${VERSION} (${SHA:0:12})"
```

Note the ordering: the pin commit lands *after* the tag, so the tag itself never
contains a self-reference. Users who run the installer from `master` get the
pinned SHA; users who download the tagged archive get a matching tree.

### 4. Publish

```bash
gh release create "v${VERSION}" \
  --title "AIStudio Server ${VERSION}" \
  --notes-file CHANGELOG.md \
  --verify-tag
```

Do **not** upload a hand-built tarball. GitHub attaches the source archives
automatically.

### 5. Publish checksums

```bash
mkdir -p /tmp/rel && cd /tmp/rel
gh release download "v${VERSION}" --archive=tar.gz
gh release download "v${VERSION}" --archive=zip
sha256sum ./* > SHA256SUMS
gpg --detach-sign --armor SHA256SUMS
gh release upload "v${VERSION}" SHA256SUMS SHA256SUMS.asc
```

### 6. Verify as a user would

On a clean machine:

```bash
curl -fsSL https://raw.githubusercontent.com/corespan/aistudio-server/v${VERSION}/install.sh | bash
```

Confirm the script reports the expected SHA and refuses to continue if it does
not match.

---

## Workload images

The workload images are versioned independently of the server, via
`WORKLOAD_IMAGE_TAG` and `JUPYTER_IMAGE_TAG` in `.env`, and mirrored in
`catalog.json`.

For each image release:

1. Build and push with an immutable version tag. Never `latest`.
2. Record the digest:
   ```bash
   docker buildx imagetools inspect <image>:<tag> --format '{{.Manifest.Digest}}'
   ```
3. Run `./scripts/generate_sbom.sh` and commit `sbom/`.
4. Review `sbom/REPORT.md` for flagged licences before publishing.
5. Update `catalog.json` and `.env.example` together — they drift otherwise.

---

## Signing checklist

| Item | Command to verify |
| --- | --- |
| Tag is annotated | `git cat-file -t v<version>` → `tag` |
| Tag is signed | `git tag -v v<version>` → `Good signature` |
| Installers pin a SHA | `grep -E '^(COMMIT=\|\$Commit)' install.sh install.ps1` |
| Checksums published | `gh release view v<version> --json assets` |
| No stub tarball | assets contain only source archives + `SHA256SUMS*` |
| SBOM committed | `ls sbom/*.spdx.json` |
