"""Allow order-derived funding report costs.

Revision ID: 20260803_0016
Revises: 20260803_0015
"""

from alembic import op
import sqlalchemy as sa


revision = '20260803_0016'
down_revision = '20260803_0015'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column('funding_report_lines', 'mapping_id', existing_type=sa.BigInteger(), nullable=True)


def downgrade() -> None:
    op.alter_column('funding_report_lines', 'mapping_id', existing_type=sa.BigInteger(), nullable=False)
