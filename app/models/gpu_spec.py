import uuid
from typing import Optional

from sqlalchemy import CheckConstraint, Float, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class GpuSpec(Base):
    """
    Hardware catalog for GPU types.

    gpu_type slug is the join key to benchmark_results.gpu_type.
    tier_rank drives leaderboard ordering: 1 = most powerful GPU first.

    This table is populated by the seed script and updated by admins via
    POST /api/v1/gpu-specs. Benchmark results reference it by gpu_type slug.
    """

    __tablename__ = "gpu_specs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    gpu_type: Mapped[str] = mapped_column(
        String(32), nullable=False,
        comment="Slug matching benchmark_results.gpu_type. e.g. 'h100', 't4'",
    )
    display_name: Mapped[str] = mapped_column(
        String(64), nullable=False,
        comment="Human-readable name shown in UI. e.g. 'NVIDIA H100 NVL'",
    )
    vendor: Mapped[str] = mapped_column(
        String(16), nullable=False,
        comment="'nvidia' or 'amd'",
    )
    arch: Mapped[str] = mapped_column(
        String(32), nullable=False,
        comment="GPU microarchitecture. e.g. 'hopper', 'ampere', 'cdna3'",
    )
    vram_gb: Mapped[int] = mapped_column(
        Integer, nullable=False,
        comment="VRAM capacity in GB",
    )
    tdp_watts: Mapped[int] = mapped_column(
        Integer, nullable=False,
        comment="Thermal design power in watts",
    )
    tier_rank: Mapped[int] = mapped_column(
        Integer, nullable=False, index=True,
        comment="Performance tier. 1 = best (H100), higher = less powerful",
    )
    fp16_tflops: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True,
        comment="Peak FP16 throughput in TFLOPS",
    )
    fp8_tflops: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True,
        comment="Peak FP8 throughput in TFLOPS. NULL if GPU does not support FP8",
    )

    __table_args__ = (
        UniqueConstraint("gpu_type", name="uq_gpu_specs_gpu_type"),
        CheckConstraint("gpu_type = lower(gpu_type)", name="ck_gpu_specs_type_lower"),
        CheckConstraint("vendor   = lower(vendor)",   name="ck_gpu_specs_vendor_lower"),
        CheckConstraint("arch     = lower(arch)",     name="ck_gpu_specs_arch_lower"),
        CheckConstraint("tier_rank >= 1",             name="ck_gpu_specs_tier_positive"),
    )

    def __repr__(self) -> str:
        return f"<GpuSpec {self.gpu_type!r} tier={self.tier_rank}>"
