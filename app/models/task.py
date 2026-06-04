import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Task(Base):
    """
    Represents a single benchmark execution run within a workload.

    When the Celery 'execute_benchmark' task fires, it creates one Task row.
    A workload can theoretically have multiple tasks if it is run multiple times
    (e.g. user stops and restarts). Each run gets its own Task record.

    The Task is the link between the Workload (orchestration) and
    BenchmarkResult (metrics storage). After execution completes,
    the Celery worker posts the metrics via POST /api/v1/metrics which
    creates a BenchmarkResult row with the same run_id as this Task.
    """

    __tablename__ = "tasks"

    # ── Primary Key ───────────────────────────────────────────────────────────
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # ── Foreign Keys ──────────────────────────────────────────────────────────
    workload_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workloads.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="The workload this task belongs to.",
    )
    node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("nodes.id", ondelete="SET NULL"),
        nullable=True,
        comment="The node this task ran on. SET NULL if the node is deleted.",
    )

    # ── Run Identity ──────────────────────────────────────────────────────────
    # run_name is the human-readable identifier shown in the UI.
    # Format: {model}-{concurrency}-{MMDDYYYY}-{seq}
    # e.g. "llama3-8b-c8-06012026-001"
    # This same value is used as run_id when posting to POST /api/v1/metrics.
    # It is the join key between Task and BenchmarkResult.
    run_name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        unique=True,
        index=True,
        comment="Human-readable run identifier. Also used as run_id in BenchmarkResult.",
    )

    # ── Task Config ───────────────────────────────────────────────────────────
    # Snapshot of the exact config used for this run.
    # Copied from workload.workload_config at task creation time.
    # Stored separately so future runs with different configs don't overwrite history.
    task_config: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        comment="Exact benchmark config used for this run. Snapshot from workload_config.",
    )

    # ── Status ────────────────────────────────────────────────────────────────
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="running",
        comment="Execution status: 'running', 'success', 'failed'.",
    )

    # ── Timestamps ────────────────────────────────────────────────────────────
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="When the execute_benchmark Celery task started.",
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When execution finished. Null while still running.",
    )

    # ── Relationships ─────────────────────────────────────────────────────────
    workload: Mapped["Workload"] = relationship(
        "Workload",
        back_populates="tasks",
    )
    node: Mapped[Optional["Node"]] = relationship(
        "Node",
        back_populates="tasks",
    )
    logs: Mapped[list["TaskLog"]] = relationship(
        "TaskLog",
        back_populates="task",
        order_by="TaskLog.logged_at",
        lazy="select",
    )

    def __repr__(self) -> str:
        return (
            f"<Task run_name={self.run_name!r} "
            f"status={self.status!r}>"
        )
