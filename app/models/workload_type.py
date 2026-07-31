import uuid
from typing import Optional
from sqlalchemy import String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class WorkloadType(Base):
    """
    Registry of supported workload types.

    In AIStudio, we are currently focusing purely on 'LLMInference'.
    This table allows the UI to query available workload types dynamically
    via GET /api/v1/workload-types, rather than hardcoding them.

    image_tag stores the Docker image tag for the workload container,
    e.g. '2.3.1-nvidia'. Seeded from catalog.json at startup.
    """

    __tablename__ = "workload_types"

    # ── Primary Key ───────────────────────────────────────────────────────────
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # ── Type Identity ─────────────────────────────────────────────────────────
    name: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        unique=True,
        index=True,
        comment="Internal identifier for the workload type. e.g. 'LLMInference'.",
    )

    display_name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="Human-readable name shown in the UI. e.g. 'LLM Inference (vLLM)'.",
    )

    description: Mapped[str] = mapped_column(
        Text,
        nullable=True,
        comment="Detailed description of what this workload type does.",
    )

    # ── Docker Image Tag ──────────────────────────────────────────────────────
    image_tag: Mapped[Optional[str]] = mapped_column(
        String(64),
        nullable=True,
        comment=(
            "Docker image tag for the workload container. "
            "e.g. '2.3.1-nvidia'. Seeded from catalog.json. "
            "Used by ManifestBuilder to build the docker run command."
        ),
    )

    def __repr__(self) -> str:
        return f"<WorkloadType name={self.name!r} image_tag={self.image_tag!r}>"
