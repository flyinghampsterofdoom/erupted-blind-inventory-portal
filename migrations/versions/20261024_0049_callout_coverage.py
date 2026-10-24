"""Reuse audited coverage assignments for optional partial-shift intervals."""
from alembic import op
import sqlalchemy as sa

revision = '20261024_0049'
down_revision = '20261021_0048'
branch_labels = depends_on = None


def upgrade():
    op.execute("ALTER TYPE attendance_event_type ADD VALUE IF NOT EXISTS 'LEFT_EARLY'")
    op.add_column('schedule_coverage_commitments', sa.Column('start_time', sa.Time(), nullable=True))
    op.add_column('schedule_coverage_commitments', sa.Column('end_time', sa.Time(), nullable=True))
    op.add_column('schedule_coverage_commitments', sa.Column('unpaid_break_minutes', sa.Integer(), nullable=True))
    op.create_check_constraint('coverage_commitment_interval_ck', 'schedule_coverage_commitments',
        '(start_time IS NULL AND end_time IS NULL) OR '
        '(start_time IS NOT NULL AND end_time IS NOT NULL AND end_time > start_time)')
    op.create_check_constraint('coverage_commitment_break_ck', 'schedule_coverage_commitments',
        'unpaid_break_minutes IS NULL OR unpaid_break_minutes >= 0')


def downgrade():
    if op.get_bind().execute(sa.text("""SELECT
        EXISTS(SELECT 1 FROM schedule_coverage_commitments WHERE start_time IS NOT NULL
            OR end_time IS NOT NULL OR unpaid_break_minutes IS NOT NULL)
        OR EXISTS(SELECT 1 FROM schedule_attendance_events WHERE event_type::text = 'LEFT_EARLY')
        OR EXISTS(SELECT 1 FROM attendance_point_reasons WHERE attendance_event_type::text = 'LEFT_EARLY')
        """)).scalar():
        raise RuntimeError('Coverage and attendance audit history must be preserved; downgrade refused.')
    op.drop_constraint('coverage_commitment_break_ck', 'schedule_coverage_commitments', type_='check')
    op.drop_constraint('coverage_commitment_interval_ck', 'schedule_coverage_commitments', type_='check')
    op.drop_column('schedule_coverage_commitments', 'unpaid_break_minutes')
    op.drop_column('schedule_coverage_commitments', 'end_time')
    op.drop_column('schedule_coverage_commitments', 'start_time')
    # PostgreSQL enum values are additive. An unused value remains compatible
    # with the previous application; never rewrite retained attendance rows.
