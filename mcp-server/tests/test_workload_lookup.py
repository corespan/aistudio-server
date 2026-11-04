"""Completed benchmark runs are looked up in benchmark_results."""

import pytest

from app.tools.aistudio import (
    _format_completed_run,
    _format_no_exact_match,
    _format_run_list,
    _format_workload_detail,
)

_ROWS = [
    {"run_id": "mi210-llama31-70b-tp8-bench", "model_name": "llama3.1-70b-instruct",
     "gpu_type": "mi210", "gpu_count": 8, "precision": "fp16", "node_ips": ["gpu-node-c"],
     "sub_run_index": 0, "concurrency": 4, "input_tokens": 1023, "output_tokens": 1024,
     "status": "success", "total_token_throughput": 812.4},
    {"run_id": "mi210-llama31-70b-tp8-bench", "model_name": "llama3.1-70b-instruct",
     "gpu_type": "mi210", "gpu_count": 8, "precision": "fp16", "node_ips": ["gpu-node-c"],
     "sub_run_index": 1, "concurrency": 8, "input_tokens": 1023, "output_tokens": 1024,
     "status": "success", "total_token_throughput": 905.1},
]


def test_completed_success_run_reads_as_succeeded_not_missing():
    out = _format_completed_run("mi210-llama31-70b-tp8-bench", _ROWS)
    assert "COMPLETED benchmark run" in out
    assert "SUCCEEDED" in out
    assert "does NOT mean the run doesn't exist" in out
    assert "throughput_compare" in out
    assert "812" in out and "905" in out
    assert "gpu-node-c" in out


def test_sub_run_breakdown_is_addressable():
    out = _format_completed_run("mi210-llama31-70b-tp8-bench", _ROWS)
    assert "sub_run 0: concurrency=4" in out
    assert "sub_run 1: concurrency=8" in out
    assert "no separate per-sub-run id" in out
    assert "not a regression" in out
    assert "MIN sub_run 0" in out and "MAX sub_run 1" in out


def test_partial_failure_points_to_detail():
    rows = [dict(_ROWS[0], status="success"), dict(_ROWS[1], status="error")]
    out = _format_completed_run("r", rows)
    assert "Not all sub-runs succeeded" in out


def test_no_match_lists_candidates():
    matches = [{"id": "mi210-llama31-70b-tp8", "state": "success",
                "model_name": "llama3.1-70b-instruct", "gpu_type": "mi210", "gpu_count": 8}]
    out = _format_no_exact_match("mi210-llama31-70b-tpX", matches)
    assert "mi210-llama31-70b-tp8" in out and "success" in out


def test_no_match_at_all_is_honest():
    out = _format_no_exact_match("bogus-run", [])
    assert "No workload or benchmark run matches" in out
    assert "does not exist" not in out.lower()


def test_run_list_shows_completed_benchmark_runs():
    runs = [
        {"run_id": "mi210-llama31-70b-tp8-bench", "model_name": "llama3.1-70b-instruct",
         "gpu_type": "mi210", "gpu_count": 8, "sub_runs": 3, "all_success": True,
         "started_at": None},
        {"run_id": "rtx5090-tp4-bf16-v1", "model_name": "qwen2.5-32b-instruct",
         "gpu_type": "rtx5090", "gpu_count": 4, "sub_runs": 2, "all_success": False,
         "started_at": None},
    ]
    out = _format_run_list(runs, only_failed=False)
    assert "mi210-llama31-70b-tp8-bench" in out and "[success]" in out
    assert "rtx5090-tp4-bf16-v1" in out and "had failures" in out
    assert "mi210×8" in out


def test_a_failed_task_points_to_its_logs_without_guessing_a_cause():
    wl = {"workload_id": "wl-x", "state": "FAILED", "updated_at": None,
          "error_message": "Node inspection failed: nvidia-smi: command not found"}
    out = _format_workload_detail(wl)
    assert "nvidia-smi" in out and "get_task_logs" in out
    for gone in ("ROOT CAUSE", "HIGH confidence", "conclusive", "no further tools"):
        assert gone not in out


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


@pytest.mark.parametrize("sub, role, shown", [
    ("bob", "viewer", True), ("carol", "operator", False), ("root", "admin", True), (None, None, False)])
async def test_a_notebook_url_is_shown_to_its_creator_or_an_admin(monkeypatch, sub, role, shown):
    import httpx

    from app.services import request_ctx
    from app.tools import aistudio as T
    from conftest import ident

    async def get(path, **kw):
        return httpx.Response(200, json={"state": "READY", "created_by": "bob",
                                         "jupyter_url": "http://10.0.0.6:8888/lab"},
                              request=httpx.Request("GET", "http://api" + path))

    monkeypatch.setattr(T._client, "get", get)
    token = request_ctx.caller.set(ident(sub=sub, role=role) if sub else None)
    try:
        out = await T.get_task_status.ainvoke({"task_id": "jup-20260101-abc123"})
    finally:
        request_ctx.caller.reset(token)
    assert ("10.0.0.6:8888" in out) is shown, out
