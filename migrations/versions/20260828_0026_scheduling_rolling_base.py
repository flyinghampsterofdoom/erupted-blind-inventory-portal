"""Add rolling-horizon alternating base schedules.

Revision ID: 20260828_0026
Revises: 20260826_0025
Create Date: 2026-08-28
"""

import sqlalchemy as sa
from alembic import op


revision = '20260828_0026'
down_revision = '20260826_0025'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        'scheduling_org_policy_length_positive_ck',
        'scheduling_organization_policies', type_='check')
    op.execute(sa.text(
        'UPDATE scheduling_organization_policies SET schedule_length_weeks = 8 '
        'WHERE schedule_length_weeks = 3 OR schedule_length_weeks > 8'))
    op.create_check_constraint(
        'scheduling_org_policy_length_positive_ck',
        'scheduling_organization_policies',
        'schedule_length_weeks BETWEEN 1 AND 8')
    op.alter_column(
        'scheduling_organization_policies', 'schedule_length_weeks',
        server_default=sa.text('8'))
    op.add_column('employee_scheduling_profiles', sa.Column(
        'week_a_workdays_mask', sa.Integer(), nullable=True))
    op.add_column('employee_scheduling_profiles', sa.Column(
        'week_b_workdays_mask', sa.Integer(), nullable=True))
    op.create_check_constraint(
        'employee_scheduling_profiles_week_a_mask_ck',
        'employee_scheduling_profiles',
        'week_a_workdays_mask IS NULL OR week_a_workdays_mask BETWEEN 0 AND 127')
    op.create_check_constraint(
        'employee_scheduling_profiles_week_b_mask_ck',
        'employee_scheduling_profiles',
        'week_b_workdays_mask IS NULL OR week_b_workdays_mask BETWEEN 0 AND 127')

    op.add_column('schedule_periods', sa.Column(
        'alternating_week', sa.String(length=1), nullable=True))
    op.create_check_constraint(
        'schedule_periods_alternating_week_ck', 'schedule_periods',
        "alternating_week IS NULL OR alternating_week IN ('A', 'B')")
    # Stable parity anchor: Sunday 2026-01-04 is Week A. Exceptions never alter parity.
    op.execute(sa.text(
        "UPDATE schedule_periods SET alternating_week = CASE "
        "WHEN MOD(((week_start_date - DATE '2026-01-04') / 7), 2) = 0 THEN 'A' ELSE 'B' END"
    ))

    op.add_column('schedule_shifts', sa.Column(
        'base_pattern_expected_day', sa.Boolean(), nullable=True))
    op.add_column('schedule_shifts', sa.Column(
        'base_pattern_deviation_reason', sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column('schedule_shifts', 'base_pattern_deviation_reason')
    op.drop_column('schedule_shifts', 'base_pattern_expected_day')
    op.drop_constraint(
        'schedule_periods_alternating_week_ck', 'schedule_periods', type_='check')
    op.drop_column('schedule_periods', 'alternating_week')
    op.drop_constraint(
        'employee_scheduling_profiles_week_b_mask_ck',
        'employee_scheduling_profiles', type_='check')
    op.drop_constraint(
        'employee_scheduling_profiles_week_a_mask_ck',
        'employee_scheduling_profiles', type_='check')
    op.drop_column('employee_scheduling_profiles', 'week_b_workdays_mask')
    op.drop_column('employee_scheduling_profiles', 'week_a_workdays_mask')
    op.drop_constraint(
        'scheduling_org_policy_length_positive_ck',
        'scheduling_organization_policies', type_='check')
    op.create_check_constraint(
        'scheduling_org_policy_length_positive_ck',
        'scheduling_organization_policies', 'schedule_length_weeks > 0')
    op.alter_column(
        'scheduling_organization_policies', 'schedule_length_weeks',
        server_default=sa.text('3'))
