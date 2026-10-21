"""Add auditable attendance point entries.

Revision ID: 20260828_0028
Revises: 20260828_0027
Create Date: 2026-08-28
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = '20260828_0028'
down_revision = '20260828_0027'
branch_labels = None
depends_on = None

attendance_event_type = postgresql.ENUM(
    'WORKED_AS_SCHEDULED', 'CALLED_OUT', 'COVERED_SHIFT', 'LATE',
    'OPENED_STORE_LATE', 'NO_CALL_NO_SHOW', name='attendance_event_type', create_type=False)


def upgrade() -> None:
    op.create_table(
        'attendance_point_reasons',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('code', sa.String(length=100), nullable=False),
        sa.Column('label', sa.String(length=200), nullable=False),
        sa.Column('point_value', sa.Numeric(8, 2), nullable=False),
        sa.Column('description', sa.Text(), server_default='', nullable=False),
        sa.Column('attendance_event_type', attendance_event_type, nullable=True),
        sa.Column('active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('created_by_principal_id', sa.BigInteger(), nullable=False),
        sa.Column('updated_by_principal_id', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("code ~ '^[A-Z][A-Z0-9_]*$'", name='attendance_point_reasons_code_format_ck'),
        sa.CheckConstraint('point_value <> 0', name='attendance_point_reasons_value_nonzero_ck'),
        sa.CheckConstraint('char_length(btrim(label)) BETWEEN 1 AND 200', name='attendance_point_reasons_label_length_ck'),
        sa.CheckConstraint('char_length(description) <= 2000', name='attendance_point_reasons_description_length_ck'),
        sa.ForeignKeyConstraint(['created_by_principal_id'], ['principals.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['updated_by_principal_id'], ['principals.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'), sa.UniqueConstraint('code'),
    )
    op.create_table(
        'attendance_point_entries',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('employee_id', sa.BigInteger(), nullable=False),
        sa.Column('attendance_event_id', sa.BigInteger(), nullable=True),
        sa.Column('schedule_shift_id', sa.BigInteger(), nullable=True),
        sa.Column('amount', sa.Numeric(8, 2), nullable=False),
        sa.Column('entry_kind', sa.String(length=16), nullable=False),
        sa.Column('point_reason_id', sa.BigInteger(), nullable=True),
        sa.Column('reason_code_snapshot', sa.String(length=100), nullable=True),
        sa.Column('reason_label_snapshot', sa.String(length=200), nullable=True),
        sa.Column('category', sa.String(length=100), nullable=False),
        sa.Column('effective_date', sa.Date(), nullable=False),
        sa.Column('management_note', sa.Text(), server_default='', nullable=False),
        sa.Column('assigned_by_principal_id', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('reversed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('reversed_by_principal_id', sa.BigInteger(), nullable=True),
        sa.Column('reversal_reason', sa.Text(), nullable=True),
        sa.Column('replaces_point_entry_id', sa.BigInteger(), nullable=True),
        sa.CheckConstraint('amount <> 0', name='attendance_point_entries_amount_nonzero_ck'),
        sa.CheckConstraint("entry_kind IN ('POLICY', 'MANUAL')", name='attendance_point_entries_kind_ck'),
        sa.CheckConstraint(
            "(entry_kind = 'POLICY' AND point_reason_id IS NOT NULL AND reason_code_snapshot IS NOT NULL) OR "
            "(entry_kind = 'MANUAL' AND point_reason_id IS NULL)", name='attendance_point_entries_policy_link_ck'),
        sa.CheckConstraint(
            "char_length(btrim(category)) BETWEEN 1 AND 100",
            name='attendance_point_entries_category_length_ck'),
        sa.CheckConstraint(
            'char_length(management_note) <= 2000',
            name='attendance_point_entries_note_length_ck'),
        sa.CheckConstraint(
            'reversal_reason IS NULL OR char_length(reversal_reason) <= 2000',
            name='attendance_point_entries_reversal_reason_length_ck'),
        sa.CheckConstraint(
            '(reversed_at IS NULL AND reversed_by_principal_id IS NULL AND reversal_reason IS NULL) OR '
            '(reversed_at IS NOT NULL AND reversed_by_principal_id IS NOT NULL '
            "AND char_length(btrim(reversal_reason)) > 0)",
            name='attendance_point_entries_reversal_state_ck'),
        sa.CheckConstraint(
            'replaces_point_entry_id IS NULL OR replaces_point_entry_id <> id',
            name='attendance_point_entries_replacement_distinct_ck'),
        sa.ForeignKeyConstraint(['employee_id'], ['employees.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(
            ['attendance_event_id'], ['schedule_attendance_events.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['schedule_shift_id'], ['schedule_shifts.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['point_reason_id'], ['attendance_point_reasons.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(
            ['assigned_by_principal_id'], ['principals.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(
            ['reversed_by_principal_id'], ['principals.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(
            ['replaces_point_entry_id'], ['attendance_point_entries.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'idx_attendance_point_entries_employee', 'attendance_point_entries',
        ['employee_id', 'effective_date', 'created_at'])
    op.create_index(
        'idx_attendance_point_entries_event', 'attendance_point_entries', ['attendance_event_id'])
    op.create_index(
        'idx_attendance_point_entries_shift', 'attendance_point_entries', ['schedule_shift_id'])


def downgrade() -> None:
    op.drop_index('idx_attendance_point_entries_shift', table_name='attendance_point_entries')
    op.drop_index('idx_attendance_point_entries_event', table_name='attendance_point_entries')
    op.drop_index('idx_attendance_point_entries_employee', table_name='attendance_point_entries')
    op.drop_table('attendance_point_entries')
    op.drop_table('attendance_point_reasons')
