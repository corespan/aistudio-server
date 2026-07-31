"""add image_tag to workload_types

Revision ID: a1b2c3d4e5f6
Revises: f0e43419bf9b
Create Date: 2026-07-03

Adds image_tag column to workload_types table.
Stores the Docker image tag for each workload type (e.g. '2.3.1-nvidia').
Seeded from catalog.json at startup via catalog_seeder.
"""

from alembic import op
import sqlalchemy as sa

revision = "a1b2c3d4e5f6"
down_revision = "f0e43419bf9b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workload_types",
        sa.Column(
            "image_tag",
            sa.String(length=64),
            nullable=True,
            comment=(
                "Docker image tag for the workload container. "
                "e.g. '2.3.1-nvidia'. Seeded from catalog.json."
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("workload_types", "image_tag")
