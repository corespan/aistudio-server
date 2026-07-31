"""REVERTED — was run_recipe, replaced by ci_run_url approach

Revision ID: e1f2a3b4c5d6
Revises: d1e2f3a4b5c6
Create Date: 2026-07-22

This migration is intentionally a no-op. The run_recipe JSONB approach
was replaced by a ci_run_url field (GitHub Actions run link).
"""
from alembic import op

revision = "e1f2a3b4c5d6"
down_revision = "d1e2f3a4b5c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
