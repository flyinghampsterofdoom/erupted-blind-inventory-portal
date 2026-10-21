"""Add standard Scheduling shifts and employee shift targets.

Revision ID: 20260826_0025
Revises: 20260826_0024
Create Date: 2026-08-26
"""

import sqlalchemy as sa
from alembic import op


revision = '20260826_0025'
down_revision = '20260826_0024'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('schedule_shifts', sa.Column(
        'generated_from_coverage_requirement', sa.Boolean(), nullable=False,
        server_default=sa.text('false')))
    op.add_column('scheduling_store_defaults', sa.Column(
        'standard_shift_start', sa.Time(), nullable=True))
    op.add_column('scheduling_store_defaults', sa.Column(
        'standard_shift_end', sa.Time(), nullable=True))
    op.create_check_constraint(
        'scheduling_store_defaults_standard_shift_ck', 'scheduling_store_defaults',
        '(standard_shift_start IS NULL AND standard_shift_end IS NULL) OR '
        '(standard_shift_start IS NOT NULL AND standard_shift_end IS NOT NULL '
        'AND standard_shift_end > standard_shift_start)')
    op.execute(sa.text(
        "UPDATE scheduling_store_defaults "
        "SET standard_shift_start = TIME '08:45:00', standard_shift_end = TIME '22:00:00'"
    ))

    op.add_column('employee_scheduling_profiles', sa.Column(
        'target_shifts_per_week', sa.Integer(), nullable=True))
    op.create_check_constraint(
        'employee_scheduling_profiles_target_shifts_range_ck',
        'employee_scheduling_profiles',
        'target_shifts_per_week IS NULL OR target_shifts_per_week BETWEEN 0 AND 7')
    op.execute(sa.text(
        'UPDATE employee_scheduling_profiles AS profile '
        'SET target_shifts_per_week = 3 '
        'FROM employees AS employee '
        'WHERE employee.id = profile.employee_id '
        'AND employee.scheduling_active IS TRUE '
        'AND profile.active IS TRUE'
    ))


def downgrade() -> None:
    op.drop_constraint(
        'employee_scheduling_profiles_target_shifts_range_ck',
        'employee_scheduling_profiles', type_='check')
    op.drop_column('employee_scheduling_profiles', 'target_shifts_per_week')
    op.drop_constraint(
        'scheduling_store_defaults_standard_shift_ck',
        'scheduling_store_defaults', type_='check')
    op.drop_column('scheduling_store_defaults', 'standard_shift_end')
    op.drop_column('scheduling_store_defaults', 'standard_shift_start')
    op.drop_column('schedule_shifts', 'generated_from_coverage_requirement')
