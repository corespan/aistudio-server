"""
app/routers/results.py — Benchmark Results: Leaderboard, Dropdowns, Analytics
===============================================================================

Internal helpers
----------------
_build_filter_conditions(...)
    Returns a list of SQLAlchemy WHERE clauses from the standard filter params.
    Used by both list_benchmarks (SELECT) and delete_benchmarks_by_filter
    (DELETE + COUNT) so the predicate logic is never duplicated.

_distinct_values(column, db, date, ...)
    Generic SELECT DISTINCT + optional date-filter helper for dropdown endpoints.
    list_nodes is the only exception — it uses func.unnest() on the node_ips
    array column and stays inline.
"""

from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import case, delete, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.benchmark_result import BenchmarkResult
from app.models.gpu_spec import GpuSpec
from app.schemas.benchmark import BenchmarkDetailResponse, BenchmarkDetailSubRun, BenchmarkResultResponse


class BulkDeleteRequest(BaseModel):
    run_ids: List[str]

router = APIRouter(tags=["Results Leaderboard"])


# ── Internal helpers ───────────────────────────────────────────────────────────

def _build_filter_conditions(
    *,
    model: Optional[str],
    gpu_type: Optional[str],
    node_ip: Optional[str],
    concurrency: Optional[int],
    precision: Optional[str],
    input_tokens: Optional[int],
    output_tokens: Optional[int],
    status: Optional[str],
    date: Optional[str],
    server_name: Optional[str] = None,
    workload_type: Optional[str] = None,
    run_id: Optional[str] = None,
) -> list:
    """
    Build the standard BenchmarkResult WHERE conditions from filter params.

    Returns a plain list of SQLAlchemy clause objects — callers apply them to
    any statement type (SELECT or DELETE) with a simple ``for c in conditions``
    loop.  Raises HTTP 400 on a bad date string.
    """
    conditions = []
    if model:
        conditions.append(BenchmarkResult.model_name == model)
    if gpu_type:
        conditions.append(BenchmarkResult.gpu_type == gpu_type.lower())
    if node_ip:
        # node_ips is a PostgreSQL array column; .any() checks for membership.
        conditions.append(BenchmarkResult.node_ips.any(node_ip))
    if concurrency:
        conditions.append(BenchmarkResult.concurrency == concurrency)
    if precision:
        conditions.append(BenchmarkResult.precision == precision.lower())
    if input_tokens:
        conditions.append(BenchmarkResult.input_tokens == input_tokens)
    if output_tokens:
        conditions.append(BenchmarkResult.output_tokens == output_tokens)
    if status:
        conditions.append(BenchmarkResult.status == status.lower())
    if server_name:
        conditions.append(BenchmarkResult.server_name == server_name)
    if workload_type:
        conditions.append(BenchmarkResult.workload_type == workload_type.lower())
    if run_id:
        conditions.append(BenchmarkResult.run_id == run_id)
    if date:
        try:
            target_date = datetime.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")
        conditions.append(func.date(BenchmarkResult.completed_at) == target_date)
    return conditions


async def _distinct_values(
    column,
    db: AsyncSession,
    date: Optional[str] = None,
    *,
    sort: bool = True,
    cast_str: bool = False,
    exclude_none: bool = False,
) -> list:
    """
    Return distinct non-null values for a BenchmarkResult column.

    ``exclude_none=True``  uses ``r is not None`` (keeps 0/False — good for ints).
    ``exclude_none=False`` uses truthiness (drops None and empty strings).
    """
    query = select(column).distinct()
    if date:
        try:
            target_date = datetime.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")
        query = query.where(func.date(BenchmarkResult.completed_at) == target_date)
    result = await db.execute(query)
    raw = result.scalars().all()
    values = [r for r in raw if r is not None] if exclude_none else [r for r in raw if r]
    if cast_str:
        values = [str(v) for v in values]
    return sorted(values) if sort else values


# ── Leaderboard ────────────────────────────────────────────────────────────────

@router.get("/api/v1/benchmarks", response_model=list[BenchmarkResultResponse])
async def list_benchmarks(
    model: Optional[str] = Query(None),
    gpu_type: Optional[str] = Query(None),
    node_ip: Optional[str] = Query(None),
    concurrency: Optional[int] = Query(None),
    precision: Optional[str] = Query(None),
    input_tokens: Optional[int] = Query(None),
    output_tokens: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    date: Optional[str] = Query(None, description="YYYY-MM-DD"),
    server_name: Optional[str] = Query(None),
    workload_type: Optional[str] = Query(None, description="e.g. 'llm', 'resnet'"),
    run_id: Optional[str] = Query(None, description="Filter by exact run_id"),
    limit: int = Query(100, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns benchmark results for the leaderboard table.

    Ordering:
      1. PRU server first — Corespan's own hardware always comes first across all tiers.
      2. tier_rank ASC   — best GPUs next (H100 before RTX 5090 before T4).
                           Results for unregistered GPU types go to the bottom (NULLS LAST).
      3. total_token_throughput DESC — within the same tier+server, highest throughput first.
    """
    conditions = _build_filter_conditions(
        model=model, gpu_type=gpu_type, node_ip=node_ip, concurrency=concurrency,
        precision=precision, input_tokens=input_tokens, output_tokens=output_tokens,
        status=status, date=date, server_name=server_name, workload_type=workload_type,
        run_id=run_id,
    )
    query = (
        select(BenchmarkResult)
        .outerjoin(GpuSpec, BenchmarkResult.gpu_type == GpuSpec.gpu_type)
    )
    for c in conditions:
        query = query.where(c)
    query = query.order_by(
        # In-progress runs always float to the top of the leaderboard.
        case((BenchmarkResult.status == "running", 0), else_=1).asc(),
        # Server priority: PRU first, JOHNAIC second, everything else after.
        case(
            (BenchmarkResult.server_name == "PRU",     0),
            (BenchmarkResult.server_name == "JOHNAIC", 1),
            else_=2,
        ).asc(),
        # tier_rank next — H100 before RTX 5090 before A100, etc.
        GpuSpec.tier_rank.asc().nulls_last(),
        desc(BenchmarkResult.total_token_throughput),
    ).limit(limit)
    result = await db.execute(query)
    return [BenchmarkResultResponse.from_orm_row(row) for row in result.scalars().all()]


@router.get("/api/v1/benchmarks/compare")
async def compare_benchmarks(
    run_a: str = Query(..., description="First run_id to compare"),
    run_b: str = Query(..., description="Second run_id to compare"),
    db: AsyncSession = Depends(get_db),
):
    """Returns two benchmark runs side-by-side for comparison."""
    res_a = await db.execute(
        select(BenchmarkResult).where(
            BenchmarkResult.run_id == run_a, BenchmarkResult.sub_run_index == 0,
        )
    )
    res_b = await db.execute(
        select(BenchmarkResult).where(
            BenchmarkResult.run_id == run_b, BenchmarkResult.sub_run_index == 0,
        )
    )
    data_a = res_a.scalar_one_or_none()
    data_b = res_b.scalar_one_or_none()

    if not data_a:
        raise HTTPException(status_code=404, detail="Run A '%s' not found." % run_a)
    if not data_b:
        raise HTTPException(status_code=404, detail="Run B '%s' not found." % run_b)

    return {"run_a": data_a, "run_b": data_b}


@router.get("/api/v1/benchmarks/{run_id}", response_model=BenchmarkDetailResponse)
async def get_benchmark(run_id: str, db: AsyncSession = Depends(get_db)):
    """
    Full detail for a single benchmark run.

    Returns all sub-runs grouped under run_id.
    Each sub-run includes:
    - All leaderboard fields
    - parallelism (hot column, e.g. 'tp4')
    - metrics (full raw JSONB blob)
    - run_recipe (reproducibility recipe — docker image, commands, dataset, driver versions)
    """
    result = await db.execute(
        select(BenchmarkResult)
        .where(BenchmarkResult.run_id == run_id)
        .order_by(BenchmarkResult.sub_run_index)
    )
    runs = result.scalars().all()
    if not runs:
        raise HTTPException(status_code=404, detail="Run '%s' not found." % run_id)
    return BenchmarkDetailResponse(
        run_id=run_id,
        sub_runs=[BenchmarkDetailSubRun.from_orm_row(r) for r in runs],
    )


# ── DELETE endpoints — MUST be registered before DELETE /{run_id} ─────────────

@router.delete("/api/v1/benchmarks/bulk")
async def delete_benchmarks_bulk(
    body: BulkDeleteRequest,
    db: AsyncSession = Depends(get_db),
):
    """Delete multiple runs by a list of run_ids in one request."""
    if not body.run_ids:
        raise HTTPException(status_code=400, detail="run_ids list must not be empty.")

    count = (
        await db.execute(select(func.count()).where(BenchmarkResult.run_id.in_(body.run_ids)))
    ).scalar()
    if not count:
        raise HTTPException(status_code=404, detail="None of the provided run_ids were found.")

    await db.execute(delete(BenchmarkResult).where(BenchmarkResult.run_id.in_(body.run_ids)))
    await db.commit()
    return {"status": "deleted", "run_ids": body.run_ids, "rows_deleted": count}


@router.delete("/api/v1/benchmarks/filter")
async def delete_benchmarks_by_filter(
    model: Optional[str] = Query(None),
    gpu_type: Optional[str] = Query(None),
    node_ip: Optional[str] = Query(None),
    concurrency: Optional[int] = Query(None),
    precision: Optional[str] = Query(None),
    input_tokens: Optional[int] = Query(None),
    output_tokens: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    date: Optional[str] = Query(None, description="YYYY-MM-DD"),
    server_name: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Delete all runs matching the given filters. At least one filter required."""
    if not any([model, gpu_type, node_ip, concurrency, precision,
                input_tokens, output_tokens, status, date, server_name]):
        raise HTTPException(
            status_code=400,
            detail="At least one filter is required. Use DELETE /api/v1/benchmarks/all to wipe everything.",
        )

    # _build_filter_conditions returns a list — apply to both DELETE and COUNT
    # in one loop so they always stay in sync.
    conditions = _build_filter_conditions(
        model=model, gpu_type=gpu_type, node_ip=node_ip, concurrency=concurrency,
        precision=precision, input_tokens=input_tokens, output_tokens=output_tokens,
        status=status, date=date, server_name=server_name,
    )
    q = delete(BenchmarkResult)
    count_q = select(func.count()).select_from(BenchmarkResult)
    for c in conditions:
        q = q.where(c)
        count_q = count_q.where(c)

    count = (await db.execute(count_q)).scalar()
    if not count:
        raise HTTPException(status_code=404, detail="No matching benchmark results found.")

    await db.execute(q)
    await db.commit()
    return {
        "status": "deleted",
        "rows_deleted": count,
        "filters_applied": {
            "model": model, "gpu_type": gpu_type, "node_ip": node_ip,
            "concurrency": concurrency, "precision": precision,
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "status": status, "date": date,
        },
    }


@router.delete("/api/v1/benchmarks/all")
async def delete_all_benchmarks(
    confirm: bool = Query(False, description="Must be true to execute."),
    db: AsyncSession = Depends(get_db),
):
    """Delete every row in benchmark_results. Requires ?confirm=true."""
    if not confirm:
        raise HTTPException(
            status_code=400,
            detail="Pass ?confirm=true to confirm wiping all benchmark results.",
        )
    count = (await db.execute(select(func.count()).select_from(BenchmarkResult))).scalar()
    await db.execute(delete(BenchmarkResult))
    await db.commit()
    return {"status": "deleted", "rows_deleted": count}


@router.delete("/api/v1/benchmarks/{run_id}")
async def delete_benchmark(run_id: str, db: AsyncSession = Depends(get_db)):
    """Delete all BenchmarkResult rows for a single run_id."""
    count = (
        await db.execute(select(func.count()).where(BenchmarkResult.run_id == run_id))
    ).scalar()
    if not count:
        raise HTTPException(status_code=404, detail="Run '%s' not found." % run_id)
    await db.execute(delete(BenchmarkResult).where(BenchmarkResult.run_id == run_id))
    await db.commit()
    return {"status": "deleted", "run_id": run_id, "rows_deleted": count}


# ── Filter dropdowns ───────────────────────────────────────────────────────────
#
# All use _distinct_values() except list_nodes — node_ips is a PostgreSQL array
# column so it needs func.unnest() to expand individual IPs before DISTINCT.

@router.get("/api/v1/models")
async def list_models(date: Optional[str] = None, db: AsyncSession = Depends(get_db)):
    """
    Returns the union of:
      1. Catalog models — defined in system.py, these are the models Corespan
         provides via GCR images. Always present regardless of past run history.
      2. Historical models — distinct model_name values from past benchmark results,
         in case a user ran a model not in the current catalog.
    Catalog models come first; historical extras are appended.
    """
    from app.catalog import _MODEL_CONFIGS
    catalog = list(_MODEL_CONFIGS.keys())
    historical = await _distinct_values(BenchmarkResult.model_name, db, date, sort=False)
    seen = set(catalog)
    extra = [m for m in historical if m not in seen]
    return catalog + extra


@router.get("/api/v1/gpu-types")
async def list_gpu_types(date: Optional[str] = None, db: AsyncSession = Depends(get_db)):
    return await _distinct_values(BenchmarkResult.gpu_type, db, date, sort=False)


@router.get("/api/v1/servers")
async def list_servers(date: Optional[str] = None, db: AsyncSession = Depends(get_db)):
    """Distinct server names for the leaderboard filter dropdown."""
    return await _distinct_values(BenchmarkResult.server_name, db, date)


@router.get("/api/v1/nodes")
async def list_nodes(date: Optional[str] = None, db: AsyncSession = Depends(get_db)):
    """Distinct node IPs — unnests the node_ips array column before deduplication."""
    query = select(func.unnest(BenchmarkResult.node_ips)).distinct()
    if date:
        try:
            target_date = datetime.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")
        query = query.where(func.date(BenchmarkResult.completed_at) == target_date)
    result = await db.execute(query)
    return sorted([r for r in result.scalars().all() if r])


@router.get("/api/v1/concurrencies")
async def list_concurrencies(db: AsyncSession = Depends(get_db)):
    return await _distinct_values(BenchmarkResult.concurrency, db, cast_str=True, exclude_none=True)


@router.get("/api/v1/precisions")
async def list_precisions(db: AsyncSession = Depends(get_db)):
    return await _distinct_values(BenchmarkResult.precision, db)


@router.get("/api/v1/input-tokens")
async def list_input_tokens(db: AsyncSession = Depends(get_db)):
    return await _distinct_values(BenchmarkResult.input_tokens, db, exclude_none=True)


@router.get("/api/v1/output-tokens")
async def list_output_tokens(db: AsyncSession = Depends(get_db)):
    return await _distinct_values(BenchmarkResult.output_tokens, db, exclude_none=True)


# ── Analytics / Summary ────────────────────────────────────────────────────────

@router.get("/api/v1/summary")
async def get_summary(date: Optional[str] = None, db: AsyncSession = Depends(get_db)):
    """Aggregate stats for the dashboard summary cards."""
    query = select(
        func.count(BenchmarkResult.id).label("total_runs"),
        func.sum(
            case((BenchmarkResult.status == "success", 1), else_=0)
        ).label("successful_runs"),
        func.avg(BenchmarkResult.total_token_throughput).label("avg_throughput"),
    )

    if date:
        target_date = datetime.strptime(date, "%Y-%m-%d").date()
        query = query.where(func.date(BenchmarkResult.completed_at) == target_date)
        date_label = date
    else:
        thirty_days_ago = datetime.utcnow() - timedelta(days=30)
        query = query.where(BenchmarkResult.completed_at >= thirty_days_ago)
        date_label = "last_30d"

    result = await db.execute(query)
    stats = result.first()

    total = stats.total_runs or 0
    success = stats.successful_runs or 0
    throughput = float(stats.avg_throughput) if stats.avg_throughput else 0.0
    success_rate = (success / total * 100) if total > 0 else 0.0

    return {
        "total_runs": total,
        "successful_runs": success,
        "success_rate": round(success_rate, 2),
        "avg_throughput": round(throughput, 2),
        "date": date_label,
    }
