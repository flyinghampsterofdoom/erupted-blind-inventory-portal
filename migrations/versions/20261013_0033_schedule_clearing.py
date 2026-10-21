"""Retire operational schedules while retaining linked historical evidence."""
from alembic import op
import sqlalchemy as sa
revision = '20261013_0033'
down_revision = '20261013_0032'
branch_labels = None
depends_on = None

def upgrade():
    op.add_column('schedule_periods', sa.Column('cleared_at', sa.DateTime(timezone=True)))

def downgrade():
    if op.get_bind().execute(sa.text('SELECT 1 FROM schedule_periods WHERE cleared_at IS NOT NULL LIMIT 1')).first():
        raise RuntimeError('Cleared schedule evidence requires compatible recovery code; downgrade refused.')
    op.drop_column('schedule_periods', 'cleared_at')
