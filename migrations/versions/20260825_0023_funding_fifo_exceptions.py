"""Persist owner-resolved Funding Report FIFO exceptions.

Revision ID: 20260825_0023
Revises: 20260825_0022
Create Date: 2026-08-25
"""

import sqlalchemy as sa
from alembic import op


revision = '20260825_0023'
down_revision = '20260825_0022'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'funding_report_fifo_exceptions',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column(
            'report_id', sa.BigInteger(),
            sa.ForeignKey('funding_reports.id', ondelete='CASCADE'), nullable=False,
        ),
        sa.Column(
            'sale_fact_id', sa.BigInteger(),
            sa.ForeignKey('consignment_sale_facts.id', ondelete='RESTRICT'), nullable=False,
        ),
        sa.Column('square_variation_id', sa.Text(), nullable=False),
        sa.Column('product_name_snapshot', sa.Text(), nullable=False),
        sa.Column('variation_name_snapshot', sa.Text()),
        sa.Column('sku_snapshot', sa.Text()),
        sa.Column('store_id', sa.BigInteger(), sa.ForeignKey('stores.id')),
        sa.Column('sale_business_date', sa.Date(), nullable=False),
        sa.Column('sale_transacted_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('quantity_affected', sa.Numeric(14, 3), nullable=False),
        sa.Column('sold_through_quantity', sa.Numeric(14, 3), nullable=False),
        sa.Column('received_through_quantity', sa.Numeric(14, 3), nullable=False),
        sa.Column('status', sa.String(16), nullable=False, server_default='PENDING'),
        sa.Column('unit_cost_snapshot', sa.Numeric(14, 4)),
        sa.Column('cost_basis', sa.String(40)),
        sa.Column('resolution_reason', sa.Text()),
        sa.Column('resolved_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id')),
        sa.Column('resolved_at', sa.DateTime(timezone=True)),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            'report_id', 'sale_fact_id',
            name='funding_report_fifo_exceptions_report_sale_uniq',
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'IGNORED', 'INCLUDED')",
            name='funding_report_fifo_exceptions_status_ck',
        ),
        sa.CheckConstraint(
            'quantity_affected > 0',
            name='funding_report_fifo_exceptions_quantity_ck',
        ),
        sa.CheckConstraint(
            "status <> 'INCLUDED' OR (unit_cost_snapshot IS NOT NULL AND unit_cost_snapshot > 0)",
            name='funding_report_fifo_exceptions_included_cost_ck',
        ),
    )
    op.create_index(
        'idx_funding_report_fifo_exceptions_report_status',
        'funding_report_fifo_exceptions', ['report_id', 'status'],
    )


def downgrade() -> None:
    op.drop_table('funding_report_fifo_exceptions')
