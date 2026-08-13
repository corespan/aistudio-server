import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Enum, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class WorkloadState(str, enum.Enum):
    """
    State machine for a workload lifecycle.

    Valid transitions (enforced in the service layer, not the DB):
        CREATED     → VALIDATING  (Celery starts validate_node task)
        VALIDATING  → VALIDATED   (node passed GPU/driver checks)
        VALIDATED   → INSTALLING  (Celery starts install_dependencies task)
        INSTALLING  → READY       (dependencies installed on node)
        READY       → RUNNING     (Celery starts execute_benchmark task)
        RUNNING     → READY       (benchmark completed successfully)
        ANY         → FAILED      (any Celery task raised an exception)

    FAILED workloads are terminal — user must start a new workload.
    """
    CREATED    = "CREATED"
    VALIDATING = "VALIDATING"
    VALIDATED  = "VALIDATED"
    INSTALLING = "INSTALLING"
    READY      = "READY"
    RUNNING    = "RUNNING"
    FAILED     = "FAILED"


# PostgreSQL ENUM type — stored as a native PG enum, not a VARCHAR.
# This means the DB itself enforces valid state values.
workload_state_enum = Enum(
    "CREATED",
    "VALIDATING",
    "VALIDATED",
    "INSTALLING",
    "READY",
    "RUNNING",
    "FAILED",
    name="workload_state",      # name of the PG enum type in the DB
)


class Workload(Base):
    """
    Represents a single benchmark workload managed by AIStudio.

    A workload is created when the UI calls POST /api/v1/benchmarks/start.
    The Celery chain then drives it through the state machine until it reaches
    READY (success) or FAILED (error).

    One workload → one Celery chain → one or more BenchmarkResult rows.
    """

    __tablename__ = "workloads"

    # ── Primary Key ───────────────────────────────────────────────────────────
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # ── Identity ──────────────────────────────────────────────────────────────
    # Human-readable ID returned to the UI as 'task_id'.
    # Format: wl-{YYYYMMDD}-{6 random chars}. e.g. "wl-20260601-a3f9bc"
    # Generated in the service layer before insert.
    workload_id: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        unique=True,
        index=True,
        comment="Human-readable ID returned to the UI as task_id.",
    )

    # ── Model & Config ────────────────────────────────────────────────────────
    model_name: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="LLM model to benchmark. e.g. 'llama3-8b-instruct'.",
    )

    # Stores the full user-submitted config from the start request.
    # e.g. {"concurrency": 8, "max_new_tokens": 512, "temperature": 0.7}
    # Passed to the Celery chain and written into the run manifest.
    workload_config: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        comment="User-submitted benchmark configuration from the start request.",
    )

    # ── State Machine ─────────────────────────────────────────────────────────
    state: Mapped[str] = mapped_column(
        workload_state_enum,
        nullable=False,
        default=WorkloadState.CREATED,
        index=True,
        comment="Current lifecycle state. Driven by the Celery chain.",
    )

    # Set when state transitions to FAILED.
    # Contains the exception message from the failed Celery task.
    error_message: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Error detail if state is FAILED.",
    )

    # ── Ports ─────────────────────────────────────────────────────────────────
    # Ports allocated for the inference server process on the node.
    # Assigned from available port range.
    workload_port: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
        comment="Port the inference server listens on.",
    )
    communication_port: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
        comment="Port used for internal communication with the node agent.",
    )

    # ── Timestamps ────────────────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
        comment="Updated automatically on every state transition.",
    )

    # ── Relationships ─────────────────────────────────────────────────────────
    # One workload has many nodes, events, and tasks.
    # 'lazy=select' means related rows are NOT loaded unless explicitly accessed.
    nodes: Mapped[list["Node"]] = relationship(
        "Node",
        back_populates="workload",
        lazy="select",
    )
    events: Mapped[list["WorkloadEvent"]] = relationship(
        "WorkloadEvent",
        back_populates="workload",
        order_by="WorkloadEvent.event_time",
        lazy="select",
    )
    tasks: Mapped[list["Task"]] = relationship(
        "Task",
        back_populates="workload",
        lazy="select",
    )

    def __repr__(self) -> str:
        return (
            f"<Workload id={self.workload_id!r} "
            f"model={self.model_name!r} "
            f"state={self.state!r}>"
        )
