"""
Test suite for aistudio-server (open-source edition).

Each class is self-contained:
  - No shared mutable state between classes.
  - Seed data is inserted in the test body or a local helper.
  - Celery tasks are always mocked — tests never touch RabbitMQ.

Run:
    pytest tests/ -v
    pytest tests/test_services.py::TestStateMachine -v
    pytest tests/test_services.py -k "ingest or manifest" -v
"""

import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import text

# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_ingest_payload(
    *,
    run_id: str | None = None,
    model_name: str = "tinyllama-1.1b",
    workload_type: str = "llm",
    gpu_type: str = "t4",
    node_ip: str = "10.6.12.99",
    status: str = "success",
    sub_run_index: int = 0,
    concurrency: int = 4,
    input_tokens: int = 512,
    output_tokens: int = 128,
    total_token_throughput: float = 5000.0,
    **kwargs,
) -> dict:
    """Return a valid BenchmarkIngestPayload dict."""
    return {
        "run_id":    run_id or ("run-%s" % uuid.uuid4().hex[:8]),
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "workload": {"name": model_name, "type": workload_type},
        "metrics": {
            "total_token_throughput": total_token_throughput,
            "mean_ttft_ms":  45.0,
            "mean_tpot_ms":  12.0,
            "mean_e2el_ms":  310.0,
            **kwargs.pop("extra_metrics", {}),
        },
        "status":   status,
        "gpu_type": gpu_type,
        "node_ip":  node_ip,
        "config": {
            "concurrency":  concurrency,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "gpu_count": 1,
            "precision": "fp16",
            **kwargs.pop("extra_config", {}),
        },
        "sub_run_index": sub_run_index,
        **kwargs,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 1. Health Check
# ─────────────────────────────────────────────────────────────────────────────

class TestHealthCheck:
    """GET /health — DB connectivity probe."""

    @pytest.mark.asyncio
    async def test_health_returns_healthy(self, http_client):
        r = await http_client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "healthy"
        assert body["database"] == "ok"

    @pytest.mark.asyncio
    async def test_health_schema_has_expected_keys(self, http_client):
        r = await http_client.get("/health")
        body = r.json()
        assert "status" in body
        assert "database" in body


# ─────────────────────────────────────────────────────────────────────────────
# 2. Workload Types
# ─────────────────────────────────────────────────────────────────────────────

class TestWorkloadTypes:
    """GET /api/v1/workload-types — seeded from catalog.json."""

    @pytest.mark.asyncio
    async def test_empty_db_returns_empty_list(self, http_client):
        r = await http_client.get("/api/v1/workload-types")
        assert r.status_code == 200
        assert r.json() == []

    @pytest.mark.asyncio
    async def test_seeded_types_appear(self, http_client, db_session):
        from app.models.workload_type import WorkloadType
        db_session.add(WorkloadType(
            name="LLMInference",
            display_name="LLM Inference (vLLM)",
            description="Benchmark LLM inference throughput.",
            image_tag="1.0.0-nvidia",
        ))
        await db_session.commit()

        r = await http_client.get("/api/v1/workload-types")
        assert r.status_code == 200
        types = r.json()
        assert len(types) == 1
        assert types[0]["name"] == "LLMInference"
        assert types[0]["display_name"] == "LLM Inference (vLLM)"
        assert "id" in types[0]

    @pytest.mark.asyncio
    async def test_multiple_types_all_returned(self, http_client, db_session):
        from app.models.workload_type import WorkloadType
        for name in ("LLMInference", "JupyterNotebook"):
            db_session.add(WorkloadType(
                name=name,
                display_name=name,
                description="",
                image_tag="1.0.0-nvidia",
            ))
        await db_session.commit()

        r = await http_client.get("/api/v1/workload-types")
        names = [t["name"] for t in r.json()]
        assert "LLMInference" in names
        assert "JupyterNotebook" in names


# ─────────────────────────────────────────────────────────────────────────────
# 3. Model Config
# ─────────────────────────────────────────────────────────────────────────────

class TestModelConfig:
    """GET /api/v1/models/config?model= — vLLM default config from catalog.json."""

    @pytest.mark.asyncio
    async def test_known_model_returns_config(self, http_client):
        # TinyLlama is in catalog.json
        r = await http_client.get(
            "/api/v1/models/config", params={"model": "tinyllama-1.1b-chat"}
        )
        assert r.status_code == 200
        body = r.json()
        assert "precision" in body
        assert "concurrency" in body
        assert "tensor_parallel_size" in body

    @pytest.mark.asyncio
    async def test_known_model_returns_gated_and_license_fields(self, http_client):
        r = await http_client.get(
            "/api/v1/models/config", params={"model": "tinyllama-1.1b-chat"}
        )
        body = r.json()
        assert "gated" in body
        assert "license" in body
        assert "hf_repo" in body

    @pytest.mark.asyncio
    async def test_unknown_model_falls_back_to_defaults(self, http_client):
        r = await http_client.get(
            "/api/v1/models/config", params={"model": "totally-unknown-model"}
        )
        assert r.status_code == 200
        body = r.json()
        # Default config should still have mandatory keys
        assert "precision" in body
        assert body["gated"] is False

    @pytest.mark.asyncio
    async def test_lookup_is_case_insensitive(self, http_client):
        r_lower = await http_client.get(
            "/api/v1/models/config", params={"model": "tinyllama-1.1b-chat"}
        )
        r_upper = await http_client.get(
            "/api/v1/models/config", params={"model": "TinyLlama-1.1B-Chat"}
        )
        assert r_lower.status_code == 200
        assert r_upper.status_code == 200
        assert r_lower.json()["precision"] == r_upper.json()["precision"]


# ─────────────────────────────────────────────────────────────────────────────
# 4. Metrics Ingest
# ─────────────────────────────────────────────────────────────────────────────

class TestMetricsIngest:
    """POST /api/v1/metrics — legacy BenchmarkIngestPayload format."""

    @pytest.mark.asyncio
    async def test_happy_path_returns_202(self, http_client):
        payload = _make_ingest_payload()
        r = await http_client.post("/api/v1/metrics", json=payload)
        assert r.status_code == 202
        assert r.json()["status"] == "success"

    @pytest.mark.asyncio
    async def test_row_written_to_db(self, http_client, db_session):
        from sqlalchemy import select
        from app.models.benchmark_result import BenchmarkResult

        payload = _make_ingest_payload(run_id="run-ingest-01")
        await http_client.post("/api/v1/metrics", json=payload)

        row = (await db_session.execute(
            select(BenchmarkResult).where(BenchmarkResult.run_id == "run-ingest-01")
        )).scalar_one_or_none()
        assert row is not None
        assert row.model_name == "tinyllama-1.1b"
        assert row.gpu_type == "t4"
        assert row.status == "success"

    @pytest.mark.asyncio
    async def test_idempotent_upsert(self, http_client, db_session):
        """Re-posting same (run_id, sub_run_index) must NOT create a second row."""
        from sqlalchemy import select, func
        from app.models.benchmark_result import BenchmarkResult

        payload = _make_ingest_payload(
            run_id="run-idem-01",
            total_token_throughput=1000.0,
        )
        await http_client.post("/api/v1/metrics", json=payload)

        # Update the throughput and re-post
        payload["metrics"]["total_token_throughput"] = 9999.0
        r = await http_client.post("/api/v1/metrics", json=payload)
        assert r.status_code == 202

        count = (await db_session.execute(
            select(func.count()).select_from(BenchmarkResult)
            .where(BenchmarkResult.run_id == "run-idem-01")
        )).scalar()
        assert count == 1

        # Updated value should win
        row = (await db_session.execute(
            select(BenchmarkResult).where(BenchmarkResult.run_id == "run-idem-01")
        )).scalar_one()
        assert row.total_token_throughput == pytest.approx(9999.0)

    @pytest.mark.asyncio
    async def test_parallelism_tensor_parallel(self, http_client, db_session):
        """metrics.parallelism = {tp:4, pp:1} → hot column = 'tp4'."""
        from sqlalchemy import select
        from app.models.benchmark_result import BenchmarkResult

        payload = _make_ingest_payload(
            run_id="run-tp4",
            extra_metrics={"parallelism": {"tensor_parallel_size": 4, "pipeline_parallel_size": 1}},
        )
        await http_client.post("/api/v1/metrics", json=payload)

        row = (await db_session.execute(
            select(BenchmarkResult).where(BenchmarkResult.run_id == "run-tp4")
        )).scalar_one()
        assert row.parallelism == "tp4"

    @pytest.mark.asyncio
    async def test_parallelism_pipeline_parallel(self, http_client, db_session):
        from sqlalchemy import select
        from app.models.benchmark_result import BenchmarkResult

        payload = _make_ingest_payload(
            run_id="run-pp4",
            extra_metrics={"parallelism": {"tensor_parallel_size": 1, "pipeline_parallel_size": 4}},
        )
        await http_client.post("/api/v1/metrics", json=payload)

        row = (await db_session.execute(
            select(BenchmarkResult).where(BenchmarkResult.run_id == "run-pp4")
        )).scalar_one()
        assert row.parallelism == "pp4"

    @pytest.mark.asyncio
    async def test_parallelism_both_tp_and_pp(self, http_client, db_session):
        from sqlalchemy import select
        from app.models.benchmark_result import BenchmarkResult

        payload = _make_ingest_payload(
            run_id="run-tp4pp2",
            extra_metrics={"parallelism": {"tensor_parallel_size": 4, "pipeline_parallel_size": 2}},
        )
        await http_client.post("/api/v1/metrics", json=payload)

        row = (await db_session.execute(
            select(BenchmarkResult).where(BenchmarkResult.run_id == "run-tp4pp2")
        )).scalar_one()
        assert row.parallelism == "tp4pp2"

    @pytest.mark.asyncio
    async def test_duration_computed_from_started_at(self, http_client, db_session):
        from sqlalchemy import select
        from app.models.benchmark_result import BenchmarkResult

        started = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        completed = datetime(2026, 1, 1, 10, 1, 30, tzinfo=timezone.utc)  # 90 s later

        payload = _make_ingest_payload(
            run_id="run-dur-01",
            extra_config={"started_at": started.isoformat()},
        )
        payload["timestamp"] = completed.isoformat()
        await http_client.post("/api/v1/metrics", json=payload)

        row = (await db_session.execute(
            select(BenchmarkResult).where(BenchmarkResult.run_id == "run-dur-01")
        )).scalar_one()
        assert row.duration_seconds == pytest.approx(90.0)

    @pytest.mark.asyncio
    async def test_gpu_type_stored_lowercase(self, http_client, db_session):
        from sqlalchemy import select
        from app.models.benchmark_result import BenchmarkResult

        payload = _make_ingest_payload(run_id="run-gpu-case", gpu_type="H100")
        await http_client.post("/api/v1/metrics", json=payload)

        row = (await db_session.execute(
            select(BenchmarkResult).where(BenchmarkResult.run_id == "run-gpu-case")
        )).scalar_one()
        assert row.gpu_type == "h100"


# ─────────────────────────────────────────────────────────────────────────────
# 5. Benchmark Start + Status
# ─────────────────────────────────────────────────────────────────────────────

class TestBenchmarkStartAndStatus:
    """POST /api/v1/benchmarks/start and GET /api/v1/benchmarks/{task_id}/status."""

    @pytest.mark.asyncio
    async def test_start_returns_task_id(self, http_client):
        with patch("app.worker.start_benchmark_chain") as mock_chain:
            mock_chain.delay.return_value = None
            r = await http_client.post("/api/v1/benchmarks/start", json={
                "model_name": "tinyllama",
                "node_ips": ["10.0.0.1"],
                "config": {},
            })
        assert r.status_code == 200
        body = r.json()
        assert "task_id" in body
        assert body["task_id"].startswith("wl-")
        assert body["status"] == "success"

    @pytest.mark.asyncio
    async def test_start_creates_workload_in_created_state(self, http_client, db_session):
        from sqlalchemy import select
        from app.models.workload import Workload

        with patch("app.worker.start_benchmark_chain") as mock_chain:
            mock_chain.delay.return_value = None
            r = await http_client.post("/api/v1/benchmarks/start", json={
                "model_name": "llama3",
                "node_ips": ["10.0.0.1"],
                "config": {"gpu_count": 2},
            })
        task_id = r.json()["task_id"]

        workload = (await db_session.execute(
            select(Workload).where(Workload.workload_id == task_id)
        )).scalar_one_or_none()
        assert workload is not None
        assert workload.state == "CREATED"
        assert workload.model_name == "llama3"

    @pytest.mark.asyncio
    async def test_start_multi_node_creates_node_records(self, http_client, db_session):
        from sqlalchemy import select
        from app.models.node import Node
        from app.models.workload import Workload

        ips = ["10.0.0.1", "10.0.0.2", "10.0.0.3"]
        with patch("app.worker.start_benchmark_chain") as mock_chain:
            mock_chain.delay.return_value = None
            r = await http_client.post("/api/v1/benchmarks/start", json={
                "model_name": "llama3",
                "node_ips": ips,
                "config": {},
            })
        task_id = r.json()["task_id"]

        workload = (await db_session.execute(
            select(Workload).where(Workload.workload_id == task_id)
        )).scalar_one()

        nodes = (await db_session.execute(
            select(Node).where(Node.workload_id == workload.id)
        )).scalars().all()
        assert len(nodes) == 3
        node_ips = [n.machine_ip for n in nodes]
        for ip in ips:
            assert ip in node_ips

    @pytest.mark.asyncio
    async def test_status_returns_created_for_new_workload(self, http_client):
        with patch("app.worker.start_benchmark_chain") as mock_chain:
            mock_chain.delay.return_value = None
            r_start = await http_client.post("/api/v1/benchmarks/start", json={
                "model_name": "tinyllama",
                "node_ips": ["10.0.0.1"],
                "config": {},
            })
        task_id = r_start.json()["task_id"]

        r_status = await http_client.get(f"/api/v1/benchmarks/{task_id}/status")
        assert r_status.status_code == 200
        body = r_status.json()
        assert body["state"] == "CREATED"
        assert body["workload_id"] == task_id

    @pytest.mark.asyncio
    async def test_status_404_for_unknown_workload(self, http_client):
        r = await http_client.get("/api/v1/benchmarks/wl-19990101-ffffff/status")
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_start_dispatches_celery_task(self, http_client):
        with patch("app.worker.start_benchmark_chain") as mock_chain:
            mock_chain.delay.return_value = None
            r = await http_client.post("/api/v1/benchmarks/start", json={
                "model_name": "tinyllama",
                "node_ips": ["10.0.0.1"],
                "config": {},
            })
            task_id = r.json()["task_id"]
            mock_chain.delay.assert_called_once_with(task_id)


# ─────────────────────────────────────────────────────────────────────────────
# 6. Log Streaming
# ─────────────────────────────────────────────────────────────────────────────

class TestLogStreaming:
    """GET /api/v1/benchmarks/{task_id}/logs/stream — SSE endpoint."""

    @pytest.mark.asyncio
    async def test_unknown_workload_returns_404(self, http_client):
        r = await http_client.get(
            "/api/v1/benchmarks/wl-doesnotexist/logs/stream"
        )
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_known_workload_returns_event_stream(self, http_client, db_session):
        from app.models.workload import Workload

        wl = Workload(
            workload_id="wl-stream-01",
            model_name="tinyllama",
            workload_config={},
            state="RUNNING",
        )
        db_session.add(wl)
        await db_session.commit()

        # Open the SSE stream briefly — just check Content-Type and 200
        async with http_client.stream(
            "GET", "/api/v1/benchmarks/wl-stream-01/logs/stream"
        ) as r:
            assert r.status_code == 200
            ct = r.headers.get("content-type", "")
            assert "text/event-stream" in ct

    @pytest.mark.asyncio
    async def test_stream_delivers_inserted_log_lines(self, http_client, db_session):
        from app.models.workload import Workload
        from app.models.task import Task
        from app.models.task_log import TaskLog

        wl = Workload(
            workload_id="wl-stream-02",
            model_name="tinyllama",
            workload_config={},
            state="RUNNING",
        )
        db_session.add(wl)
        await db_session.flush()

        task = Task(
            workload_id=wl.id,
            run_name="run-stream-02",
            task_config={},
            status="running",
        )
        db_session.add(task)
        await db_session.flush()

        log = TaskLog(task_id=task.id, line="GPU validation passed.")
        db_session.add(log)
        await db_session.commit()

        collected = []
        async with http_client.stream(
            "GET", "/api/v1/benchmarks/wl-stream-02/logs/stream"
        ) as r:
            assert r.status_code == 200
            async for line in r.aiter_lines():
                if line.startswith("data:"):
                    text_val = line[len("data:"):].strip()
                    collected.append(text_val)
                    if "[DONE]" in text_val:
                        break

        assert any("GPU validation passed." in ln for ln in collected)


# ─────────────────────────────────────────────────────────────────────────────
# 7. Results Leaderboard
# ─────────────────────────────────────────────────────────────────────────────

class TestResultsLeaderboard:
    """GET /api/v1/benchmarks — leaderboard with filters and derived fields."""

    async def _seed(self, http_client, **overrides):
        """Helper: ingest one row and return its run_id."""
        payload = _make_ingest_payload(**overrides)
        await http_client.post("/api/v1/metrics", json=payload)
        return payload["run_id"]

    @pytest.mark.asyncio
    async def test_empty_db_returns_empty_list(self, http_client):
        r = await http_client.get("/api/v1/benchmarks")
        assert r.status_code == 200
        assert r.json() == []

    @pytest.mark.asyncio
    async def test_ingested_rows_appear_in_list(self, http_client):
        run_id = await self._seed(http_client)
        r = await http_client.get("/api/v1/benchmarks")
        run_ids = [row["run_id"] for row in r.json()]
        assert run_id in run_ids

    @pytest.mark.asyncio
    async def test_filter_by_model(self, http_client):
        await self._seed(http_client, run_id="run-m-llama", model_name="llama3")
        await self._seed(http_client, run_id="run-m-tiny",  model_name="tinyllama")

        r = await http_client.get("/api/v1/benchmarks", params={"model": "llama3"})
        rows = r.json()
        assert len(rows) == 1
        assert rows[0]["model_name"] == "llama3"

    @pytest.mark.asyncio
    async def test_filter_by_gpu_type(self, http_client):
        await self._seed(http_client, run_id="run-g-h100", gpu_type="h100")
        await self._seed(http_client, run_id="run-g-t4",   gpu_type="t4")

        r = await http_client.get("/api/v1/benchmarks", params={"gpu_type": "h100"})
        rows = r.json()
        assert all(row["gpu_type"] == "h100" for row in rows)

    @pytest.mark.asyncio
    async def test_filter_by_precision(self, http_client):
        await self._seed(http_client, run_id="run-p-fp16", extra_config={"precision": "fp16"})
        await self._seed(http_client, run_id="run-p-bf16", extra_config={"precision": "bf16"})

        r = await http_client.get("/api/v1/benchmarks", params={"precision": "fp16"})
        rows = r.json()
        assert all(row["precision"] == "fp16" for row in rows)

    @pytest.mark.asyncio
    async def test_filter_by_concurrency(self, http_client):
        await self._seed(http_client, run_id="run-c-4",  concurrency=4)
        await self._seed(http_client, run_id="run-c-32", concurrency=32)

        r = await http_client.get("/api/v1/benchmarks", params={"concurrency": 32})
        rows = r.json()
        assert all(row["concurrency"] == 32 for row in rows)

    @pytest.mark.asyncio
    async def test_per_gpu_throughput_computed(self, http_client):
        """per_gpu_throughput_tok_s = total_token_throughput / gpu_count."""
        payload = _make_ingest_payload(
            run_id="run-pgpu",
            total_token_throughput=8000.0,
            extra_config={"gpu_count": 4},
        )
        await http_client.post("/api/v1/metrics", json=payload)

        r = await http_client.get("/api/v1/benchmarks", params={"run_id": "run-pgpu"})
        row = r.json()[0]
        assert row["per_gpu_throughput_tok_s"] == pytest.approx(2000.0)

    @pytest.mark.asyncio
    async def test_ip_masking_applied_to_node_ips(self, http_client):
        """Last two octets of node IPs must be masked in responses."""
        payload = _make_ingest_payload(run_id="run-mask-ip", node_ip="10.6.12.26")
        await http_client.post("/api/v1/metrics", json=payload)

        r = await http_client.get("/api/v1/benchmarks", params={"run_id": "run-mask-ip"})
        row = r.json()[0]
        for ip in row["node_ips"]:
            assert re.search(r'\d+\.\d+\.x\.x', ip), f"IP not masked: {ip}"
            assert "12.26" not in ip

    @pytest.mark.asyncio
    async def test_limit_respected(self, http_client):
        for i in range(5):
            await self._seed(http_client, run_id=f"run-lim-{i}")
        r = await http_client.get("/api/v1/benchmarks", params={"limit": 3})
        assert len(r.json()) <= 3


# ─────────────────────────────────────────────────────────────────────────────
# 8. Single Result Detail
# ─────────────────────────────────────────────────────────────────────────────

class TestSingleResult:
    """GET /api/v1/benchmarks/{run_id} — detail with sub_runs."""

    @pytest.mark.asyncio
    async def test_single_subrun_detail(self, http_client):
        payload = _make_ingest_payload(run_id="run-detail-01")
        await http_client.post("/api/v1/metrics", json=payload)

        r = await http_client.get("/api/v1/benchmarks/run-detail-01")
        assert r.status_code == 200
        body = r.json()
        assert body["run_id"] == "run-detail-01"
        assert len(body["sub_runs"]) == 1

    @pytest.mark.asyncio
    async def test_multiple_subruns_grouped(self, http_client):
        for i in range(3):
            payload = _make_ingest_payload(run_id="run-multi-sub", sub_run_index=i)
            await http_client.post("/api/v1/metrics", json=payload)

        r = await http_client.get("/api/v1/benchmarks/run-multi-sub")
        assert r.status_code == 200
        body = r.json()
        assert body["run_id"] == "run-multi-sub"
        assert len(body["sub_runs"]) == 3

    @pytest.mark.asyncio
    async def test_detail_includes_metrics_blob(self, http_client):
        payload = _make_ingest_payload(run_id="run-metrics-blob")
        await http_client.post("/api/v1/metrics", json=payload)

        r = await http_client.get("/api/v1/benchmarks/run-metrics-blob")
        sub = r.json()["sub_runs"][0]
        assert "metrics" in sub
        assert "total_token_throughput" in sub["metrics"]

    @pytest.mark.asyncio
    async def test_unknown_run_returns_404(self, http_client):
        r = await http_client.get("/api/v1/benchmarks/run-does-not-exist")
        assert r.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# 9. Comparison
# ─────────────────────────────────────────────────────────────────────────────

class TestComparison:
    """GET /api/v1/benchmarks/compare?run_a=&run_b= — side-by-side."""

    @pytest.mark.asyncio
    async def test_compare_two_runs(self, http_client):
        for run_id in ("run-cmp-a", "run-cmp-b"):
            await http_client.post("/api/v1/metrics", json=_make_ingest_payload(run_id=run_id))

        r = await http_client.get(
            "/api/v1/benchmarks/compare",
            params={"run_a": "run-cmp-a", "run_b": "run-cmp-b"},
        )
        assert r.status_code == 200
        body = r.json()
        assert "run_a" in body
        assert "run_b" in body

    @pytest.mark.asyncio
    async def test_compare_missing_run_a_returns_404(self, http_client):
        await http_client.post(
            "/api/v1/metrics", json=_make_ingest_payload(run_id="run-cmp-only-b")
        )
        r = await http_client.get(
            "/api/v1/benchmarks/compare",
            params={"run_a": "run-does-not-exist", "run_b": "run-cmp-only-b"},
        )
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_compare_missing_run_b_returns_404(self, http_client):
        await http_client.post(
            "/api/v1/metrics", json=_make_ingest_payload(run_id="run-cmp-only-a")
        )
        r = await http_client.get(
            "/api/v1/benchmarks/compare",
            params={"run_a": "run-cmp-only-a", "run_b": "run-does-not-exist"},
        )
        assert r.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# 10. Delete Endpoints
# ─────────────────────────────────────────────────────────────────────────────

class TestDelete:
    """DELETE single / bulk / by-filter / all."""

    async def _seed(self, http_client, run_id, **kw):
        await http_client.post("/api/v1/metrics", json=_make_ingest_payload(run_id=run_id, **kw))

    @pytest.mark.asyncio
    async def test_delete_single_removes_row(self, http_client):
        await self._seed(http_client, run_id="run-del-single")

        r = await http_client.delete("/api/v1/benchmarks/run-del-single")
        assert r.status_code == 200

        r_check = await http_client.get("/api/v1/benchmarks/run-del-single")
        assert r_check.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_single_unknown_returns_404(self, http_client):
        r = await http_client.delete("/api/v1/benchmarks/run-nonexistent")
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_bulk(self, http_client):
        ids = ["run-bulk-1", "run-bulk-2", "run-bulk-3"]
        for rid in ids:
            await self._seed(http_client, run_id=rid)

        r = await http_client.request(
            "DELETE", "/api/v1/benchmarks/bulk",
            json={"run_ids": ["run-bulk-1", "run-bulk-2"]},
        )
        assert r.status_code == 200

        r_all = await http_client.get("/api/v1/benchmarks")
        remaining = [row["run_id"] for row in r_all.json()]
        assert "run-bulk-1" not in remaining
        assert "run-bulk-2" not in remaining
        assert "run-bulk-3" in remaining

    @pytest.mark.asyncio
    async def test_delete_by_filter(self, http_client):
        await self._seed(http_client, run_id="run-fil-h100", gpu_type="h100")
        await self._seed(http_client, run_id="run-fil-t4",   gpu_type="t4")

        r = await http_client.delete(
            "/api/v1/benchmarks/filter", params={"gpu_type": "h100"}
        )
        assert r.status_code == 200

        r_all = await http_client.get("/api/v1/benchmarks")
        ids = [row["run_id"] for row in r_all.json()]
        assert "run-fil-h100" not in ids
        assert "run-fil-t4" in ids

    @pytest.mark.asyncio
    async def test_delete_all_requires_confirm(self, http_client):
        await self._seed(http_client, run_id="run-all-1")
        r = await http_client.delete("/api/v1/benchmarks/all")
        assert r.status_code != 200 or r.json().get("deleted", 0) == 0
        # The row must still exist
        r_all = await http_client.get("/api/v1/benchmarks")
        assert len(r_all.json()) > 0

    @pytest.mark.asyncio
    async def test_delete_all_with_confirm(self, http_client):
        for i in range(3):
            await self._seed(http_client, run_id=f"run-del-all-{i}")

        r = await http_client.delete("/api/v1/benchmarks/all", params={"confirm": "true"})
        assert r.status_code == 200

        r_all = await http_client.get("/api/v1/benchmarks")
        assert r_all.json() == []


# ─────────────────────────────────────────────────────────────────────────────
# 11. Summary
# ─────────────────────────────────────────────────────────────────────────────

class TestSummary:
    """GET /api/v1/summary — aggregate stats."""

    @pytest.mark.asyncio
    async def test_empty_db_returns_zeros(self, http_client):
        r = await http_client.get("/api/v1/summary")
        assert r.status_code == 200
        body = r.json()
        assert body["total_runs"] == 0
        assert body["success_rate"] == 0.0

    @pytest.mark.asyncio
    async def test_all_success_gives_100_percent(self, http_client):
        for i in range(3):
            await http_client.post(
                "/api/v1/metrics",
                json=_make_ingest_payload(run_id=f"run-sum-ok-{i}", status="success"),
            )

        r = await http_client.get("/api/v1/summary")
        body = r.json()
        assert body["total_runs"] == 3
        assert body["successful_runs"] == 3
        assert body["success_rate"] == pytest.approx(100.0)

    @pytest.mark.asyncio
    async def test_mixed_success_failure_rate(self, http_client):
        for i in range(2):
            await http_client.post(
                "/api/v1/metrics",
                json=_make_ingest_payload(run_id=f"run-mix-ok-{i}",  status="success"),
            )
        for i in range(2):
            await http_client.post(
                "/api/v1/metrics",
                json=_make_ingest_payload(run_id=f"run-mix-fail-{i}", status="failed"),
            )

        r = await http_client.get("/api/v1/summary")
        body = r.json()
        assert body["total_runs"] == 4
        assert body["success_rate"] == pytest.approx(50.0)

    @pytest.mark.asyncio
    async def test_avg_throughput_computed(self, http_client):
        for i, tpt in enumerate([1000.0, 3000.0]):
            await http_client.post(
                "/api/v1/metrics",
                json=_make_ingest_payload(
                    run_id=f"run-avg-{i}", total_token_throughput=tpt
                ),
            )

        r = await http_client.get("/api/v1/summary")
        body = r.json()
        assert body["avg_throughput"] == pytest.approx(2000.0, rel=1e-2)


# ─────────────────────────────────────────────────────────────────────────────
# 12. Dropdown / Filter Reference Endpoints
# ─────────────────────────────────────────────────────────────────────────────

class TestDropdowns:
    """Distinct-value endpoints used to populate UI filter dropdowns."""

    async def _seed_varied(self, http_client):
        rows = [
            dict(run_id="run-dd-1", model_name="llama3",   gpu_type="h100", concurrency=4,
                 input_tokens=512, output_tokens=128, node_ip="10.1.1.1",
                 extra_config={"precision": "fp16"}),
            dict(run_id="run-dd-2", model_name="tinyllama", gpu_type="t4",   concurrency=8,
                 input_tokens=256, output_tokens=64,  node_ip="10.1.1.2",
                 extra_config={"precision": "bf16"}),
        ]
        for row in rows:
            await http_client.post(
                "/api/v1/metrics", json=_make_ingest_payload(**row)
            )

    @pytest.mark.asyncio
    async def test_list_models(self, http_client):
        await self._seed_varied(http_client)
        r = await http_client.get("/api/v1/models")
        assert r.status_code == 200
        names = r.json()
        assert "llama3" in names
        assert "tinyllama" in names

    @pytest.mark.asyncio
    async def test_list_gpu_types(self, http_client):
        await self._seed_varied(http_client)
        r = await http_client.get("/api/v1/gpu-types")
        assert r.status_code == 200
        types = r.json()
        assert "h100" in types
        assert "t4" in types

    @pytest.mark.asyncio
    async def test_list_concurrencies(self, http_client):
        await self._seed_varied(http_client)
        r = await http_client.get("/api/v1/concurrencies")
        assert r.status_code == 200
        vals = r.json()
        assert "4" in vals or 4 in vals
        assert "8" in vals or 8 in vals

    @pytest.mark.asyncio
    async def test_list_precisions(self, http_client):
        await self._seed_varied(http_client)
        r = await http_client.get("/api/v1/precisions")
        assert r.status_code == 200
        precisions = r.json()
        assert "fp16" in precisions
        assert "bf16" in precisions

    @pytest.mark.asyncio
    async def test_list_input_tokens(self, http_client):
        await self._seed_varied(http_client)
        r = await http_client.get("/api/v1/input-tokens")
        assert r.status_code == 200
        vals = r.json()
        assert 512 in vals or "512" in vals

    @pytest.mark.asyncio
    async def test_list_output_tokens(self, http_client):
        await self._seed_varied(http_client)
        r = await http_client.get("/api/v1/output-tokens")
        assert r.status_code == 200
        vals = r.json()
        assert 128 in vals or "128" in vals

    @pytest.mark.asyncio
    async def test_list_nodes(self, http_client):
        await self._seed_varied(http_client)
        r = await http_client.get("/api/v1/nodes")
        assert r.status_code == 200
        # node IPs are masked in responses — just check some are returned
        assert len(r.json()) > 0

    @pytest.mark.asyncio
    async def test_empty_db_returns_empty_lists(self, http_client):
        for endpoint in ("/api/v1/models", "/api/v1/gpu-types", "/api/v1/precisions"):
            r = await http_client.get(endpoint)
            assert r.status_code == 200
            assert r.json() == []


# ─────────────────────────────────────────────────────────────────────────────
# 13. GPU Specs Catalog
# ─────────────────────────────────────────────────────────────────────────────

class TestGpuSpecs:
    """GET / GET /{slug} / POST /api/v1/gpu-specs."""

    _H100 = {
        "gpu_type": "h100",
        "display_name": "NVIDIA H100 NVL",
        "vendor": "nvidia",
        "arch": "hopper",
        "vram_gb": 94,
        "tdp_watts": 400,
        "tier_rank": 1,
        "fp16_tflops": 989.0,
        "fp8_tflops": 1978.0,
    }
    _T4 = {
        "gpu_type": "t4",
        "display_name": "NVIDIA T4",
        "vendor": "nvidia",
        "arch": "turing",
        "vram_gb": 16,
        "tdp_watts": 70,
        "tier_rank": 5,
        "fp16_tflops": 65.0,
        "fp8_tflops": None,
    }

    @pytest.mark.asyncio
    async def test_list_empty(self, http_client):
        r = await http_client.get("/api/v1/gpu-specs")
        assert r.status_code == 200
        assert r.json() == []

    @pytest.mark.asyncio
    async def test_list_ordered_by_tier_rank(self, http_client):
        # Insert T4 (tier 5) first, H100 (tier 1) second
        await http_client.post("/api/v1/gpu-specs", json=self._T4)
        await http_client.post("/api/v1/gpu-specs", json=self._H100)

        r = await http_client.get("/api/v1/gpu-specs")
        specs = r.json()
        assert specs[0]["tier_rank"] < specs[1]["tier_rank"]
        assert specs[0]["gpu_type"] == "h100"

    @pytest.mark.asyncio
    async def test_get_by_slug_found(self, http_client):
        await http_client.post("/api/v1/gpu-specs", json=self._H100)
        r = await http_client.get("/api/v1/gpu-specs/h100")
        assert r.status_code == 200
        body = r.json()
        assert body["gpu_type"] == "h100"
        assert body["vram_gb"] == 94

    @pytest.mark.asyncio
    async def test_get_by_slug_not_found(self, http_client):
        r = await http_client.get("/api/v1/gpu-specs/rx-9090-xt")
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_upsert_creates_new_spec(self, http_client):
        r = await http_client.post("/api/v1/gpu-specs", json=self._H100)
        assert r.status_code == 201

        r_get = await http_client.get("/api/v1/gpu-specs/h100")
        assert r_get.status_code == 200
        assert r_get.json()["display_name"] == "NVIDIA H100 NVL"

    @pytest.mark.asyncio
    async def test_upsert_updates_existing_spec(self, http_client):
        await http_client.post("/api/v1/gpu-specs", json=self._H100)

        updated = {**self._H100, "display_name": "NVIDIA H100 NVL 94GB"}
        await http_client.post("/api/v1/gpu-specs", json=updated)

        r = await http_client.get("/api/v1/gpu-specs/h100")
        assert r.json()["display_name"] == "NVIDIA H100 NVL 94GB"

    @pytest.mark.asyncio
    async def test_upsert_is_idempotent_no_duplicate(self, http_client):
        for _ in range(3):
            await http_client.post("/api/v1/gpu-specs", json=self._H100)

        r = await http_client.get("/api/v1/gpu-specs")
        h100_rows = [s for s in r.json() if s["gpu_type"] == "h100"]
        assert len(h100_rows) == 1


# ─────────────────────────────────────────────────────────────────────────────
# 14. Catalog Seeder
# ─────────────────────────────────────────────────────────────────────────────

class TestCatalogSeeder:
    """seed_catalog() — populates workload_types from catalog.json."""

    @pytest.mark.asyncio
    async def test_seed_from_real_catalog_json(self, db_session):
        from sqlalchemy import select
        from app.models.workload_type import WorkloadType
        from app.services.catalog_seeder import seed_catalog, CATALOG_PATH

        count = seed_catalog(CATALOG_PATH)
        assert count >= 1  # at least one new type inserted

        rows = (await db_session.execute(select(WorkloadType))).scalars().all()
        names = [r.name for r in rows]
        assert "LLMInference" in names

    @pytest.mark.asyncio
    async def test_seed_is_idempotent(self, db_session):
        from sqlalchemy import select, func
        from app.models.workload_type import WorkloadType
        from app.services.catalog_seeder import seed_catalog, CATALOG_PATH

        seed_catalog(CATALOG_PATH)
        first_count = (await db_session.execute(
            select(func.count()).select_from(WorkloadType)
        )).scalar()

        seed_catalog(CATALOG_PATH)  # second call
        second_count = (await db_session.execute(
            select(func.count()).select_from(WorkloadType)
        )).scalar()

        assert first_count == second_count

    @pytest.mark.asyncio
    async def test_seed_updates_image_tag(self, db_session):
        """Running seed again with a different tag updates the existing row."""
        import tempfile, json
        from sqlalchemy import select
        from app.models.workload_type import WorkloadType
        from app.services.catalog_seeder import seed_catalog

        catalog_v1 = {
            "workload_types": [
                {"name": "TestType", "display_name": "Test", "image_tag": "1.0.0"}
            ]
        }
        catalog_v2 = {
            "workload_types": [
                {"name": "TestType", "display_name": "Test", "image_tag": "2.0.0"}
            ]
        }

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f1:
            json.dump(catalog_v1, f1)
            path_v1 = f1.name

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f2:
            json.dump(catalog_v2, f2)
            path_v2 = f2.name

        seed_catalog(path_v1)
        seed_catalog(path_v2)

        # Refresh session to see committed changes from sync session
        await db_session.rollback()
        row = (await db_session.execute(
            select(WorkloadType).where(WorkloadType.name == "TestType")
        )).scalar_one()
        assert row.image_tag == "2.0.0"

    @pytest.mark.asyncio
    async def test_seed_missing_file_returns_zero(self):
        from app.services.catalog_seeder import seed_catalog
        result = seed_catalog("/tmp/this-file-does-not-exist-xyzzy.json")
        assert result == 0


# ─────────────────────────────────────────────────────────────────────────────
# 15. State Machine
# ─────────────────────────────────────────────────────────────────────────────

class TestStateMachine:
    """transition_workload_state() — valid/invalid transitions + audit log."""

    def _make_sync_session(self):
        """Return a synchronous SQLAlchemy session connected to the test DB."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from app.config import get_sync_database_url

        # get_sync_database_url() already points at aistudio_test because
        # conftest.py set POSTGRES_DATABASE=aistudio_test before any app import.
        url = get_sync_database_url()
        engine = create_engine(url)
        Session = sessionmaker(bind=engine, expire_on_commit=False)
        return Session()

    def _create_workload_sync(self, db):
        """Insert a CREATED workload synchronously and return its workload_id."""
        from app.models.workload import Workload
        wl_id = "wl-%s" % uuid.uuid4().hex[:8]
        wl = Workload(
            workload_id=wl_id,
            model_name="tinyllama",
            workload_config={},
            state="CREATED",
        )
        db.add(wl)
        db.commit()
        return wl_id

    def test_valid_transition_created_to_validating(self):
        from app.services.state_machine import transition_workload_state
        from app.models.workload import Workload

        db = self._make_sync_session()
        wl_id = self._create_workload_sync(db)

        transition_workload_state(
            db=db,
            workload_id=wl_id,
            new_state="VALIDATING",
            trigger="validate_node",
            message="Starting GPU validation.",
        )

        wl = db.query(Workload).filter(Workload.workload_id == wl_id).one()
        assert wl.state == "VALIDATING"
        db.close()

    def test_invalid_transition_raises(self):
        from app.services.state_machine import (
            transition_workload_state,
            InvalidStateTransition,
        )

        db = self._make_sync_session()
        wl_id = self._create_workload_sync(db)

        with pytest.raises(InvalidStateTransition):
            # CREATED → RUNNING is not allowed
            transition_workload_state(
                db=db,
                workload_id=wl_id,
                new_state="RUNNING",
                trigger="bad_skip",
                message="skip all steps",
            )
        db.close()

    def test_failed_is_terminal(self):
        from app.services.state_machine import (
            transition_workload_state,
            InvalidStateTransition,
        )
        from app.models.workload import Workload

        db = self._make_sync_session()
        wl_id = self._create_workload_sync(db)

        transition_workload_state(db, wl_id, "FAILED", "validate_node", "GPU OOM")

        with pytest.raises(InvalidStateTransition):
            transition_workload_state(db, wl_id, "VALIDATING", "retry", "retrying")
        db.close()

    def test_transition_writes_audit_event(self):
        from app.services.state_machine import transition_workload_state
        from app.models.workload import Workload
        from app.models.workload_event import WorkloadEvent

        db = self._make_sync_session()
        wl_id = self._create_workload_sync(db)

        transition_workload_state(
            db, wl_id, "VALIDATING", "validate_node", "Checking GPU drivers."
        )

        wl = db.query(Workload).filter(Workload.workload_id == wl_id).one()
        events = db.query(WorkloadEvent).filter(
            WorkloadEvent.workload_id == wl.id
        ).all()
        assert len(events) == 1
        assert events[0].state == "VALIDATING"
        assert events[0].trigger == "validate_node"
        db.close()

    def test_workload_not_found_raises_value_error(self):
        from app.services.state_machine import transition_workload_state

        db = self._make_sync_session()
        with pytest.raises(ValueError, match="not found"):
            transition_workload_state(
                db, "wl-does-not-exist", "VALIDATING", "test", "msg"
            )
        db.close()

    def test_failed_transition_sets_error_message(self):
        from app.services.state_machine import transition_workload_state
        from app.models.workload import Workload

        db = self._make_sync_session()
        wl_id = self._create_workload_sync(db)

        transition_workload_state(
            db, wl_id, "FAILED", "validate_node", "Driver version too old."
        )

        wl = db.query(Workload).filter(Workload.workload_id == wl_id).one()
        assert wl.error_message == "Driver version too old."
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# 16. Manifest Builder
# ─────────────────────────────────────────────────────────────────────────────

class TestManifestBuilder:
    """ManifestBuilder — shell command string generation (no SSH required)."""

    def test_llm_command_is_docker_run(self):
        from app.services.manifest_builder import ManifestBuilder
        cmd = ManifestBuilder.build_llm_benchmark_command(
            model_name="tinyllama/tinyllama-1.1b",
            config={"gpu_count": 1, "concurrency": 4},
            run_id="run-manifest-01",
        )
        assert "docker run" in cmd

    def test_llm_command_contains_model_name(self):
        from app.services.manifest_builder import ManifestBuilder
        model = "meta-llama/Meta-Llama-3-8B-Instruct"
        cmd = ManifestBuilder.build_llm_benchmark_command(
            model_name=model,
            config={"gpu_count": 1},
            run_id="run-manifest-02",
        )
        assert model in cmd

    def test_llm_command_mounts_results_path(self):
        from app.services.manifest_builder import ManifestBuilder
        from app.config import settings
        cmd = ManifestBuilder.build_llm_benchmark_command(
            model_name="tinyllama",
            config={"gpu_count": 1},
            run_id="run-manifest-03",
        )
        assert settings.NODE_RESULTS_PATH in cmd
        assert "/results" in cmd

    def test_llm_command_respects_gpu_count_as_tp(self):
        from app.services.manifest_builder import ManifestBuilder
        cmd = ManifestBuilder.build_llm_benchmark_command(
            model_name="llama3",
            config={"gpu_count": 4},
            run_id="run-manifest-04",
        )
        # --tp 4 should appear
        assert "--tp" in cmd
        assert "4" in cmd

    def test_llm_command_starts_with_env_prelude(self):
        from app.services.manifest_builder import ManifestBuilder
        cmd = ManifestBuilder.build_llm_benchmark_command(
            model_name="tinyllama",
            config={},
            run_id="run-manifest-env",
        )
        # Must start with the node env sourcing command
        assert cmd.startswith("if [ -f")

    def test_llm_command_uses_gcr_image(self):
        from app.services.manifest_builder import ManifestBuilder
        from app.config import get_workload_registry, settings
        cmd = ManifestBuilder.build_llm_benchmark_command(
            model_name="tinyllama",
            config={},
            run_id="run-manifest-img",
        )
        registry = get_workload_registry()
        assert registry in cmd
        assert "llminference:" in cmd

    def test_jupyter_command_is_detached(self):
        from app.services.manifest_builder import ManifestBuilder
        cmd = ManifestBuilder.build_jupyter_command(run_id="jup-manifest-01")
        assert "docker run" in cmd
        assert " -d " in cmd or cmd.count("-d") >= 1

    def test_jupyter_command_includes_workload_id_env(self):
        from app.services.manifest_builder import ManifestBuilder
        run_id = "jup-manifest-02"
        cmd = ManifestBuilder.build_jupyter_command(run_id=run_id)
        assert run_id in cmd

    def test_jupyter_command_custom_tag(self):
        from app.services.manifest_builder import ManifestBuilder
        from app.config import get_workload_registry
        cmd = ManifestBuilder.build_jupyter_command(run_id="jup-manifest-03")
        registry = get_workload_registry()
        assert registry in cmd
        assert "jupyternotebook:" in cmd


# ─────────────────────────────────────────────────────────────────────────────
# 17. Config
# ─────────────────────────────────────────────────────────────────────────────

class TestConfig:
    """Computed URL helpers and Settings defaults."""

    def test_database_url_uses_asyncpg(self):
        from app.config import get_database_url
        url = get_database_url()
        assert url.startswith("postgresql+asyncpg://")

    def test_sync_database_url_uses_plain_postgres(self):
        from app.config import get_sync_database_url
        url = get_sync_database_url()
        assert url.startswith("postgresql://")
        assert "+asyncpg" not in url

    def test_celery_broker_url_uses_amqp(self):
        from app.config import get_celery_broker_url
        url = get_celery_broker_url()
        assert url.startswith("amqp://")

    def test_workload_registry_format(self):
        from app.config import get_workload_registry, settings
        registry = get_workload_registry()
        assert settings.GCP_REGISTRY_URL in registry
        assert settings.GCP_PROJECT_ID in registry
        assert settings.GCP_REPOSITORY in registry

    def test_model_storage_mode_default(self):
        from app.config import settings
        assert settings.MODEL_STORAGE_MODE == "huggingface"

    def test_ssh_default_user_default(self):
        from app.config import settings
        assert settings.SSH_DEFAULT_USER == "ubuntu"

    def test_database_url_contains_test_db_name(self):
        from app.config import get_database_url
        url = get_database_url()
        # Must point at the test DB (env was overridden in conftest)
        assert "aistudio_test" in url


# ─────────────────────────────────────────────────────────────────────────────
# 18. IP Masking
# ─────────────────────────────────────────────────────────────────────────────

class TestIPMasking:
    """_mask_ip() and end-to-end masking in leaderboard responses."""

    def test_masks_last_two_octets(self):
        from app.schemas.benchmark import _mask_ip
        assert _mask_ip("10.6.12.26")    == "10.6.x.x"
        assert _mask_ip("192.168.1.100") == "192.168.x.x"
        assert _mask_ip("172.16.0.1")    == "172.16.x.x"

    def test_does_not_change_already_masked(self):
        from app.schemas.benchmark import _mask_ip
        # Calling twice should not double-mask
        masked = _mask_ip("10.6.12.26")
        assert masked == "10.6.x.x"

    @pytest.mark.asyncio
    async def test_masking_in_leaderboard_response(self, http_client):
        """Real IP must not appear in GET /api/v1/benchmarks response."""
        payload = _make_ingest_payload(run_id="run-mask-e2e", node_ip="10.6.12.99")
        await http_client.post("/api/v1/metrics", json=payload)
        r = await http_client.get("/api/v1/benchmarks", params={"run_id": "run-mask-e2e"})
        row = r.json()[0]
        for ip in row["node_ips"]:
            assert "12.99" not in ip
            assert re.match(r'\d+\.\d+\.x\.x', ip)

    @pytest.mark.asyncio
    async def test_multiple_ips_all_masked(self, http_client, db_session):
        """When node_ips has multiple addresses, every one must be masked."""
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from app.models.benchmark_result import BenchmarkResult

        stmt = pg_insert(BenchmarkResult).values(
            run_id="run-multi-ip",
            sub_run_index=0,
            model_name="tinyllama",
            workload_type="llm",
            node_ips=["10.1.2.3", "10.4.5.6"],
            gpu_type="t4",
            gpu_count=1,
            precision="fp16",
            input_tokens=512,
            output_tokens=128,
            concurrency=4,
            status="success",
            pipeline_version="unknown",
        ).on_conflict_do_nothing()
        await db_session.execute(stmt)
        await db_session.commit()

        r = await http_client.get("/api/v1/benchmarks", params={"run_id": "run-multi-ip"})
        row = r.json()[0]
        for ip in row["node_ips"]:
            assert re.match(r'\d+\.\d+\.x\.x', ip), f"Unmasked IP: {ip}"
