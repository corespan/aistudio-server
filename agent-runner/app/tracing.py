"""Langfuse traces: one trace per run, with each agent, LLM call and tool round inside it.

Off unless LANGFUSE_HOST and both keys are set. Fail-open: a Langfuse that is down or rejects
the keys costs a run nothing.
"""
from __future__ import annotations

import asyncio
import warnings
from contextlib import ExitStack, contextmanager

import structlog

from app.config import settings

log = structlog.get_logger()

_client = None
_off = False


def _get():
    """The Langfuse client, or None when tracing is off."""
    global _client, _off
    if _client is not None or _off:
        return _client
    if not (settings.langfuse_host and settings.langfuse_public_key and settings.langfuse_secret_key):
        _off = True
        return None
    try:
        from langfuse import Langfuse
        # Built once; CallbackHandler() finds it as the SDK's default client.
        _client = Langfuse(public_key=settings.langfuse_public_key,
                           secret_key=settings.langfuse_secret_key,
                           base_url=settings.langfuse_host, timeout=settings.langfuse_timeout_seconds)
    except Exception as exc:  # noqa: BLE001 - tracing is never worth a failed run
        log.warning("tracing.init_failed", error=str(exc))
        _off = True
    return _client


@contextmanager
def root(run_id: str, sub: str, message: str | None):
    """The run's root span; yields ``finish(answer, status)``.

    Langfuse server v3 shows a trace's input and output from trace-level fields, which SDK v4 no
    longer fills in (server v4 reads the root span instead), so finish() sets them itself.
    """
    client = _get()
    if client is None:
        yield lambda answer, status: None
        return
    spans = ExitStack()
    try:
        from langfuse import propagate_attributes
        spans.enter_context(propagate_attributes(user_id=sub, session_id=run_id, trace_name="agent-run"))
        span = spans.enter_context(client.start_as_current_observation(
            name="agent-run", as_type="agent", input=message))
    except Exception as exc:  # noqa: BLE001 - tracing is never worth a failed run
        log.warning("tracing.root_failed", error=str(exc))
        spans.close()
        yield lambda answer, status: None
        return

    def finish(answer: str | None, status: str) -> None:
        try:
            span.update(output=answer, metadata={"status": status})
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                # ponytail: only for a v3 server; drop when Langfuse is on v4.
                span.set_trace_io(input=message, output=answer)
        except Exception as exc:  # noqa: BLE001
            log.warning("tracing.finish_failed", error=str(exc))

    with spans:
        yield finish


def run_config(run_id: str, sub: str) -> dict:
    """LangGraph config for one run: ``{}`` when tracing is off."""
    if _get() is None:
        return {}
    try:
        from langfuse.langchain import CallbackHandler
        handler = CallbackHandler()
    except Exception as exc:  # noqa: BLE001
        log.warning("tracing.handler_failed", error=str(exc))
        return {}
    # A resumed run carries the same run id, so it lands in the same session.
    return {"callbacks": [handler], "run_name": "graph",     # under root()'s agent-run span
            "metadata": {"langfuse_session_id": run_id, "langfuse_user_id": sub,
                         "langfuse_trace_name": "agent-run"}}


async def check() -> None:
    """Say once, at startup, whether traces can land."""
    client = _get()
    if client is None:
        log.info("tracing.disabled", detail="LANGFUSE_HOST / LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY unset")
        return
    try:
        ok = await asyncio.to_thread(client.auth_check)
    except Exception as exc:  # noqa: BLE001 - usually the network path, so name the host
        log.warning("tracing.unreachable", host=settings.langfuse_host, error=str(exc))
        return
    if ok:
        log.info("tracing.ready", host=settings.langfuse_host)
    else:
        log.warning("tracing.auth_failed", host=settings.langfuse_host)


def shutdown() -> None:
    """Flush what is still batched."""
    if _client is not None:
        try:
            _client.shutdown()
        except Exception as exc:  # noqa: BLE001
            log.warning("tracing.shutdown_failed", error=str(exc))


class Span:
    """A claude-code agent's span: a generation per turn and a span per tool call under it.

    Fail-open like the rest: with tracing off, or any SDK error, every method does nothing.
    """

    def __init__(self, obs=None):
        self.obs = obs

    def _try(self, fn, default=None):
        if self.obs is None:
            return default
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - tracing is never worth a failed run
            log.warning("tracing.span_failed", error=str(exc))
            return default

    def generation(self, name: str, model: str, tokens: dict, cost_usd: float | None = None) -> None:
        """One turn's usage. Cost is set explicitly (0 per turn, the reported total on the last
        row), so Langfuse doesn't add its own price estimate on top."""
        def go():
            g = self.obs.start_observation(
                name=name, as_type="generation", model=model,
                usage_details={"input": tokens["input"], "output": tokens["output"],
                               "cache_read_input_tokens": tokens["cache_read"],
                               "cache_creation_input_tokens": tokens["cache_write"]},
                cost_details={"total": float(cost_usd or 0.0)})
            g.end()
        self._try(go)

    def tool(self, name: str, args) -> "Span":
        return Span(self._try(lambda: self.obs.start_observation(name=f"tool {name}", as_type="tool",
                                                                  input=args)))

    def end(self, output=None, metadata: dict | None = None) -> None:
        def go():
            self.obs.update(output=output, metadata=metadata)
            self.obs.end()
        self._try(go)


def agent_span(name: str, task: str, metadata: dict) -> Span:
    """A span under the run's root; ``end()`` it when the step is done."""
    client = _get()
    if client is None:
        return Span()
    try:
        return Span(client.start_observation(name=name, as_type="agent", input=task, metadata=metadata))
    except Exception as exc:  # noqa: BLE001
        log.warning("tracing.span_failed", error=str(exc))
        return Span()
