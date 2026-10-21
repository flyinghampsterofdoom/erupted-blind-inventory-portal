"""Add private saved views for the V2 Reporting Workbench.

Revision ID: 20260812_0019
Revises: 20260810_0018
Create Date: 2026-08-12
"""

import sqlalchemy as sa
from alembic import op

revision = '20260812_0019'
down_revision = '20260810_0018'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'reporting_saved_views',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column(
            'principal_id', sa.BigInteger(),
            sa.ForeignKey('principals.id', ondelete='CASCADE'), nullable=False,
        ),
        sa.Column('name', sa.String(120), nullable=False),
        sa.Column('report_type', sa.String(32), nullable=False),
        sa.Column('configuration', sa.JSON(), nullable=False, server_default='{}'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('principal_id', 'name', name='reporting_saved_views_principal_name_uniq'),
        sa.CheckConstraint(
            "report_type IN ('sales_analysis', 'stock_value')",
            name='reporting_saved_views_report_type_ck',
        ),
    )
    op.create_index(
        'idx_reporting_saved_views_principal_name',
        'reporting_saved_views', ['principal_id', 'name'],
    )


def downgrade() -> None:
    op.drop_index('idx_reporting_saved_views_principal_name', table_name='reporting_saved_views')
    op.drop_table('reporting_saved_views')
