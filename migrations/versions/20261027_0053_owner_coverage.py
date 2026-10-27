"""Owner coverage references an account, never an employee or payroll identity."""
from alembic import op
import sqlalchemy as sa
revision = '20261027_0053'
down_revision = '20261026_0052'
branch_labels = depends_on = None

def upgrade():
    op.add_column('schedule_shifts', sa.Column('owner_principal_id', sa.BigInteger(), nullable=True))
    op.create_foreign_key('schedule_shift_owner_fk', 'schedule_shifts', 'principals', ['owner_principal_id'], ['id'], ondelete='RESTRICT')
    op.create_check_constraint('schedule_shift_owner_exclusive_ck', 'schedule_shifts', 'owner_principal_id IS NULL OR employee_id IS NULL')

def downgrade():
    if op.get_bind().execute(sa.text('SELECT 1 FROM schedule_shifts WHERE owner_principal_id IS NOT NULL LIMIT 1')).first():
        raise RuntimeError('Preserve Owner Coverage evidence; use a schema-compatible rollback.')
    op.drop_constraint('schedule_shift_owner_exclusive_ck', 'schedule_shifts', type_='check')
    op.drop_constraint('schedule_shift_owner_fk', 'schedule_shifts', type_='foreignkey')
    op.drop_column('schedule_shifts', 'owner_principal_id')
