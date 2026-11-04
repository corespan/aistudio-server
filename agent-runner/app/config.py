"""Configuration for agent-runner, loaded from environment variables."""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Central configuration for agent-runner."""

    # agentgateway. Each connector in the registry is its route /mcp/<name>; every call carries the
    # caller's own JWT (gateway/config.yaml).
    # agentgateway's inside listener (:8052, never published); :8050 is HTTPS for MCP clients outside.
    gateway_url: str = "http://agentgateway:8052"

    # OpenAI-compatible endpoint: agentgateway's LLM route in front of the site's vLLM. The caller's
    # JWT is the API key, so the gateway checks it on every LLM call too. The runner asks for logical
    # models; the route maps them to the served one.
    llm_base_url: str = "http://agentgateway:8052/llm/v1"
    model_planner: str = "planner"
    model_worker: str = "worker"
    model_summarizer: str = "summarizer"
    llm_timeout_seconds: int = 120
    llm_temperature: float = 0.1
    # Thinking models (Qwen3 on Ollama): "none" answers a tool round in seconds instead of a minute.
    # Empty: not sent.
    llm_reasoning_effort: str = "none"
    # Output cap per call: a model that loops can't run until its context is full.
    llm_max_tokens: int = 2048

    # Caller JWT: Keycloak's (realm aistudio), audience agent-runner. Compose sets the URL and the
    # issuer (https://<MCP_PUBLIC_HOST>:8050/realms/aistudio); a JWKS file is for tests.
    jwks_url: str = ""
    jwks_file: str = ""
    jwks_cache_seconds: int = 300
    jwt_issuer: str = "aistudio"
    jwt_audience: str = "agent-runner"
    jwt_leeway_seconds: int = 30

    # LangGraph checkpoints and the run log. Empty: in memory, nothing survives a restart.
    database_url: str = ""

    # platform.yaml, the packs it enables and their registry.lock.json (app/registry.py).
    platform_dir: str = str(Path(__file__).resolve().parent.parent / "platform")

    # Caps.
    max_parallel_agents: int = 2      # per run
    max_runs_per_user: int = 2        # active at once
    max_llm_inflight: int = 4         # per process; set from the backend's measured throughput

    # Budget per run, shared by all its agents.
    max_tasks: int = 6
    max_replans: int = 2
    run_max_steps: int = 40           # tool rounds, all agents together
    run_max_tokens: int = 200_000
    run_max_seconds: int = 600

    # claude-code agents (docs/agent-runner.md#usage-and-limits). Weighted tokens: input + cache
    # writes + output + 0.1 x cache reads. Per-step caps are the agent file's budgets.tokens.
    claude_run_max_weighted: int = 2_000_000
    claude_window_5h_weighted: int = 4_000_000     # rolling, per user, from the usage table
    claude_day_weighted: int = 12_000_000          # rolling 24 hours
    claude_run_max_seconds: int = 2700
    claude_steps_at_once: int = 1                  # per process: it's one subscription
    workflows_at_once: int = 1                     # per user
    workflow_starts_per_hour: int = 6              # per user

    # Conversations (the panel's chat): earlier turns given to a new turn's agents, as data.
    conversation_turns: int = 6
    conversation_chars: int = 6_000

    # Runs are live: one with no stream attached for this long is cancelled.
    stream_grace_seconds: int = 30

    # Caps on text that reaches a model: one tool result, one task output passed to another agent.
    # A lower tool cap reads faster on slow hardware but can cut off the rows a question needs.
    tool_result_chars: int = 12_000
    task_output_chars: int = 6_000

    # Langfuse traces (app/tracing.py): off unless all three are set. Traces hold prompts, tool
    # output and the caller's sub, like the run log.
    langfuse_host: str = ""
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_timeout_seconds: int = 5

    # Browser panels on other origins. Auth is a bearer token, never a cookie.
    cors_origins: list[str] = ["*"]


settings = Settings()
