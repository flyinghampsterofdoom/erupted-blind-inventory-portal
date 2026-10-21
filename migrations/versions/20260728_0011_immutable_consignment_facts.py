"""Add immutable consignment sales, returns, attribution, and report facts.

Revision ID: 20260728_0011
Revises: 20260728_0010
Create Date: 2026-07-28
"""

import sqlalchemy as sa
from alembic import op


revision = '20260728_0011'
down_revision = '20260728_0010'
branch_labels = None
depends_on = None


def created_at():
    return sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def upgrade() -> None:
    op.add_column('consignment_reports', sa.Column('source_sync_through_at', sa.DateTime(timezone=True)))
    op.add_column('consignment_reports', sa.Column('prior_unreplenished_cogs_snapshot', sa.Numeric(14, 2)))
    op.add_column('consignment_reports', sa.Column('replenishment_applied_period_snapshot', sa.Numeric(14, 2)))
    op.add_column('consignment_reports', sa.Column('cash_settlements_period_snapshot', sa.Numeric(14, 2)))
    op.add_column('consignment_reports', sa.Column('approved_credits_period_snapshot', sa.Numeric(14, 2)))
    op.add_column('consignment_reports', sa.Column('void_reversals_period_snapshot', sa.Numeric(14, 2)))
    op.add_column('consignment_reports', sa.Column('available_credit_snapshot', sa.Numeric(14, 2)))
    op.add_column('consignment_reports', sa.Column('ending_unreplenished_cogs_snapshot', sa.Numeric(14, 2)))

    op.create_table('vendor_variation_assignments',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('vendor_id', sa.BigInteger(), sa.ForeignKey('vendors.id'), nullable=False),
        sa.Column('square_variation_id', sa.Text(), nullable=False),
        sa.Column('is_consignment', sa.Boolean(), nullable=False),
        sa.Column('effective_start_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('effective_end_at', sa.DateTime(timezone=True)),
        sa.Column('source', sa.String(32), nullable=False, server_default='OWNER'),
        sa.Column('notes', sa.Text()),
        sa.Column('created_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        created_at(),
        sa.UniqueConstraint('vendor_id', 'square_variation_id', 'effective_start_at', name='vendor_variation_assignments_start_uniq'),
        sa.CheckConstraint('effective_end_at IS NULL OR effective_end_at > effective_start_at', name='vendor_variation_assignments_period_ck'))
    op.create_index('idx_vendor_variation_assignments_lookup', 'vendor_variation_assignments',
                    ['square_variation_id', 'effective_start_at', 'effective_end_at'])

    op.create_table('vendor_variation_costs',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('vendor_id', sa.BigInteger(), sa.ForeignKey('vendors.id'), nullable=False),
        sa.Column('square_variation_id', sa.Text(), nullable=False),
        sa.Column('unit_cost', sa.Numeric(14, 4), nullable=False),
        sa.Column('currency', sa.String(3), nullable=False, server_default='USD'),
        sa.Column('effective_start_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('effective_end_at', sa.DateTime(timezone=True)),
        sa.Column('source', sa.String(32), nullable=False, server_default='OWNER'),
        sa.Column('notes', sa.Text()),
        sa.Column('created_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        created_at(),
        sa.UniqueConstraint('vendor_id', 'square_variation_id', 'effective_start_at', name='vendor_variation_costs_start_uniq'),
        sa.CheckConstraint('unit_cost >= 0', name='vendor_variation_costs_non_negative_ck'),
        sa.CheckConstraint('effective_end_at IS NULL OR effective_end_at > effective_start_at', name='vendor_variation_costs_period_ck'))
    op.create_index('idx_vendor_variation_costs_lookup', 'vendor_variation_costs',
                    ['vendor_id', 'square_variation_id', 'effective_start_at', 'effective_end_at'])

    op.create_table('consignment_sale_facts',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('square_payment_id', sa.Text()), sa.Column('square_order_id', sa.Text(), nullable=False),
        sa.Column('square_line_item_uid', sa.Text(), nullable=False), sa.Column('square_variation_id', sa.Text()),
        sa.Column('square_product_id', sa.Text()), sa.Column('square_location_id', sa.Text(), nullable=False),
        sa.Column('store_id', sa.BigInteger(), sa.ForeignKey('stores.id')), sa.Column('business_date', sa.Date(), nullable=False),
        sa.Column('transacted_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('quantity_sold', sa.Numeric(14, 3), nullable=False),
        sa.Column('gross_sales_amount', sa.Numeric(14, 2), nullable=False),
        sa.Column('discount_amount', sa.Numeric(14, 2), nullable=False),
        sa.Column('tax_amount', sa.Numeric(14, 2), nullable=False),
        sa.Column('net_sales_amount', sa.Numeric(14, 2), nullable=False),
        sa.Column('currency', sa.String(3), nullable=False), sa.Column('product_name_snapshot', sa.Text(), nullable=False),
        sa.Column('variation_name_snapshot', sa.Text()), sa.Column('sku_snapshot', sa.Text()),
        sa.Column('vendor_id_snapshot', sa.BigInteger(), sa.ForeignKey('vendors.id')),
        sa.Column('vendor_name_snapshot', sa.Text()), sa.Column('is_consignment_snapshot', sa.Boolean()),
        sa.Column('unit_cost_snapshot', sa.Numeric(14, 4)), sa.Column('extended_cogs_snapshot', sa.Numeric(14, 2)),
        sa.Column('attribution_status', sa.String(24), nullable=False),
        sa.Column('attribution_source', sa.String(40), nullable=False), sa.Column('attribution_reason', sa.Text()),
        sa.Column('attributed_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id')),
        sa.Column('attributed_at', sa.DateTime(timezone=True)),
        sa.Column('source_synchronized_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('source_order_version', sa.Integer()), created_at(),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('square_order_id', 'square_line_item_uid', name='consignment_sale_facts_source_uniq'),
        sa.CheckConstraint("attribution_status IN ('ATTRIBUTED','MISSING_VENDOR','MISSING_COST','AMBIGUOUS_VENDOR','NON_CONSIGNMENT','EXCLUDED','SOURCE_INCOMPLETE')", name='consignment_sale_facts_attribution_ck'))
    for name, columns in (
        ('idx_consignment_sale_facts_vendor_date', ['vendor_id_snapshot','business_date']),
        ('idx_consignment_sale_facts_variation_date', ['square_variation_id','business_date']),
        ('idx_consignment_sale_facts_store_date', ['store_id','business_date']),
        ('idx_consignment_sale_facts_status_date', ['attribution_status','business_date']),
        ('idx_consignment_sale_facts_source_sync', ['source_synchronized_at'])):
        op.create_index(name, 'consignment_sale_facts', columns)

    op.create_table('consignment_return_facts',
        sa.Column('id', sa.BigInteger(), primary_key=True), sa.Column('square_return_order_id', sa.Text(), nullable=False),
        sa.Column('square_return_uid', sa.Text(), nullable=False), sa.Column('square_return_line_uid', sa.Text(), nullable=False),
        sa.Column('original_square_order_id', sa.Text()), sa.Column('original_square_line_uid', sa.Text()),
        sa.Column('square_variation_id', sa.Text()), sa.Column('square_location_id', sa.Text(), nullable=False),
        sa.Column('store_id', sa.BigInteger(), sa.ForeignKey('stores.id')), sa.Column('business_date', sa.Date(), nullable=False),
        sa.Column('returned_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('quantity_returned', sa.Numeric(14, 3)), sa.Column('refund_amount', sa.Numeric(14, 2), nullable=False),
        sa.Column('currency', sa.String(3), nullable=False), sa.Column('product_name_snapshot', sa.Text(), nullable=False),
        sa.Column('variation_name_snapshot', sa.Text()), sa.Column('sku_snapshot', sa.Text()),
        sa.Column('vendor_id_snapshot', sa.BigInteger(), sa.ForeignKey('vendors.id')), sa.Column('vendor_name_snapshot', sa.Text()),
        sa.Column('unit_cost_snapshot', sa.Numeric(14, 4)), sa.Column('extended_cogs_reversal', sa.Numeric(14, 2)),
        sa.Column('attribution_status', sa.String(24), nullable=False),
        sa.Column('original_sale_fact_id', sa.BigInteger(), sa.ForeignKey('consignment_sale_facts.id')),
        sa.Column('match_method', sa.String(40)), sa.Column('attribution_reason', sa.Text()),
        sa.Column('attributed_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id')),
        sa.Column('attributed_at', sa.DateTime(timezone=True)),
        sa.Column('source_synchronized_at', sa.DateTime(timezone=True), nullable=False), created_at(),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('square_return_order_id','square_return_uid','square_return_line_uid', name='consignment_return_facts_source_uniq'),
        sa.CheckConstraint("attribution_status IN ('ATTRIBUTED','MISSING_VENDOR','MISSING_COST','AMBIGUOUS_VENDOR','NON_CONSIGNMENT','EXCLUDED','SOURCE_INCOMPLETE','UNMATCHED_RETURN')", name='consignment_return_facts_attribution_ck'))
    for name, columns in (
        ('idx_consignment_return_facts_vendor_date',['vendor_id_snapshot','business_date']),
        ('idx_consignment_return_facts_variation_date',['square_variation_id','business_date']),
        ('idx_consignment_return_facts_store_date',['store_id','business_date']),
        ('idx_consignment_return_facts_status_date',['attribution_status','business_date']),
        ('idx_consignment_return_facts_original_sale',['original_sale_fact_id'])):
        op.create_index(name, 'consignment_return_facts', columns)

    op.create_table('consignment_sales_sync_state',
        sa.Column('id', sa.Integer(), primary_key=True, server_default='1'),
        sa.Column('last_successful_start_at', sa.DateTime(timezone=True)),
        sa.Column('last_successful_through_at', sa.DateTime(timezone=True)),
        sa.Column('last_successful_at', sa.DateTime(timezone=True)), sa.Column('last_attempted_at', sa.DateTime(timezone=True)),
        sa.Column('last_result', sa.String(16), nullable=False, server_default='NEVER'), sa.Column('last_error', sa.Text()),
        sa.Column('updated_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id')), created_at(),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint('id = 1', name='consignment_sales_sync_state_singleton_ck'))

    op.create_table('consignment_report_lines',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('report_id', sa.BigInteger(), sa.ForeignKey('consignment_reports.id', ondelete='CASCADE'), nullable=False),
        sa.Column('square_variation_id', sa.Text(), nullable=False), sa.Column('store_id', sa.BigInteger(), sa.ForeignKey('stores.id')),
        sa.Column('product_name_snapshot', sa.Text(), nullable=False), sa.Column('variation_name_snapshot', sa.Text()),
        sa.Column('sku_snapshot', sa.Text()), sa.Column('units_sold', sa.Numeric(14,3), nullable=False),
        sa.Column('units_returned', sa.Numeric(14,3), nullable=False), sa.Column('net_units', sa.Numeric(14,3), nullable=False),
        sa.Column('unit_cost_snapshot', sa.Numeric(14,4), nullable=False),
        sa.Column('extended_cogs', sa.Numeric(14,2), nullable=False), sa.Column('source_transaction_count', sa.Integer(), nullable=False), created_at(),
        sa.UniqueConstraint('report_id','square_variation_id','store_id','unit_cost_snapshot', name='consignment_report_lines_group_uniq'))
    op.create_index('idx_consignment_report_lines_report','consignment_report_lines',['report_id'])

    op.create_table('consignment_report_fact_links',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('report_id', sa.BigInteger(), sa.ForeignKey('consignment_reports.id', ondelete='CASCADE'), nullable=False),
        sa.Column('report_line_id', sa.BigInteger(), sa.ForeignKey('consignment_report_lines.id', ondelete='CASCADE'), nullable=False),
        sa.Column('sale_fact_id', sa.BigInteger(), sa.ForeignKey('consignment_sale_facts.id')),
        sa.Column('return_fact_id', sa.BigInteger(), sa.ForeignKey('consignment_return_facts.id')),
        sa.Column('cogs_amount_snapshot', sa.Numeric(14,2), nullable=False), created_at(),
        sa.CheckConstraint('(sale_fact_id IS NOT NULL AND return_fact_id IS NULL) OR (sale_fact_id IS NULL AND return_fact_id IS NOT NULL)', name='consignment_report_fact_links_one_source_ck'),
        sa.UniqueConstraint('report_id','sale_fact_id', name='consignment_report_fact_links_sale_uniq'),
        sa.UniqueConstraint('report_id','return_fact_id', name='consignment_report_fact_links_return_uniq'))
    op.create_index('idx_consignment_report_fact_links_line','consignment_report_fact_links',['report_line_id'])

    op.create_table('consignment_inventory_snapshots',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('report_id', sa.BigInteger(), sa.ForeignKey('consignment_reports.id', ondelete='CASCADE'), nullable=False),
        sa.Column('vendor_id', sa.BigInteger(), sa.ForeignKey('vendors.id'), nullable=False),
        sa.Column('square_variation_id', sa.Text(), nullable=False), sa.Column('store_id', sa.BigInteger(), sa.ForeignKey('stores.id'), nullable=False),
        sa.Column('quantity_on_hand', sa.Numeric(14,3), nullable=False), sa.Column('unit_cost_snapshot', sa.Numeric(14,4)),
        sa.Column('inventory_value_snapshot', sa.Numeric(14,2)), sa.Column('product_name_snapshot', sa.Text(), nullable=False),
        sa.Column('variation_name_snapshot', sa.Text()), sa.Column('sku_snapshot', sa.Text()),
        sa.Column('inventory_retrieved_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('attribution_status', sa.String(24), nullable=False), created_at(),
        sa.UniqueConstraint('report_id','square_variation_id','store_id', name='consignment_inventory_snapshots_source_uniq'))
    op.create_index('idx_consignment_inventory_snapshots_report','consignment_inventory_snapshots',['report_id'])

    op.add_column('consignment_email_deliveries', sa.Column('body_snapshot', sa.Text()))

    op.create_index('uniq_consignment_ledger_report_cogs', 'consignment_ledger_entries', ['report_id'],
                    unique=True, postgresql_where=sa.text("entry_type = 'COGS_GENERATED'"))
    op.create_index('uniq_consignment_ledger_report_void', 'consignment_ledger_entries', ['report_id'],
                    unique=True, postgresql_where=sa.text("entry_type = 'VOID_REVERSAL'"))


def downgrade() -> None:
    op.drop_index('uniq_consignment_ledger_report_void', table_name='consignment_ledger_entries')
    op.drop_index('uniq_consignment_ledger_report_cogs', table_name='consignment_ledger_entries')
    op.drop_index('idx_consignment_inventory_snapshots_report', table_name='consignment_inventory_snapshots')
    op.drop_table('consignment_inventory_snapshots')
    op.drop_column('consignment_email_deliveries', 'body_snapshot')
    op.drop_index('idx_consignment_report_fact_links_line', table_name='consignment_report_fact_links')
    op.drop_table('consignment_report_fact_links')
    op.drop_index('idx_consignment_report_lines_report', table_name='consignment_report_lines')
    op.drop_table('consignment_report_lines')
    op.drop_table('consignment_sales_sync_state')
    for name in ('idx_consignment_return_facts_original_sale','idx_consignment_return_facts_status_date',
                 'idx_consignment_return_facts_store_date','idx_consignment_return_facts_variation_date',
                 'idx_consignment_return_facts_vendor_date'):
        op.drop_index(name, table_name='consignment_return_facts')
    op.drop_table('consignment_return_facts')
    for name in ('idx_consignment_sale_facts_source_sync','idx_consignment_sale_facts_status_date',
                 'idx_consignment_sale_facts_store_date','idx_consignment_sale_facts_variation_date',
                 'idx_consignment_sale_facts_vendor_date'):
        op.drop_index(name, table_name='consignment_sale_facts')
    op.drop_table('consignment_sale_facts')
    op.drop_index('idx_vendor_variation_costs_lookup', table_name='vendor_variation_costs')
    op.drop_table('vendor_variation_costs')
    op.drop_index('idx_vendor_variation_assignments_lookup', table_name='vendor_variation_assignments')
    op.drop_table('vendor_variation_assignments')
    for column in ('ending_unreplenished_cogs_snapshot','available_credit_snapshot',
                   'void_reversals_period_snapshot','approved_credits_period_snapshot',
                   'cash_settlements_period_snapshot','replenishment_applied_period_snapshot',
                   'prior_unreplenished_cogs_snapshot','source_sync_through_at'):
        op.drop_column('consignment_reports', column)
