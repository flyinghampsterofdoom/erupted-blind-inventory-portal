"""Add Reports V2 replenishment finalization idempotency.

Revision ID: 20260814_0020
Revises: 20260812_0019
Create Date: 2026-08-14
"""

import sqlalchemy as sa
from alembic import op

revision = "20260814_0020"
down_revision = "20260812_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "purchase_orders",
        sa.Column("creation_idempotency_key", sa.String(64), nullable=True),
    )
    op.create_unique_constraint(
        "purchase_orders_creation_idempotency_key_uniq",
        "purchase_orders",
        ["creation_idempotency_key"],
    )
    op.drop_constraint(
        "reporting_saved_views_report_type_ck",
        "reporting_saved_views",
        type_="check",
    )
    op.create_check_constraint(
        "reporting_saved_views_report_type_ck",
        "reporting_saved_views",
        "report_type IN ('sales_analysis', 'stock_value', 'replenishment')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "reporting_saved_views_report_type_ck",
        "reporting_saved_views",
        type_="check",
    )
    op.create_check_constraint(
        "reporting_saved_views_report_type_ck",
        "reporting_saved_views",
        "report_type IN ('sales_analysis', 'stock_value')",
    )
    op.drop_constraint(
        "purchase_orders_creation_idempotency_key_uniq",
        "purchase_orders",
        type_="unique",
    )
    op.drop_column("purchase_orders", "creation_idempotency_key")
