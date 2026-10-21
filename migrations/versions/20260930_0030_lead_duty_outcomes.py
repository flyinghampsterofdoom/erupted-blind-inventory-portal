"""Audited company-wide Lead responsibility exceptions."""
from alembic import op
import sqlalchemy as sa

revision = '20260930_0030'
down_revision = '20260930_0029'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('lead_duty_outcomes',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('business_date', sa.Date(), nullable=False),
        sa.Column('outcome', sa.String(20), nullable=False),
        sa.Column('employee_id', sa.BigInteger(), sa.ForeignKey('employees.id', ondelete='RESTRICT')),
        sa.Column('scheduled_shift_id', sa.BigInteger(), sa.ForeignKey('schedule_shifts.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('recorded_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('voided_at', sa.DateTime(timezone=True)),
        sa.Column('voided_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id', ondelete='RESTRICT')),
        sa.Column('void_reason', sa.Text()),
        sa.CheckConstraint("outcome IN ('PERFORMED', 'UNCOVERED', 'UNRESOLVED')", name='lead_duty_outcome_kind_ck'),
        sa.CheckConstraint("(outcome = 'PERFORMED') = (employee_id IS NOT NULL)", name='lead_duty_outcome_employee_ck'),
        sa.CheckConstraint('char_length(reason) BETWEEN 1 AND 2000', name='lead_duty_outcome_reason_ck'))
    op.create_index('lead_duty_one_active_day_uniq', 'lead_duty_outcomes', ['business_date'],
                    unique=True, postgresql_where=sa.text('voided_at IS NULL'))


def downgrade():
    if op.get_bind().execute(sa.text('SELECT 1 FROM lead_duty_outcomes LIMIT 1')).first():
        raise RuntimeError('Lead duty audit history must be preserved; downgrade refused.')
    op.drop_table('lead_duty_outcomes')
