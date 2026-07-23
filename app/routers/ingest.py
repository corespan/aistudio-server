from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.benchmark_result import BenchmarkResult
from app.schemas.benchmark import BenchmarkIngestPayload

router = APIRouter(tags=["Ingest"])


@router.post("/api/v1/metrics", status_code=status.HTTP_202_ACCEPTED)
async def ingest_metrics(
    payload: BenchmarkIngestPayload,
    db: AsyncSession = Depends(get_db),
):
    """
    Universal ingestion endpoint for all benchmark runners (Celery, Kubeflow, CLI).

    Accepts the legacy dashboard JSON payload to ensure 100%% backward compatibility.
    Uses PostgreSQL ON CONFLICT to ensure idempotency.
    """
    model_name = payload.workload.name

    metrics = payload.metrics or {}
    total_token_throughput = metrics.get("total_token_throughput")
    mean_ttft_ms = metrics.get("mean_ttft_ms")
    mean_tpot_ms = metrics.get("mean_tpot_ms")
    mean_e2el_ms = metrics.get("mean_e2el_ms")
    # server_name is stored both as a hot column (for filtering) and kept in the
    # metrics blob (for backward compatibility with older rows).
    server_name = metrics.get("server_name") or payload.config.get("server_name")

    # Extract parallelism strategy as a compact string from the nested dict
    # metrics.parallelism = {"tensor_parallel_size": 4, "pipeline_parallel_size": 1}
    # → "tp4". Only stored as a hot column; the raw dict stays in the metrics blob.
    parallelism: Optional[str] = None
    p_obj = metrics.get("parallelism")
    if isinstance(p_obj, dict):
        tp = int(p_obj.get("tensor_parallel_size", 1) or 1)
        pp = int(p_obj.get("pipeline_parallel_size", 1) or 1)
        if tp > 1 and pp > 1:
            parallelism = f"tp{tp}pp{pp}"
        elif pp > 1:
            parallelism = f"pp{pp}"
        elif tp > 1:
            parallelism = f"tp{tp}"
        else:
            parallelism = "tp1"

    # Resolve started_at: prefer config value, fall back to timestamp
    started_at = payload.config.get("started_at")
    if isinstance(started_at, str):
        try:
            started_at = datetime.fromisoformat(started_at)
        except ValueError:
            started_at = payload.timestamp
    elif not started_at:
        started_at = payload.timestamp

    # Calculate duration if both timestamps are available
    duration_seconds = None
    if started_at and payload.timestamp:
        delta = (payload.timestamp - started_at).total_seconds()
        duration_seconds = delta if delta > 0 else None

    insert_values = {
        "run_id": payload.run_id,
        "sub_run_index": payload.sub_run_index,
        "model_name": model_name,
        "pipeline_version": payload.config.get("pipeline_version", "unknown"),
        "node_ips": [payload.node_ip],
        "gpu_type": payload.gpu_type.lower(),
        "gpu_count": payload.config.get("gpu_count", 1),
        "gpu_model": payload.config.get("gpu_model"),
        "precision": payload.config.get("precision", "fp16"),
        "input_tokens": payload.config.get("input_tokens", 0),
        "output_tokens": payload.config.get("output_tokens", 0),
        "concurrency": payload.config.get("concurrency", 1),
        "status": payload.status.lower(),
        "server_name": server_name,
        "parallelism": parallelism,
        "total_token_throughput": total_token_throughput,
        "mean_ttft_ms": mean_ttft_ms,
        "mean_tpot_ms": mean_tpot_ms,
        "mean_e2el_ms": mean_e2el_ms,
        "started_at": started_at,
        "completed_at": payload.timestamp,
        "duration_seconds": duration_seconds,
        "metrics": metrics,
    }

    stmt = insert(BenchmarkResult).values(**insert_values)

    update_dict = {
        c.name: c
        for c in stmt.excluded
        if c.name not in ["id", "run_id", "sub_run_index", "created_at"]
    }

    stmt = stmt.on_conflict_do_update(
        index_elements=["run_id", "sub_run_index"],
        set_=update_dict,
    )

    try:
        await db.execute(stmt)
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=500,
            detail="Database error during ingestion: %s" % str(e),
        )

    return {
        "status": "success",
        "run_id": payload.run_id,
        "message": "Ingested successfully into PostgreSQL.",
    }
