"""Add the V2 Ordering-owned current inventory read model.

Revision ID: 20260725_0009
Revises: 20260725_0008
Create Date: 2026-07-25
"""

import sqlalchemy as sa
from alembic import op


revision = '20260725_0009'
down_revision = '20260725_0008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'ordering_inventory_refresh_runs',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('correlation_id', sa.String(36), nullable=False, unique=True),
        sa.Column('result', sa.String(16), nullable=False),
        sa.Column('expected_variation_count', sa.Integer(), nullable=False),
        sa.Column('active_store_count', sa.Integer(), nullable=False),
        sa.Column('expected_pair_count', sa.Integer(), nullable=False),
        sa.Column('covered_pair_count', sa.Integer(), nullable=False),
        sa.Column('missing_pair_count', sa.Integer(), nullable=False),
        sa.Column('square_request_count', sa.Integer(), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('error_code', sa.String(64)),
        sa.Column('error_summary', sa.Text()),
        sa.Column('refreshed_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "result IN ('COMPLETE', 'PARTIAL', 'FAILED')",
            name='ordering_inventory_refresh_runs_result_ck',
        ),
        sa.CheckConstraint(
            'expected_variation_count >= 0 AND active_store_count >= 0 '
            'AND expected_pair_count >= 0 AND covered_pair_count >= 0 '
            'AND missing_pair_count >= 0 AND square_request_count >= 0',
            name='ordering_inventory_refresh_runs_counts_non_negative_ck',
        ),
        sa.CheckConstraint(
            'covered_pair_count + missing_pair_count = expected_pair_count',
            name='ordering_inventory_refresh_runs_coverage_ck',
        ),
        sa.CheckConstraint(
            "(result = 'COMPLETE' AND missing_pair_count = 0) OR "
            "(result = 'PARTIAL' AND covered_pair_count > 0 AND missing_pair_count > 0) OR "
            "(result = 'FAILED' AND covered_pair_count = 0)",
            name='ordering_inventory_refresh_runs_outcome_ck',
        ),
        sa.CheckConstraint(
            'completed_at >= started_at',
            name='ordering_inventory_refresh_runs_time_order_ck',
        ),
    )
    op.create_index(
        'idx_ordering_inventory_refresh_runs_completed',
        'ordering_inventory_refresh_runs',
        ['completed_at', 'id'],
    )
    op.create_table(
        'ordering_current_inventory',
        sa.Column('square_variation_id', sa.Text(), primary_key=True),
        sa.Column('store_id', sa.BigInteger(), sa.ForeignKey('stores.id'), primary_key=True),
        sa.Column('square_location_id', sa.Text(), nullable=False),
        sa.Column('counted_quantity', sa.Numeric(14, 3), nullable=False),
        sa.Column('source_calculated_at', sa.DateTime(timezone=True)),
        sa.Column('refreshed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('freshness_state', sa.String(16), nullable=False),
        sa.Column(
            'refresh_run_id',
            sa.BigInteger(),
            sa.ForeignKey('ordering_inventory_refresh_runs.id'),
            nullable=False,
        ),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "freshness_state IN ('FRESH', 'STALE', 'CRITICAL')",
            name='ordering_current_inventory_freshness_ck',
        ),
    )
    op.create_index(
        'idx_ordering_current_inventory_variation',
        'ordering_current_inventory',
        ['square_variation_id'],
    )
    op.create_index(
        'idx_ordering_current_inventory_refresh_run',
        'ordering_current_inventory',
        ['refresh_run_id'],
    )


def downgrade() -> None:
    op.drop_index('idx_ordering_current_inventory_refresh_run', table_name='ordering_current_inventory')
    op.drop_index('idx_ordering_current_inventory_variation', table_name='ordering_current_inventory')
    op.drop_table('ordering_current_inventory')
    op.drop_index('idx_ordering_inventory_refresh_runs_completed', table_name='ordering_inventory_refresh_runs')
    op.drop_table('ordering_inventory_refresh_runs')
