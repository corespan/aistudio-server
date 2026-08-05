from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalog import _DEFAULT_CONFIG, _MODEL_CONFIGS, _MODEL_INFO
from app.database import get_db
from app.models.workload_type import WorkloadType

router = APIRouter(tags=["System"])


@router.get("/health")
async def health_check(db: AsyncSession = Depends(get_db)):
    """
    Kubernetes readiness/liveness probe endpoint.
    Verifies that the API is running and PostgreSQL is reachable.
    """
    try:
        # Simple ping to PostgreSQL
        await db.execute(select(1))
        db_status = "ok"
    except Exception as e:
        db_status = f"unreachable: {str(e)}"
        
    return {
        "status": "healthy" if db_status == "ok" else "degraded",
        "database": db_status,
        # AIStudio doesn't connect to Nexus or NFS, so no checks needed for those!
    }


@router.get("/api/v1/models/config")
async def get_model_config(model: str = Query(..., description="Model name, e.g. TinyLlama/TinyLlama-1.1B-Chat-v1.0")):
    """
    Returns default vLLM configuration for a given model.
    The Start Run UI fetches this after the user enters a node + model and clicks Next.
    Falls back to generic defaults for unknown models.
    """
    config = _MODEL_CONFIGS.get(model.lower(), _DEFAULT_CONFIG)
    info   = _MODEL_INFO.get(model.lower(), {})
    return {
        **config,
        "gated":       info.get("gated", False),
        "license":     info.get("license", ""),
        "license_url": info.get("license_url", ""),
        "hf_repo":     info.get("hf_repo", ""),
    }


@router.get("/api/v1/workload-types")
async def list_workload_types(db: AsyncSession = Depends(get_db)):
    """
    Returns the list of supported workload types (e.g. LLMInference).
    The UI uses this to populate the main selection screen dynamically.
    """
    result = await db.execute(select(WorkloadType))
    types = result.scalars().all()
    
    return [
        {
            "id": str(wt.id),
            "name": wt.name,
            "display_name": wt.display_name,
            "description": wt.description,
        }
        for wt in types
    ]
