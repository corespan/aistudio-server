# python:3.11-slim, pinned by digest so a rebuild gets the image that was reviewed. Bump both together.
FROM python:3.11-slim@sha256:db20d8506e2f15b6db39b40b6015ca46bae855dec6dd326d462cfe79ae972893

WORKDIR /app

# The CLI tools the probes use. No compiler: every pinned package ships a Linux wheel, and
# --only-binary keeps it that way (a source-only dependency fails the build instead of
# quietly needing gcc, a GPL toolchain, in the image). See sbom/REPORT.md.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        git iputils-ping iproute2 tcpdump openssh-client && \
    rm -rf /var/lib/apt/lists/*

# The hashed lock (the same pins as requirements.txt): a swapped wheel on the index fails the build.
COPY requirements.lock.txt .
RUN pip install --no-cache-dir --only-binary :all: --require-hashes -r requirements.lock.txt

COPY . .

# ponytail: runs as root, with every capability dropped but NET_RAW and DAC_READ_SEARCH
# (docker-compose.yml). A non-root user would need ambient capabilities for tcpdump and for reading
# the host-owned SSH key and /var/log; add them with setcap if root inside the container matters.
# Serves /mcp over streamable HTTP, reached through agentgateway.
EXPOSE 8081
CMD ["python", "-m", "app.mcp_server"]
