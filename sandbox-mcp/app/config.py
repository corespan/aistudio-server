"""Configuration for the sandbox MCP server, loaded from SANDBOX_* environment variables."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central configuration for the sandbox MCP server."""

    model_config = SettingsConfigDict(env_prefix="SANDBOX_")

    host: str = "0.0.0.0"
    port: int = 8082
    # Host values agentgateway sends (it drops the port), for DNS-rebinding protection.
    allowed_hosts: str = "sandbox-mcp,sandbox-mcp:8082"

    # Caller JWT: the keys agentgateway checks (gateway/mint.py), audience mcp:sandbox.
    # Keycloak's (realm aistudio): compose sets the URL and the issuer. A JWKS file is for tests.
    jwks_url: str = ""
    jwks_file: str = ""
    jwks_cache_seconds: int = 300
    jwt_issuer: str = "aistudio"
    jwt_audience: str = "mcp:sandbox"
    jwt_leeway_seconds: int = 30

    # The sandbox. The Docker API comes from DOCKER_HOST (a socket proxy in compose).
    image: str = "python:3.11-slim"
    runtime: str = "runsc"            # gVisor; "runc" only for a host without it, and say so
    memory: str = "512m"
    cpus: float = 1.0
    pids: int = 128
    default_seconds: int = 30
    max_seconds: int = 60

    # Sandboxes at once: all callers, and one caller.
    max_concurrent: int = 4
    max_per_user: int = 2

    # Caps on what goes in and comes back.
    max_input_bytes: int = 2_000_000
    output_chars: int = 20_000
    file_chars: int = 64_000

    # Workspaces (docs/sandbox.md#workspaces): a repo checkout kept between calls as snapshots.
    # Containers join workspace_network, where agentgateway-sandbox (workspace_gateway_ip) is the
    # only other thing: /llm/anthropic and read-only git. gVisor can't use Docker's DNS, hence the IP.
    workspace_image: str = "aistudio-workspace:local"
    workspace_network: str = "aistudio-sandbox-egress"
    workspace_gateway_ip: str = "172.30.51.10"
    workspace_dir: str = "/var/lib/sandbox/workspaces"
    workspace_memory: str = "2g"
    workspace_cpus: float = 2.0
    workspace_pids: int = 512
    workspace_max_seconds: int = 900          # one Claude step or check, at most
    workspace_snapshot_bytes: int = 200_000_000
    workspace_files_bytes: int = 20_000_000    # what workspace_files returns at once (a PR's files)
    workspace_ttl_hours: int = 24             # snapshots left behind by a run that never closed
    workspace_max_concurrent: int = 2
    workspace_max_per_user: int = 1           # one step at a time: it's your subscription's window


settings = Settings()
