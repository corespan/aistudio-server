from typing import Optional
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc, case

from app.database import get_db
from app.models.benchmark_result import BenchmarkResult

router = APIRouter(tags=["Results Leaderboard"])


@router.get("/api/v1/benchmarks")
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
    limit: int = Query(100, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
):
    """Returns a list of benchmark results for the leaderboard table."""
    query = select(BenchmarkResult)

    if model:
        query = query.where(BenchmarkResult.model_name == model)
    if gpu_type:
        query = query.where(BenchmarkResult.gpu_type == gpu_type.lower())
    if node_ip:
        query = query.where(BenchmarkResult.node_ips.any(node_ip))
    if concurrency:
        query = query.where(BenchmarkResult.concurrency == concurrency)
    if precision:
        query = query.where(BenchmarkResult.precision == precision.lower())
    if input_tokens:
        query = query.where(BenchmarkResult.input_tokens == input_tokens)
    if output_tokens:
        query = query.where(BenchmarkResult.output_tokens == output_tokens)
    if status:
        query = query.where(BenchmarkResult.status == status.lower())
    if date:
        try:
            target_date = datetime.strptime(date, "%Y-%m-%d").date()
            query = query.where(func.date(BenchmarkResult.completed_at) == target_date)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")

    query = query.order_by(desc(BenchmarkResult.completed_at)).limit(limit)

    result = await db.execute(query)
    runs = result.scalars().all()
    return runs


@router.get("/api/v1/benchmarks/compare")
async def compare_benchmarks(
    run_a: str = Query(..., description="First run_id to compare"),
    run_b: str = Query(..., description="Second run_id to compare"),
    db: AsyncSession = Depends(get_db),
):
    """Returns two benchmark runs side-by-side for comparison."""
    query_a = select(BenchmarkResult).where(
        BenchmarkResult.run_id == run_a,
        BenchmarkResult.sub_run_index == 0,
    )
    query_b = select(BenchmarkResult).where(
        BenchmarkResult.run_id == run_b,
        BenchmarkResult.sub_run_index == 0,
    )

    res_a = await db.execute(query_a)
    res_b = await db.execute(query_b)

    data_a = res_a.scalar_one_or_none()
    data_b = res_b.scalar_one_or_none()

    if not data_a:
        raise HTTPException(status_code=404, detail="Run A '%s' not found." % run_a)
    if not data_b:
        raise HTTPException(status_code=404, detail="Run B '%s' not found." % run_b)

    return {"run_a": data_a, "run_b": data_b}


@router.get("/api/v1/benchmarks/{run_id}")
async def get_benchmark(run_id: str, db: AsyncSession = Depends(get_db)):
    """Returns the full detail for a single benchmark run."""
    query = (
        select(BenchmarkResult)
        .where(BenchmarkResult.run_id == run_id)
        .order_by(BenchmarkResult.sub_run_index)
    )
    result = await db.execute(query)
    runs = result.scalars().all()

    if not runs:
        raise HTTPException(status_code=404, detail="Run '%s' not found." % run_id)

    return {"run_id": run_id, "sub_runs": runs}


# Dropdown / Reference Data

@router.get("/api/v1/models")
async def list_models(date: Optional[str] = None, db: AsyncSession = Depends(get_db)):
    query = select(BenchmarkResult.model_name).distinct()
    if date:
        target_date = datetime.strptime(date, "%Y-%m-%d").date()
        query = query.where(func.date(BenchmarkResult.completed_at) == target_date)
    result = await db.execute(query)
    return [r for r in result.scalars().all() if r]


@router.get("/api/v1/gpu-types")
async def list_gpu_types(date: Optional[str] = None, db: AsyncSession = Depends(get_db)):
    query = select(BenchmarkResult.gpu_type).distinct()
    if date:
        target_date = datetime.strptime(date, "%Y-%m-%d").date()
        query = query.where(func.date(BenchmarkResult.completed_at) == target_date)
    result = await db.execute(query)
    return [r for r in result.scalars().all() if r]


@router.get("/api/v1/nodes")
async def list_nodes(date: Optional[str] = None, db: AsyncSession = Depends(get_db)):
    query = select(func.unnest(BenchmarkResult.node_ips)).distinct()
    if date:
        target_date = datetime.strptime(date, "%Y-%m-%d").date()
        query = query.where(func.date(BenchmarkResult.completed_at) == target_date)
    result = await db.execute(query)
    return sorted([r for r in result.scalars().all() if r])


@router.get("/api/v1/concurrencies")
async def list_concurrencies(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(BenchmarkResult.concurrency).distinct())
    return sorted([str(r) for r in result.scalars().all() if r is not None])


@router.get("/api/v1/precisions")
async def list_precisions(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(BenchmarkResult.precision).distinct())
    return sorted([r for r in result.scalars().all() if r])


@router.get("/api/v1/input-tokens")
async def list_input_tokens(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(BenchmarkResult.input_tokens).distinct())
    return sorted([r for r in result.scalars().all() if r is not None])


@router.get("/api/v1/output-tokens")
async def list_output_tokens(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(BenchmarkResult.output_tokens).distinct())
    return sorted([r for r in result.scalars().all() if r is not None])


# Analytics / Summary

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
