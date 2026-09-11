"""Represent unknown vendor SKU cost without manufacturing zero.

Revision ID: 20260911_0024
Revises: 20260828_0028
Create Date: 2026-09-11
"""

import sqlalchemy as sa
from alembic import op


revision = '20260911_0024'
down_revision = '20260828_0028'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        'vendor_sku_configs',
        'unit_cost',
        existing_type=sa.Numeric(14, 4),
        nullable=True,
        server_default=None,
    )


def downgrade() -> None:
    # Refuse a lossy downgrade: NULL means unknown and must not be converted to $0.
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM vendor_sku_configs WHERE unit_cost IS NULL) THEN
            RAISE EXCEPTION
              'Cannot downgrade while vendor_sku_configs.unit_cost contains unknown values';
          END IF;
        END
        $$
        """
    )
    op.alter_column(
        'vendor_sku_configs',
        'unit_cost',
        existing_type=sa.Numeric(14, 4),
        nullable=False,
        server_default=sa.text('0'),
    )
