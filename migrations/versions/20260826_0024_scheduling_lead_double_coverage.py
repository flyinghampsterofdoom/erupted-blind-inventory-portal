"""Add Scheduling Lead and Double Coverage persistence.

Revision ID: 20260826_0024
Revises: 20260825_0023
Create Date: 2026-08-26
"""

import sqlalchemy as sa
from alembic import op


revision = '20260826_0024'
down_revision = '20260825_0023'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('employees', sa.Column(
        'scheduling_lead_capable', sa.Boolean(), nullable=False, server_default=sa.text('false')))
    op.add_column('employees', sa.Column(
        'scheduling_double_coverage', sa.Boolean(), nullable=False, server_default=sa.text('false')))
    for name in (
        'is_lead_of_day', 'lead_of_day_manually_assigned',
        'is_double_coverage', 'double_coverage_manually_assigned',
    ):
        op.add_column('schedule_shifts', sa.Column(
            name, sa.Boolean(), nullable=False, server_default=sa.text('false')))
    op.create_check_constraint(
        'schedule_shifts_lead_assigned_ck', 'schedule_shifts',
        'NOT is_lead_of_day OR employee_id IS NOT NULL')
    op.create_check_constraint(
        'schedule_shifts_double_coverage_assigned_ck', 'schedule_shifts',
        'NOT is_double_coverage OR employee_id IS NOT NULL')
    op.create_index(
        'schedule_shifts_one_lead_per_day_uniq', 'schedule_shifts',
        ['schedule_period_id', 'shift_date'], unique=True,
        postgresql_where=sa.text('is_lead_of_day'))
    op.create_index(
        'schedule_shifts_one_double_coverage_per_employee_week_uniq', 'schedule_shifts',
        ['schedule_period_id', 'employee_id'], unique=True,
        postgresql_where=sa.text('is_double_coverage AND employee_id IS NOT NULL'))
    op.create_table(
        'scheduling_store_defaults',
        sa.Column('id', sa.Integer(), server_default='1', nullable=False),
        sa.Column('double_coverage_store_id', sa.BigInteger(), nullable=True),
        sa.Column('updated_by_principal_id', sa.BigInteger(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint('id = 1', name='scheduling_store_defaults_singleton_ck'),
        sa.ForeignKeyConstraint(['double_coverage_store_id'], ['stores.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['updated_by_principal_id'], ['principals.id']),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('scheduling_store_defaults')
    op.drop_index('schedule_shifts_one_double_coverage_per_employee_week_uniq', table_name='schedule_shifts')
    op.drop_index('schedule_shifts_one_lead_per_day_uniq', table_name='schedule_shifts')
    op.drop_constraint('schedule_shifts_double_coverage_assigned_ck', 'schedule_shifts', type_='check')
    op.drop_constraint('schedule_shifts_lead_assigned_ck', 'schedule_shifts', type_='check')
    for name in (
        'double_coverage_manually_assigned', 'is_double_coverage',
        'lead_of_day_manually_assigned', 'is_lead_of_day',
    ):
        op.drop_column('schedule_shifts', name)
    op.drop_column('employees', 'scheduling_double_coverage')
    op.drop_column('employees', 'scheduling_lead_capable')
