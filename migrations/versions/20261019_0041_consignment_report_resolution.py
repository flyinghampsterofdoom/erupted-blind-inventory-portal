"""Add report snapshots and reversible consignment reporting preferences only."""
from alembic import op
import sqlalchemy as sa
revision = '20261019_0041'
down_revision = '20261019_0040'
branch_labels = depends_on = None

def upgrade():
    op.create_table('consignment_reporting_preferences',
        sa.Column('mapping_id',sa.BigInteger(),sa.ForeignKey('vendor_sku_configs.id'),primary_key=True),
        sa.Column('excluded',sa.Boolean(),nullable=False),sa.Column('zero_cost_stamp',sa.Text()),
        sa.Column('revision',sa.Integer(),nullable=False),
        sa.Column('updated_by',sa.BigInteger(),sa.ForeignKey('principals.id'),nullable=False),
        sa.Column('updated_at',sa.DateTime(timezone=True),server_default=sa.func.now(),nullable=False),
        sa.CheckConstraint('revision > 0',name='consignment_reporting_preferences_revision_ck'))
    op.create_table('consignment_reporting_drafts',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('vendor_id',sa.BigInteger(),sa.ForeignKey('vendors.id'),nullable=False),
        sa.Column('actor_id',sa.BigInteger(),sa.ForeignKey('principals.id'),nullable=False),
        sa.Column('snapshot',sa.JSON(),nullable=False),sa.Column('decisions',sa.JSON(),nullable=False),
        sa.Column('revision',sa.Integer(),nullable=False),sa.Column('status',sa.String(16),nullable=False),
        sa.Column('last_request',sa.String(64)),sa.Column('last_digest',sa.String(64)),
        sa.Column('created_at',sa.DateTime(timezone=True),server_default=sa.func.now(),nullable=False),
        sa.Column('finalized_at',sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('DRAFT','FINALIZED')",name='consignment_reporting_drafts_status_ck'),
        sa.CheckConstraint('revision > 0',name='consignment_reporting_drafts_revision_ck'))
    op.create_index('ix_consignment_reporting_drafts_vendor_created','consignment_reporting_drafts',['vendor_id','created_at'])

def downgrade():
    if op.get_bind().execute(sa.text('SELECT EXISTS(SELECT 1 FROM consignment_reporting_drafts) OR EXISTS(SELECT 1 FROM consignment_reporting_preferences)')).scalar():
        raise RuntimeError('Preserve report decisions and audit evidence; use code rollback.')
    op.drop_table('consignment_reporting_drafts');op.drop_table('consignment_reporting_preferences')
