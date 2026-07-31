"""
app/routers/gpu_specs.py — GPU Hardware Catalog
================================================

GET  /api/v1/gpu-specs              All GPUs ordered by tier_rank (best first).
GET  /api/v1/gpu-specs/{gpu_type}   Single GPU by its type slug.
POST /api/v1/gpu-specs              Upsert a GPU spec (used by seed script / admin).
"""

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.gpu_spec import GpuSpec

router = APIRouter(tags=["GPU Catalog"])


class GpuSpecPayload(BaseModel):
    gpu_type:     str
    display_name: str
    vendor:       str
    arch:         str
    vram_gb:      int
    tdp_watts:    int
    tier_rank:    int
    fp16_tflops:  Optional[float] = None
    fp8_tflops:   Optional[float] = None


class GpuSpecResponse(BaseModel):
    gpu_type:     str
    display_name: str
    vendor:       str
    arch:         str
    vram_gb:      int
    tdp_watts:    int
    tier_rank:    int
    fp16_tflops:  Optional[float]
    fp8_tflops:   Optional[float]

    model_config = {"from_attributes": True}


@router.get("/api/v1/gpu-specs", response_model=list[GpuSpecResponse])
async def list_gpu_specs(db: AsyncSession = Depends(get_db)):
    """
    Returns all GPU specs ordered by tier_rank ascending (tier 1 = best first).
    Used by the UI to populate the GPU filter dropdown with tier-sorted options
    and to show hardware specs alongside benchmark results.
    """
    result = await db.execute(select(GpuSpec).order_by(GpuSpec.tier_rank))
    return result.scalars().all()


@router.get("/api/v1/gpu-specs/{gpu_type}", response_model=GpuSpecResponse)
async def get_gpu_spec(gpu_type: str, db: AsyncSession = Depends(get_db)):
    """Returns hardware details for a single GPU type slug (e.g. 'h100', 't4')."""
    result = await db.execute(
        select(GpuSpec).where(GpuSpec.gpu_type == gpu_type.lower())
    )
    spec = result.scalar_one_or_none()
    if not spec:
        raise HTTPException(
            status_code=404,
            detail=f"GPU type '{gpu_type}' not found in catalog.",
        )
    return spec


@router.post("/api/v1/gpu-specs", status_code=status.HTTP_201_CREATED)
async def upsert_gpu_spec(
    payload: GpuSpecPayload,
    db: AsyncSession = Depends(get_db),
):
    """
    Upsert a GPU spec by gpu_type slug.
    Safe to call multiple times — updates existing record if gpu_type already exists.
    Used by the seed script and admin tooling.
    """
    stmt = insert(GpuSpec).values(
        id=uuid.uuid4(),
        gpu_type=payload.gpu_type.lower(),
        display_name=payload.display_name,
        vendor=payload.vendor.lower(),
        arch=payload.arch.lower(),
        vram_gb=payload.vram_gb,
        tdp_watts=payload.tdp_watts,
        tier_rank=payload.tier_rank,
        fp16_tflops=payload.fp16_tflops,
        fp8_tflops=payload.fp8_tflops,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["gpu_type"],
        set_={
            "display_name": stmt.excluded.display_name,
            "vendor":       stmt.excluded.vendor,
            "arch":         stmt.excluded.arch,
            "vram_gb":      stmt.excluded.vram_gb,
            "tdp_watts":    stmt.excluded.tdp_watts,
            "tier_rank":    stmt.excluded.tier_rank,
            "fp16_tflops":  stmt.excluded.fp16_tflops,
            "fp8_tflops":   stmt.excluded.fp8_tflops,
        },
    )
    await db.execute(stmt)
    await db.commit()
    return {"status": "ok", "gpu_type": payload.gpu_type.lower()}
