"""Optional Square version and exact source freshness, preserving schema 39 data."""
from alembic import op
import sqlalchemy as sa
revision = '20261019_0040'
down_revision = '20261019_0039'
branch_labels = depends_on = None


def upgrade():
    op.alter_column('purchasing_sale_corrections', 'order_version', nullable=True)
    op.create_table('purchasing_order_source_state',
        sa.Column('square_order_id', sa.Text(), primary_key=True),
        sa.Column('location_id', sa.Text(), nullable=False),
        sa.Column('order_version', sa.BigInteger()),
        sa.Column('source_updated_at', sa.Text(), nullable=False),
        sa.Column('source_digest', sa.String(64), nullable=False),
        sa.Column('synchronized_at', sa.DateTime(timezone=True), nullable=False))


def downgrade():
    connection = op.get_bind()
    if connection.execute(sa.text('SELECT EXISTS(SELECT 1 FROM purchasing_order_source_state)')).scalar() or connection.execute(sa.text('SELECT EXISTS(SELECT 1 FROM purchasing_sale_corrections WHERE order_version IS NULL)')).scalar():
        raise RuntimeError('Retain populated source freshness and unversioned correction evidence; use code rollback.')
    op.drop_table('purchasing_order_source_state')
    op.alter_column('purchasing_sale_corrections', 'order_version', nullable=False)
