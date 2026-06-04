import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class WorkloadEvent(Base):
    """
    Append-only audit log of every state transition a workload goes through.

    Written by the Celery tasks as they execute each step of the chain.
    Used by the SSE log stream endpoint to show the user what is happening.

    Example sequence for a successful run:
        CREATED     → VALIDATING  | "Starting node validation"
        VALIDATING  → VALIDATED   | "Node 10.6.12.15 passed all checks"
        VALIDATED   → INSTALLING  | "Installing vLLM dependencies"
        INSTALLING  → READY       | "Installation complete"
        READY       → RUNNING     | "Benchmark started. Run: wl-20260601-a3f9bc"
        RUNNING     → READY       | "Benchmark complete. Results stored."
    """

    __tablename__ = "workload_events"

    # ── Primary Key ───────────────────────────────────────────────────────────
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # ── Foreign Key ───────────────────────────────────────────────────────────
    workload_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workloads.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="The workload this event belongs to.",
    )

    # ── Event Data ────────────────────────────────────────────────────────────
    # The state the workload was in when this event was written.
    state: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        comment="Workload state at the time of this event.",
    )

    # Short label identifying which part of the chain triggered this event.
    # e.g. "validate_node", "install_dependencies", "execute_benchmark", "error"
    trigger: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="Celery task name or internal trigger that wrote this event.",
    )

    # Human-readable message shown in the UI log stream.
    message: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Log message shown to the user in the SSE stream.",
    )

    # ── Timestamp ─────────────────────────────────────────────────────────────
    # server_default=func.now() means the DB sets this — not the application.
    # Ensures correct ordering even if clocks drift between app and DB servers.
    event_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="When this event was recorded. Set by the database server.",
    )

    # ── Relationship ──────────────────────────────────────────────────────────
    workload: Mapped["Workload"] = relationship(
        "Workload",
        back_populates="events",
    )

    def __repr__(self) -> str:
        return (
            f"<WorkloadEvent workload={self.workload_id!r} "
            f"state={self.state!r} "
            f"trigger={self.trigger!r}>"
        )
