"""add_server_name_to_benchmark_results

Revision ID: c1d2e3f4a5b6
Revises: b1c2d3e4f5a6
Create Date: 2026-07-22

Adds server_name as a hot (indexed) column to benchmark_results.
Populated at result-save time from the hostname captured via SSH during
node validation. Null for rows ingested before this migration.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "c1d2e3f4a5b6"
down_revision = "b1c2d3e4f5a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "benchmark_results",
        sa.Column(
            "server_name",
            sa.String(128),
            nullable=True,
            comment="Hostname of the GPU node captured via SSH. Used for leaderboard filtering.",
        ),
    )
    op.create_index(
        "ix_benchmark_results_server_name",
        "benchmark_results",
        ["server_name"],
    )


def downgrade() -> None:
    op.drop_index("ix_benchmark_results_server_name", table_name="benchmark_results")
    op.drop_column("benchmark_results", "server_name")
