"""Diagnostic tools that call the aistudio-server REST API as the calling user."""

from __future__ import annotations

import asyncio
import json
from urllib.parse import quote
import statistics
from collections import deque

import httpx
import structlog
from langchain_core.tools import tool

from app.config import settings
from app.services import platform_api

logger = structlog.get_logger()


def _fmt_ts(ts) -> str:
    return ts.strftime("%Y-%m-%d %H:%M:%S") if ts else "?"


def _format_run_list(runs: list[dict], only_failed: bool) -> str:
    """Format completed benchmark runs."""
    scope = "non-success " if only_failed else ""
    out = [f"{len(runs)} completed {scope}benchmark run(s) (benchmark_results, newest first):"]
    for r in runs:
        state = "success" if r.get("all_success") else "had failures"
        out.append(
            f"  • {r['run_id']}  [{state}]  {r['model_name']}  {r['gpu_type']}×{r['gpu_count']}"
            f"  ({r['sub_runs']} sub-run(s), {_fmt_ts(r['started_at'])})"
        )
    out.append("For one run: workload_history(workload_id='<run>') or "
               "throughput_compare(run_id='<run>').")
    return "\n".join(out)


def _format_completed_run(run_id: str, rows: list[dict]) -> str:
    """Format a completed run from its benchmark_results rows."""
    r0 = rows[0]
    statuses = sorted({r.get("status") for r in rows if r.get("status")})
    tps = [r["total_token_throughput"] for r in rows if r.get("total_token_throughput") is not None]
    out = [
        f"'{run_id}' is a COMPLETED benchmark run — it's in benchmark_results, not the "
        "workloads lifecycle table (that's why a workload-id lookup missed it; it does "
        "NOT mean the run doesn't exist).",
        f"  model: {r0['model_name']}   hardware: {r0['gpu_type']}×{r0['gpu_count']}"
        f"   precision: {r0.get('precision')}",
        f"  sub-runs: {len(rows)}   status: {', '.join(statuses) or 'unknown'}",
    ]
    if r0.get("node_ips"):
        out.append(f"  node(s): {', '.join(r0['node_ips'])}  (node_exec here for GPU/PCIe/throttle health)")
    if len(rows) > 1:
        out.append(f"  sub-runs (all share run_id '{run_id}'; a sub-run's id IS run_id + "
                   "sub_run_index — there is no separate per-sub-run id):")
        for r in sorted(rows, key=lambda x: x.get("sub_run_index") or 0):
            tp = r.get("total_token_throughput")
            tp_s = f"{tp:.1f} tok/s" if tp is not None else "no metric"
            out.append(
                f"    - sub_run {r.get('sub_run_index')}: concurrency={r.get('concurrency')}"
                f"  io={r.get('input_tokens')}/{r.get('output_tokens')}  {tp_s}  [{r.get('status')}]"
            )
        rows_tp = [r for r in rows if r.get("total_token_throughput") is not None]
        if rows_tp:
            lo = min(rows_tp, key=lambda r: r["total_token_throughput"])
            hi = max(rows_tp, key=lambda r: r["total_token_throughput"])
            out.append(
                f"  → throughput spans {lo['total_token_throughput']:.1f}–"
                f"{hi['total_token_throughput']:.1f} tok/s — the two ENDPOINTS are "
                f"MIN sub_run {lo.get('sub_run_index')} (concurrency={lo.get('concurrency')}) and "
                f"MAX sub_run {hi.get('sub_run_index')} (concurrency={hi.get('concurrency')}). "
                "This is the min–max of the sweep (throughput rises with concurrency; expected, "
                "not a regression). If asked 'which two had this throughput', it's these two."
            )
    elif tps:
        out.append(f"  throughput: {tps[0]:.1f} tok/s")
    if statuses == ["success"]:
        out.append(f"  → This run SUCCEEDED; nothing failed. If you mean it was SLOW or "
                   f"regressed, call throughput_compare(run_id='{run_id}').")
    else:
        out.append("  → Not all sub-runs succeeded; call throughput_compare(run_id=...) "
                   "or get_task_logs for the failing detail.")
    return "\n".join(out)


def _format_no_exact_match(term: str, matches: list[dict]) -> str:
    if not matches:
        return (
            f"No workload or benchmark run matches {term!r} — searched the task status "
            "routes AND completed benchmark_results, by id and by model. It may "
            "be a typo or an older run outside the search. Give me the exact id if you "
            "have it, or ask me to list recent runs."
        )
    out = [f"{len(matches)} run(s)/workload(s) matching {term!r} (id | state | model):"]
    for m in matches:
        hw = f"  {m['gpu_type']}×{m['gpu_count']}" if m.get("gpu_type") else ""
        out.append(f"  • {m['id']}  [{m['state']}]  {m['model_name']}{hw}")
    out.append("One run's detail: workload_history(workload_id='<id>'); "
               "slowness: throughput_compare(run_id='<id>').")
    return "\n".join(out)


def _format_workload_detail(wl: dict) -> str:
    out = [
        f"Workload {wl['workload_id']}",
        f"  state: {wl.get('state')}   updated: {_fmt_ts(wl.get('updated_at'))}",
    ]
    if wl.get("jupyter_url"):
        out.append(f"  jupyter_url: {wl['jupyter_url']}")
    err = (wl.get("error_message") or "").strip()
    if err:
        out.append(f"  error_message: {err}")
    if wl.get("state") == "FAILED":
        out.append(
            "\n  → get_task_logs(task_id) has the full validate → install → benchmark output, "
            "which shows the failing stage. If it failed in the benchmark container, "
            "node_docker_logs(node_ip, container='<task id>') has its crash output for as "
            "long as that container still exists on the node.")
    return "\n".join(out)


@tool
async def workload_history(
    only_failed: bool = False,
    workload_id: str = "",
    limit: int = 10,
) -> str:
    """Show recent completed benchmark runs (`only_failed` keeps runs with failures, `limit` caps the list), or, when `workload_id` is given, that task's state and error or that run's results."""
    logger.info("tool.aistudio.workload_history", only_failed=only_failed,
                workload_id=workload_id or None)
    try:
        if workload_id:
            wl = await platform_api.workload(workload_id)
            if wl is not None:
                return _format_workload_detail(wl)
            bench = await platform_api.benchmark_results(workload_id)
            if bench:
                return _format_completed_run(workload_id, bench)
            return _format_no_exact_match(workload_id, await platform_api.search_runs(workload_id))

        runs = await platform_api.recent_runs(only_failed, limit)
        if not runs:
            scope = "non-success " if only_failed else ""
            return f"No {scope}benchmark runs on record."
        return _format_run_list(runs, only_failed)
    except Exception as exc:
        return f"Could not read workload history from aistudio-server: {exc}"

# Regression threshold, in percent.
_REGRESSION_PCT = 10.0
_DIFF_FIELDS = ("node_ips", "server_name", "gpu_model")


def _ms(v) -> str:
    return f"{v:.0f}ms" if isinstance(v, (int, float)) else "?"


async def _compare_one(t: dict, limit: int) -> str:
    """Compare one benchmark row against same-config past runs."""
    tag = (f"{t['run_id']} (sub_run {t['sub_run_index']}) — {t['model_name']}, "
           f"{t['gpu_type']}×{t['gpu_count']}, {t['precision']}, "
           f"concurrency={t['concurrency']}, io={t['input_tokens']}/{t['output_tokens']}"
           f"{', ' + t['parallelism'] if t.get('parallelism') else ''})")
    lines = [f"▶ {tag}"]

    cur = t["total_token_throughput"]
    if t["status"] != "success" or cur is None:
        lines.append(f"  status={t['status']}, throughput={cur} — no usable metric "
                     "(pending/failed row); nothing to compare.")
        return "\n".join(lines)

    peers = await platform_api.comparable_results(t, limit)
    lines.append(f"  this run:  {cur:.1f} tok/s   ttft {_ms(t['mean_ttft_ms'])}  "
                 f"tpot {_ms(t['mean_tpot_ms'])}  e2e {_ms(t['mean_e2el_ms'])}")
    if not peers:
        lines.append("  baseline:  none — no past runs with this exact config, so "
                     "slowness can't be proven as a regression. Absolute numbers only.")
        return "\n".join(lines)

    tps = [p["total_token_throughput"] for p in peers]
    base = statistics.median(tps)
    lines.append(f"  baseline:  {base:.1f} tok/s   (median of {len(peers)} past run(s); "
                 f"range {min(tps):.1f}–{max(tps):.1f})")
    if base == 0:
        lines.append("  delta:     n/a (baseline throughput is zero)")
        return "\n".join(lines)

    d = (cur - base) / base * 100.0
    verdict = ("REGRESSION" if d <= -_REGRESSION_PCT
               else "improved" if d >= _REGRESSION_PCT
               else "in line with baseline")
    lines.append(f"  delta:     {d:+.1f}%  → {verdict}")

    ref = peers[0]
    diffs = [(f, ref.get(f), t.get(f)) for f in _DIFF_FIELDS if ref.get(f) != t.get(f)]
    if diffs:
        lines.append(f"  what changed vs most-recent baseline ({ref['run_id']}):")
        for f, was, now in diffs:
            lines.append(f"    - {f}: {was}  →  {now}")
    else:
        lines.append("  config identical to baseline — cause is runtime, not config: "
                     "check node health during the run (node_exec: throttle/PCIe/topo) "
                     "or contention from concurrent workloads.")
    return "\n".join(lines)


@tool
async def throughput_compare(run_id: str, limit: int = 10) -> str:
    """Compare benchmark run `run_id` against up to `limit` comparable past runs and report the throughput delta."""
    logger.info("tool.aistudio.throughput_compare", run_id=run_id, limit=limit)
    try:
        target_rows = await platform_api.benchmark_results(run_id)
    except Exception as exc:
        return f"Could not read the run's results from aistudio-server: {exc}"
    if not target_rows:
        return (f"No benchmark_results row for run {run_id!r} — the run may have failed "
                "before producing metrics. Check workload_history(workload_id=...) for the "
                "lifecycle/error, or get_task_logs for the benchmark output.")
    blocks = [await _compare_one(t, limit) for t in target_rows]
    return "\n\n".join(blocks)


def _seg(value: str) -> str:
    """Escape one id for a URL path segment."""
    return quote(str(value), safe="")


# Shared API client.
_client = httpx.AsyncClient(base_url=settings.aistudio_api_url, timeout=15.0)


async def _fetch_queues() -> list[dict]:
    """Read all queues from the RabbitMQ management API."""
    url = settings.rabbitmq_mgmt_url.rstrip("/") + "/api/queues"
    async with httpx.AsyncClient(timeout=10.0) as c:
        resp = await c.get(url, auth=(settings.rabbitmq_user, settings.rabbitmq_password))
        resp.raise_for_status()
        return resp.json()


@tool
async def queue_status(queue: str = "") -> str:
    """Show RabbitMQ queue depth and consumers, for queues whose name contains `queue` (default all)."""
    logger.info("tool.aistudio.queue_status", queue=queue or None)
    try:
        queues = await _fetch_queues()
    except httpx.ConnectError:
        return (f"Cannot reach the RabbitMQ management API at {settings.rabbitmq_mgmt_url}. "
                "The broker may be down (Celery can't queue or run tasks at all), or the "
                "management port is unreachable from the agent.")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            return "RabbitMQ management API rejected the configured credentials (HTTP 401)."
        return f"RabbitMQ management API returned HTTP {exc.response.status_code}."
    except Exception as exc:
        return f"Unexpected error querying RabbitMQ: {exc}"

    if queue:
        queues = [q for q in queues if queue in q.get("name", "")]
    if not queues:
        scope = f" matching {queue!r}" if queue else ""
        return (f"No queues{scope} on the broker. If the worker never declared its "
                "queue, that itself points at a worker that never started.")

    lines = [f"RabbitMQ queues ({settings.rabbitmq_mgmt_url}):"]
    for q in sorted(queues, key=lambda x: x.get("messages", 0) or 0, reverse=True):
        ready = q.get("messages_ready", 0) or 0
        unacked = q.get("messages_unacknowledged", 0) or 0
        consumers = q.get("consumers", 0) or 0
        name = q.get("name", "?")
        flag = ""
        if ready > 0 and consumers == 0:
            flag = "   ⚠️ NOTHING CONSUMING — worker down? (tasks will never start)"
        elif unacked > 0 and consumers > 0:
            flag = "   (in-flight — a consumer is holding messages)"
        lines.append(
            f"  • {name}: ready={ready} unacked={unacked} consumers={consumers}{flag}"
        )
    return "\n".join(lines)


@tool
async def check_service_health() -> str:
    """Check that the aistudio-server API is healthy and its database reachable."""
    logger.info("tool.aistudio.health_check_start")
    try:
        resp = await _client.get("/health")
        resp.raise_for_status()
        data = resp.json()
        return (
            f"API is healthy. Status: {data.get('status')}, "
            f"Database: {data.get('database')}"
        )
    except httpx.ConnectError:
        return (
            "CRITICAL: Cannot connect to aistudio-server at "
            f"{settings.aistudio_api_url}. The API may be down or unreachable."
        )
    except httpx.HTTPStatusError as exc:
        return (
            f"API returned HTTP {exc.response.status_code}: "
            f"{exc.response.text[:500]}"
        )
    except Exception as exc:
        return f"Unexpected error checking API health: {exc}"


@tool
async def get_task_status(task_id: str) -> str:
    """Get the state and any error of benchmark or Jupyter task `task_id` (wl-... or jup-...)."""
    logger.info("tool.aistudio.get_task_status", task_id=task_id)
    endpoint = platform_api.status_path(task_id)

    try:
        resp = await _client.get(endpoint)
        resp.raise_for_status()
        data = resp.json()

        state = data.get("state", "UNKNOWN")
        error = data.get("error_message")
        jupyter_url = data.get("jupyter_url")

        parts = [f"Task {task_id} is in state: {state}"]
        if error:
            parts.append(f"Error message: {error}")
        if jupyter_url:
            parts.append(f"Jupyter URL: {jupyter_url}")

        for key in ("workload_port", "updated_at"):
            if key in data:
                parts.append(f"{key}: {data[key]}")

        return "\n".join(parts)

    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return f"Task {task_id} not found. It may not exist or has been deleted."
        return f"Error querying task status: HTTP {exc.response.status_code}"
    except httpx.ConnectError:
        return (
            "Cannot connect to aistudio-server to check task status. "
            "The API may be down."
        )
    except Exception as exc:
        return f"Unexpected error checking task status: {exc}"


async def _collect_sse_logs(endpoint: str, recent: deque) -> str | None:
    """Drain the SSE log stream into `recent`; return the close reason if any."""
    prev_was_close = False
    async with _client.stream("GET", endpoint, timeout=30.0) as resp:
        resp.raise_for_status()
        async for raw in resp.aiter_lines():
            if raw.startswith("event:") and raw[6:].strip() == "close":
                prev_was_close = True
                continue
            if raw.startswith("data:"):
                payload = raw[5:].strip()
                if prev_was_close:
                    return payload
                recent.append(payload)
    return None


@tool
async def get_task_logs(task_id: str, tail: int = 50) -> str:
    """Fetch the last `tail` archived log lines of benchmark or Jupyter task `task_id`, highlighting errors."""
    logger.info("tool.aistudio.get_task_logs", task_id=task_id, tail=tail)
    endpoint = platform_api.logs_path(task_id)

    recent: deque[str] = deque(maxlen=max(1, min(tail, 5000)))
    closed_reason: str | None = None
    try:
        closed_reason = await asyncio.wait_for(
            _collect_sse_logs(endpoint, recent), timeout=20.0
        )
    except asyncio.TimeoutError:
        pass
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return f"Task {task_id} not found. It may not exist or has been deleted."
        return f"Error fetching logs: HTTP {exc.response.status_code}"
    except httpx.ConnectError:
        return "Cannot connect to aistudio-server to fetch logs."
    except Exception as exc:
        if not recent:
            return f"Unexpected error fetching logs: {exc}"

    lines = list(recent)
    if not lines:
        return f"No logs available yet for task {task_id}."

    error_lines = [
        ln for ln in lines if any(kw in ln.lower() for kw in
        ("error", "fail", "exception", "traceback", "refused", "denied",
         "timeout", "oom", "killed", "critical"))
    ]

    status_note = f"stream closed: {closed_reason}" if closed_reason else "task still running"
    result_parts = [f"Last {len(lines)} log line(s) for task {task_id} ({status_note}):"]
    if error_lines:
        result_parts.append(f"\n⚠️  {len(error_lines)} error/warning line(s):")
        for el in error_lines[-10:]:
            result_parts.append(f"  • {el}")

    result_parts.append("\nMost recent lines:")
    for ln in lines[-20:]:
        result_parts.append(f"  {ln}")

    return "\n".join(result_parts)


# catalog
_CATALOG_KINDS = ("models", "nodes", "servers", "gpu-types", "workload-types", "concurrencies",
                  "precisions", "input-tokens", "output-tokens")
# Max values shown per catalog.
_CATALOG_MAX = 40


def _scalars(raw) -> list[str]:
    """Convert an option endpoint response to display strings."""
    if isinstance(raw, dict):
        raw = next((v for v in raw.values() if isinstance(v, list)), [])
    out = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict):
            item = item.get("name") or item.get("id") or item.get("value")
        if item is not None:
            out.append(str(item))
    return out


CATALOG_HEADER = "aistudio catalog — what can be run:"


def _catalog_line(kind: str, vals: list[str]) -> str:
    """Format one catalog line."""
    if not vals:
        return f"  {kind}: none configured"
    shown, extra = vals[:_CATALOG_MAX], len(vals) - _CATALOG_MAX
    line = f"  {kind} ({len(vals)}): " + ", ".join(shown)
    return line + (f", … +{extra} more" if extra > 0 else "")


@tool
async def platform_catalog(kind: str = "") -> str:
    """List what can be run: `kind` is one of models, nodes, servers, gpu-types, workload-types, concurrencies, precisions, input-tokens, output-tokens (omit for all)."""
    kind = kind.strip().lower().replace("_", "-")
    if kind and kind not in _CATALOG_KINDS:
        return (f"No catalog named {kind!r}. Available: {', '.join(_CATALOG_KINDS)} "
                "(or omit the arg for all of them).")
    wanted = [kind] if kind else list(_CATALOG_KINDS)
    logger.info("tool.aistudio.platform_catalog", kind=kind or "all")

    async def one(k: str) -> str:
        try:
            resp = await _client.get(f"/api/v1/{k}")
            resp.raise_for_status()
            vals = _scalars(resp.json())
        except httpx.ConnectError:
            return f"  {k}: cannot reach aistudio at {settings.aistudio_api_url}"
        except Exception as exc:
            return f"  {k}: could not read ({exc})"
        return _catalog_line(k, vals)

    blocks = await asyncio.gather(*(one(k) for k in wanted))
    return CATALOG_HEADER + "\n" + "\n".join(blocks)


# benchmark results
async def _api_json(path: str, **params) -> str:
    """GET an API path and return its JSON text, or an error message."""
    query = {k: v for k, v in params.items() if v not in (None, "", 0)}
    try:
        resp = await _client.get(path, params=query)
        resp.raise_for_status()
        return json.dumps(resp.json(), indent=2, default=str)
    except httpx.ConnectError:
        return f"CRITICAL: cannot connect to aistudio-server at {settings.aistudio_api_url}."
    except httpx.HTTPStatusError as exc:
        return f"API returned HTTP {exc.response.status_code}: {exc.response.text[:500]}"
    except Exception as exc:
        return f"Unexpected error calling {path}: {exc}"


@tool
async def list_benchmarks(
    model: str = "", gpu_type: str = "", node_ip: str = "", concurrency: int = 0,
    precision: str = "", input_tokens: int = 0, output_tokens: int = 0,
    status: str = "", date: str = "", server_name: str = "", workload_type: str = "",
    limit: int = 100,
) -> str:
    """List completed benchmark results, newest first, optionally filtered by model, gpu_type, node_ip, concurrency, precision, input_tokens, output_tokens, status, date (YYYY-MM-DD), server_name, workload_type; `limit` is 1-1000."""
    logger.info("tool.aistudio.list_benchmarks", model=model, limit=limit)
    return await _api_json(
        "/api/v1/benchmarks", model=model, gpu_type=gpu_type, node_ip=node_ip,
        concurrency=concurrency, precision=precision, input_tokens=input_tokens,
        output_tokens=output_tokens, status=status, date=date, server_name=server_name,
        workload_type=workload_type, limit=limit)


@tool
async def get_benchmark(run_id: str) -> str:
    """Get benchmark run `run_id` (from list_benchmarks) in full with its sub-runs."""
    logger.info("tool.aistudio.get_benchmark", run_id=run_id)
    return await _api_json(f"/api/v1/benchmarks/{_seg(run_id)}")


@tool
async def summary(date: str = "") -> str:
    """Get dashboard totals (run counts, aggregates); `date` (YYYY-MM-DD) narrows to one day."""
    logger.info("tool.aistudio.summary", date=date)
    return await _api_json("/api/v1/summary", date=date)


@tool
async def model_config(model: str) -> str:
    """Get the default vLLM configuration aistudio would use for `model`."""
    logger.info("tool.aistudio.model_config", model=model)
    return await _api_json("/api/v1/models/config", model=model)
