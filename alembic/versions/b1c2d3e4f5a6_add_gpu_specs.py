"""add_gpu_specs

Revision ID: b1c2d3e4f5a6
Revises: a1b2c3d4e5f6
Create Date: 2026-07-22

Creates the gpu_specs hardware catalog table.
gpu_type slug is the foreign key linking to benchmark_results.gpu_type.
tier_rank: 1 = most powerful, higher = less powerful.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
import uuid

revision = 'b1c2d3e4f5a6'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'gpu_specs',
        sa.Column('id', UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column('gpu_type',     sa.String(32),  nullable=False, comment='Slug matching benchmark_results.gpu_type. e.g. h100, t4'),
        sa.Column('display_name', sa.String(64),  nullable=False, comment='Human-readable name. e.g. NVIDIA H100 NVL'),
        sa.Column('vendor',       sa.String(16),  nullable=False, comment='nvidia or amd (lowercase)'),
        sa.Column('arch',         sa.String(32),  nullable=False, comment='GPU microarchitecture. e.g. hopper, ampere, cdna3'),
        sa.Column('vram_gb',      sa.Integer(),   nullable=False, comment='VRAM in GB'),
        sa.Column('tdp_watts',    sa.Integer(),   nullable=False, comment='Thermal design power in watts'),
        sa.Column('tier_rank',    sa.Integer(),   nullable=False, comment='Performance tier. 1 = best, higher = lower tier'),
        sa.Column('fp16_tflops',  sa.Float(),     nullable=True,  comment='FP16 peak throughput in TFLOPS'),
        sa.Column('fp8_tflops',   sa.Float(),     nullable=True,  comment='FP8 peak throughput in TFLOPS. NULL if not supported'),
        sa.UniqueConstraint('gpu_type', name='uq_gpu_specs_gpu_type'),
        sa.CheckConstraint("gpu_type = lower(gpu_type)", name='ck_gpu_specs_type_lower'),
        sa.CheckConstraint("vendor  = lower(vendor)",   name='ck_gpu_specs_vendor_lower'),
        sa.CheckConstraint("arch    = lower(arch)",     name='ck_gpu_specs_arch_lower'),
        sa.CheckConstraint("tier_rank >= 1",            name='ck_gpu_specs_tier_positive'),
    )
    op.create_index('ix_gpu_specs_tier_rank', 'gpu_specs', ['tier_rank'])


def downgrade() -> None:
    op.drop_index('ix_gpu_specs_tier_rank', table_name='gpu_specs')
    op.drop_table('gpu_specs')
