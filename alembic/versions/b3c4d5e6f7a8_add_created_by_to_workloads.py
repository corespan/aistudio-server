"""add created_by to workloads

Revision ID: b3c4d5e6f7a8
Revises: a2b3c4d5e6f7
Create Date: 2026-10-28

Records who started a workload, as the caller reports it. The MCP server sets it
from the verified JWT sub and lets only that user or an admin stop the workload.
Existing rows stay NULL (creator unknown).
"""

from alembic import op
import sqlalchemy as sa

revision = "b3c4d5e6f7a8"
down_revision = "a2b3c4d5e6f7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workloads",
        sa.Column(
            "created_by",
            sa.String(length=256),
            nullable=True,
            comment="Who started the workload, as the caller reports it (MCP: the JWT sub).",
        ),
    )
    op.create_index("ix_workloads_created_by", "workloads", ["created_by"])


def downgrade() -> None:
    op.drop_index("ix_workloads_created_by", table_name="workloads")
    op.drop_column("workloads", "created_by")
