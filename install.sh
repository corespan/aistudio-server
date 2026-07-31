#!/bin/bash
set -e

REPO="https://github.com/corespan/aistudio-server.git"
TAG="ai-studio-server-1.0.0-1-opensource"
DIR="aistudio-server"

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
    echo "Directory '$DIR' already exists. Pulling latest..."
    cd $DIR
    git fetch --tags
else
    echo "Cloning aistudio-server..."
    git clone $REPO $DIR
    cd $DIR
fi

echo "Checking out $TAG..."
git checkout $TAG

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
