FROM python:3.11-slim

WORKDIR /app

# The CLI tools the probes use. No compiler: every pinned package ships a Linux wheel, and
# --only-binary keeps it that way (a source-only dependency fails the build instead of
# quietly needing gcc, a GPL toolchain, in the image). See sbom/REPORT.md.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        git iputils-ping iproute2 tcpdump openssh-client && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --only-binary :all: -r requirements.txt

COPY . .

# Serves /mcp over streamable HTTP, reached through agentgateway.
EXPOSE 8081
CMD ["python", "-m", "app.mcp_server"]
