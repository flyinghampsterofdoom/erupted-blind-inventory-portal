"""Effective weekly allocations with an undated compatibility baseline."""
from alembic import op
import sqlalchemy as sa

revision = '20261016_0037'
down_revision = '20261015_0036'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('scheduling_allocation_changes',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('employee_id', sa.BigInteger(), sa.ForeignKey('employees.id'), nullable=False),
        sa.Column('effective_date', sa.Date()),
        sa.Column('shifts_per_week', sa.Integer()),
        sa.Column('baseline', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('revision', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('replaces_id', sa.BigInteger(), sa.ForeignKey('scheduling_allocation_changes.id')),
        sa.Column('created_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id')),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint('shifts_per_week IS NULL OR shifts_per_week BETWEEN 0 AND 7', name='scheduling_allocation_range_ck'),
        sa.CheckConstraint('effective_date IS NOT NULL OR baseline', name='scheduling_allocation_baseline_ck'),
        sa.CheckConstraint('NOT baseline OR effective_date IS NULL', name='scheduling_allocation_undated_ck'),
        sa.CheckConstraint('baseline OR shifts_per_week IS NOT NULL', name='scheduling_allocation_value_ck'))
    op.create_index('uq_scheduling_allocation_date', 'scheduling_allocation_changes', ['employee_id', 'effective_date'], unique=True, postgresql_where=sa.text('active AND NOT baseline'))
    op.create_index('uq_scheduling_allocation_baseline', 'scheduling_allocation_changes', ['employee_id'], unique=True, postgresql_where=sa.text('baseline'))
    op.execute('''INSERT INTO scheduling_allocation_changes
        (employee_id, shifts_per_week, baseline, created_by_principal_id)
        SELECT employee_id, target_shifts_per_week, true, updated_by_principal_id
        FROM employee_scheduling_profiles''')


def downgrade():
    if op.get_bind().execute(sa.text('SELECT 1 FROM scheduling_allocation_changes WHERE NOT baseline LIMIT 1')).first():
        raise RuntimeError('Allocation history exists. Use schema-compatible forward recovery; destructive downgrade refused.')
    op.drop_table('scheduling_allocation_changes')
