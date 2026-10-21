"""Add the V2 Staff Scheduling backend foundation.

Revision ID: 20260718_0003
Revises: 20260716_0002
Create Date: 2026-07-18
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = '20260718_0003'
down_revision = '20260716_0002'
branch_labels = None
depends_on = None


def _actor_columns() -> tuple[sa.Column, ...]:
    return (
        sa.Column('created_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        sa.Column('updated_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def upgrade() -> None:
    schedule_period_status = sa.Enum('DRAFT', 'PUBLISHED', 'ARCHIVED', name='schedule_period_status')
    scheduling_window_kind = sa.Enum('PREFERRED', 'AVAILABLE', 'HARD_UNAVAILABLE', name='scheduling_window_kind')
    time_off_request_status = sa.Enum('PENDING', 'APPROVED', 'DENIED', 'CANCELLED', name='time_off_request_status')
    schedule_warning_severity = sa.Enum('INFO', 'CONFLICT', 'SERIOUS', name='schedule_warning_severity')
    op.add_column('employees', sa.Column('principal_id', sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        'employees_principal_id_fkey',
        'employees',
        'principals',
        ['principal_id'],
        ['id'],
        ondelete='SET NULL',
    )
    op.create_unique_constraint('employees_principal_id_key', 'employees', ['principal_id'])

    op.create_table(
        'schedule_shift_types',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('name', postgresql.CITEXT(), nullable=False),
        sa.Column('description', sa.Text()),
        sa.Column('display_order', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        *_actor_columns(),
        sa.UniqueConstraint('name', name='schedule_shift_types_name_key'),
    )
    op.create_table(
        'schedule_templates',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('name', postgresql.CITEXT(), nullable=False),
        sa.Column('description', sa.Text()),
        sa.Column('week_count', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        *_actor_columns(),
        sa.UniqueConstraint('name', name='schedule_templates_name_key'),
        sa.CheckConstraint('week_count > 0', name='schedule_templates_week_count_positive_ck'),
    )
    op.create_table(
        'schedule_periods',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('week_start_date', sa.Date(), nullable=False),
        sa.Column('week_end_date', sa.Date(), nullable=False),
        sa.Column('status', schedule_period_status, nullable=False, server_default='DRAFT'),
        sa.Column('revision_number', sa.Integer(), nullable=False),
        sa.Column('supersedes_schedule_period_id', sa.BigInteger(), sa.ForeignKey('schedule_periods.id')),
        sa.Column('source_schedule_period_id', sa.BigInteger(), sa.ForeignKey('schedule_periods.id')),
        sa.Column('source_schedule_template_id', sa.BigInteger(), sa.ForeignKey('schedule_templates.id')),
        sa.Column('notes', sa.Text()),
        sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('created_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('published_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id')),
        sa.Column('published_at', sa.DateTime(timezone=True)),
        sa.UniqueConstraint('week_start_date', 'revision_number', name='schedule_periods_week_revision_uniq'),
        sa.CheckConstraint('week_end_date = week_start_date + 6', name='schedule_periods_seven_day_ck'),
        sa.CheckConstraint('revision_number > 0', name='schedule_periods_revision_positive_ck'),
        sa.CheckConstraint('version > 0', name='schedule_periods_version_positive_ck'),
    )
    op.create_index(
        'schedule_periods_one_draft_per_week_uniq',
        'schedule_periods',
        ['week_start_date'],
        unique=True,
        postgresql_where=sa.text("status = 'DRAFT'"),
    )
    op.create_index(
        'schedule_periods_one_published_per_week_uniq',
        'schedule_periods',
        ['week_start_date'],
        unique=True,
        postgresql_where=sa.text("status = 'PUBLISHED'"),
    )
    op.create_index('idx_schedule_periods_week_status', 'schedule_periods', ['week_start_date', 'status'])

    op.create_table(
        'schedule_shifts',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('schedule_period_id', sa.BigInteger(), sa.ForeignKey('schedule_periods.id'), nullable=False),
        sa.Column('employee_id', sa.BigInteger(), sa.ForeignKey('employees.id')),
        sa.Column('store_id', sa.BigInteger(), sa.ForeignKey('stores.id'), nullable=False),
        sa.Column('shift_date', sa.Date(), nullable=False),
        sa.Column('start_time', sa.Time(), nullable=False),
        sa.Column('end_time', sa.Time(), nullable=False),
        sa.Column('unpaid_break_minutes', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('shift_type_id', sa.BigInteger(), sa.ForeignKey('schedule_shift_types.id')),
        sa.Column('is_opener', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('is_closer', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('employee_note', sa.Text()),
        sa.Column('source_shift_id', sa.BigInteger(), sa.ForeignKey('schedule_shifts.id')),
        *_actor_columns(),
        sa.UniqueConstraint('schedule_period_id', 'id', name='schedule_shifts_period_id_uniq'),
        sa.CheckConstraint('end_time > start_time', name='schedule_shifts_time_order_ck'),
        sa.CheckConstraint('unpaid_break_minutes >= 0', name='schedule_shifts_break_non_negative_ck'),
    )
    op.create_index('idx_schedule_shifts_period_date', 'schedule_shifts', ['schedule_period_id', 'shift_date'])
    op.create_index('idx_schedule_shifts_employee_date', 'schedule_shifts', ['employee_id', 'shift_date'])
    op.create_index('idx_schedule_shifts_store_date', 'schedule_shifts', ['store_id', 'shift_date'])

    op.create_table(
        'employee_scheduling_profiles',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('employee_id', sa.BigInteger(), sa.ForeignKey('employees.id'), nullable=False),
        sa.Column('home_store_id', sa.BigInteger(), sa.ForeignKey('stores.id')),
        sa.Column('target_weekly_hours', sa.Numeric(6, 2), nullable=False, server_default='0'),
        sa.Column('minimum_weekly_hours', sa.Numeric(6, 2)),
        sa.Column('maximum_weekly_hours', sa.Numeric(6, 2)),
        sa.Column('preferred_workdays', sa.Integer()),
        sa.Column('scheduler_note', sa.Text()),
        sa.Column('active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        *_actor_columns(),
        sa.UniqueConstraint('employee_id', name='employee_scheduling_profiles_employee_id_key'),
        sa.CheckConstraint('target_weekly_hours >= 0', name='employee_scheduling_profiles_target_non_negative_ck'),
        sa.CheckConstraint('minimum_weekly_hours IS NULL OR minimum_weekly_hours >= 0', name='employee_scheduling_profiles_min_non_negative_ck'),
        sa.CheckConstraint('maximum_weekly_hours IS NULL OR maximum_weekly_hours >= 0', name='employee_scheduling_profiles_max_non_negative_ck'),
        sa.CheckConstraint('minimum_weekly_hours IS NULL OR maximum_weekly_hours IS NULL OR minimum_weekly_hours <= maximum_weekly_hours', name='employee_scheduling_profiles_min_max_ck'),
        sa.CheckConstraint('preferred_workdays IS NULL OR preferred_workdays BETWEEN 0 AND 7', name='employee_scheduling_profiles_workdays_ck'),
    )
    op.create_table(
        'employee_scheduling_windows',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('employee_id', sa.BigInteger(), sa.ForeignKey('employees.id'), nullable=False),
        sa.Column('day_of_week', sa.Integer(), nullable=False),
        sa.Column('start_time', sa.Time(), nullable=False),
        sa.Column('end_time', sa.Time(), nullable=False),
        sa.Column('kind', scheduling_window_kind, nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        *_actor_columns(),
        sa.CheckConstraint('day_of_week BETWEEN 0 AND 6', name='employee_scheduling_windows_weekday_ck'),
        sa.CheckConstraint('end_time > start_time', name='employee_scheduling_windows_time_order_ck'),
    )
    op.create_index('idx_employee_scheduling_windows_employee_day', 'employee_scheduling_windows', ['employee_id', 'day_of_week'])
    op.create_table(
        'employee_scheduling_store_preferences',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('employee_id', sa.BigInteger(), sa.ForeignKey('employees.id'), nullable=False),
        sa.Column('store_id', sa.BigInteger(), sa.ForeignKey('stores.id'), nullable=False),
        sa.Column('preference_rank', sa.Integer()),
        sa.Column('active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        *_actor_columns(),
        sa.UniqueConstraint('employee_id', 'store_id', name='employee_scheduling_store_preferences_employee_store_uniq'),
        sa.CheckConstraint('preference_rank IS NULL OR preference_rank > 0', name='employee_scheduling_store_preferences_rank_ck'),
    )
    op.create_table(
        'time_off_reason_categories',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('name', postgresql.CITEXT(), nullable=False),
        sa.Column('display_order', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        *_actor_columns(),
        sa.UniqueConstraint('name', name='time_off_reason_categories_name_key'),
    )
    op.create_table(
        'time_off_requests',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('employee_id', sa.BigInteger(), sa.ForeignKey('employees.id'), nullable=False),
        sa.Column('start_date', sa.Date(), nullable=False),
        sa.Column('end_date', sa.Date(), nullable=False),
        sa.Column('full_day', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('start_time', sa.Time()),
        sa.Column('end_time', sa.Time()),
        sa.Column('reason_category_id', sa.BigInteger(), sa.ForeignKey('time_off_reason_categories.id'), nullable=False),
        sa.Column('employee_note', sa.Text()),
        sa.Column('management_review_note', sa.Text()),
        sa.Column('status', time_off_request_status, nullable=False, server_default='PENDING'),
        sa.Column('submitted_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id')),
        sa.Column('reviewed_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id')),
        sa.Column('submitted_at', sa.DateTime(timezone=True)),
        sa.Column('reviewed_at', sa.DateTime(timezone=True)),
        *_actor_columns(),
        sa.CheckConstraint('end_date >= start_date', name='time_off_requests_date_order_ck'),
        sa.CheckConstraint("(full_day AND start_time IS NULL AND end_time IS NULL) OR (NOT full_day AND start_date = end_date AND start_time IS NOT NULL AND end_time IS NOT NULL AND end_time > start_time)", name='time_off_requests_full_partial_ck'),
    )
    op.create_index('idx_time_off_requests_employee_dates', 'time_off_requests', ['employee_id', 'start_date', 'end_date'])
    op.create_index('idx_time_off_requests_status_dates', 'time_off_requests', ['status', 'start_date', 'end_date'])

    op.create_table(
        'store_operating_hours',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('store_id', sa.BigInteger(), sa.ForeignKey('stores.id'), nullable=False),
        sa.Column('day_of_week', sa.Integer(), nullable=False),
        sa.Column('opening_time', sa.Time(), nullable=False),
        sa.Column('closing_time', sa.Time(), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        *_actor_columns(),
        sa.UniqueConstraint('store_id', 'day_of_week', 'opening_time', 'closing_time', name='store_operating_hours_interval_uniq'),
        sa.CheckConstraint('day_of_week BETWEEN 0 AND 6', name='store_operating_hours_weekday_ck'),
        sa.CheckConstraint('closing_time > opening_time', name='store_operating_hours_time_order_ck'),
    )
    op.create_table(
        'store_special_hours',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('store_id', sa.BigInteger(), sa.ForeignKey('stores.id'), nullable=False),
        sa.Column('calendar_date', sa.Date(), nullable=False),
        sa.Column('event_name', sa.Text(), nullable=False),
        sa.Column('closed_all_day', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('opening_time', sa.Time()),
        sa.Column('closing_time', sa.Time()),
        sa.Column('staffing_note', sa.Text()),
        sa.Column('active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('batch_correlation_id', sa.String(36)),
        *_actor_columns(),
        sa.CheckConstraint("(closed_all_day AND opening_time IS NULL AND closing_time IS NULL) OR (NOT closed_all_day AND opening_time IS NOT NULL AND closing_time IS NOT NULL AND closing_time > opening_time)", name='store_special_hours_closed_open_ck'),
    )
    op.create_index('store_special_hours_one_active_per_date_uniq', 'store_special_hours', ['store_id', 'calendar_date'], unique=True, postgresql_where=sa.text('active'))
    op.create_table(
        'coverage_requirements',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('store_id', sa.BigInteger(), sa.ForeignKey('stores.id'), nullable=False),
        sa.Column('day_of_week', sa.Integer(), nullable=False),
        sa.Column('start_time', sa.Time(), nullable=False),
        sa.Column('end_time', sa.Time(), nullable=False),
        sa.Column('minimum_employee_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('required_shift_type_id', sa.BigInteger(), sa.ForeignKey('schedule_shift_types.id')),
        sa.Column('requires_opener', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('requires_closer', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        *_actor_columns(),
        sa.CheckConstraint('day_of_week BETWEEN 0 AND 6', name='coverage_requirements_weekday_ck'),
        sa.CheckConstraint('end_time > start_time', name='coverage_requirements_time_order_ck'),
        sa.CheckConstraint('minimum_employee_count >= 0', name='coverage_requirements_count_non_negative_ck'),
    )
    op.create_table(
        'shift_templates',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('store_id', sa.BigInteger(), sa.ForeignKey('stores.id'), nullable=False),
        sa.Column('day_of_week', sa.Integer(), nullable=False),
        sa.Column('start_time', sa.Time(), nullable=False),
        sa.Column('end_time', sa.Time(), nullable=False),
        sa.Column('unpaid_break_minutes', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('shift_type_id', sa.BigInteger(), sa.ForeignKey('schedule_shift_types.id')),
        sa.Column('is_opener', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('is_closer', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('note', sa.Text()),
        sa.Column('active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        *_actor_columns(),
        sa.CheckConstraint('day_of_week BETWEEN 0 AND 6', name='shift_templates_weekday_ck'),
        sa.CheckConstraint('end_time > start_time', name='shift_templates_time_order_ck'),
        sa.CheckConstraint('unpaid_break_minutes >= 0', name='shift_templates_break_non_negative_ck'),
    )
    op.create_table(
        'schedule_template_shifts',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('schedule_template_id', sa.BigInteger(), sa.ForeignKey('schedule_templates.id', ondelete='CASCADE'), nullable=False),
        sa.Column('day_offset', sa.Integer(), nullable=False),
        sa.Column('employee_id', sa.BigInteger(), sa.ForeignKey('employees.id')),
        sa.Column('store_id', sa.BigInteger(), sa.ForeignKey('stores.id'), nullable=False),
        sa.Column('start_time', sa.Time(), nullable=False),
        sa.Column('end_time', sa.Time(), nullable=False),
        sa.Column('unpaid_break_minutes', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('shift_type_id', sa.BigInteger(), sa.ForeignKey('schedule_shift_types.id')),
        sa.Column('is_opener', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('is_closer', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('note', sa.Text()),
        sa.Column('source_shift_id', sa.BigInteger(), sa.ForeignKey('schedule_shifts.id')),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint('day_offset >= 0', name='schedule_template_shifts_offset_non_negative_ck'),
        sa.CheckConstraint('end_time > start_time', name='schedule_template_shifts_time_order_ck'),
        sa.CheckConstraint('unpaid_break_minutes >= 0', name='schedule_template_shifts_break_non_negative_ck'),
    )
    op.create_index('idx_schedule_template_shifts_template_offset', 'schedule_template_shifts', ['schedule_template_id', 'day_offset'])
    op.create_table(
        'schedule_warnings',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('schedule_period_id', sa.BigInteger(), sa.ForeignKey('schedule_periods.id', ondelete='CASCADE'), nullable=False),
        sa.Column('warning_type', sa.String(64), nullable=False),
        sa.Column('severity', schedule_warning_severity, nullable=False),
        sa.Column('store_id', sa.BigInteger(), sa.ForeignKey('stores.id'), nullable=False),
        sa.Column('warning_date', sa.Date(), nullable=False),
        sa.Column('start_time', sa.Time()),
        sa.Column('end_time', sa.Time()),
        sa.Column('employee_id', sa.BigInteger(), sa.ForeignKey('employees.id')),
        sa.Column('shift_id', sa.BigInteger(), sa.ForeignKey('schedule_shifts.id', ondelete='CASCADE')),
        sa.Column('required_count', sa.Integer()),
        sa.Column('actual_count', sa.Integer()),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('evaluated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index('idx_schedule_warnings_period_severity', 'schedule_warnings', ['schedule_period_id', 'severity'])
    op.create_index('idx_schedule_warnings_store_date', 'schedule_warnings', ['store_id', 'warning_date'])
    op.create_table(
        'employee_compensation_rates',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('employee_id', sa.BigInteger(), sa.ForeignKey('employees.id'), nullable=False),
        sa.Column('effective_start_date', sa.Date(), nullable=False),
        sa.Column('effective_end_date', sa.Date()),
        sa.Column('hourly_rate', sa.Numeric(10, 2), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        *_actor_columns(),
        sa.UniqueConstraint('employee_id', 'effective_start_date', name='employee_compensation_rates_employee_start_uniq'),
        sa.CheckConstraint('effective_end_date IS NULL OR effective_end_date >= effective_start_date', name='employee_compensation_rates_date_order_ck'),
        sa.CheckConstraint('hourly_rate >= 0', name='employee_compensation_rates_rate_non_negative_ck'),
    )
    op.create_index('idx_employee_compensation_rates_employee_dates', 'employee_compensation_rates', ['employee_id', 'effective_start_date', 'effective_end_date'])


def downgrade() -> None:
    op.drop_index('idx_employee_compensation_rates_employee_dates', table_name='employee_compensation_rates')
    op.drop_table('employee_compensation_rates')
    op.drop_index('idx_schedule_warnings_store_date', table_name='schedule_warnings')
    op.drop_index('idx_schedule_warnings_period_severity', table_name='schedule_warnings')
    op.drop_table('schedule_warnings')
    op.drop_index('idx_schedule_template_shifts_template_offset', table_name='schedule_template_shifts')
    op.drop_table('schedule_template_shifts')
    op.drop_table('shift_templates')
    op.drop_table('coverage_requirements')
    op.drop_index('store_special_hours_one_active_per_date_uniq', table_name='store_special_hours')
    op.drop_table('store_special_hours')
    op.drop_table('store_operating_hours')
    op.drop_index('idx_time_off_requests_status_dates', table_name='time_off_requests')
    op.drop_index('idx_time_off_requests_employee_dates', table_name='time_off_requests')
    op.drop_table('time_off_requests')
    op.drop_table('time_off_reason_categories')
    op.drop_table('employee_scheduling_store_preferences')
    op.drop_index('idx_employee_scheduling_windows_employee_day', table_name='employee_scheduling_windows')
    op.drop_table('employee_scheduling_windows')
    op.drop_table('employee_scheduling_profiles')
    op.drop_index('idx_schedule_shifts_store_date', table_name='schedule_shifts')
    op.drop_index('idx_schedule_shifts_employee_date', table_name='schedule_shifts')
    op.drop_index('idx_schedule_shifts_period_date', table_name='schedule_shifts')
    op.drop_table('schedule_shifts')
    op.drop_index('idx_schedule_periods_week_status', table_name='schedule_periods')
    op.drop_index('schedule_periods_one_published_per_week_uniq', table_name='schedule_periods')
    op.drop_index('schedule_periods_one_draft_per_week_uniq', table_name='schedule_periods')
    op.drop_table('schedule_periods')
    op.drop_table('schedule_templates')
    op.drop_table('schedule_shift_types')
    op.drop_constraint('employees_principal_id_key', 'employees', type_='unique')
    op.drop_constraint('employees_principal_id_fkey', 'employees', type_='foreignkey')
    op.drop_column('employees', 'principal_id')

    bind = op.get_bind()
    for name in (
        'schedule_warning_severity',
        'time_off_request_status',
        'scheduling_window_kind',
        'schedule_period_status',
    ):
        sa.Enum(name=name).drop(bind, checkfirst=True)
