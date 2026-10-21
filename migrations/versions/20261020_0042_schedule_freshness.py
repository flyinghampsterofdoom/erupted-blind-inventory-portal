"""Track hard-constraint invalidation without changing any saved assignment."""
from alembic import op
import sqlalchemy as sa

revision = '20261020_0042'
down_revision = '20261019_0041'
branch_labels = depends_on = None


def upgrade():
    op.add_column('schedule_periods', sa.Column('constraints_changed_at', sa.DateTime(timezone=True), nullable=True))


def downgrade():
    op.drop_column('schedule_periods', 'constraints_changed_at')
