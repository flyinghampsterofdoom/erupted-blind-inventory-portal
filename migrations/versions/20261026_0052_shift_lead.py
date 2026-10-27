"""Shift-scoped Lead designations, separate from employment and account roles."""
from alembic import op
import sqlalchemy as sa
revision = '20261026_0052'
down_revision = '20261026_0051'
branch_labels = depends_on = None


def upgrade():
    for table in ('schedule_shifts', 'schedule_coverage_commitments'):
        op.add_column(table, sa.Column('is_shift_lead', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_check_constraint('schedule_shift_lead_assigned_ck', 'schedule_shifts',
                               'NOT is_shift_lead OR employee_id IS NOT NULL')


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM schedule_shifts WHERE is_shift_lead UNION ALL SELECT 1 FROM schedule_coverage_commitments WHERE is_shift_lead LIMIT 1")).first():
        raise RuntimeError('Retain shift Lead evidence; use a schema-compatible application rollback.')
    op.drop_constraint('schedule_shift_lead_assigned_ck', 'schedule_shifts', type_='check')
    for table in ('schedule_shifts', 'schedule_coverage_commitments'):
        op.drop_column(table, 'is_shift_lead')
