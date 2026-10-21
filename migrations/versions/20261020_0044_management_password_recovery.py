"""Allow confirmed standalone management identity recovery; no account changes."""
from alembic import op
import sqlalchemy as sa
revision = '20261020_0044'
down_revision = '20261020_0043'
branch_labels = depends_on = None


def upgrade():
    op.alter_column('password_reset_tokens', 'employee_id', existing_type=sa.BigInteger(), nullable=True)


def downgrade():
    if op.get_bind().execute(sa.text('SELECT EXISTS(SELECT 1 FROM password_reset_tokens WHERE employee_id IS NULL)')).scalar():
        raise RuntimeError('Preserve standalone management reset evidence before downgrade.')
    op.alter_column('password_reset_tokens', 'employee_id', existing_type=sa.BigInteger(), nullable=False)
