"""Allow individual Store employees to use scheduled scope without a fixed store.

No account links, role defaults, schedules, or operational records are rewritten.
"""
from alembic import op
import sqlalchemy as sa

revision = '20261015_0035'
down_revision = '20261014_0034'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint('principals_store_role_ck', 'principals', type_='check')
    op.create_check_constraint('principals_store_role_ck', 'principals',
                               "role = 'STORE' OR store_id IS NULL")


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM principals WHERE role='STORE' AND store_id IS NULL LIMIT 1")).first():
        raise RuntimeError('Store accounts without fixed assignments require compatible forward recovery; downgrade refused.')
    op.drop_constraint('principals_store_role_ck', 'principals', type_='check')
    op.create_check_constraint('principals_store_role_ck', 'principals',
        "(role = 'STORE' AND store_id IS NOT NULL) OR (role IN ('ADMIN','MANAGER','LEAD') AND store_id IS NULL)")
