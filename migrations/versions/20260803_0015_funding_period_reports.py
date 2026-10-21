"""Add shared funding accounts and period COGS reports.

Revision ID: 20260803_0015
Revises: 20260801_0014
Create Date: 2026-08-03
"""

import sqlalchemy as sa
from alembic import op


revision = '20260803_0015'
down_revision = '20260801_0014'
branch_labels = None
depends_on = None


def _created_at():
    return sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def upgrade() -> None:
    op.create_table(
        'funding_accounts',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('account_type', sa.String(20), nullable=False),
        sa.Column('vendor_id', sa.BigInteger(), sa.ForeignKey('vendors.id')),
        sa.Column('payment_method_id', sa.BigInteger(), sa.ForeignKey('payment_methods.id')),
        sa.Column('display_name', sa.Text(), nullable=False),
        sa.Column('issuer', sa.Text()),
        sa.Column('account_nickname', sa.Text()),
        sa.Column('last_four', sa.String(4)),
        sa.Column('credit_limit', sa.Numeric(14, 2)),
        sa.Column('promotional_apr', sa.Numeric(7, 4)),
        sa.Column('promotional_start_date', sa.Date()),
        sa.Column('promotional_expiration_date', sa.Date()),
        sa.Column('standard_apr', sa.Numeric(7, 4)),
        sa.Column('internal_notes', sa.Text()),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('created_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        sa.Column('updated_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        _created_at(),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("account_type IN ('CONSIGNMENT','CREDIT_CARD')", name='funding_accounts_type_ck'),
        sa.CheckConstraint(
            "(account_type = 'CONSIGNMENT' AND vendor_id IS NOT NULL AND payment_method_id IS NULL) OR "
            "(account_type = 'CREDIT_CARD' AND payment_method_id IS NOT NULL AND vendor_id IS NULL)",
            name='funding_accounts_owner_ck',
        ),
        sa.UniqueConstraint('vendor_id', name='funding_accounts_vendor_uniq'),
        sa.UniqueConstraint('payment_method_id', name='funding_accounts_method_uniq'),
    )
    op.create_index('idx_funding_accounts_type_active', 'funding_accounts', ['account_type', 'is_active'])

    op.create_table(
        'funding_sku_mappings',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('account_id', sa.BigInteger(), sa.ForeignKey('funding_accounts.id'), nullable=False),
        sa.Column('normalized_sku', sa.Text(), nullable=False),
        sa.Column('sku_snapshot', sa.Text(), nullable=False),
        sa.Column('square_variation_id', sa.Text()),
        sa.Column('product_name_snapshot', sa.Text()),
        sa.Column('variation_name_snapshot', sa.Text()),
        sa.Column('effective_start_date', sa.Date(), nullable=False),
        sa.Column('effective_end_date', sa.Date()),
        sa.Column('unit_cost', sa.Numeric(14, 4)),
        sa.Column('status', sa.String(16), nullable=False, server_default='ACTIVE'),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('created_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        _created_at(),
        sa.CheckConstraint('effective_end_date IS NULL OR effective_end_date >= effective_start_date', name='funding_sku_mappings_period_ck'),
        sa.CheckConstraint('unit_cost >= 0', name='funding_sku_mappings_cost_ck'),
        sa.UniqueConstraint('account_id', 'normalized_sku', 'effective_start_date', name='funding_sku_mappings_start_uniq'),
    )
    op.create_index('idx_funding_sku_mappings_lookup', 'funding_sku_mappings', ['normalized_sku', 'effective_start_date', 'effective_end_date'])
    op.create_index('idx_funding_sku_mappings_account', 'funding_sku_mappings', ['account_id', 'effective_start_date'])

    op.create_table(
        'funding_reports',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('account_id', sa.BigInteger(), sa.ForeignKey('funding_accounts.id'), nullable=False),
        sa.Column('report_number', sa.String(80), nullable=False),
        sa.Column('account_name_snapshot', sa.Text(), nullable=False),
        sa.Column('account_type_snapshot', sa.String(20), nullable=False),
        sa.Column('sales_start_date', sa.Date(), nullable=False),
        sa.Column('sales_end_date', sa.Date(), nullable=False),
        sa.Column('store_ids', sa.JSON(), nullable=False, server_default='[]'),
        sa.Column('sku_filter', sa.Text()),
        sa.Column('internal_note', sa.Text()),
        sa.Column('overlap_acknowledged', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('overlapping_report_ids', sa.JSON(), nullable=False, server_default='[]'),
        sa.Column('status', sa.String(24), nullable=False, server_default='DRAFT'),
        sa.Column('units_sold', sa.Numeric(14, 3), nullable=False, server_default='0'),
        sa.Column('units_returned', sa.Numeric(14, 3), nullable=False, server_default='0'),
        sa.Column('net_units', sa.Numeric(14, 3), nullable=False, server_default='0'),
        sa.Column('calculated_cogs', sa.Numeric(14, 2), nullable=False, server_default='0'),
        sa.Column('inventory_units_snapshot', sa.Numeric(14, 3), nullable=False, server_default='0'),
        sa.Column('inventory_value_snapshot', sa.Numeric(14, 2), nullable=False, server_default='0'),
        sa.Column('inventory_snapshot_at', sa.DateTime(timezone=True)),
        sa.Column('warning_summary', sa.JSON(), nullable=False, server_default='{}'),
        sa.Column('finalized_snapshot', sa.JSON(), nullable=False, server_default='{}'),
        sa.Column('finalized_at', sa.DateTime(timezone=True)),
        sa.Column('finalized_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id')),
        sa.Column('voided_at', sa.DateTime(timezone=True)),
        sa.Column('voided_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id')),
        sa.Column('void_reason', sa.Text()),
        sa.Column('created_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        _created_at(),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('report_number', name='funding_reports_number_uniq'),
        sa.CheckConstraint('sales_end_date >= sales_start_date', name='funding_reports_period_ck'),
        sa.CheckConstraint("status IN ('DRAFT','FINALIZED','PARTIALLY_SETTLED','SETTLED','ADJUSTED','VOIDED')", name='funding_reports_status_ck'),
    )
    op.create_index('idx_funding_reports_account_period', 'funding_reports', ['account_id', 'sales_start_date', 'sales_end_date'])
    op.create_index('idx_funding_reports_account_status', 'funding_reports', ['account_id', 'status'])

    op.create_table(
        'funding_report_lines',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('report_id', sa.BigInteger(), sa.ForeignKey('funding_reports.id', ondelete='CASCADE'), nullable=False),
        sa.Column('mapping_id', sa.BigInteger(), sa.ForeignKey('funding_sku_mappings.id'), nullable=False),
        sa.Column('normalized_sku', sa.Text(), nullable=False),
        sa.Column('sku_snapshot', sa.Text(), nullable=False),
        sa.Column('square_variation_id', sa.Text()),
        sa.Column('product_name_snapshot', sa.Text(), nullable=False),
        sa.Column('variation_name_snapshot', sa.Text()),
        sa.Column('store_id', sa.BigInteger(), sa.ForeignKey('stores.id')),
        sa.Column('units_sold', sa.Numeric(14, 3), nullable=False),
        sa.Column('units_returned', sa.Numeric(14, 3), nullable=False),
        sa.Column('net_units', sa.Numeric(14, 3), nullable=False),
        sa.Column('unit_cost_snapshot', sa.Numeric(14, 4), nullable=False),
        sa.Column('extended_cogs', sa.Numeric(14, 2), nullable=False),
        sa.Column('inventory_units_snapshot', sa.Numeric(14, 3), nullable=False, server_default='0'),
        sa.Column('inventory_value_snapshot', sa.Numeric(14, 2), nullable=False, server_default='0'),
        sa.Column('mapping_effective_date_snapshot', sa.Date(), nullable=False),
        sa.Column('source_transaction_count', sa.Integer(), nullable=False),
        sa.Column('warning_state', sa.String(40)),
        _created_at(),
    )
    op.create_index('idx_funding_report_lines_report', 'funding_report_lines', ['report_id', 'id'])

    op.create_table(
        'funding_report_fact_links',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('report_id', sa.BigInteger(), sa.ForeignKey('funding_reports.id', ondelete='CASCADE'), nullable=False),
        sa.Column('report_line_id', sa.BigInteger(), sa.ForeignKey('funding_report_lines.id', ondelete='CASCADE'), nullable=False),
        sa.Column('sale_fact_id', sa.BigInteger(), sa.ForeignKey('consignment_sale_facts.id')),
        sa.Column('return_fact_id', sa.BigInteger(), sa.ForeignKey('consignment_return_facts.id')),
        sa.Column('cogs_amount_snapshot', sa.Numeric(14, 2), nullable=False),
        _created_at(),
        sa.CheckConstraint('(sale_fact_id IS NOT NULL AND return_fact_id IS NULL) OR (sale_fact_id IS NULL AND return_fact_id IS NOT NULL)', name='funding_report_fact_links_one_source_ck'),
        sa.UniqueConstraint('report_id', 'sale_fact_id', name='funding_report_fact_links_sale_uniq'),
        sa.UniqueConstraint('report_id', 'return_fact_id', name='funding_report_fact_links_return_uniq'),
    )
    op.create_index('idx_funding_report_fact_links_line', 'funding_report_fact_links', ['report_line_id'])

    op.create_table(
        'funding_report_exclusions',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('report_id', sa.BigInteger(), sa.ForeignKey('funding_reports.id', ondelete='CASCADE'), nullable=False),
        sa.Column('source_type', sa.String(12), nullable=False),
        sa.Column('source_id', sa.BigInteger(), nullable=False),
        sa.Column('reason_code', sa.String(40), nullable=False),
        sa.Column('sku_snapshot', sa.Text()),
        sa.Column('product_name_snapshot', sa.Text(), nullable=False),
        sa.Column('variation_name_snapshot', sa.Text()),
        sa.Column('store_id', sa.BigInteger(), sa.ForeignKey('stores.id')),
        sa.Column('quantity_snapshot', sa.Numeric(14, 3)),
        sa.Column('amount_snapshot', sa.Numeric(14, 2)),
        _created_at(),
    )
    op.create_index('idx_funding_report_exclusions_report', 'funding_report_exclusions', ['report_id', 'reason_code'])

    op.create_table(
        'funding_report_adjustments',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('report_id', sa.BigInteger(), sa.ForeignKey('funding_reports.id'), nullable=False),
        sa.Column('adjustment_type', sa.String(40), nullable=False),
        sa.Column('direction', sa.String(12), nullable=False),
        sa.Column('amount', sa.Numeric(14, 2), nullable=False),
        sa.Column('effective_date', sa.Date(), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('internal_note', sa.Text()),
        sa.Column('owner_confirmed', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('reversed_adjustment_id', sa.BigInteger(), sa.ForeignKey('funding_report_adjustments.id')),
        sa.Column('replacement_for_adjustment_id', sa.BigInteger(), sa.ForeignKey('funding_report_adjustments.id')),
        sa.Column('created_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        _created_at(),
        sa.CheckConstraint("direction IN ('INCREASE','DECREASE')", name='funding_report_adjustments_direction_ck'),
        sa.CheckConstraint('amount > 0', name='funding_report_adjustments_amount_ck'),
        sa.UniqueConstraint('reversed_adjustment_id', name='funding_report_adjustments_reversal_uniq'),
    )
    op.create_index('idx_funding_report_adjustments_report', 'funding_report_adjustments', ['report_id', 'created_at'])

    op.create_table(
        'funding_payments',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('account_id', sa.BigInteger(), sa.ForeignKey('funding_accounts.id'), nullable=False),
        sa.Column('entry_type', sa.String(20), nullable=False),
        sa.Column('amount', sa.Numeric(14, 2), nullable=False),
        sa.Column('payment_date', sa.Date(), nullable=False),
        sa.Column('payment_source', sa.Text()),
        sa.Column('confirmation_number', sa.Text()),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('internal_note', sa.Text()),
        sa.Column('status', sa.String(12), nullable=False, server_default='ACTIVE'),
        sa.Column('reversed_payment_id', sa.BigInteger(), sa.ForeignKey('funding_payments.id')),
        sa.Column('created_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        _created_at(),
        sa.CheckConstraint("entry_type IN ('PAYMENT','REPLENISHMENT')", name='funding_payments_type_ck'),
        sa.CheckConstraint("status IN ('ACTIVE','REVERSED')", name='funding_payments_status_ck'),
        sa.CheckConstraint('amount > 0', name='funding_payments_amount_ck'),
        sa.UniqueConstraint('reversed_payment_id', name='funding_payments_reversal_uniq'),
    )
    op.create_index('idx_funding_payments_account_date', 'funding_payments', ['account_id', 'payment_date', 'id'])

    op.create_table(
        'funding_payment_allocations',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('payment_id', sa.BigInteger(), sa.ForeignKey('funding_payments.id'), nullable=False),
        sa.Column('report_id', sa.BigInteger(), sa.ForeignKey('funding_reports.id'), nullable=False),
        sa.Column('amount', sa.Numeric(14, 2), nullable=False),
        sa.Column('created_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        _created_at(),
        sa.CheckConstraint('amount > 0', name='funding_payment_allocations_amount_ck'),
        sa.UniqueConstraint('payment_id', 'report_id', name='funding_payment_allocations_report_uniq'),
    )
    op.create_index('idx_funding_payment_allocations_report', 'funding_payment_allocations', ['report_id', 'id'])

    op.create_table(
        'funding_ledger_entries',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('account_id', sa.BigInteger(), sa.ForeignKey('funding_accounts.id'), nullable=False),
        sa.Column('entry_type', sa.String(40), nullable=False),
        sa.Column('direction', sa.String(12), nullable=False),
        sa.Column('amount', sa.Numeric(14, 2), nullable=False),
        sa.Column('effective_date', sa.Date(), nullable=False),
        sa.Column('report_id', sa.BigInteger(), sa.ForeignKey('funding_reports.id')),
        sa.Column('payment_id', sa.BigInteger(), sa.ForeignKey('funding_payments.id')),
        sa.Column('order_payment_id', sa.BigInteger(), sa.ForeignKey('order_payments.id')),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('internal_note', sa.Text()),
        sa.Column('inventory_backed_estimate', sa.Numeric(14, 2)),
        sa.Column('original_entry_id', sa.BigInteger(), sa.ForeignKey('funding_ledger_entries.id')),
        sa.Column('replacement_for_entry_id', sa.BigInteger(), sa.ForeignKey('funding_ledger_entries.id')),
        sa.Column('created_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        _created_at(),
        sa.CheckConstraint("direction IN ('INCREASE','DECREASE')", name='funding_ledger_entries_direction_ck'),
        sa.CheckConstraint('amount >= 0', name='funding_ledger_entries_amount_ck'),
    )
    op.create_index('idx_funding_ledger_account_effective', 'funding_ledger_entries', ['account_id', 'effective_date', 'id'])
    op.create_index('idx_funding_ledger_report', 'funding_ledger_entries', ['report_id', 'id'])
    op.create_index(
        'uniq_funding_ledger_inventory_purchase',
        'funding_ledger_entries',
        ['order_payment_id'],
        unique=True,
        postgresql_where=sa.text("entry_type = 'INVENTORY_PURCHASE'"),
    )


def downgrade() -> None:
    op.drop_index('uniq_funding_ledger_inventory_purchase', table_name='funding_ledger_entries')
    op.drop_index('idx_funding_ledger_report', table_name='funding_ledger_entries')
    op.drop_index('idx_funding_ledger_account_effective', table_name='funding_ledger_entries')
    op.drop_table('funding_ledger_entries')
    op.drop_index('idx_funding_payment_allocations_report', table_name='funding_payment_allocations')
    op.drop_table('funding_payment_allocations')
    op.drop_index('idx_funding_payments_account_date', table_name='funding_payments')
    op.drop_table('funding_payments')
    op.drop_index('idx_funding_report_adjustments_report', table_name='funding_report_adjustments')
    op.drop_table('funding_report_adjustments')
    op.drop_index('idx_funding_report_exclusions_report', table_name='funding_report_exclusions')
    op.drop_table('funding_report_exclusions')
    op.drop_index('idx_funding_report_fact_links_line', table_name='funding_report_fact_links')
    op.drop_table('funding_report_fact_links')
    op.drop_index('idx_funding_report_lines_report', table_name='funding_report_lines')
    op.drop_table('funding_report_lines')
    op.drop_index('idx_funding_reports_account_status', table_name='funding_reports')
    op.drop_index('idx_funding_reports_account_period', table_name='funding_reports')
    op.drop_table('funding_reports')
    op.drop_index('idx_funding_sku_mappings_account', table_name='funding_sku_mappings')
    op.drop_index('idx_funding_sku_mappings_lookup', table_name='funding_sku_mappings')
    op.drop_table('funding_sku_mappings')
    op.drop_index('idx_funding_accounts_type_active', table_name='funding_accounts')
    op.drop_table('funding_accounts')
