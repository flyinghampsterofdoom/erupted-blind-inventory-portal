"""Reusable purchasing source history; no financial/inventory backfill or rewrite."""
from alembic import op
import sqlalchemy as sa
revision = '20261019_0039'
down_revision = '20261018_0038'
branch_labels = depends_on = None


def upgrade():
    op.create_table('purchasing_history_coverage',
        sa.Column('source', sa.String(16), primary_key=True),
        sa.Column('location_id', sa.Text(), primary_key=True),
        sa.Column('start_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('through_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('verified_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("source IN ('SALES','MOVEMENTS')", name='purchasing_history_source_ck'),
        sa.CheckConstraint('through_at > start_at', name='purchasing_history_interval_ck'))
    op.create_table('purchasing_inventory_events',
        sa.Column('event_type', sa.String(16), primary_key=True),
        sa.Column('event_id', sa.Text(), primary_key=True),
        sa.Column('location_id', sa.Text(), nullable=False),
        sa.Column('variation_id', sa.Text(), nullable=False),
        sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('business_date', sa.Date(), nullable=False),
        sa.Column('quantity_delta', sa.Numeric(), nullable=False),
        sa.Column('physical_count', sa.Boolean(), nullable=False),
        sa.Column('source_updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('calculated_after_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('calculated_before_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('source_digest', sa.String(64), nullable=False),
        sa.Column('synchronized_at', sa.DateTime(timezone=True), nullable=False))
    op.create_index('idx_purchasing_events_scope', 'purchasing_inventory_events', ['location_id','business_date','variation_id'])
    op.create_table('purchasing_sale_corrections',
        sa.Column('kind', sa.String(8), primary_key=True, server_default='LINE'),
        sa.Column('square_order_id', sa.Text(), primary_key=True),
        sa.Column('square_line_item_uid', sa.Text(), primary_key=True),
        sa.Column('location_id', sa.Text(), nullable=False),
        sa.Column('variation_id', sa.Text()),
        sa.Column('business_date', sa.Date(), nullable=False),
        sa.Column('quantity', sa.Numeric(), nullable=False),
        sa.Column('order_version', sa.BigInteger(), nullable=False),
        sa.Column('reason', sa.String(40), nullable=False),
        sa.Column('source_digest', sa.String(64), nullable=False),
        sa.Column('synchronized_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('LINE','RETURN')", name='purchasing_sale_correction_kind_ck'),
        sa.CheckConstraint('quantity >= 0', name='purchasing_sale_correction_quantity_ck'))
    op.create_index('idx_purchasing_sale_correction_date','purchasing_sale_corrections',['location_id','business_date'])


def downgrade():
    tables=('purchasing_sale_corrections','purchasing_inventory_events','purchasing_history_coverage')
    for table in tables:
        if op.get_bind().execute(sa.text('SELECT EXISTS(SELECT 1 FROM '+table+')')).scalar():
            raise RuntimeError('Retain nonempty purchasing source evidence during code rollback.')
    for table in tables:op.drop_table(table)
