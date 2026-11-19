"""Owner call-out relief retains employee intent and all coverage history."""
from alembic import op
import sqlalchemy as sa

revision = '20261119_0057'
down_revision = '20261107_0056'
branch_labels = depends_on = None

ASSIGNEE = (
    '(employee_id IS NOT NULL AND owner_principal_id IS NULL) OR '
    '(employee_id IS NULL AND owner_principal_id IS NOT NULL)'
)


def upgrade():
    op.add_column('schedule_coverage_commitments',
        sa.Column('owner_principal_id', sa.BigInteger(), nullable=True))
    op.create_foreign_key('coverage_commitment_owner_fk',
        'schedule_coverage_commitments', 'principals',
        ['owner_principal_id'], ['id'], ondelete='RESTRICT')
    op.alter_column('schedule_coverage_commitments', 'employee_id',
        existing_type=sa.BigInteger(), nullable=True)
    op.create_check_constraint('coverage_commitment_assignee_ck',
        'schedule_coverage_commitments', ASSIGNEE)
    op.create_check_constraint('coverage_commitment_owner_lead_ck',
        'schedule_coverage_commitments', 'owner_principal_id IS NULL OR NOT is_shift_lead')


def downgrade():
    if op.get_bind().execute(sa.text(
        'SELECT 1 FROM schedule_coverage_commitments WHERE owner_principal_id IS NOT NULL LIMIT 1'
    )).first():
        raise RuntimeError('Preserve Owner coverage audit history; use a schema-compatible rollback.')
    op.drop_constraint('coverage_commitment_owner_lead_ck',
        'schedule_coverage_commitments', type_='check')
    op.drop_constraint('coverage_commitment_assignee_ck',
        'schedule_coverage_commitments', type_='check')
    op.alter_column('schedule_coverage_commitments', 'employee_id',
        existing_type=sa.BigInteger(), nullable=False)
    op.drop_constraint('coverage_commitment_owner_fk',
        'schedule_coverage_commitments', type_='foreignkey')
    op.drop_column('schedule_coverage_commitments', 'owner_principal_id')
