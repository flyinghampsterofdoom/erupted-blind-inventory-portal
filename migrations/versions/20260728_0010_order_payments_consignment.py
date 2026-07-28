"""Add owner-preview order payments and rolling consignment settlement.

Revision ID: 20260728_0010
Revises: 20260725_0009
Create Date: 2026-07-28
"""

import sqlalchemy as sa
from alembic import op


revision = '20260728_0010'
down_revision = '20260725_0009'
branch_labels = None
depends_on = None


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def upgrade() -> None:
    op.create_table(
        'payment_methods',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('display_name', sa.Text(), nullable=False),
        sa.Column('category', sa.String(24), nullable=False),
        sa.Column('institution_or_company_name', sa.Text()),
        sa.Column('account_nickname', sa.Text()),
        sa.Column('last_four', sa.String(4)),
        sa.Column('term_days', sa.Integer()),
        sa.Column('consignment_cycle', sa.String(64)),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('notes', sa.Text()),
        sa.Column('created_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        sa.Column('updated_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "category IN ('WIRE', 'CREDIT_CARD', 'DEBIT_CARD', 'TERMS', 'CONSIGNMENT')",
            name='payment_methods_category_ck',
        ),
        sa.CheckConstraint(
            "(category = 'TERMS' AND term_days IS NOT NULL AND term_days > 0) "
            "OR (category <> 'TERMS' AND term_days IS NULL)",
            name='payment_methods_term_days_ck',
        ),
        sa.CheckConstraint(
            "last_four IS NULL OR last_four ~ '^[0-9]{4}$'",
            name='payment_methods_last_four_ck',
        ),
        sa.CheckConstraint(
            "(category = 'CONSIGNMENT' AND consignment_cycle = 'SINCE_LAST_FINALIZED_REPORT') "
            "OR (category <> 'CONSIGNMENT' AND consignment_cycle IS NULL)",
            name='payment_methods_consignment_cycle_ck',
        ),
    )
    op.create_index('idx_payment_methods_active_category', 'payment_methods', ['is_active', 'category'])

    op.create_table(
        'vendor_payment_settings',
        sa.Column(
            'vendor_id',
            sa.BigInteger(),
            sa.ForeignKey('vendors.id', ondelete='CASCADE'),
            primary_key=True,
        ),
        sa.Column(
            'default_payment_method_id',
            sa.BigInteger(),
            sa.ForeignKey('payment_methods.id', ondelete='RESTRICT'),
        ),
        sa.Column('report_email', sa.Text()),
        sa.Column('payment_notes', sa.Text()),
        sa.Column('updated_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        *_timestamps(),
    )

    op.create_table(
        'order_payments',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column(
            'purchase_order_id',
            sa.BigInteger(),
            sa.ForeignKey('purchase_orders.id', ondelete='RESTRICT'),
            nullable=False,
        ),
        sa.Column('vendor_id', sa.BigInteger(), sa.ForeignKey('vendors.id'), nullable=False),
        sa.Column(
            'payment_method_id',
            sa.BigInteger(),
            sa.ForeignKey('payment_methods.id', ondelete='RESTRICT'),
        ),
        sa.Column('payment_category_snapshot', sa.String(24)),
        sa.Column('payment_method_label_snapshot', sa.Text()),
        sa.Column('term_days_snapshot', sa.Integer()),
        sa.Column('status', sa.String(40), nullable=False),
        sa.Column('financial_treatment', sa.String(20), nullable=False),
        sa.Column('order_amount', sa.Numeric(14, 2), nullable=False, server_default='0'),
        sa.Column('order_cost_complete', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('paid_amount', sa.Numeric(14, 2)),
        sa.Column('due_date', sa.Date()),
        sa.Column('paid_date', sa.Date()),
        sa.Column('marked_paid_at', sa.DateTime(timezone=True)),
        sa.Column('marked_paid_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id')),
        *_timestamps(),
        sa.UniqueConstraint('purchase_order_id', name='order_payments_purchase_order_uniq'),
        sa.CheckConstraint(
            "status IN ('UNPAID', 'PAID', 'CONSIGNMENT_ORDERED', "
            "'CONSIGNMENT_PARTIALLY_RECEIVED', 'CONSIGNMENT_RECEIVED', "
            "'CONSIGNMENT_PARTIALLY_APPLIED', 'CONSIGNMENT_APPLIED')",
            name='order_payments_status_ck',
        ),
        sa.CheckConstraint(
            "financial_treatment IN ('INVOICE', 'REPLENISHMENT')",
            name='order_payments_treatment_ck',
        ),
    )
    op.create_index('idx_order_payments_vendor_status', 'order_payments', ['vendor_id', 'status'])
    op.create_index('idx_order_payments_payment_method', 'order_payments', ['payment_method_id'])
    op.create_index('idx_order_payments_due_date', 'order_payments', ['due_date'])

    op.create_table(
        'order_payment_events',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column(
            'order_payment_id',
            sa.BigInteger(),
            sa.ForeignKey('order_payments.id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column('prior_status', sa.String(40)),
        sa.Column('new_status', sa.String(40), nullable=False),
        sa.Column('prior_payment_method_id', sa.BigInteger(), sa.ForeignKey('payment_methods.id')),
        sa.Column('new_payment_method_id', sa.BigInteger(), sa.ForeignKey('payment_methods.id')),
        sa.Column('effective_date', sa.Date()),
        sa.Column('note', sa.Text()),
        sa.Column('actor_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        'idx_order_payment_events_payment_created',
        'order_payment_events',
        ['order_payment_id', 'created_at'],
    )

    op.create_table(
        'consignment_reports',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('vendor_id', sa.BigInteger(), sa.ForeignKey('vendors.id'), nullable=False),
        sa.Column('report_number', sa.String(64), nullable=False),
        sa.Column('start_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('end_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('status', sa.String(16), nullable=False, server_default='DRAFT'),
        sa.Column('total_units', sa.Numeric(14, 3), nullable=False, server_default='0'),
        sa.Column('total_cogs', sa.Numeric(14, 2), nullable=False, server_default='0'),
        sa.Column('inventory_quantity_snapshot', sa.Numeric(14, 3), nullable=False, server_default='0'),
        sa.Column('inventory_value_snapshot', sa.Numeric(14, 2), nullable=False, server_default='0'),
        sa.Column('inventory_snapshot_at', sa.DateTime(timezone=True)),
        sa.Column(
            'data_integrity_blockers',
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
        sa.Column('finalized_at', sa.DateTime(timezone=True)),
        sa.Column('finalized_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id')),
        sa.Column('voided_at', sa.DateTime(timezone=True)),
        sa.Column('voided_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id')),
        sa.Column('void_reason', sa.Text()),
        sa.Column('created_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint('report_number', name='consignment_reports_number_uniq'),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'PREVIEWED', 'FINALIZED', 'EMAILED', 'VOIDED')",
            name='consignment_reports_status_ck',
        ),
        sa.CheckConstraint('end_at >= start_at', name='consignment_reports_period_ck'),
    )
    op.create_index(
        'idx_consignment_reports_vendor_period',
        'consignment_reports',
        ['vendor_id', 'start_at', 'end_at'],
    )
    op.create_index(
        'idx_consignment_reports_vendor_status',
        'consignment_reports',
        ['vendor_id', 'status'],
    )

    op.create_table(
        'consignment_replenishments',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('vendor_id', sa.BigInteger(), sa.ForeignKey('vendors.id'), nullable=False),
        sa.Column('purchase_order_id', sa.BigInteger(), sa.ForeignKey('purchase_orders.id'), nullable=False),
        sa.Column('ordered_cost_value', sa.Numeric(14, 2), nullable=False),
        sa.Column('received_cost_value', sa.Numeric(14, 2), nullable=False, server_default='0'),
        sa.Column('amount_applied', sa.Numeric(14, 2), nullable=False, server_default='0'),
        sa.Column('excess_credit_created', sa.Numeric(14, 2), nullable=False, server_default='0'),
        sa.Column('status', sa.String(24), nullable=False, server_default='PENDING'),
        sa.Column('integrity_warning', sa.Text()),
        sa.Column('last_receipt_at', sa.DateTime(timezone=True)),
        sa.Column('created_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint('purchase_order_id', name='consignment_replenishments_order_uniq'),
        sa.CheckConstraint(
            "status IN ('PENDING', 'PARTIALLY_RECEIVED', 'RECEIVED', "
            "'PARTIALLY_APPLIED', 'APPLIED', 'VOIDED')",
            name='consignment_replenishments_status_ck',
        ),
    )
    op.create_index(
        'idx_consignment_replenishments_vendor_status',
        'consignment_replenishments',
        ['vendor_id', 'status'],
    )

    op.create_table(
        'consignment_ledger_entries',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('vendor_id', sa.BigInteger(), sa.ForeignKey('vendors.id'), nullable=False),
        sa.Column('entry_type', sa.String(40), nullable=False),
        sa.Column('effective_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('amount', sa.Numeric(14, 2), nullable=False),
        sa.Column('quantity', sa.Numeric(14, 3)),
        sa.Column('square_variation_id', sa.Text()),
        sa.Column('report_id', sa.BigInteger(), sa.ForeignKey('consignment_reports.id')),
        sa.Column('purchase_order_id', sa.BigInteger(), sa.ForeignKey('purchase_orders.id')),
        sa.Column('payment_method_id', sa.BigInteger(), sa.ForeignKey('payment_methods.id')),
        sa.Column('note', sa.Text()),
        sa.Column('created_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "entry_type IN ('COGS_GENERATED', 'REPLENISHMENT_RECEIVED', 'REPLENISHMENT_APPLIED', "
            "'REPLENISHMENT_CREDIT_CREATED', 'REPLENISHMENT_CREDIT_USED', 'VENDOR_RETURN', "
            "'INVENTORY_ADJUSTMENT', 'CASH_SETTLEMENT', 'APPROVED_CREDIT', 'MANUAL_CORRECTION', "
            "'VOID_REVERSAL')",
            name='consignment_ledger_entries_type_ck',
        ),
        sa.CheckConstraint('amount >= 0', name='consignment_ledger_entries_amount_ck'),
    )
    op.create_index(
        'idx_consignment_ledger_vendor_effective',
        'consignment_ledger_entries',
        ['vendor_id', 'effective_at', 'id'],
    )
    op.create_index('idx_consignment_ledger_report', 'consignment_ledger_entries', ['report_id'])
    op.create_index('idx_consignment_ledger_order', 'consignment_ledger_entries', ['purchase_order_id'])

    op.create_table(
        'consignment_allocations',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('vendor_id', sa.BigInteger(), sa.ForeignKey('vendors.id'), nullable=False),
        sa.Column(
            'replenishment_id',
            sa.BigInteger(),
            sa.ForeignKey('consignment_replenishments.id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column(
            'cogs_report_id',
            sa.BigInteger(),
            sa.ForeignKey('consignment_reports.id'),
            nullable=False,
        ),
        sa.Column('amount_applied', sa.Numeric(14, 2), nullable=False),
        sa.Column('created_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            'replenishment_id',
            'cogs_report_id',
            name='consignment_allocations_replenishment_report_uniq',
        ),
        sa.CheckConstraint('amount_applied > 0', name='consignment_allocations_amount_ck'),
    )
    op.create_index('idx_consignment_allocations_report', 'consignment_allocations', ['cogs_report_id'])

    op.create_table(
        'consignment_replenishment_receipts',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('replenishment_id', sa.BigInteger(), sa.ForeignKey('consignment_replenishments.id', ondelete='CASCADE'), nullable=False),
        sa.Column('purchase_order_id', sa.BigInteger(), sa.ForeignKey('purchase_orders.id'), nullable=False),
        sa.Column('received_ledger_entry_id', sa.BigInteger(), sa.ForeignKey('consignment_ledger_entries.id'), nullable=False),
        sa.Column('received_value_delta', sa.Numeric(14, 2), nullable=False),
        sa.Column('source_observed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('received_ledger_entry_id', name='consignment_replenishment_receipts_ledger_uniq'),
    )
    op.create_index(
        'idx_consignment_replenishment_receipts_replenishment',
        'consignment_replenishment_receipts', ['replenishment_id', 'created_at'],
    )

    op.create_table(
        'consignment_replenishment_receipt_lines',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('receipt_id', sa.BigInteger(), sa.ForeignKey('consignment_replenishment_receipts.id', ondelete='CASCADE'), nullable=False),
        sa.Column('purchase_order_line_id', sa.BigInteger(), sa.ForeignKey('purchase_order_lines.id'), nullable=False),
        sa.Column('purchase_order_store_allocation_id', sa.BigInteger(), sa.ForeignKey('purchase_order_store_allocations.id')),
        sa.Column('store_id', sa.BigInteger(), sa.ForeignKey('stores.id')),
        sa.Column('prior_received_qty', sa.Integer(), nullable=False),
        sa.Column('received_qty_snapshot', sa.Integer(), nullable=False),
        sa.Column('received_qty_delta', sa.Integer(), nullable=False),
        sa.Column('unit_cost_snapshot', sa.Numeric(14, 4), nullable=False),
        sa.Column('received_value_delta', sa.Numeric(14, 2), nullable=False),
        sa.Column('credit_ledger_entry_id', sa.BigInteger(), sa.ForeignKey('consignment_ledger_entries.id')),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            'receipt_id', 'purchase_order_line_id', 'purchase_order_store_allocation_id',
            name='consignment_replenishment_receipt_lines_source_uniq',
        ),
        sa.CheckConstraint('prior_received_qty >= 0', name='consignment_replenishment_receipt_lines_prior_qty_ck'),
        sa.CheckConstraint('received_qty_snapshot >= prior_received_qty', name='consignment_replenishment_receipt_lines_snapshot_qty_ck'),
        sa.CheckConstraint('received_qty_delta > 0', name='consignment_replenishment_receipt_lines_delta_qty_ck'),
        sa.CheckConstraint('unit_cost_snapshot >= 0', name='consignment_replenishment_receipt_lines_cost_ck'),
        sa.CheckConstraint('received_value_delta >= 0', name='consignment_replenishment_receipt_lines_value_ck'),
    )
    op.create_index(
        'idx_consignment_replenishment_receipt_lines_order_line',
        'consignment_replenishment_receipt_lines', ['purchase_order_line_id'],
    )

    op.create_table(
        'consignment_receipt_allocations',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('receipt_line_id', sa.BigInteger(), sa.ForeignKey('consignment_replenishment_receipt_lines.id', ondelete='CASCADE'), nullable=False),
        sa.Column('cogs_report_id', sa.BigInteger(), sa.ForeignKey('consignment_reports.id'), nullable=False),
        sa.Column('applied_ledger_entry_id', sa.BigInteger(), sa.ForeignKey('consignment_ledger_entries.id'), nullable=False),
        sa.Column('amount_applied', sa.Numeric(14, 2), nullable=False),
        sa.Column('created_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('receipt_line_id', 'cogs_report_id', name='consignment_receipt_allocations_line_report_uniq'),
        sa.UniqueConstraint('applied_ledger_entry_id', name='consignment_receipt_allocations_ledger_uniq'),
        sa.CheckConstraint('amount_applied > 0', name='consignment_receipt_allocations_amount_ck'),
    )
    op.create_index('idx_consignment_receipt_allocations_report', 'consignment_receipt_allocations', ['cogs_report_id'])

    op.create_table(
        'consignment_email_deliveries',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('report_id', sa.BigInteger(), sa.ForeignKey('consignment_reports.id'), nullable=False),
        sa.Column('recipient', sa.Text(), nullable=False),
        sa.Column('subject', sa.Text(), nullable=False),
        sa.Column('provider_message_id', sa.Text()),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('error_summary', sa.Text()),
        sa.Column('sent_at', sa.DateTime(timezone=True)),
        sa.Column('sent_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        'idx_consignment_email_deliveries_report_created',
        'consignment_email_deliveries',
        ['report_id', 'created_at'],
    )


def downgrade() -> None:
    op.drop_index(
        'idx_consignment_email_deliveries_report_created',
        table_name='consignment_email_deliveries',
    )
    op.drop_table('consignment_email_deliveries')
    op.drop_index('idx_consignment_receipt_allocations_report', table_name='consignment_receipt_allocations')
    op.drop_table('consignment_receipt_allocations')
    op.drop_index(
        'idx_consignment_replenishment_receipt_lines_order_line',
        table_name='consignment_replenishment_receipt_lines',
    )
    op.drop_table('consignment_replenishment_receipt_lines')
    op.drop_index(
        'idx_consignment_replenishment_receipts_replenishment',
        table_name='consignment_replenishment_receipts',
    )
    op.drop_table('consignment_replenishment_receipts')
    op.drop_index('idx_consignment_allocations_report', table_name='consignment_allocations')
    op.drop_table('consignment_allocations')
    op.drop_index('idx_consignment_ledger_order', table_name='consignment_ledger_entries')
    op.drop_index('idx_consignment_ledger_report', table_name='consignment_ledger_entries')
    op.drop_index('idx_consignment_ledger_vendor_effective', table_name='consignment_ledger_entries')
    op.drop_table('consignment_ledger_entries')
    op.drop_index(
        'idx_consignment_replenishments_vendor_status',
        table_name='consignment_replenishments',
    )
    op.drop_table('consignment_replenishments')
    op.drop_index('idx_consignment_reports_vendor_status', table_name='consignment_reports')
    op.drop_index('idx_consignment_reports_vendor_period', table_name='consignment_reports')
    op.drop_table('consignment_reports')
    op.drop_index('idx_order_payment_events_payment_created', table_name='order_payment_events')
    op.drop_table('order_payment_events')
    op.drop_index('idx_order_payments_due_date', table_name='order_payments')
    op.drop_index('idx_order_payments_payment_method', table_name='order_payments')
    op.drop_index('idx_order_payments_vendor_status', table_name='order_payments')
    op.drop_table('order_payments')
    op.drop_table('vendor_payment_settings')
    op.drop_index('idx_payment_methods_active_category', table_name='payment_methods')
    op.drop_table('payment_methods')
