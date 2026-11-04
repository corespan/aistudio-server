# Pinned by digest: bump tag and digest together.
FROM python:3.11-slim@sha256:db20d8506e2f15b6db39b40b6015ca46bae855dec6dd326d462cfe79ae972893

WORKDIR /app

# The probes' CLI tools; no compiler (wheels only, see sbom/REPORT.md).
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        git iputils-ping iproute2 tcpdump openssh-client && \
    rm -rf /var/lib/apt/lists/*

# Hashed lock: a swapped wheel on the index fails the build.
COPY requirements.lock.txt .
RUN pip install --no-cache-dir --only-binary :all: --require-hashes -r requirements.lock.txt

COPY . .

# ponytail: root, with only the capabilities docker-compose.yml adds; setcap if that matters.
EXPOSE 8081
CMD ["python", "-m", "app.mcp_server"]
