import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


# PostgreSQL native ENUM for node state.
# A node follows its own state machine separate from the workload.
node_state_enum = Enum(
    "ADDED",        # Node record created, no checks done yet
    "VALIDATING",   # SSH connection open, running GPU/driver checks
    "VALIDATED",    # Passed all checks, ready for installation
    "INSTALLING",   # Dependencies being installed via SSH
    "READY",        # Software installed, ready to accept benchmark runs
    "RUNNING",      # Actively executing a benchmark task
    "FAILED",       # Validation or installation failed
    name="node_state",
)


class Node(Base):
    """
    Represents a GPU node added to a workload.

    A workload can have one or more nodes (multi-node runs).
    Each node goes through its own validation and installation steps
    driven by the Celery chain via SSH.

    The node records are created when the UI submits POST /api/v1/benchmarks/start
    with the node_ips list. One Node row per IP.
    """

    __tablename__ = "nodes"

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
        comment="The workload this node is assigned to.",
    )

    # ── Node Identity ─────────────────────────────────────────────────────────
    # machine_id is a short human-readable identifier generated at creation.
    # e.g. "node-a3f9bc". Used in log messages.
    machine_id: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        comment="Short human-readable node identifier. e.g. 'node-a3f9bc'.",
    )
    machine_ip: Mapped[str] = mapped_column(
        String(45),        # 45 chars covers both IPv4 and IPv6
        nullable=False,
        comment="IP address used to SSH into this node.",
    )
    machine_username: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        comment="SSH username. Configured via SSH_DEFAULT_USER in .env.",
    )

    # ── State ─────────────────────────────────────────────────────────────────
    state: Mapped[str] = mapped_column(
        node_state_enum,
        nullable=False,
        default="ADDED",
        index=True,
        comment="Current lifecycle state of this node.",
    )

    # ── Software ──────────────────────────────────────────────────────────────
    sw_installed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="True once dependencies have been installed on this node.",
    )

    # ── Hardware Specs ────────────────────────────────────────────────────────
    # Collected via SSH during the validate_node Celery task.
    # Stored as JSONB — schema varies depending on GPU vendor and driver version.
    # e.g. {"gpu_name": "A100-SXM4-80GB", "driver_version": "535.104", "cuda": "12.2"}
    gpus: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        nullable=True,
        comment="GPU inventory collected from nvidia-smi during validation.",
    )
    specs: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        nullable=True,
        comment="Full hardware spec dict: CPU, RAM, disk, CUDA version etc.",
    )

    # Raw PCIe topology string from nvidia-smi topo -m.
    # Stored as text — used for multi-node bandwidth analysis.
    pcie_tree: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Raw output of nvidia-smi topo -m for this node.",
    )

    # ── Heartbeat ─────────────────────────────────────────────────────────────
    # The Celery worker pings the node during execution to detect hangs.
    heartbeat_interval: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=30,
        comment="Seconds between heartbeat checks during benchmark execution.",
    )
    hb_error_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="Consecutive missed heartbeats. Triggers FAILED state when threshold hit.",
    )

    # Name of the task currently running on this node.
    # Cleared when the task finishes or fails.
    running_task: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True,
        comment="workload_id of the task currently executing on this node. Null if idle.",
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
    )

    # ── Relationships ─────────────────────────────────────────────────────────
    workload: Mapped["Workload"] = relationship(
        "Workload",
        back_populates="nodes",
    )
    tasks: Mapped[list["Task"]] = relationship(
        "Task",
        back_populates="node",
        lazy="select",
    )

    def __repr__(self) -> str:
        return (
            f"<Node id={self.machine_id!r} "
            f"ip={self.machine_ip!r} "
            f"state={self.state!r}>"
        )
