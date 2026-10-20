"""Add non-alternating preferences without rewriting employee configuration."""
from alembic import op
import sqlalchemy as sa

revision = '20261020_0043'
down_revision = '20261020_0042'
branch_labels = depends_on = None


def upgrade():
    op.add_column('employee_scheduling_profiles', sa.Column('weekday_preferences', sa.JSON(), nullable=True))


def downgrade():
    if op.get_bind().execute(sa.text('SELECT EXISTS(SELECT 1 FROM employee_scheduling_profiles WHERE weekday_preferences IS NOT NULL)')).scalar():
        raise RuntimeError('Preserve saved weekday preferences; use a compatible code rollback.')
    op.drop_column('employee_scheduling_profiles', 'weekday_preferences')
