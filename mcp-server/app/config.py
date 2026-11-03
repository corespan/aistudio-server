"""Configuration for the MCP tool server, loaded from environment variables."""
from __future__ import annotations

import os

from pydantic_settings import BaseSettings


# Docker/compose secrets, read from files when the directory exists.
_SECRETS_DIR = os.getenv("MCP_SECRETS_DIR", "/run/secrets")
_SECRETS_SOURCE = {"secrets_dir": _SECRETS_DIR} if os.path.isdir(_SECRETS_DIR) else {}


class Settings(BaseSettings):
    """Central configuration for the MCP tool server."""

    aistudio_api_url: str = "http://localhost:8002"

    # Caller JWTs: agentgateway checks them first, this server again (gateway/mint.py makes them).
    mcp_jwks_url: str = ""
    mcp_jwks_file: str = ""
    mcp_jwt_issuer: str = "aistudio"
    mcp_jwt_audience: str = "mcp:aistudio"
    mcp_jwt_leeway_seconds: int = 30
    mcp_jwt_max_lifetime_seconds: int = 86400  # longest exp - iat accepted; 0 = no limit

    # Off: the write tools are not even registered.
    mcp_writes_enabled: bool = False
    # A retried identical write within this window replays the first result; 0 = off.
    mcp_write_dedup_seconds: int = 60

    ssh_key_path: str = "/root/.ssh/id_rsa"
    ssh_default_user: str = "ubuntu"
    ssh_connect_timeout: int = 10
    node_ssh_user: str = ""
    ssh_known_hosts: str = "~/.ssh/aistudio_mcp_known_hosts"
    ssh_host_key_policy: str = "tofu"  # or "strict"
    node_allowed_cidrs: str = ""  # comma-separated; empty = node probes off

    docker_host: str = "unix:///var/run/docker.sock"

    repo_path: str = "/repo"
    readable_roots: str = "/repo,/var/log"
    # Where each service's code lives; not all of /AIStudio, which holds .env too.
    container_readable_roots: str = "/app,/repo,/usr/share/nginx/html,/AIStudio/app,/AIStudio/alembic"

    net_probe_timeout: int = 5  # seconds

    browser_cdp_url: str = ""

    rabbitmq_mgmt_url: str = "http://localhost:15672"
    rabbitmq_user: str = ""
    rabbitmq_password: str = ""

    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8081
    mcp_tls_certfile: str = ""
    mcp_tls_keyfile: str = ""
    mcp_allowed_hosts: str = ""  # comma-separated Host headers

    model_config = {
        "env_file": os.getenv("MCP_ENV_FILE", ".env"),
        "env_file_encoding": "utf-8",
        **_SECRETS_SOURCE,
    }


settings = Settings()
