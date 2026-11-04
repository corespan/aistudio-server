# AI Studio MCP tool server

34 tools for an AI Studio deployment: benchmark results, tasks and Jupyter from the REST API,
container logs and inspection, GPU-node probes over SSH, and network and TLS checks. 31 only
read; `start_workload`, `start_jupyter` and `stop_workload` act, and exist only with
`MCP_WRITES_ENABLED=true`.

Tools only: no model, no reasoning loop; the MCP client supplies those. It is reached **only
through agentgateway** with a JWT from `gateway/mint.py`. Setup, roles, connecting a client and
the safety gates: [docs/mcp-server.md](../docs/mcp-server.md).

## Run with Compose (the normal way)

From the repository root:

```bash
make mcp-init && make mcp-up && make mcp-check
```

## Run on its own

```bash
cp .env.example .env      # then edit; unknown keys are rejected at startup
pip install -r requirements.txt
python -m app.mcp_server  # streamable HTTP on MCP_HOST:MCP_PORT, /mcp
```

```bash
curl localhost:8081/healthz        # {"status":"ok"}, no token needed
curl -i -X POST localhost:8081/mcp # 401: every other path needs the JWT
```

Healthy log: `mcp.built tools=31 writes=False` (34 with `MCP_WRITES_ENABLED=true`), then `mcp.serving`.

## The role rules

agentgateway's role → tool rules are generated from this server's manifest (`app/policy.py`), so
they cannot drift from what it enforces:

```bash
python -m app.manifest ../agent-runner/platform/packs/aistudio/connectors/aistudio.yaml   # regenerate the block between the markers
python -m app.manifest --check ../agent-runner/platform/packs/aistudio/connectors/aistudio.yaml
```

The block lives in the aistudio connector file; `make registry` then renders `gateway/config.yaml`
from it (see [docs/agent-runner.md](../docs/agent-runner.md#packs)).

## Tests

```bash
pip install -r requirements-dev.txt && pytest -q
```

No network, database or Docker needed. The tests use a throwaway ES256 key for the JWTs.
