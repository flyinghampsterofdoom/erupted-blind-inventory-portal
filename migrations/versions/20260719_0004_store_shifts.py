"""Add reusable Store Shifts to V2 Staff Scheduling.

Revision ID: 20260719_0004
Revises: 20260718_0003
Create Date: 2026-07-19
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = '20260719_0004'
down_revision = '20260718_0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'store_shifts',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('label', postgresql.CITEXT(), nullable=False),
        sa.Column('store_id', sa.BigInteger(), sa.ForeignKey('stores.id'), nullable=False),
        sa.Column('start_time', sa.Time(), nullable=False),
        sa.Column('end_time', sa.Time(), nullable=False),
        sa.Column('active_weekdays', sa.Integer(), nullable=False, server_default='127'),
        sa.Column('active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('display_order', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('manager_note', sa.Text()),
        sa.Column('created_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        sa.Column('updated_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('store_id', 'label', name='store_shifts_store_label_uniq'),
        sa.CheckConstraint('end_time > start_time', name='store_shifts_time_order_ck'),
        sa.CheckConstraint('active_weekdays BETWEEN 1 AND 127', name='store_shifts_weekdays_ck'),
    )
    op.create_index('idx_store_shifts_store_active_order', 'store_shifts', ['store_id', 'active', 'display_order'])
    op.add_column('schedule_shifts', sa.Column('source_store_shift_id', sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        'schedule_shifts_source_store_shift_id_fkey',
        'schedule_shifts',
        'store_shifts',
        ['source_store_shift_id'],
        ['id'],
        ondelete='SET NULL',
    )
    op.add_column('schedule_template_shifts', sa.Column('source_store_shift_id', sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        'schedule_template_shifts_source_store_shift_id_fkey',
        'schedule_template_shifts',
        'store_shifts',
        ['source_store_shift_id'],
        ['id'],
        ondelete='SET NULL',
    )


def downgrade() -> None:
    op.drop_constraint(
        'schedule_template_shifts_source_store_shift_id_fkey',
        'schedule_template_shifts',
        type_='foreignkey',
    )
    op.drop_column('schedule_template_shifts', 'source_store_shift_id')
    op.drop_constraint('schedule_shifts_source_store_shift_id_fkey', 'schedule_shifts', type_='foreignkey')
    op.drop_column('schedule_shifts', 'source_store_shift_id')
    op.drop_index('idx_store_shifts_store_active_order', table_name='store_shifts')
    op.drop_table('store_shifts')
