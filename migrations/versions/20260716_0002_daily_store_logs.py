"""Add V2 Daily Store Logs.

Revision ID: 20260716_0002
Revises: 20260715_0001
Create Date: 2026-07-16
"""

import sqlalchemy as sa
from alembic import op


revision = '20260716_0002'
down_revision = '20260715_0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'web_sessions',
        sa.Column('current_store_id', sa.BigInteger(), sa.ForeignKey('stores.id'), nullable=True),
    )
    op.add_column(
        'web_sessions',
        sa.Column('current_store_checked_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        'daily_store_logs',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('store_id', sa.BigInteger(), sa.ForeignKey('stores.id'), nullable=False),
        sa.Column('log_date', sa.Date(), nullable=False),
        sa.Column('general_summary', sa.Text()),
        sa.Column('customer_incidents', sa.Text()),
        sa.Column('inventory_concerns', sa.Text()),
        sa.Column('facility_equipment_issues', sa.Text()),
        sa.Column('staffing_coverage_notes', sa.Text()),
        sa.Column('follow_up_items', sa.Text()),
        sa.Column('no_issues_reported', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('follow_up_required', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('lifecycle_status', sa.String(length=32), nullable=False, server_default='SUBMITTED'),
        sa.Column(
            'submitted_by_principal_id',
            sa.BigInteger(),
            sa.ForeignKey('principals.id'),
            nullable=False,
        ),
        sa.Column('submission_fingerprint', sa.String(length=64), nullable=False),
        sa.Column(
            'store_selection_source',
            sa.String(length=32),
            nullable=False,
            server_default='CURRENT_STORE',
        ),
        sa.Column('store_confirmed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('store_id', 'log_date', name='daily_store_logs_store_date_uniq'),
        sa.UniqueConstraint('submission_fingerprint', name='daily_store_logs_submission_fingerprint_uniq'),
        sa.CheckConstraint(
            "lifecycle_status IN ('SUBMITTED', 'ACKNOWLEDGED', 'RESOLVED')",
            name='daily_store_logs_status_ck',
        ),
        sa.CheckConstraint(
            "store_selection_source IN ('CURRENT_STORE')",
            name='daily_store_logs_selection_source_ck',
        ),
        sa.CheckConstraint(
            'NOT (no_issues_reported AND follow_up_required)',
            name='daily_store_logs_no_issues_follow_up_ck',
        ),
    )
    op.create_index(
        'idx_daily_store_logs_store_date',
        'daily_store_logs',
        ['store_id', 'log_date'],
    )
    op.create_index(
        'idx_daily_store_logs_actor_date',
        'daily_store_logs',
        ['submitted_by_principal_id', 'log_date'],
    )
    op.create_index(
        'idx_daily_store_logs_status_follow_up',
        'daily_store_logs',
        ['lifecycle_status', 'follow_up_required', 'log_date'],
    )

    op.create_table(
        'daily_store_log_actions',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column(
            'daily_store_log_id',
            sa.BigInteger(),
            sa.ForeignKey('daily_store_logs.id'),
            nullable=False,
        ),
        sa.Column('action_type', sa.String(length=32), nullable=False),
        sa.Column('from_status', sa.String(length=32), nullable=False),
        sa.Column('to_status', sa.String(length=32), nullable=False),
        sa.Column('follow_up_required_after', sa.Boolean(), nullable=False),
        sa.Column('response_note', sa.Text()),
        sa.Column('actor_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        sa.Column('action_fingerprint', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('action_fingerprint', name='daily_store_log_actions_fingerprint_uniq'),
        sa.CheckConstraint(
            "action_type IN ('ACKNOWLEDGED', 'MARKED_FOLLOW_UP', 'RESOLVED')",
            name='daily_store_log_actions_type_ck',
        ),
        sa.CheckConstraint(
            "from_status IN ('SUBMITTED', 'ACKNOWLEDGED', 'RESOLVED')",
            name='daily_store_log_actions_from_status_ck',
        ),
        sa.CheckConstraint(
            "to_status IN ('SUBMITTED', 'ACKNOWLEDGED', 'RESOLVED')",
            name='daily_store_log_actions_to_status_ck',
        ),
    )
    op.create_index(
        'idx_daily_store_log_actions_log_created',
        'daily_store_log_actions',
        ['daily_store_log_id', 'created_at'],
    )


def downgrade() -> None:
    op.drop_index('idx_daily_store_log_actions_log_created', table_name='daily_store_log_actions')
    op.drop_table('daily_store_log_actions')
    op.drop_index('idx_daily_store_logs_status_follow_up', table_name='daily_store_logs')
    op.drop_index('idx_daily_store_logs_actor_date', table_name='daily_store_logs')
    op.drop_index('idx_daily_store_logs_store_date', table_name='daily_store_logs')
    op.drop_table('daily_store_logs')
    op.drop_column('web_sessions', 'current_store_checked_at')
    op.drop_column('web_sessions', 'current_store_id')
