import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class TaskLog(Base):
    """
    Stores individual log lines emitted during a task execution.

    Written by the Celery worker as it executes SSH commands on the node.
    Each stdout/stderr line from the node becomes one TaskLog row.

    These rows are streamed to the UI via the SSE endpoint:
        GET /api/v1/benchmarks/{task_id}/logs/stream

    The SSE handler polls this table for new rows and pushes them
    to the client as they arrive — giving the user a live terminal view
    of what is happening on the GPU node.

    Append-only — rows are never updated, only inserted and read.
    """

    __tablename__ = "task_logs"

    # ── Primary Key ───────────────────────────────────────────────────────────
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # ── Foreign Key ───────────────────────────────────────────────────────────
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="The task this log line belongs to.",
    )

    # ── Log Content ───────────────────────────────────────────────────────────
    # A single line of output from the SSH session.
    # Could be stdout or stderr from the node.
    line: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Single log line from the SSH session stdout/stderr.",
    )

    # ── Timestamp ─────────────────────────────────────────────────────────────
    # Set by the DB server — ensures correct ordering even across
    # multiple Celery worker processes writing concurrently.
    logged_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,        # indexed — SSE handler queries WHERE logged_at > last_seen
        comment="When this log line was inserted. Used for SSE cursor-based streaming.",
    )

    # ── Relationship ──────────────────────────────────────────────────────────
    task: Mapped["Task"] = relationship(
        "Task",
        back_populates="logs",
    )

    # ── Composite Index ──────────────────────────────────────�