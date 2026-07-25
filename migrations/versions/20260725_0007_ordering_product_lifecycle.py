"""Add explicit V2 Ordering product lifecycle overrides.

Revision ID: 20260725_0007
Revises: 20260720_0006
Create Date: 2026-07-25
"""

import sqlalchemy as sa
from alembic import op


revision = '20260725_0007'
down_revision = '20260720_0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'ordering_product_lifecycle',
        sa.Column('square_variation_id', sa.Text(), primary_key=True),
        sa.Column('status', sa.String(32), nullable=False, server_default='ACTIVE'),
        sa.Column('pre_archive_status', sa.String(32)),
        sa.Column('sku_snapshot', sa.Text()),
        sa.Column('product_name_snapshot', sa.Text()),
        sa.Column('status_note', sa.Text()),
        sa.Column('no_future_reorder_at', sa.DateTime(timezone=True)),
        sa.Column('no_future_reorder_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id')),
        sa.Column('archived_at', sa.DateTime(timezone=True)),
        sa.Column('archived_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id')),
        sa.Column('restored_at', sa.DateTime(timezone=True)),
        sa.Column('restored_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id')),
        sa.Column('row_version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'NO_FUTURE_REORDER', 'ARCHIVED')",
            name='ordering_product_lifecycle_status_ck',
        ),
        sa.CheckConstraint(
            "pre_archive_status IS NULL OR pre_archive_status IN ('ACTIVE', 'NO_FUTURE_REORDER')",
            name='ordering_product_lifecycle_pre_archive_status_ck',
        ),
        sa.CheckConstraint('row_version > 0', name='ordering_product_lifecycle_row_version_ck'),
        sa.CheckConstraint(
            'status_note IS NULL OR char_length(status_note) <= 1000',
            name='ordering_product_lifecycle_note_length_ck',
        ),
        sa.CheckConstraint(
            'sku_snapshot IS NULL OR char_length(sku_snapshot) <= 255',
            name='ordering_product_lifecycle_sku_length_ck',
        ),
        sa.CheckConstraint(
            'product_name_snapshot IS NULL OR char_length(product_name_snapshot) <= 500',
            name='ordering_product_lifecycle_name_length_ck',
        ),
        sa.CheckConstraint(
            "status <> 'ARCHIVED' OR (archived_at IS NOT NULL AND archived_by_principal_id IS NOT NULL)",
            name='ordering_product_lifecycle_archive_evidence_ck',
        ),
        sa.CheckConstraint(
            "status <> 'NO_FUTURE_REORDER' OR "
            '(no_future_reorder_at IS NOT NULL AND no_future_reorder_by_principal_id IS NOT NULL)',
            name='ordering_product_lifecycle_nfr_evidence_ck',
        ),
    )
    op.create_index(
        'idx_ordering_product_lifecycle_status',
        'ordering_product_lifecycle',
        ['status'],
    )


def downgrade() -> None:
    op.drop_index('idx_ordering_product_lifecycle_status', table_name='ordering_product_lifecycle')
    op.drop_table('ordering_product_lifecycle')
