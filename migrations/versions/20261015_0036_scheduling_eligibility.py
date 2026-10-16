"""Date-based scheduling eligibility; no inferred hire dates or business rewrites."""
from alembic import op
import sqlalchemy as sa

revision = '20261015_0036'
down_revision = '20261015_0035'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('scheduling_eligibility_periods',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('employee_id', sa.BigInteger(), sa.ForeignKey('employees.id'), nullable=False),
        sa.Column('start_date', sa.Date()), sa.Column('end_date', sa.Date()),
        sa.Column('legacy_unknown_start', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('created_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id')),
        sa.Column('updated_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id')),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint('end_date IS NULL OR start_date IS NULL OR end_date >= start_date', name='scheduling_eligibility_dates_ck'),
        sa.CheckConstraint('start_date IS NOT NULL OR legacy_unknown_start', name='scheduling_eligibility_start_ck'))
    op.create_index('ix_scheduling_eligibility_employee', 'scheduling_eligibility_periods', ['employee_id'])
    # Built-in range GiST operators enforce overlap under concurrent SQL writes
    # at every isolation level, without installing btree_gist or another extension.
    op.execute("""ALTER TABLE scheduling_eligibility_periods ADD CONSTRAINT
        scheduling_eligibility_no_overlap EXCLUDE USING gist
        (int8range(employee_id, employee_id, '[]') WITH &&,
         daterange(start_date, end_date, '[]') WITH &&)""")
    # Existing rows stay on the legacy resolver until an explicit dated action.
    # No false hire dates, activation changes, or assignment rewrites.


def downgrade():
    if op.get_bind().execute(sa.text('SELECT 1 FROM scheduling_eligibility_periods LIMIT 1')).first():
        raise RuntimeError('Eligibility history exists; use schema-compatible forward recovery. Destructive downgrade refused.')
    op.drop_table('scheduling_eligibility_periods')
