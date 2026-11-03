"""Configuration for the MCP tool server, loaded from environment variables."""
from __future__ import annotations

import os

from pydantic_settings import BaseSettings


# Secrets read from files in this directory (Docker/compose secrets), when it exists.
_SECRETS_DIR = os.getenv("MCP_SECRETS_DIR", "/run/secrets")
_SECRETS_SOURCE = {"secrets_dir": _SECRETS_DIR} if os.path.isdir(_SECRETS_DIR) else {}


class Settings(BaseSettings):
    """Central configuration for the MCP tool server."""

    # aistudio-server connection (compose: the api service on its internal port).
    aistudio_api_url: str = "http://localhost:8002"

    # Caller JWT verification: JWKS source, issuer, audience. agentgateway checks the same
    # token first; this server checks it again (gateway/mint.py makes the keys and tokens).
    mcp_jwks_url: str = ""
    mcp_jwks_file: str = ""         # compose mounts gateway/jwks.json here
    mcp_jwt_issuer: str = "aistudio"
    mcp_jwt_audience: str = "mcp:aistudio"
    mcp_jwt_leeway_seconds: int = 30

    # The write tools (start_workload, start_jupyter, stop_workload) exist only when the site switches
    # them on: off, they are not registered (not in tools/list) and refused if called anyway.
    mcp_writes_enabled: bool = False
    # An identical write (same caller, tool and arguments) repeated within this window returns
    # the first result instead of running again. 0 = off.
    mcp_write_dedup_seconds: int = 600

    # SSH for node diagnostics.
    ssh_key_path: str = "/root/.ssh/id_rsa"
    ssh_default_user: str = "ubuntu"
    ssh_connect_timeout: int = 10
    node_ssh_user: str = ""
    # Host-key policy ("tofu" or "strict") and known_hosts file.
    ssh_known_hosts: str = "~/.ssh/aistudio_mcp_known_hosts"
    ssh_host_key_policy: str = "tofu"
    # Comma-separated CIDRs a node probe may reach.
    node_allowed_cidrs: str = ""

    # Docker daemon.
    docker_host: str = "unix:///var/run/docker.sock"

    # Codebase / filesystem inspection.
    repo_path: str = "/repo"
    readable_roots: str = "/repo,/var/log"
    # Readable paths inside containers for container_diff.
    container_readable_roots: str = "/app,/srv,/usr/share/nginx/html,/repo,/AIStudio"

    # Network diagnostics.
    net_probe_timeout: int = 5 # seconds

    # CDP URL of the headless-browser sandbox.
    browser_cdp_url: str = ""

    # RabbitMQ.
    rabbitmq_mgmt_url: str = "http://localhost:15672"
    rabbitmq_user: str = ""
    rabbitmq_password: str = ""

    # ── HTTP transport ───────────────────────────────────────────────────────
    # Bind address and port.
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8081
    # TLS certificate and key files.
    mcp_tls_certfile: str = ""
    mcp_tls_keyfile: str = ""
    # Allowed Host headers, comma separated.
    mcp_allowed_hosts: str = ""

    model_config = {
        "env_file": os.getenv("MCP_ENV_FILE", ".env"),
        "env_file_encoding": "utf-8",
        **_SECRETS_SOURCE,
    }


# Shared settings instance.
settings = Settings()
