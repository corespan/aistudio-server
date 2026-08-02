#!/bin/bash
set -e

REPO="https://github.com/corespan/aistudio-server.git"
DIR="aistudio-server"

# Pin to an immutable commit SHA, not a tag name.
#
# A tag is a movable reference. Anyone with write access to the repository can
# repoint it at different code, and every subsequent install silently picks up
# the change with nothing downstream able to detect it. A commit SHA is content-
# addressed and cannot be repointed.
#
# Updated by the release process — see RELEASE.md step 3.
COMMIT="103f529"          # v1.0.0  — replace with the full 40-char SHA at release
TAG="ai-studio-server-1.0.0-1-opensource"   # informational only, for the banner

# ── Prerequisites check ────────────────────────────────────────────────────────
echo "Checking prerequisites..."
for cmd in git docker; do
    if ! command -v $cmd &> /dev/null; then
        echo "ERROR: '$cmd' is not installed. Please install it and re-run."
        exit 1
    fi
done

if ! docker compose version &> /dev/null; then
    echo "ERROR: 'docker compose' plugin not found. Please install Docker Desktop or the Compose plugin."
    exit 1
fi

# ── Clone ──────────────────────────────────────────────────────────────────────
if [ -d "$DIR" ]; then
    echo "Directory '$DIR' already exists. Fetching..."
    cd "$DIR"
    git fetch --tags origin
else
    echo "Cloning aistudio-server..."
    git clone "$REPO" "$DIR"
    cd "$DIR"
fi

echo "Checking out $TAG ($COMMIT)..."
git checkout --quiet "$COMMIT"

# ── Integrity verification ─────────────────────────────────────────────────────
#
# Checking that HEAD equals $COMMIT after `git checkout $COMMIT` proves nothing:
# git resolves commits by content address, so it either succeeded or `set -e`
# already aborted. The check worth making is whether the *tag* still points at
# the commit this installer was published against. That is precisely the attack
# a mutable tag allows, and git will not warn about it.
ACTUAL="$(git rev-parse HEAD)"
echo "Checked out: $ACTUAL"

TAG_COMMIT="$(git rev-list -n 1 "$TAG" 2>/dev/null || true)"
if [ -z "$TAG_COMMIT" ]; then
    echo "NOTE: tag '$TAG' not found in this clone — cannot cross-check the pin."
elif [ "$TAG_COMMIT" != "$ACTUAL" ]; then
    echo "" >&2
    echo "ERROR: tag/commit mismatch." >&2
    echo "  installer pins:  $ACTUAL" >&2
    echo "  tag '$TAG' points at: $TAG_COMMIT" >&2
    echo "" >&2
    echo "The release tag has been moved since this installer was published." >&2
    echo "You have the pinned code, which is the safe outcome, but this should" >&2
    echo "not happen. Report it: https://github.com/corespan/aistudio-server/issues" >&2
    echo "" >&2
    exit 1
else
    echo "Verified: tag '$TAG' still points at the pinned commit."
fi

# Verify the signature on the release tag when one is present. Not fatal if the
# signer's key is not in the local keyring — most users will not have imported
# it — but the outcome is reported either way.
if [ -n "$TAG_COMMIT" ]; then
    if [ "$(git cat-file -t "$TAG" 2>/dev/null)" = "tag" ]; then
        if git tag -v "$TAG" >/dev/null 2>&1; then
            echo "Verified: tag signature is good."
        else
            echo "NOTE: could not verify the tag signature (signing key not in your keyring)."
            echo "      Import it with:  gpg --recv-keys <CORESPAN_KEY_ID>"
        fi
    else
        echo "NOTE: '$TAG' is a lightweight tag and carries no signature."
    fi
fi

# ── Environment ────────────────────────────────────────────────────────────────
if [ ! -f .env ]; then
    echo "Creating .env from .env.example..."
    cp .env.example .env
    echo ""
    echo "NOTE: Edit .env with your SSH key path, GPU node details, and other settings before running benchmarks."
    echo ""
fi

# ── Start services ─────────────────────────────────────────────────────────────
echo "Starting services..."
make setup

echo ""
echo "✓ AIStudio Server is running!"
echo "  API:          http://localhost:8002"
echo "  API Docs:     http://localhost:8002/docs"
echo "  DB Admin:     http://localhost:5050"
echo "  RabbitMQ UI:  http://localhost:15672"
echo ""
echo "To stop: cd $DIR && docker compose down"
