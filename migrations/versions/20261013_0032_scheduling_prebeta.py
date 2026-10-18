"""Future-week planning, approvals, requests and bounded emergency inventory access."""
from alembic import op
import sqlalchemy as sa

revision = '20261013_0032'
down_revision = '20260930_0031'
branch_labels = None
depends_on = None


def identity():
    return sa.Column('id', sa.BigInteger(), primary_key=True)


def actor(name):
    return sa.Column(name, sa.BigInteger(), sa.ForeignKey('principals.id', ondelete='RESTRICT'), nullable=False)


def employee():
    return sa.Column('employee_id', sa.BigInteger(), sa.ForeignKey('employees.id', ondelete='RESTRICT'), nullable=False)


def created():
    return sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def upgrade():
    op.drop_constraint('scheduling_org_policy_length_positive_ck', 'scheduling_organization_policies')
    op.create_check_constraint('scheduling_org_policy_length_positive_ck', 'scheduling_organization_policies', 'schedule_length_weeks BETWEEN 1 AND 52')
    op.alter_column('scheduling_organization_policies', 'schedule_length_weeks', server_default='9')
    op.add_column('scheduling_organization_policies', sa.Column('replenish_at_weeks', sa.Integer(), nullable=False, server_default='8'))
    # Preserve established policy values; keep their new threshold valid. Release setup
    # must separately approve changing an existing production horizon to nine.
    op.execute('UPDATE scheduling_organization_policies SET replenish_at_weeks = LEAST(8, schedule_length_weeks - 1)')
    op.create_check_constraint('scheduling_replenish_threshold_ck', 'scheduling_organization_policies', 'replenish_at_weeks >= 0 AND replenish_at_weeks < schedule_length_weeks')
    op.add_column('employees', sa.Column('working_context_version', sa.Integer(), nullable=False, server_default='0'))
    op.create_table('schedule_approvals', identity(),
        sa.Column('schedule_period_id', sa.BigInteger(), sa.ForeignKey('schedule_periods.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False), actor('actor_principal_id'), created(),
        sa.UniqueConstraint('schedule_period_id', 'version', name='schedule_approval_version_uniq'))
    op.create_table('schedule_generation_runs', identity(), actor('actor_principal_id'),
        sa.Column('origin', sa.String(40), nullable=False), sa.Column('status', sa.String(20), nullable=False),
        sa.Column('result', sa.JSON(), nullable=False), created())
    op.create_table('scheduling_preference_requests', identity(), employee(), actor('requested_by_principal_id'),
        sa.Column('start_date', sa.Date(), nullable=False), sa.Column('end_date', sa.Date()),
        sa.Column('preferences', sa.JSON(), nullable=False), sa.Column('status', sa.String(20), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('decided_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id', ondelete='RESTRICT')),
        sa.Column('decided_at', sa.DateTime(timezone=True)), sa.Column('revoked_on', sa.Date()), created(),
        sa.CheckConstraint("status IN ('PENDING','APPROVED','DENIED','WITHDRAWN','REVOKED')", name='preference_request_status_ck'),
        sa.CheckConstraint('end_date IS NULL OR end_date >= start_date', name='preference_request_dates_ck'))
    op.create_table('inventory_store_access_grants', identity(), employee(),
        sa.Column('store_id', sa.BigInteger(), sa.ForeignKey('stores.id', ondelete='RESTRICT'), nullable=False),
        actor('authorizing_principal_id'), sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('context_version', sa.Integer(), nullable=False), created(),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False), sa.Column('revoked_at', sa.DateTime(timezone=True)),
        sa.CheckConstraint('expires_at > created_at', name='inventory_store_grant_expiry_ck'))


def downgrade():
    for table in ('schedule_approvals', 'schedule_generation_runs', 'scheduling_preference_requests', 'inventory_store_access_grants'):
        if op.get_bind().execute(sa.text(f'SELECT 1 FROM {table} LIMIT 1')).first():
            raise RuntimeError('Pre-beta evidence must be preserved; destructive downgrade refused.')
    if op.get_bind().execute(sa.text('SELECT 1 FROM scheduling_organization_policies WHERE schedule_length_weeks > 8 LIMIT 1')).first():
        raise RuntimeError('Nine-week policy requires compatible recovery code; downgrade refused.')
    for table in ('inventory_store_access_grants', 'scheduling_preference_requests', 'schedule_generation_runs', 'schedule_approvals'):
        op.drop_table(table)
    op.drop_column('employees', 'working_context_version')
    op.drop_constraint('scheduling_replenish_threshold_ck', 'scheduling_organization_policies')
    op.drop_column('scheduling_organization_policies', 'replenish_at_weeks')
    op.drop_constraint('scheduling_org_policy_length_positive_ck', 'scheduling_organization_policies')
    op.create_check_constraint('scheduling_org_policy_length_positive_ck', 'scheduling_organization_policies', 'schedule_length_weeks BETWEEN 1 AND 8')
    op.alter_column('scheduling_organization_policies', 'schedule_length_weeks', server_default='8')
