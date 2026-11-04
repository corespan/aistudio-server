"""Platform data through aistudio-server's API, in the shape the formatters read."""

import asyncio
from datetime import datetime, timezone

import httpx
import pytest

from app.config import settings
from app.services import platform_api
from app.tools import aistudio as T

NOW = datetime.now(timezone.utc)


def _row(run_id="wl-1", sub=0, gpus=1, tps=100.0, status="success", model="m", ts=None, **kw):
    """A row as GET /api/v1/benchmarks and the sub_runs of GET /api/v1/benchmarks/{run_id} return it."""
    return {"run_id": run_id, "sub_run_index": sub, "model_name": model, "gpu_type": "H100",
            "gpu_count": gpus, "node_ips": ["10.0.0.5"], "precision": "fp16",
            "input_tokens": 128, "output_tokens": 64, "concurrency": 4,
            "workload_type": "llm", "status": status, "total_token_throughput": tps,
            "mean_ttft_ms": 10.0, "mean_tpot_ms": 2.5, "mean_e2el_ms": 20.0,
            "duration_seconds": 30.0, "started_at": (ts or NOW).isoformat(), **kw}


class _Server:
    def __init__(self):
        self.routes: dict = {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, body = self.routes.get(request.url.path, (404, {"detail": "Not Found"}))
        return httpx.Response(status, json=body)


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(settings, "aistudio_api_url", "http://aistudio")
    srv = _Server()
    real = httpx.AsyncClient
    monkeypatch.setattr(platform_api.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(srv),
                                          **{k: v for k, v in kw.items() if k != "transport"}))
    return srv


def run(coro):
    return asyncio.run(coro)


def test_results_come_back_in_the_shape_the_formatters_read(server):
    server.routes["/api/v1/benchmarks/wl-1"] = (200, {"run_id": "wl-1", "sub_runs": [_row(sub=0), _row(sub=1)]})
    rows = run(platform_api.benchmark_results("wl-1"))
    assert [r["sub_run_index"] for r in rows] == [0, 1]
    r = rows[0]
    assert (r["gpu_count"], r["node_ips"], r["mean_tpot_ms"]) == (1, ["10.0.0.5"], 2.5)
    assert isinstance(r["started_at"], datetime)


def test_an_unknown_run_is_an_empty_list_not_an_error(server):
    assert run(platform_api.benchmark_results("nope")) == []


def test_an_id_cannot_walk_to_another_endpoint(server):
    run(platform_api.workload("../all"))
    assert server.requests[0].url.raw_path.decode().startswith("/api/v1/benchmarks/..%2Fall/status")


def test_a_jupyter_id_is_looked_up_on_the_jupyter_route(server):
    server.routes["/api/v1/jupyter/instances/jup-1/status"] = (200, {
        "task_id": "jup-1", "state": "READY", "jupyter_url": "https://h/jup-1/", "updated_at": NOW.isoformat()})
    wl = run(platform_api.workload("jup-1"))
    assert (wl["workload_id"], wl["state"], wl["jupyter_url"]) == ("jup-1", "READY", "https://h/jup-1/")


def test_recent_runs_groups_sub_runs_and_filters_failures(server):
    server.routes["/api/v1/benchmarks"] = (200, [
        _row("a", 0), _row("a", 1, status="failed"), _row("b", 0)])
    runs = run(platform_api.recent_runs(only_failed=False, limit=10))
    assert {r["run_id"]: (r["sub_runs"], r["all_success"]) for r in runs} == {"a": (2, False), "b": (1, True)}
    failed = run(platform_api.recent_runs(only_failed=True, limit=10))
    assert [r["run_id"] for r in failed] == ["a"]


def test_an_error_names_the_route(server):
    server.routes["/api/v1/benchmarks"] = (500, {"detail": "boom"})
    with pytest.raises(platform_api.PlatformApiError, match="500 for /api/v1/benchmarks"):
        run(platform_api.recent_runs(False, 10))


def test_comparable_results_keep_only_the_identical_config(server):
    target = {"run_id": "t", "model_name": "m", "gpu_type": "H100", "gpu_count": 1, "precision": "fp16",
              "concurrency": 4, "input_tokens": 128, "output_tokens": 64, "workload_type": "llm"}
    server.routes["/api/v1/benchmarks"] = (200, [
        _row("t"),                                   # the target itself
        _row("p1"), _row("p2", gpus=2),              # a different GPU count
        _row("p3", tps=None)])                       # no metric
    peers = run(platform_api.comparable_results(target, 10))
    assert [p["run_id"] for p in peers] == ["p1"]
    query = server.requests[0].url.params
    assert (query["status"], query["model"], query["workload_type"]) == ("success", "m", "llm")


def test_search_matches_id_and_model(server):
    server.routes["/api/v1/benchmarks"] = (200, [_row("t4-tinyllama-1b-tp1", model="tinyllama"),
                                                 _row("wl-9", model="TinyLlama"), _row("x", model="other")])
    found = run(platform_api.search_runs("TinyLlama"))
    assert {f["id"] for f in found} == {"t4-tinyllama-1b-tp1", "wl-9"}


# ── the tools on top
def test_workload_history_for_one_task_reads_its_state_and_error(server):
    server.routes["/api/v1/benchmarks/wl-1/status"] = (200, {
        "workload_id": "wl-1", "state": "FAILED", "error_message": "node unreachable",
        "updated_at": NOW.isoformat()})
    out = run(T.workload_history.ainvoke({"workload_id": "wl-1"}))
    assert "FAILED" in out and "node unreachable" in out and "get_task_logs" in out


def test_workload_history_falls_back_to_completed_results(server):
    server.routes["/api/v1/benchmarks/wl-2"] = (200, {"run_id": "wl-2", "sub_runs": [_row("wl-2")]})
    out = run(T.workload_history.ainvoke({"workload_id": "wl-2"}))
    assert "COMPLETED benchmark run" in out and "SUCCEEDED" in out and "10.0.0.5" in out


def test_workload_history_lists_recent_runs(server):
    server.routes["/api/v1/benchmarks"] = (200, [_row("wl-3")])
    out = run(T.workload_history.ainvoke({}))
    assert "wl-3" in out


def test_throughput_compare_flags_a_regression(server):
    server.routes["/api/v1/benchmarks/wl-1"] = (200, {"run_id": "wl-1", "sub_runs": [_row("wl-1", tps=50.0)]})
    server.routes["/api/v1/benchmarks"] = (200, [_row("p1", tps=100.0), _row("p2", tps=110.0)])
    out = run(T.throughput_compare.ainvoke({"run_id": "wl-1"}))
    assert "REGRESSION" in out and "median of 2" in out
