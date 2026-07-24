"""Add workload_type to benchmark_results

Revision ID: f1a2b3c4d5e6
Revises: e1f2a3b4c5d6
Create Date: 2026-07-23

Adds:
  - workload_type VARCHAR(32) NOT NULL DEFAULT 'llm'   (indexed)
  - CHECK (workload_type = lower(workload_type))

The DEFAULT 'llm' backfills all existing rows automatically.
"""
import sqlalchemy as sa
from alembic import op

revision = "f1a2b3c4d5e6"
down_revision = "e1f2a3b4c5d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "benchmark_results",
        sa.Column(
            "workload_type",
            sa.String(32),
            nullable=False,
            server_default="llm",
            comment="Workload category. e.g. 'llm', 'resnet', 'vgg'. Always lowercase.",
        ),
    )
    op.create_index(
        "ix_benchmark_results_workload_type",
        "benchmark_results",
        ["workload_type"],
    )
    op.create_check_constraint(
        "ck_workload_type_lower",
        "benchmark_results",
        "workload_type = lower(workload_type)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_workload_type_lower", "benchmark_results", type_="check")
    op.drop_index("ix_benchmark_results_workload_type", table_name="benchmark_results")
    op.drop_column("benchmark_results", "workload_type")
