"""add parallelism to benchmark_results

Revision ID: d1e2f3a4b5c6
Revises: c1d2e3f4a5b6
Create Date: 2026-07-22 00:00:00.000000

Adds a 'parallelism' hot column to benchmark_results for storing the
parallelism strategy as a compact string (e.g. 'tp4', 'pp4', 'tp8').

Extracted by the ingest router from metrics.parallelism dict:
    {"tensor_parallel_size": 4, "pipeline_parallel_size": 1} → "tp4"
    {"tensor_parallel_size": 1, "pipeline_parallel_size": 4} → "pp4"
    {"tensor_parallel_size": 4, "pipeline_parallel_size": 2} → "tp4pp2"

Exposed only on the detail endpoint (GET /api/v1/benchmarks/{run_id}),
not on the leaderboard list.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "d1e2f3a4b5c6"
down_revision = "c1d2e3f4a5b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "benchmark_results",
        sa.Column(
            "parallelism",
            sa.String(16),
            nullable=True,
            comment="Parallelism strategy. e.g. 'tp4', 'pp4', 'tp8'. Null for single-GPU runs.",
        ),
    )
    op.create_index(
        "ix_benchmark_results_parallelism",
        "benchmark_results",
        ["parallelism"],
    )


def downgrade() -> None:
    op.drop_index("ix_benchmark_results_parallelism", table_name="benchmark_results")
    op.drop_column("benchmark_results", "parallelism")
