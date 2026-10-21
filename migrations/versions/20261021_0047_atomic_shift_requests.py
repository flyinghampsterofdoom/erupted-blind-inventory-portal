"""Extend existing requests for atomic swaps; preserve all historical rows."""
from alembic import op
import sqlalchemy as sa
revision = '20261021_0047'
down_revision = '20261020_0046'
branch_labels = depends_on = None

def upgrade():
    op.add_column('shift_transfer_requests', sa.Column('counterpart_shift_id', sa.BigInteger(), sa.ForeignKey('schedule_shifts.id')))
    op.add_column('shift_transfer_requests', sa.Column('assignment_snapshot', sa.JSON()))
    op.add_column('shift_transfer_requests', sa.Column('operation_key', sa.String(100)))
    op.add_column('shift_transfer_requests', sa.Column('policy_warnings', sa.JSON()))
    op.add_column('shift_transfer_requests', sa.Column('invalidated_at', sa.DateTime(timezone=True)))
    op.add_column('shift_transfer_requests', sa.Column('resolution_reason', sa.Text()))
    op.create_index('shift_request_operation_uq', 'shift_transfer_requests', ['initiated_by_principal_id', 'operation_key'], unique=True)
    op.create_check_constraint('shift_request_distinct_shifts', 'shift_transfer_requests', 'counterpart_shift_id IS NULL OR counterpart_shift_id <> shift_id')

def downgrade():
    # Retain evidence rather than silently losing two-way request semantics.
    bind=op.get_bind()
    if bind.execute(sa.text('SELECT 1 FROM shift_transfer_requests WHERE counterpart_shift_id IS NOT NULL LIMIT 1')).first():
        raise RuntimeError('Use the schema-compatible application rollback; swap evidence must be retained.')
    op.drop_constraint('shift_request_distinct_shifts','shift_transfer_requests',type_='check')
    op.drop_index('shift_request_operation_uq',table_name='shift_transfer_requests')
    for name in ('resolution_reason','invalidated_at','policy_warnings','operation_key','assignment_snapshot','counterpart_shift_id'):
        op.drop_column('shift_transfer_requests', name)
