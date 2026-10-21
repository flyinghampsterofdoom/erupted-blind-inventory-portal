"""Add policy-driven scheduling, automation, rotation, locks, and transfers.

Revision ID: 20260824_0021
Revises: 20260814_0020
Create Date: 2026-08-24
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = '20260824_0021'
down_revision = '20260814_0020'
branch_labels = None
depends_on = None


def upgrade() -> None:
    lifecycle = postgresql.ENUM('PLANNED', 'GENERATED', 'REVIEW', 'PUBLISHED', 'CLOSED', name='schedule_lifecycle_stage', create_type=False)
    preference = postgresql.ENUM('PREFERRED', 'ACCEPTABLE', 'AVOID', 'NEVER', name='store_preference_level', create_type=False)
    participation = postgresql.ENUM('NONE', 'PRIMARY', 'ROTATION', name='special_store_participation', create_type=False)
    transfer_status = postgresql.ENUM(
        'PENDING_RECIPIENT', 'DECLINED', 'PENDING_MANAGER', 'APPROVED', 'REJECTED',
        'COMPLETED', 'CANCELLED', name='shift_transfer_status', create_type=False,
    )
    lifecycle.create(op.get_bind(), checkfirst=True)
    preference.create(op.get_bind(), checkfirst=True)
    participation.create(op.get_bind(), checkfirst=True)
    transfer_status.create(op.get_bind(), checkfirst=True)

    op.add_column('schedule_periods', sa.Column('lifecycle_stage', lifecycle, nullable=False, server_default='PLANNED'))
    op.add_column('schedule_periods', sa.Column('generated_at', sa.DateTime(timezone=True)))
    op.add_column('schedule_periods', sa.Column('automatic_publication_at', sa.DateTime(timezone=True)))
    op.add_column('schedule_periods', sa.Column('publication_hold', sa.Boolean(), nullable=False, server_default=sa.text('false')))
    op.add_column('schedule_periods', sa.Column('publication_hold_reason', sa.Text()))
    op.execute("UPDATE schedule_periods SET lifecycle_stage = CASE status::text WHEN 'PUBLISHED' THEN 'PUBLISHED'::schedule_lifecycle_stage WHEN 'ARCHIVED' THEN 'CLOSED'::schedule_lifecycle_stage ELSE 'REVIEW'::schedule_lifecycle_stage END")

    op.add_column('schedule_shifts', sa.Column('manually_locked', sa.Boolean(), nullable=False, server_default=sa.text('false')))
    op.add_column('schedule_shifts', sa.Column('locked_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id')))
    op.add_column('schedule_shifts', sa.Column('locked_at', sa.DateTime(timezone=True)))
    op.add_column('schedule_shifts', sa.Column('lock_reason', sa.Text()))

    op.add_column('employee_scheduling_profiles', sa.Column('approval_weekly_hours', sa.Numeric(6, 2)))
    op.add_column('employee_scheduling_profiles', sa.Column('max_consecutive_work_days', sa.Integer()))
    op.add_column('employee_scheduling_profiles', sa.Column('minimum_days_off_after_max_block', sa.Integer(), nullable=False, server_default='1'))
    op.add_column('employee_scheduling_profiles', sa.Column('special_store_participation', participation, nullable=False, server_default='NONE'))
    op.create_check_constraint('employee_scheduling_profiles_approval_hours_positive_ck', 'employee_scheduling_profiles', 'approval_weekly_hours IS NULL OR approval_weekly_hours > 0')
    op.create_check_constraint('employee_scheduling_profiles_consecutive_positive_ck', 'employee_scheduling_profiles', 'max_consecutive_work_days IS NULL OR max_consecutive_work_days > 0')
    op.create_check_constraint('employee_scheduling_profiles_days_off_non_negative_ck', 'employee_scheduling_profiles', 'minimum_days_off_after_max_block >= 0')
    op.add_column('employee_scheduling_store_preferences', sa.Column('preference_level', preference, nullable=False, server_default='ACCEPTABLE'))
    op.execute("UPDATE employee_scheduling_store_preferences SET preference_level = CASE WHEN preference_rank = 1 THEN 'PREFERRED'::store_preference_level WHEN preference_rank IS NULL OR preference_rank <= 3 THEN 'ACCEPTABLE'::store_preference_level ELSE 'AVOID'::store_preference_level END")

    op.create_table(
        'scheduling_organization_policies',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('weekly_approval_hours', sa.Numeric(6, 2), nullable=False, server_default='40'),
        sa.Column('schedule_length_weeks', sa.Integer(), nullable=False, server_default='3'),
        sa.Column('generate_days_before_end', sa.Integer(), nullable=False, server_default='7'),
        sa.Column('publish_days_before_end', sa.Integer(), nullable=False, server_default='3'),
        sa.Column('publication_local_time', sa.Time(), nullable=False, server_default='09:00:00'),
        sa.Column('timezone_name', sa.String(64), nullable=False, server_default='America/Los_Angeles'),
        sa.Column('active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('updated_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint('weekly_approval_hours > 0', name='scheduling_org_policy_hours_positive_ck'),
        sa.CheckConstraint('schedule_length_weeks > 0', name='scheduling_org_policy_length_positive_ck'),
        sa.CheckConstraint('generate_days_before_end >= 0', name='scheduling_org_policy_generate_non_negative_ck'),
        sa.CheckConstraint('publish_days_before_end >= 0', name='scheduling_org_policy_publish_non_negative_ck'),
    )
    # No principal is fabricated here. The service creates the singleton policy on first authorized save/use.
    op.create_table(
        'special_store_policies',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('store_id', sa.BigInteger(), sa.ForeignKey('stores.id'), nullable=False, unique=True),
        sa.Column('active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('created_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        sa.Column('updated_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        'special_store_rotation_states',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('store_id', sa.BigInteger(), sa.ForeignKey('stores.id'), nullable=False),
        sa.Column('employee_id', sa.BigInteger(), sa.ForeignKey('employees.id'), nullable=False),
        sa.Column('participation', participation, nullable=False, server_default='ROTATION'),
        sa.Column('queue_position', sa.Integer(), nullable=False),
        sa.Column('assignment_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_assigned_at', sa.DateTime(timezone=True)),
        sa.Column('last_assigned_shift_id', sa.BigInteger(), sa.ForeignKey('schedule_shifts.id')),
        sa.Column('temporarily_skipped_at', sa.DateTime(timezone=True)),
        sa.Column('skip_reason', sa.Text()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('store_id', 'employee_id', name='special_store_rotation_employee_uniq'),
    )
    op.create_table(
        'scheduling_notifications',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        sa.Column('event_type', sa.String(64), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('entity_type', sa.String(64), nullable=False),
        sa.Column('entity_id', sa.BigInteger(), nullable=False),
        sa.Column('read_at', sa.DateTime(timezone=True)),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index('idx_scheduling_notifications_principal_unread', 'scheduling_notifications', ['principal_id', 'read_at'])
    op.create_table(
        'shift_transfer_requests',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('shift_id', sa.BigInteger(), sa.ForeignKey('schedule_shifts.id'), nullable=False),
        sa.Column('from_employee_id', sa.BigInteger(), sa.ForeignKey('employees.id'), nullable=False),
        sa.Column('to_employee_id', sa.BigInteger(), sa.ForeignKey('employees.id'), nullable=False),
        sa.Column('initiated_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        sa.Column('status', transfer_status, nullable=False, server_default='PENDING_RECIPIENT'),
        sa.Column('existing_scheduled_hours', sa.Numeric(6, 2)),
        sa.Column('shift_hours', sa.Numeric(6, 2)),
        sa.Column('resulting_scheduled_hours', sa.Numeric(6, 2)),
        sa.Column('approval_threshold_hours', sa.Numeric(6, 2)),
        sa.Column('amount_over_threshold', sa.Numeric(6, 2)),
        sa.Column('recipient_responded_at', sa.DateTime(timezone=True)),
        sa.Column('manager_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id')),
        sa.Column('manager_responded_at', sa.DateTime(timezone=True)),
        sa.Column('manager_note', sa.Text()),
        sa.Column('completed_at', sa.DateTime(timezone=True)),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index('shift_transfer_one_active_per_shift_uniq', 'shift_transfer_requests', ['shift_id'], unique=True, postgresql_where=sa.text("status IN ('PENDING_RECIPIENT', 'PENDING_MANAGER')"))


def downgrade() -> None:
    op.drop_table('shift_transfer_requests')
    op.drop_table('scheduling_notifications')
    op.drop_table('special_store_rotation_states')
    op.drop_table('special_store_policies')
    op.drop_table('scheduling_organization_policies')
    op.drop_column('employee_scheduling_store_preferences', 'preference_level')
    for name in ('employee_scheduling_profiles_days_off_non_negative_ck', 'employee_scheduling_profiles_consecutive_positive_ck', 'employee_scheduling_profiles_approval_hours_positive_ck'):
        op.drop_constraint(name, 'employee_scheduling_profiles', type_='check')
    for name in ('special_store_participation', 'minimum_days_off_after_max_block', 'max_consecutive_work_days', 'approval_weekly_hours'):
        op.drop_column('employee_scheduling_profiles', name)
    for name in ('lock_reason', 'locked_at', 'locked_by_principal_id', 'manually_locked'):
        op.drop_column('schedule_shifts', name)
    for name in ('publication_hold_reason', 'publication_hold', 'automatic_publication_at', 'generated_at', 'lifecycle_stage'):
        op.drop_column('schedule_periods', name)
    bind = op.get_bind()
    for enum_name in ('shift_transfer_status', 'special_store_participation', 'store_preference_level', 'schedule_lifecycle_stage'):
        sa.Enum(name=enum_name).drop(bind, checkfirst=True)
