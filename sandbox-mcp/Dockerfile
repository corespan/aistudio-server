FROM python:3.11-slim

WORKDIR /app

# Hash-locked and wheels only: a changed or source-only package fails the build.
COPY requirements.lock.txt .
RUN pip install --no-cache-dir --only-binary :all: --require-hashes -r requirements.lock.txt

COPY app app

# Workspace snapshots: compose's named volume takes this owner when it is first created.
RUN mkdir -p /var/lib/sandbox/workspaces && chown 10001:10001 /var/lib/sandbox/workspaces

# Reaches Docker over TCP (a socket proxy), so it needs no root and no socket.
USER 10001:10001

# Reached only by agentgateway: http://sandbox-mcp:8082/mcp
EXPOSE 8082
CMD ["python", "-m", "app.server"]
