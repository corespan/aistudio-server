"""Platform data read through aistudio-server's API."""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import quote

import httpx

from app.config import settings


class PlatformApiError(Exception):
    """aistudio-server could not answer, or does not serve the endpoint."""


def _ts(value) -> datetime | None:
    """An ISO timestamp from the API as a datetime (None stays None)."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


async def _get(path: str, params: dict | None = None) -> httpx.Response:
    base = settings.aistudio_api_url.rstrip("/")
    async with httpx.AsyncClient(timeout=15.0) as client:
        return await client.get(f"{base}{path}", params=params)


def _seg(value: str) -> str:
    """One id escaped for a URL path segment."""
    value = str(value)
    # httpx resolves dot segments: an id of ".." would name the parent route (/api/v1/status).
    if value.strip() in ("", ".", ".."):
        raise PlatformApiError(f"{value!r} is not a task or run id")
    return quote(value, safe="")


def _rows(r: httpx.Response, what: str) -> list | dict:
    if r.status_code == 404:
        raise PlatformApiError(f"this aistudio-server does not serve {what}")
    if r.status_code >= 400:
        raise PlatformApiError(f"aistudio-server answered {r.status_code} for {what}")
    return r.json()


def status_path(task_id: str) -> str:
    """The status route for a benchmark (wl-...) or Jupyter (jup-...) task."""
    if task_id.startswith("jup-"):
        return f"/api/v1/jupyter/instances/{_seg(task_id)}/status"
    return f"/api/v1/benchmarks/{_seg(task_id)}/status"


def logs_path(task_id: str) -> str:
    """The SSE log route for a benchmark (wl-...) or Jupyter (jup-...) task."""
    if task_id.startswith("jup-"):
        return f"/api/v1/jupyter/instances/{_seg(task_id)}/logs/stream"
    return f"/api/v1/benchmarks/{_seg(task_id)}/logs/stream"


async def workload(workload_id: str) -> dict | None:
    """One workload's state and error by its id (wl-… / jup-…), or None if unknown."""
    r = await _get(status_path(workload_id))
    if r.status_code == 404:
        return None
    row = _rows(r, "the task status route")
    return {**row, "workload_id": row.get("workload_id") or workload_id,
            "updated_at": _ts(row.get("updated_at")), "created_at": _ts(row.get("created_at"))}


def _bench(row: dict) -> dict:
    """A result row in the shape the formatters read."""
    return {**row, "started_at": _ts(row.get("started_at")), "node_ips": row.get("node_ips") or []}


async def benchmark_results(run_id: str) -> list[dict]:
    """A run's result rows, one per sub-run (sweep). Empty when the run has none."""
    r = await _get(f"/api/v1/benchmarks/{_seg(run_id)}")
    if r.status_code == 404:
        return []
    body = _rows(r, "/api/v1/benchmarks/{run_id}")
    return [_bench(x) for x in body.get("sub_runs", [])]


# Max rows GET /api/v1/benchmarks returns (no offset to page further).
ROW_CAP = 1000


async def _result_rows(**filters) -> list[dict]:
    """The newest ROW_CAP result rows matching `filters`, each with a run_id."""
    params = {k: v for k, v in filters.items() if v not in (None, "")}
    params["limit"] = ROW_CAP
    rows = _rows(await _get("/api/v1/benchmarks", params), "/api/v1/benchmarks")
    return [r for r in rows if r.get("run_id")]


async def recent_runs(only_failed: bool, limit: int) -> list[dict]:
    """The latest completed runs, grouped from result rows, newest first."""
    runs: dict[str, dict] = {}
    for row in await _result_rows():
        run = runs.setdefault(row["run_id"], {
            "run_id": row["run_id"], "model_name": row.get("model_name"),
            "gpu_type": row.get("gpu_type"), "gpu_count": row.get("gpu_count"),
            "sub_runs": 0, "all_success": True, "started_at": None})
        run["sub_runs"] += 1
        run["all_success"] = run["all_success"] and row.get("status") == "success"
        started = _ts(row.get("started_at"))
        if started and (run["started_at"] is None or started > run["started_at"]):
            run["started_at"] = started
    out = [r for r in runs.values() if not (only_failed and r["all_success"])]
    out.sort(key=lambda r: r["started_at"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return out[:max(1, min(int(limit), 100))]


async def jupyter_instances() -> list[dict]:
    """Jupyter instances aistudio-server lists (READY ones and the persistent one)."""
    body = _rows(await _get("/api/v1/jupyter/instances"), "/api/v1/jupyter/instances")
    return body if isinstance(body, list) else body.get("instances", [])


async def comparable_results(target: dict, limit: int) -> list[dict]:
    """Past successful runs with the identical benchmark config."""
    rows = await _result_rows(
        model=target["model_name"], gpu_type=target["gpu_type"],
        concurrency=target["concurrency"], precision=target["precision"],
        input_tokens=target["input_tokens"], output_tokens=target["output_tokens"],
        workload_type=target.get("workload_type"), status="success")
    peers = [_bench(r) for r in rows
             if r.get("run_id") != target["run_id"]
             and r.get("gpu_count") == target["gpu_count"]
             and r.get("total_token_throughput") is not None]
    return peers[:max(1, min(int(limit), 50))]


async def search_runs(term: str, limit: int = 10) -> list[dict]:
    """Fuzzy 'did you mean' across completed runs, by id and model."""
    needle = term.lower()
    out: dict[str, dict] = {}
    for row in await _result_rows():
        if needle in str(row.get("run_id", "")).lower() or needle in str(row.get("model_name", "")).lower():
            out.setdefault(row["run_id"], {
                "id": row["run_id"], "model_name": row.get("model_name"),
                "state": row.get("status"), "gpu_type": row.get("gpu_type"),
                "gpu_count": row.get("gpu_count")})
    return list(out.values())[:max(1, min(int(limit), 100))]
