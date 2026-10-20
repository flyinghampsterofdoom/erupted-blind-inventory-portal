"""Employee PTO amendments and explicit weekly leave allocations; no backfill."""
from alembic import op
import sqlalchemy as sa
revision = '20261020_0046'
down_revision = '20261020_0045'
branch_labels = depends_on = None


def upgrade():
    op.add_column('time_off_requests', sa.Column('revision', sa.Integer(), nullable=False, server_default='1'))
    op.add_column('time_off_requests', sa.Column('replaces_request_id', sa.BigInteger(), sa.ForeignKey('time_off_requests.id')))
    op.add_column('time_off_requests', sa.Column('requests_cancellation', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_index('time_off_pending_amendment_uq', 'time_off_requests', ['replaces_request_id'], unique=True,
                    postgresql_where=sa.text("status = 'PENDING' AND replaces_request_id IS NOT NULL"))
    op.create_table('scheduling_leave_allocation_exceptions',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('employee_id', sa.BigInteger(), sa.ForeignKey('employees.id'), nullable=False),
        sa.Column('request_id', sa.BigInteger(), sa.ForeignKey('time_off_requests.id'), nullable=False),
        sa.Column('week_start_date', sa.Date(), nullable=False),
        sa.Column('normal_shifts', sa.Integer(), nullable=False),
        sa.Column('work_shifts', sa.Integer(), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('revoked_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id')),
        sa.Column('revoked_at', sa.DateTime(timezone=True)),
        sa.Column('revocation_reason', sa.Text()),
        sa.CheckConstraint('work_shifts = 0', name='leave_allocation_zero_ck'),
        sa.CheckConstraint('EXTRACT(DOW FROM week_start_date) = 0', name='leave_allocation_sunday_ck'))
    op.create_index('leave_allocation_request_idx', 'scheduling_leave_allocation_exceptions', ['request_id'])
    op.create_index('leave_allocation_active_week_uq', 'scheduling_leave_allocation_exceptions',
                    ['employee_id', 'week_start_date'], unique=True, postgresql_where=sa.text('active'))


def downgrade():
    db = op.get_bind()
    if db.scalar(sa.text('SELECT EXISTS(SELECT 1 FROM scheduling_leave_allocation_exceptions) OR EXISTS(SELECT 1 FROM time_off_requests WHERE replaces_request_id IS NOT NULL OR revision > 1)')):
        raise RuntimeError('Preserve PTO amendments and allocation evidence; use a compatible code rollback.')
    op.drop_table('scheduling_leave_allocation_exceptions')
    op.drop_index('time_off_pending_amendment_uq', table_name='time_off_requests')
    for column in ('requests_cancellation', 'replaces_request_id', 'revision'):
        op.drop_column('time_off_requests', column)
