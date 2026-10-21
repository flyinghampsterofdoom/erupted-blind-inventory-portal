"""Add owner vendor assignment, manual payment, and order balance events.

Revision ID: 20260801_0014
Revises: 20260729_0013
"""

from alembic import op
import sqlalchemy as sa


revision = '20260801_0014'
down_revision = '20260729_0013'
branch_labels = None
depends_on = None


OLD_LEDGER_TYPES = (
    "'COGS_GENERATED', 'REPLENISHMENT_RECEIVED', 'REPLENISHMENT_APPLIED', "
    "'REPLENISHMENT_CREDIT_CREATED', 'REPLENISHMENT_CREDIT_USED', 'VENDOR_RETURN', "
    "'INVENTORY_ADJUSTMENT', 'CASH_SETTLEMENT', 'APPROVED_CREDIT', 'MANUAL_CORRECTION', "
    "'VOID_REVERSAL', 'SHIPPING_CHARGE', 'TAX_CHARGE', 'VENDOR_FEE', "
    "'MISCELLANEOUS_CHARGE', 'VENDOR_CREDIT', 'DAMAGE_CREDIT', 'PROMOTIONAL_CREDIT', "
    "'MISCELLANEOUS_CREDIT', 'CORRECTION_REVERSAL'"
)
NEW_LEDGER_TYPES = OLD_LEDGER_TYPES + ", 'VENDOR_ASSIGNMENT_TRANSFER_OUT', 'VENDOR_ASSIGNMENT_TRANSFER_IN'"


def upgrade() -> None:
    # A queue batch may contain purchase orders from multiple original vendors.
    # The immutable result rows retain each source vendor and classification;
    # these legacy summary pointers are populated only for a single-source batch.
    op.alter_column('order_payment_backfill_operations', 'vendor_id', existing_type=sa.BigInteger(), nullable=True)
    op.alter_column(
        'order_payment_backfill_operations', 'vendor_classification_id',
        existing_type=sa.BigInteger(), nullable=True,
    )
    op.drop_constraint('consignment_ledger_entries_type_ck', 'consignment_ledger_entries', type_='check')
    op.create_check_constraint('consignment_ledger_entries_type_ck', 'consignment_ledger_entries', f'entry_type IN ({NEW_LEDGER_TYPES})')

    op.create_table(
        'vendor_assignment_operations',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('scope_type', sa.String(12), nullable=False),
        sa.Column('effective_date', sa.Date(), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('internal_note', sa.Text()),
        sa.Column('created_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("scope_type IN ('SINGLE', 'BULK')", name='vendor_assignment_operations_scope_ck'),
    )
    op.create_index('idx_vendor_assignment_operations_created', 'vendor_assignment_operations', ['created_at', 'id'])
    op.create_table(
        'vendor_assignment_changes',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('operation_id', sa.BigInteger(), sa.ForeignKey('vendor_assignment_operations.id', ondelete='CASCADE'), nullable=False),
        sa.Column('purchase_order_id', sa.BigInteger(), sa.ForeignKey('purchase_orders.id'), nullable=False),
        sa.Column('order_payment_id', sa.BigInteger(), sa.ForeignKey('order_payments.id'), nullable=False),
        sa.Column('source_vendor_id', sa.BigInteger(), sa.ForeignKey('vendors.id'), nullable=False),
        sa.Column('prior_financial_vendor_id', sa.BigInteger(), sa.ForeignKey('vendors.id'), nullable=False),
        sa.Column('new_financial_vendor_id', sa.BigInteger(), sa.ForeignKey('vendors.id'), nullable=False),
        sa.Column('source_vendor_name_snapshot', sa.Text(), nullable=False),
        sa.Column('prior_vendor_name_snapshot', sa.Text(), nullable=False),
        sa.Column('new_vendor_name_snapshot', sa.Text(), nullable=False),
        sa.Column('source_square_vendor_id', sa.Text(), nullable=False),
        sa.Column('prior_square_vendor_id', sa.Text(), nullable=False),
        sa.Column('new_square_vendor_id', sa.Text(), nullable=False),
        sa.Column('prior_payment_state', sa.String(40), nullable=False),
        sa.Column('prior_consignment_state', sa.String(40)),
        sa.Column('downstream_impact', sa.JSON(), nullable=False, server_default='{}'),
        sa.Column('transfer_entry_ids', sa.JSON(), nullable=False, server_default='[]'),
        sa.Column('created_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index('idx_vendor_assignment_changes_order_created', 'vendor_assignment_changes', ['purchase_order_id', 'created_at'])
    op.create_index('idx_vendor_assignment_changes_operation', 'vendor_assignment_changes', ['operation_id', 'id'])

    op.create_table(
        'order_manual_payment_entries',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('order_payment_id', sa.BigInteger(), sa.ForeignKey('order_payments.id'), nullable=False),
        sa.Column('vendor_id', sa.BigInteger(), sa.ForeignKey('vendors.id'), nullable=False),
        sa.Column('entry_type', sa.String(16), nullable=False),
        sa.Column('amount', sa.Numeric(14, 2), nullable=False),
        sa.Column('payment_method_id', sa.BigInteger(), sa.ForeignKey('payment_methods.id'), nullable=False),
        sa.Column('effective_date', sa.Date(), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('confirmation_number', sa.Text()),
        sa.Column('internal_note', sa.Text()),
        sa.Column('original_entry_id', sa.BigInteger(), sa.ForeignKey('order_manual_payment_entries.id')),
        sa.Column('replacement_for_entry_id', sa.BigInteger(), sa.ForeignKey('order_manual_payment_entries.id')),
        sa.Column('created_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("entry_type IN ('PAYMENT', 'REVERSAL', 'REPLACEMENT')", name='order_manual_payment_entries_type_ck'),
        sa.CheckConstraint('amount > 0', name='order_manual_payment_entries_amount_ck'),
    )
    op.create_index('idx_order_manual_payment_entries_payment_effective', 'order_manual_payment_entries', ['order_payment_id', 'effective_date', 'id'])

    op.create_table(
        'order_balance_adjustments',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('order_payment_id', sa.BigInteger(), sa.ForeignKey('order_payments.id'), nullable=False),
        sa.Column('vendor_id', sa.BigInteger(), sa.ForeignKey('vendors.id'), nullable=False),
        sa.Column('direction', sa.String(12), nullable=False),
        sa.Column('adjustment_type', sa.String(40), nullable=False),
        sa.Column('amount', sa.Numeric(14, 2), nullable=False),
        sa.Column('effective_date', sa.Date(), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('internal_note', sa.Text()),
        sa.Column('original_calculated_amount', sa.Numeric(14, 2), nullable=False),
        sa.Column('prior_adjusted_amount', sa.Numeric(14, 2), nullable=False),
        sa.Column('resulting_adjusted_amount', sa.Numeric(14, 2), nullable=False),
        sa.Column('reversed_adjustment_id', sa.BigInteger(), sa.ForeignKey('order_balance_adjustments.id')),
        sa.Column('replacement_for_adjustment_id', sa.BigInteger(), sa.ForeignKey('order_balance_adjustments.id')),
        sa.Column('created_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("direction IN ('INCREASE', 'DECREASE', 'REVERSAL')", name='order_balance_adjustments_direction_ck'),
        sa.CheckConstraint('amount > 0', name='order_balance_adjustments_amount_ck'),
    )
    op.create_index('idx_order_balance_adjustments_payment_effective', 'order_balance_adjustments', ['order_payment_id', 'effective_date', 'id'])


def downgrade() -> None:
    op.drop_table('order_balance_adjustments')
    op.drop_table('order_manual_payment_entries')
    op.drop_table('vendor_assignment_changes')
    op.drop_table('vendor_assignment_operations')
    op.drop_constraint('consignment_ledger_entries_type_ck', 'consignment_ledger_entries', type_='check')
    op.create_check_constraint('consignment_ledger_entries_type_ck', 'consignment_ledger_entries', f'entry_type IN ({OLD_LEDGER_TYPES})')
    op.alter_column(
        'order_payment_backfill_operations', 'vendor_classification_id',
        existing_type=sa.BigInteger(), nullable=False,
    )
    op.alter_column('order_payment_backfill_operations', 'vendor_id', existing_type=sa.BigInteger(), nullable=False)
