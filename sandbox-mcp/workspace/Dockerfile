# The workspace image: what Claude Code and the workflow's checks run in (docs/sandbox.md#workspaces).
# Built from the repo root: docker build -f sandbox-mcp/workspace/Dockerfile -t aistudio-workspace:local .
# Workspaces have no internet, so the repo's test dependencies are baked in here.
FROM node:22-bookworm-slim

# Pinned, and about two weeks old when chosen: not a fresh release. Bump it on purpose.
ARG CLAUDE_CODE_VERSION=2.1.303
RUN apt-get update && apt-get install -y --no-install-recommends \
      git make python3 python3-venv ca-certificates postgresql-15 \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} \
 && npm cache clean --force

# The main app's tests (the workflows' check), on PATH as python. Hash-locked, wheels only.
COPY requirements.lock.txt /tmp/app.lock.txt
RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --only-binary :all: --require-hashes -r /tmp/app.lock.txt \
 && /opt/venv/bin/pip install --no-cache-dir --only-binary :all: \
      pytest==9.1.1 pytest-asyncio==1.4.0 pytest-timeout==2.4.0 \
 && rm /tmp/app.lock.txt

# agent-runner's tests, in their own venv (its pins differ): /opt/runner-venv/bin/python -m pytest
COPY agent-runner/requirements.lock.txt /tmp/runner.lock.txt
RUN python3 -m venv /opt/runner-venv \
 && /opt/runner-venv/bin/pip install --no-cache-dir --only-binary :all: --require-hashes -r /tmp/runner.lock.txt \
 && /opt/runner-venv/bin/pip install --no-cache-dir --only-binary :all: pytest==9.1.1 pytest-asyncio==1.4.0 \
 && rm /tmp/runner.lock.txt

# A throwaway PostgreSQL for a check: with-postgres <command>.
COPY sandbox-mcp/workspace/with-postgres /usr/local/bin/with-postgres
RUN chmod 0755 /usr/local/bin/with-postgres

# /work is the checkout; HOME is outside it so Claude Code's own files never land in a snapshot.
# /out: the snapshot volume (sandbox-mcp mounts an anonymous volume there, which takes this owner).
RUN mkdir /work /out && chown node:node /work /out
ENV PATH=/opt/venv/bin:$PATH HOME=/tmp/home \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 DISABLE_AUTOUPDATER=1 GIT_TERMINAL_PROMPT=0
USER node
WORKDIR /work
