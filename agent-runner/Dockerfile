FROM python:3.11-slim

WORKDIR /app

# Hash-locked and wheels only: a changed or source-only package fails the build.
COPY requirements.lock.txt .
RUN pip install --no-cache-dir --only-binary :all: --require-hashes -r requirements.lock.txt

COPY app app
COPY platform platform

# It reads model output and untrusted tool text all day: no root, no keys (compose mounts only the
# gateway's public jwks.json).
USER 10001:10001

# Reached by the panel; it reaches agentgateway (MCP and LLM) and Postgres only.
EXPOSE 8060
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8060"]
