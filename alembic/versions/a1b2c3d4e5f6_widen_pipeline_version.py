"""widen pipeline_version to 128 chars

Revision ID: a1b2c3d4e5f6
Revises: f1a2b3c4d5e6
Create Date: 2026-08-04

String(32) was too narrow for full image tags like
'rocm/vllm-dev:nightly_0624_rc2_0624_rc2_20250620' (49 chars).
Widened to 128 to accommodate any foreseeable image reference.
"""
from alembic import op
import sqlalchemy as sa

revision = 'a1b2c3d4e5f6'
down_revision = 'f1a2b3c4d5e6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        'benchmark_results',
        'pipeline_version',
        existing_type=sa.String(32),
        type_=sa.String(128),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        'benchmark_results',
        'pipeline_version',
        existing_type=sa.String(128),
        type_=sa.String(32),
        existing_nullable=False,
    )
