"""Configure Training palette hours and allow a standalone paid training shift."""
from alembic import op
import sqlalchemy as sa

revision = '20261107_0056'
down_revision = '20261103_0055'
branch_labels = depends_on = None

TRAINING_SHIFT = (
    'employee_id IS NOT NULL AND NOT is_lead_of_day AND NOT is_shift_lead '
    'AND NOT is_double_coverage AND owner_principal_id IS NULL'
)


def upgrade():
    op.add_column('scheduling_store_defaults', sa.Column('training_default_store_id', sa.BigInteger()))
    op.create_foreign_key('scheduling_training_default_store_fk', 'scheduling_store_defaults', 'stores',
        ['training_default_store_id'], ['id'], ondelete='SET NULL')
    op.add_column('scheduling_store_defaults', sa.Column('training_shift_start', sa.Time()))
    op.add_column('scheduling_store_defaults', sa.Column('training_shift_end', sa.Time()))
    op.add_column('scheduling_store_defaults', sa.Column(
        'training_unpaid_break_minutes', sa.Integer(), nullable=False, server_default='0'))
    op.create_check_constraint('scheduling_training_shift_defaults_ck', 'scheduling_store_defaults',
        "(training_shift_start IS NULL AND training_shift_end IS NULL AND training_unpaid_break_minutes = 0) OR "
        "(training_shift_start IS NOT NULL AND training_shift_end IS NOT NULL AND "
        "training_unpaid_break_minutes >= 0 AND training_shift_end - training_shift_start > "
        "training_unpaid_break_minutes * interval '1 minute')")
    op.drop_constraint('schedule_shift_training_ck', 'schedule_shifts', type_='check')
    op.create_check_constraint('schedule_shift_training_ck', 'schedule_shifts',
        f'NOT is_training OR ({TRAINING_SHIFT})')


def downgrade():
    if op.get_bind().execute(sa.text(
        'SELECT 1 FROM schedule_shifts WHERE is_training AND training_id IS NULL '
        'UNION ALL SELECT 1 FROM scheduling_store_defaults WHERE training_shift_start IS NOT NULL OR training_default_store_id IS NOT NULL LIMIT 1'
    )).first():
        raise RuntimeError('Training assignments and configured hours require a compatible rollback.')
    op.drop_constraint('schedule_shift_training_ck', 'schedule_shifts', type_='check')
    op.create_check_constraint('schedule_shift_training_ck', 'schedule_shifts',
        f'NOT is_training OR (training_id IS NOT NULL AND {TRAINING_SHIFT})')
    op.drop_constraint('scheduling_training_shift_defaults_ck', 'scheduling_store_defaults', type_='check')
    op.drop_constraint('scheduling_training_default_store_fk', 'scheduling_store_defaults', type_='foreignkey')
    for column in ('training_unpaid_break_minutes', 'training_shift_end', 'training_shift_start', 'training_default_store_id'):
        op.drop_column('scheduling_store_defaults', column)
