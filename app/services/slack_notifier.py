"""
Slack error alerting -- best-effort, fire-and-forget notifications to the
#aistudio-alerts channel.

Two alert shapes, matching exactly what the RCA agent needs to start digging
in, and nothing more (no error text is sent -- the agent recovers that
itself, from workload_events.message / workloads.error_message / task_logs,
via the tools it already has):

  1. Workload failure  -> Workload ID, Timestamp, Node IP
     Fired from app.worker._fail_workload() -- the single choke point every
     Celery task (validate_node / install_dependencies / execute_benchmark /
     launch_jupyter) already routes through on any failure, so one hook here
     covers every "error in benchmarking / starting the benchmark / etc"
     scenario without instrumenting each task individually.

  2. Service error     -> Timestamp, Service IP
     Fired when the API process itself is in trouble: an unhandled exception
     (app.main's generic_exception_handler) or a degraded /health check
     (Postgres unreachable, i.e. "down, or erroring while trying to come back
     up"). No workload is involved here, so there's no workload ID to
     report -- just where the *service* is running.

Uses httpx (already a project dependency) against a Slack Incoming Webhook
URL rather than the Slack SDK/bot token: an Incoming Webhook is a single
POST of {"text": "..."} to a per-channel URL -- no bot token, no signing
secret, no app-level OAuth needed on THIS side (that machinery only matters
for the side that has to *read* Slack, which is the RCA agent, not here).

Every public function is wrapped so a Slack outage or missing/blank webhook
URL can NEVER raise into the caller's real code path -- a Celery task
failing to post an alert must still finish marking the workload FAILED, and
a FastAPI request failing to post an alert must still return its error
response to the client.
"""

from __future__ import annotations

import logging
import socket
from datetime import datetime, timedelta, timezone

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _resolve_service_ip() -> str:
    """settings.SERVICE_HOST_IP if set, else a best-effort auto-detect.

    NOTE: inside an unmodified Docker bridge network, gethostbyname(hostname())
    resolves to the CONTAINER's internal IP, not a reachable host IP -- set
    SERVICE_HOST_IP explicitly in any deployment where that distinction matters
    (which is effectively every real deployment).
    """
    if settings.SERVICE_HOST_IP:
        return settings.SERVICE_HOST_IP
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return "unknown"


def _fmt_ts(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime(_ISO_FMT)


def _workload_failure_text(workload_id: str, node_ip: str | None, occurred_at: datetime) -> str:
    return (
        "🚨 *Workload Failed*\n"
        f"Workload: `{workload_id}`\n"
        f"Time: `{_fmt_ts(occurred_at)}`\n"
        f"Node: `{node_ip or 'unknown'}`"
    )


def _service_error_text(occurred_at: datetime, service_ip: str) -> str:
    return (
        "🔴 *aistudio-server error*\n"
        f"Time: `{_fmt_ts(occurred_at)}`\n"
        f"Service: `{service_ip}`"
    )


def _send_sync(text: str) -> None:
    """Post from a synchronous context (the Celery worker)."""
    if not settings.SLACK_ALERT_WEBHOOK_URL:
        return  # alerting disabled -- no-op, not an error
    try:
        resp = httpx.post(settings.SLACK_ALERT_WEBHOOK_URL, json={"text": text}, timeout=5.0)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 -- alerting must never break the caller
        logger.warning("slack_notifier: failed to post alert: %s", exc)


async def _send_async(text: str) -> None:
    """Post from an async context (FastAPI request handlers)."""
    if not settings.SLACK_ALERT_WEBHOOK_URL:
        return
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(settings.SLACK_ALERT_WEBHOOK_URL, json={"text": text})
            resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        logger.warning("slack_notifier: failed to post alert: %s", exc)


def notify_workload_failure(
    workload_id: str,
    node_ip: str | None,
    occurred_at: datetime | None = None,
) -> None:
    """Fire a workload-failure alert. Call from a SYNCHRONOUS context (Celery tasks).

    Args:
        workload_id: The human-readable workload id (e.g. 'wl-20260806-oom01').
        node_ip: The GPU node the workload was running on, if known.
        occurred_at: When the failure was detected. Defaults to now() -- pass
            an explicit value if the caller already captured a more precise
            moment (e.g. right when the exception was caught).
    """
    occurred_at = occurred_at or datetime.now(timezone.utc)
    _send_sync(_workload_failure_text(workload_id, node_ip, occurred_at))


# Service-level alert cooldown -- per-process only (see
# SLACK_SERVICE_ALERT_COOLDOWN_SECONDS' docstring in config.py for why).
_last_service_alert_at: datetime | None = None


def _service_cooldown_active(now: datetime) -> bool:
    global _last_service_alert_at
    cooldown = timedelta(seconds=settings.SLACK_SERVICE_ALERT_COOLDOWN_SECONDS)
    if _last_service_alert_at is not None and (now - _last_service_alert_at) < cooldown:
        return True
    _last_service_alert_at = now
    return False


async def notify_service_error(occurred_at: datetime | None = None) -> None:
    """Fire a service-level alert. Call from an ASYNC context (FastAPI).

    Rate-limited by SLACK_SERVICE_ALERT_COOLDOWN_SECONDS so a Postgres outage
    being polled by /health every few seconds doesn't flood the channel with
    one alert per poll.
    """
    now = occurred_at or datetime.now(timezone.utc)
    if _service_cooldown_active(now):
        return
    await _send_async(_service_error_text(now, _resolve_service_ip()))
