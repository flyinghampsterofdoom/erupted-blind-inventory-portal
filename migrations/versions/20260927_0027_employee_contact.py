"""Local contact information on the canonical employee; no identity backfill."""
from alembic import op
import sqlalchemy as sa

revision = '20260927_0027'
down_revision = '20260923_0026'
branch_labels = None
depends_on = None

FIELDS = (('preferred_name', 200), ('phone', 50), ('email', 254),
          ('street_address', 300), ('city', 100), ('state', 100), ('postal_code', 30))


def upgrade():
    for name, size in FIELDS:
        op.add_column('employees', sa.Column(name, sa.String(size), nullable=True))


def downgrade():
    for name, _ in reversed(FIELDS):
        op.drop_column('employees', name)
