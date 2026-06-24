import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class BenchmarkResult(Base):
    """
    Stores one completed benchmark execution.

    Design — two-layer storage (InferenceX pattern):

    Layer 1 — Hot columns (typed SQL columns):
        Used for fast leaderboard sorting, filtering, and comparison.
        These are the metrics the UI will ORDER BY and WHERE on most often.
        Indexed by PostgreSQL directly — no JSON parsing needed.

    Layer 2 — 'metrics' JSONB blob:
        Stores the full raw metrics payload from the runner.
        Anything that does not need server-side filtering lives here.
        The frontend reads and renders this for detailed charts/tables.

    This means the backend never needs to know about new metric fields
    upfront — runners just add them to the metrics dict and the frontend
    renders them. Only fields that power leaderboard sorting become hot columns.
    """

    __tablename__ = "benchmark_results"

    # ── Primary Key ───────────────────────────────────────────────────────────
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        comment="Internal surrogate key.",
    )

    # ── Run Identity ──────────────────────────────────────────────────────────
    # run_id comes from the runner (Kubeflow/Celery/CLI).
    # sub_run_index allows a single logical run to have multiple sub-configurations
    # (e.g. same model, different concurrency levels in one sweep).
    # Together they must be unique.
    run_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
        comment="Runner-assigned run identifier. Unique per logical benchmark run.",
    )
    sub_run_index: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="Index within a sweep run. 0 for single runs.",
    )

    # ── Model & Pipeline ──────────────────────────────────────────────────────
    model_name: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        index=True,
        comment="LLM model identifier. e.g. 'llama3-8b-instruct'. Always lowercase.",
    )
    pipeline_version: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="Version of the inference pipeline. e.g. 'vllm-0.4.2'.",
    )

    # ── Hardware ──────────────────────────────────────────────────────────────
    # node_ips is a PostgreSQL native TEXT[] array.
    # Supports multi-node runs where multiple IPs are involved.
    node_ips: Mapped[list[str]] = mapped_column(
        ARRAY(Text),
        nullable=False,
        comment="IP addresses of GPU nodes used. Single node: ['10.6.12.15'].",
    )
    gpu_type: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        index=True,
        comment="GPU family. e.g. 'a100', 'h100', 't4'. Always lowercase.",
    )
    gpu_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Number of GPUs used across all nodes.",
    )
    gpu_model: Mapped[Optional[str]] = mapped_column(
        String(64),
        nullable=True,
        comment="Full GPU model string from nvidia-smi. e.g. 'NVIDIA A100-SXM4-80GB'.",
    )

    # ── Execution Config ──────────────────────────────────────────────────────
    precision: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="Model precision. e.g. 'fp16', 'bf16', 'int4'. Always lowercase.",
    )
    input_tokens: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Input prompt token count used in the benchmark.",
    )
    output_tokens: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Max output token count used in the benchmark.",
    )
    concurrency: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        index=True,
        comment="Number of concurrent requests sent to the inference server.",
    )

    # ── Status ────────────────────────────────────────────────────────────────
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="success",
        comment="Execution result. 'success' or 'failed'.",
    )

    # ── Hot Columns (Layer 1) — Leaderboard metrics ───────────────────────────
    # These four are the most commonly sorted/filtered metrics in the UI.
    # Stored as typed FLOAT columns for direct SQL ORDER BY / WHERE.
    total_token_throughput: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
        comment="Total tokens/sec across all concurrent requests. Primary leaderboard sort key.",
    )
    mean_ttft_ms: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
        comment="Mean Time To First Token in milliseconds.",
    )
    mean_tpot_ms: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
        comment="Mean Time Per Output Token in milliseconds.",
    )
    mean_e2el_ms: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
        comment="Mean end-to-end latency per request in milliseconds.",
    )

    # ── Full Metrics Blob (Layer 2) — JSONB ───────────────────────────────────
    # Stores the complete metrics dict from the runner.
    # Runners can add any new fields here without a schema migration.
    # Frontend reads this for detailed charts.
    metrics: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        comment="Full raw metrics payload from the runner. No schema enforced.",
    )

    # ── Timestamps ────────────────────────────────────────────────────────────
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="When the benchmark execution started on the node.",
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the benchmark execution completed. Null if still running or failed mid-run.",
    )
    duration_seconds: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
        comment="Total wall-clock execution time in seconds.",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="When this record was inserted into the database.",
    )

    # ── Constraints ───────────────────────────────────────────────────────────
    __table_args__ = (
        # A run_id + sub_run_index pair must be unique.
        # ON CONFLICT on this pair = idempotent ingest (safe to POST twice).
        UniqueConstraint("run_id", "sub_run_index", name="uq_benchmark_run_sub"),

        # Enforce lowercase on classification fields.
        # Prevents duplicates like 'A100' vs 'a100' in leaderboard dropdowns.
        CheckConstraint("model_name = lower(model_name)", name="ck_model_name_lower"),
        CheckConstraint("gpu_type = lower(gpu_type)", name="ck_gpu_type_lower"),
        CheckConstraint("precision = lower(precision)", name="ck_precision_lower"),
    )

    def __repr__(self) -> str:
        return (
            f"<BenchmarkResult run_id={self.run_id!r} "
            f"model={self.model_name!r} "
            f"status={self.status!r}>"
        )
