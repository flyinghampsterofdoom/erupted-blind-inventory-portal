"""Retain read-only catalog category evidence for purchasing eligibility.

No product costs, sales, lifecycle decisions or finalized reports are changed.
"""
from alembic import op
import sqlalchemy as sa

revision = '20261026_0051'
down_revision = '20261024_0050'
branch_labels = depends_on = None


def upgrade():
    op.add_column('ordering_catalog_identity', sa.Column('catalog_attributes', sa.JSON(),
                  nullable=False, server_default=sa.text("'{}'")))


def downgrade():
    op.drop_column('ordering_catalog_identity', 'catalog_attributes')
