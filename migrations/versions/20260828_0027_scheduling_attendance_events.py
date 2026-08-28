"""Add authoritative post-schedule attendance events.

Revision ID: 20260828_0027
Revises: 20260828_0026
Create Date: 2026-08-28
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = '20260828_0027'
down_revision = '20260828_0026'
branch_labels = None
depends_on = None


attendance_event_type = postgresql.ENUM(
    'WORKED_AS_SCHEDULED', 'CALLED_OUT', 'COVERED_SHIFT', 'LATE',
    'OPENED_STORE_LATE', 'NO_CALL_NO_SHOW',
    name='attendance_event_type', create_type=False,
)


def upgrade() -> None:
    attendance_event_type.create(op.get_bind(), checkfirst=True)
    op.create_table(
        'schedule_attendance_events',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('schedule_shift_id', sa.BigInteger(), nullable=False),
        sa.Column('original_employee_id', sa.BigInteger(), nullable=False),
        sa.Column('replacement_employee_id', sa.BigInteger(), nullable=True),
        sa.Column('event_type', attendance_event_type, nullable=False),
        sa.Column('event_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('note', sa.Text(), server_default='', nullable=False),
        sa.Column('recorded_by_principal_id', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('voided_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('voided_by_principal_id', sa.BigInteger(), nullable=True),
        sa.Column('void_reason', sa.Text(), nullable=True),
        sa.CheckConstraint(
            "(event_type = 'COVERED_SHIFT' AND replacement_employee_id IS NOT NULL) OR "
            "(event_type <> 'COVERED_SHIFT' AND replacement_employee_id IS NULL)",
            name='schedule_attendance_events_replacement_ck'),
        sa.CheckConstraint(
            'replacement_employee_id IS NULL OR replacement_employee_id <> original_employee_id',
            name='schedule_attendance_events_distinct_replacement_ck'),
        sa.CheckConstraint('char_length(note) <= 2000', name='schedule_attendance_events_note_length_ck'),
        sa.ForeignKeyConstraint(['schedule_shift_id'], ['schedule_shifts.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['original_employee_id'], ['employees.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['replacement_employee_id'], ['employees.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['recorded_by_principal_id'], ['principals.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['voided_by_principal_id'], ['principals.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'idx_schedule_attendance_events_shift', 'schedule_attendance_events',
        ['schedule_shift_id', 'created_at'])
    op.create_index(
        'idx_schedule_attendance_events_original', 'schedule_attendance_events',
        ['original_employee_id', 'event_at'])
    op.create_index(
        'idx_schedule_attendance_events_replacement', 'schedule_attendance_events',
        ['replacement_employee_id', 'event_at'])
    op.create_index(
        'schedule_attendance_events_one_active_type_uniq', 'schedule_attendance_events',
        ['schedule_shift_id', 'event_type'], unique=True,
        postgresql_where=sa.text('voided_at IS NULL'))


def downgrade() -> None:
    op.drop_index(
        'schedule_attendance_events_one_active_type_uniq',
        table_name='schedule_attendance_events')
    op.drop_index('idx_schedule_attendance_events_replacement', table_name='schedule_attendance_events')
    op.drop_index('idx_schedule_attendance_events_original', table_name='schedule_attendance_events')
    op.drop_index('idx_schedule_attendance_events_shift', table_name='schedule_attendance_events')
    op.drop_table('schedule_attendance_events')
    attendance_event_type.drop(op.get_bind(), checkfirst=True)
