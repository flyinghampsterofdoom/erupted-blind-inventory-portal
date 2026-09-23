"""Add an optional inclusive employee Scheduling cutoff."""
from alembic import op
import sqlalchemy as sa

revision = '20260923_0026'
down_revision = '20260922_0025'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('employees', sa.Column('last_effective_date', sa.Date(), nullable=True))


def downgrade():
    op.drop_column('employees', 'last_effective_date')
