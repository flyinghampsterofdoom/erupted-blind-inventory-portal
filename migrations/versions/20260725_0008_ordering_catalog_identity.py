"""Add the V2 Ordering-owned catalog identity read model.

Revision ID: 20260725_0008
Revises: 20260725_0007
Create Date: 2026-07-25
"""

import sqlalchemy as sa
from alembic import op


revision = '20260725_0008'
down_revision = '20260725_0007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'ordering_catalog_identity',
        sa.Column('square_variation_id', sa.Text(), primary_key=True),
        sa.Column('square_item_id', sa.Text()),
        sa.Column('sku', sa.Text()),
        sa.Column('item_name', sa.Text()),
        sa.Column('variation_name', sa.Text()),
        sa.Column('product_name', sa.Text()),
        sa.Column('square_is_deleted', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('square_updated_at', sa.DateTime(timezone=True)),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            'product_name IS NULL OR char_length(product_name) <= 500',
            name='ordering_catalog_identity_product_name_length_ck',
        ),
        sa.CheckConstraint(
            'sku IS NULL OR char_length(sku) <= 255',
            name='ordering_catalog_identity_sku_length_ck',
        ),
    )
    op.create_index('idx_ordering_catalog_identity_sku', 'ordering_catalog_identity', ['sku'])
    op.create_table(
        'ordering_catalog_refresh_state',
        sa.Column('id', sa.Integer(), primary_key=True, server_default='1'),
        sa.Column('last_result', sa.String(16), nullable=False, server_default='NEVER'),
        sa.Column('expected_mapped_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('covered_mapped_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('missing_mapped_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_attempted_at', sa.DateTime(timezone=True)),
        sa.Column('last_successful_at', sa.DateTime(timezone=True)),
        sa.Column('last_error', sa.Text()),
        sa.Column('last_refreshed_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id')),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint('id = 1', name='ordering_catalog_refresh_state_singleton_ck'),
        sa.CheckConstraint(
            "last_result IN ('NEVER', 'COMPLETE', 'PARTIAL', 'FAILED')",
            name='ordering_catalog_refresh_state_result_ck',
        ),
        sa.CheckConstraint(
            'expected_mapped_count >= 0 AND covered_mapped_count >= 0 AND missing_mapped_count >= 0',
            name='ordering_catalog_refresh_state_counts_ck',
        ),
    )


def downgrade() -> None:
    op.drop_table('ordering_catalog_refresh_state')
    op.drop_index('idx_ordering_catalog_identity_sku', table_name='ordering_catalog_identity')
    op.drop_table('ordering_catalog_identity')
