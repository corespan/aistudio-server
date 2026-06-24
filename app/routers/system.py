from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.workload_type import WorkloadType

router = APIRouter(tags=["System"])

# Default vLLM configs per model. The UI fetches these on "Next" in the Start Run form.
# P40-safe defaults use fp32 since the P40 (Pascal) has no native FP16 tensor cores.
_MODEL_CONFIGS = {
    "tinyllama/tinyllama-1.1b-chat-v1.0": {
        "precision": "fp32",
        "concurrency": 4,
        "input_tokens": 512,
        "output_tokens": 128,
        "max_model_len": 2048,
        "tensor_parallel_size": 1,
        "pipeline_parallel_size": 1,
        "batch_size": 32,
    },
    "llama3-8b-instruct": {
        "precision": "fp16",
        "concurrency": 8,
        "input_tokens": 512,
        "output_tokens": 256,
        "max_model_len": 4096,
        "tensor_parallel_size": 1,
        "pipeline_parallel_size": 1,
        "batch_size": 32,
    },
    "mistral-7b-instruct": {
        "precision": "fp16",
        "concurrency": 8,
        "input_tokens": 512,
        "output_tokens": 256,
        "max_model_len": 4096,
        "tensor_parallel_size": 1,
        "pipeline_parallel_size": 1,
        "batch_size": 32,
    },
}

_DEFAULT_CONFIG = {
    "precision": "fp16",
    "concurrency": 4,
    "input_tokens": 512,
    "output_tokens": 256,
    "max_model_len": 4096,
    "tensor_parallel_size": 1,
    "pipeline_parallel_size": 1,
    "batch_size": 32,
}


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
    return _MODEL_CONFIGS.get(model.lower(), _DEFAULT_CONFIG)


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
