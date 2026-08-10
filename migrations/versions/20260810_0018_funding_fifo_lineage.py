"""Persist purchase-lot lineage for credit-card Funding reports.

Revision ID: 20260810_0018
Revises: 20260805_0017
Create Date: 2026-08-10
"""

import sqlalchemy as sa
from alembic import op


revision = '20260810_0018'
down_revision = '20260805_0017'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('funding_report_lines', sa.Column('purchase_order_line_id', sa.BigInteger()))
    op.add_column(
        'funding_report_lines', sa.Column('purchase_order_receipt_line_id', sa.BigInteger())
    )
    op.add_column(
        'funding_report_lines', sa.Column('lot_received_at_snapshot', sa.DateTime(timezone=True))
    )
    op.create_foreign_key(
        'funding_report_lines_purchase_order_line_id_fkey',
        'funding_report_lines', 'purchase_order_lines', ['purchase_order_line_id'], ['id'],
    )
    op.create_foreign_key(
        'funding_report_lines_purchase_order_receipt_line_id_fkey',
        'funding_report_lines', 'purchase_order_receipt_lines',
        ['purchase_order_receipt_line_id'], ['id'],
    )

    op.add_column(
        'funding_report_fact_links', sa.Column('allocated_quantity', sa.Numeric(14, 3))
    )
    op.drop_constraint(
        'funding_report_fact_links_sale_uniq', 'funding_report_fact_links', type_='unique'
    )
    op.drop_constraint(
        'funding_report_fact_links_return_uniq', 'funding_report_fact_links', type_='unique'
    )
    op.create_unique_constraint(
        'funding_report_fact_links_sale_line_uniq', 'funding_report_fact_links',
        ['report_id', 'sale_fact_id', 'report_line_id'],
    )
    op.create_unique_constraint(
        'funding_report_fact_links_return_line_uniq', 'funding_report_fact_links',
        ['report_id', 'return_fact_id', 'report_line_id'],
    )


def downgrade() -> None:
    op.drop_constraint(
        'funding_report_fact_links_return_line_uniq',
        'funding_report_fact_links', type_='unique',
    )
    op.drop_constraint(
        'funding_report_fact_links_sale_line_uniq',
        'funding_report_fact_links', type_='unique',
    )
    for source_column in ('sale_fact_id', 'return_fact_id'):
        op.execute(sa.text(f"""
            WITH grouped AS (
                SELECT report_id, {source_column} AS source_id,
                       MIN(id) AS keep_id,
                       SUM(cogs_amount_snapshot) AS total_cogs
                FROM funding_report_fact_links
                WHERE {source_column} IS NOT NULL
                GROUP BY report_id, {source_column}
                HAVING COUNT(*) > 1
            )
            UPDATE funding_report_fact_links AS link
            SET cogs_amount_snapshot = grouped.total_cogs
            FROM grouped
            WHERE link.id = grouped.keep_id
        """))
        op.execute(sa.text(f"""
            DELETE FROM funding_report_fact_links AS link
            USING (
                SELECT report_id, {source_column} AS source_id, MIN(id) AS keep_id
                FROM funding_report_fact_links
                WHERE {source_column} IS NOT NULL
                GROUP BY report_id, {source_column}
            ) AS grouped
            WHERE link.report_id = grouped.report_id
              AND link.{source_column} = grouped.source_id
              AND link.id <> grouped.keep_id
        """))
    op.create_unique_constraint(
        'funding_report_fact_links_return_uniq', 'funding_report_fact_links',
        ['report_id', 'return_fact_id'],
    )
    op.create_unique_constraint(
        'funding_report_fact_links_sale_uniq', 'funding_report_fact_links',
        ['report_id', 'sale_fact_id'],
    )
    op.drop_column('funding_report_fact_links', 'allocated_quantity')

    op.drop_constraint(
        'funding_report_lines_purchase_order_receipt_line_id_fkey',
        'funding_report_lines', type_='foreignkey',
    )
    op.drop_constraint(
        'funding_report_lines_purchase_order_line_id_fkey',
        'funding_report_lines', type_='foreignkey',
    )
    op.drop_column('funding_report_lines', 'lot_received_at_snapshot')
    op.drop_column('funding_report_lines', 'purchase_order_receipt_line_id')
    op.drop_column('funding_report_lines', 'purchase_order_line_id')
