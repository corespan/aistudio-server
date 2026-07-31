# AIStudio Server - Windows Installer
# Run in PowerShell: .\install.ps1

$ErrorActionPreference = "Stop"

$REPO = "https://github.com/corespan/aistudio-server.git"
$TAG  = "ai-studio-server-1.0.0-1-opensource"
$DIR  = "aistudio-server"

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
    Write-Host "Directory '$DIR' already exists. Pulling latest..."
    Set-Location $DIR
    git fetch --tags
} else {
    Write-Host "Cloning aistudio-server..."
    git clone $REPO $DIR
    Set-Location $DIR
}

Write-Host "Checking out $TAG..."
git checkout $TAG

# ── Environment ────────────────────────────────────────────────────────────────
if (-not (Test-Path ".env")) {
    Write-Host "Creating .env from .env.example..."
    Copy-Item .env.example .env
    Write-Host ""
    Write-Host "NOTE: Edit .env with your SSH key path, GPU node details, and other settings before running benchmarks."
    Write-Host ""
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
