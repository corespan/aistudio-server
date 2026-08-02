# AIStudio Server - Windows Installer
# Run in PowerShell: .\install.ps1

$ErrorActionPreference = "Stop"

$REPO = "https://github.com/corespan/aistudio-server.git"
$DIR  = "aistudio-server"

# Pin to an immutable commit SHA, not a tag name.
#
# A tag is a movable reference. Anyone with write access can repoint it at
# different code, and every subsequent install silently picks up the change with
# nothing downstream able to detect it. A commit SHA is content-addressed and
# cannot be repointed.
#
# Updated by the release process — see RELEASE.md step 3.
$Commit = "103f529"   # v1.0.0 — replace with the full 40-char SHA at release
$TAG    = "ai-studio-server-1.0.0-1-opensource"   # informational only

# ── Prerequisites check ────────────────────────────────────────────────────────
Write-Host "Checking prerequisites..."
foreach ($cmd in @("git", "docker")) {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
        Write-Error "'$cmd' is not installed. Please install it and re-run."
        exit 1
    }
}

try {
    docker compose version | Out-Null
} catch {
    Write-Error "'docker compose' plugin not found. Please install Docker Desktop and re-run."
    exit 1
}

# ── Clone ──────────────────────────────────────────────────────────────────────
if (Test-Path $DIR) {
    Write-Host "Directory '$DIR' already exists. Fetching..."
    Set-Location $DIR
    git fetch --tags origin
} else {
    Write-Host "Cloning aistudio-server..."
    git clone $REPO $DIR
    Set-Location $DIR
}

Write-Host "Checking out $TAG ($Commit)..."
git checkout --quiet $Commit

# ── Integrity verification ─────────────────────────────────────────────────────
#
# Checking that HEAD equals $Commit after `git checkout $Commit` proves nothing:
# git resolves commits by content address, so it either succeeded or already
# threw. The check worth making is whether the *tag* still points at the commit
# this installer was published against. That is precisely the attack a mutable
# tag allows, and git will not warn about it.
$Actual = (git rev-parse HEAD).Trim()
Write-Host "Checked out: $Actual"

$TagCommit = (git rev-list -n 1 $TAG 2>$null)
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($TagCommit)) {
    $TagCommit = $null
    Write-Host "NOTE: tag '$TAG' not found in this clone — cannot cross-check the pin."
} else {
    $TagCommit = $TagCommit.Trim()
    if ($TagCommit -ne $Actual) {
        Write-Host ""
        Write-Host "ERROR: tag/commit mismatch." -ForegroundColor Red
        Write-Host "  installer pins:        $Actual"
        Write-Host "  tag '$TAG' points at:  $TagCommit"
        Write-Host ""
        Write-Host "The release tag has been moved since this installer was published."
        Write-Host "You have the pinned code, which is the safe outcome, but this should"
        Write-Host "not happen. Report it: https://github.com/corespan/aistudio-server/issues"
        Write-Host ""
        exit 1
    }
    Write-Host "Verified: tag '$TAG' still points at the pinned commit."
}

# Verify the release tag signature when one is present. Not fatal if the
# signer's key is not in the local keyring.
if ($TagCommit) {
    $TagType = (git cat-file -t $TAG 2>$null)
    if ($TagType -eq "tag") {
        git tag -v $TAG 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "Verified: tag signature is good."
        } else {
            Write-Host "NOTE: could not verify the tag signature (signing key not in your keyring)."
            Write-Host "      Import it with:  gpg --recv-keys <CORESPAN_KEY_ID>"
        }
    } else {
        Write-Host "NOTE: '$TAG' is a lightweight tag and carries no signature."
    }
}

# ── Environment ────────────────────────────────────────────────────────────────
if (-not (Test-Path ".env")) {
    Write-Host "Creating .env from .env.example..."
    Copy-Item .env.example .env
    Write-Host ""
    Write-Host "NOTE: Edit .env with your SSH key path, GPU node details, and other settings before running benchmarks."
    Write-Host ""
}

# ── Frontend assets ────────────────────────────────────────────────────────────
# The demo-ui fonts and Chart.js are gitignored binaries, fetched here rather
# than loaded from a CDN at page load. Non-fatal: the dashboard degrades to
# system fonts with no charts, and the API and worker are unaffected.
if (Get-Command bash -ErrorAction SilentlyContinue) {
    Write-Host "Vendoring demo-ui assets..."
    bash ./scripts/vendor_frontend_assets.sh --if-missing
    if ($LASTEXITCODE -ne 0) {
        Write-Host "WARNING: could not vendor demo-ui assets. Dashboard charts will be unavailable."
        Write-Host "         Fix later with: make vendor-assets"
    }
} else {
    Write-Host "NOTE: bash not found — skipping demo-ui asset vendoring."
    Write-Host "      Run 'make vendor-assets' from WSL if you want the dashboard."
}

# ── Start services ─────────────────────────────────────────────────────────────
Write-Host "Starting services..."
docker compose up --build -d

Write-Host "Running database migrations..."
docker compose exec api alembic upgrade head

Write-Host "Seeding workload catalog..."
docker compose exec api python -m app.services.catalog_seeder

Write-Host ""
Write-Host "✓ AIStudio Server is running!"
Write-Host "  API:          http://localhost:8002"
Write-Host "  API Docs:     http://localhost:8002/docs"
Write-Host "  DB Admin:     http://localhost:5050"
Write-Host "  RabbitMQ UI:  http://localhost:15672"
Write-Host ""
Write-Host "To stop: cd $DIR; docker compose down"
