"""Allow mail worker to read only the immutable vendor-facing attachment columns."""
from alembic import op
revision='p8_0004'
down_revision='p8_0003'
branch_labels=None
depends_on=None

def upgrade():
 op.execute('GRANT SELECT(content,sha256,vendor_authorization_id,format) ON purchasing_v4.vendor_artifact TO erupted_v4_mail_worker')

def downgrade():raise RuntimeError('Retain compatible mail evidence and recover forward')
