"""Atomic purchasing draft save receipts; no historical backfill."""
from alembic import op
import sqlalchemy as sa
revision='20261021_0048'
down_revision='20261021_0047'
branch_labels=None
depends_on=None

def upgrade():
    op.create_table('purchasing_draft_batches',
        sa.Column('id',sa.BigInteger(),primary_key=True),
        sa.Column('session_id',sa.BigInteger(),sa.ForeignKey('purchasing_sessions.id'),nullable=False),
        sa.Column('request_key',sa.String(128),nullable=False),
        sa.Column('payload_sha256',sa.String(64),nullable=False),
        sa.Column('result',sa.JSON(),nullable=False),
        sa.Column('actor_id',sa.BigInteger(),sa.ForeignKey('principals.id'),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),server_default=sa.func.now(),nullable=False),
        sa.UniqueConstraint('session_id','request_key',name='purchasing_draft_batch_request_uniq'))

def downgrade():
    op.drop_table('purchasing_draft_batches')
