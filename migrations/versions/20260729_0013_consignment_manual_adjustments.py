"""Add auditable manual consignment adjustments.

Revision ID: 20260729_0013
Revises: 20260728_0012
"""

from alembic import op
import sqlalchemy as sa


revision = '20260729_0013'
down_revision = '20260728_0012'
branch_labels = None
depends_on = None


LEDGER_TYPES = (
    "'COGS_GENERATED', 'REPLENISHMENT_RECEIVED', 'REPLENISHMENT_APPLIED', "
    "'REPLENISHMENT_CREDIT_CREATED', 'REPLENISHMENT_CREDIT_USED', 'VENDOR_RETURN', "
    "'INVENTORY_ADJUSTMENT', 'CASH_SETTLEMENT', 'APPROVED_CREDIT', 'MANUAL_CORRECTION', "
    "'VOID_REVERSAL', 'SHIPPING_CHARGE', 'TAX_CHARGE', 'VENDOR_FEE', "
    "'MISCELLANEOUS_CHARGE', 'VENDOR_CREDIT', 'DAMAGE_CREDIT', 'PROMOTIONAL_CREDIT', "
    "'MISCELLANEOUS_CREDIT', 'CORRECTION_REVERSAL'"
)


def upgrade() -> None:
    op.drop_constraint(
        'consignment_ledger_entries_type_ck',
        'consignment_ledger_entries',
        type_='check',
    )
    op.create_check_constraint(
        'consignment_ledger_entries_type_ck',
        'consignment_ledger_entries',
        f'entry_type IN ({LEDGER_TYPES})',
    )
    op.create_table(
        'consignment_manual_adjustments',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('vendor_id', sa.BigInteger(), sa.ForeignKey('vendors.id'), nullable=False),
        sa.Column('report_id', sa.BigInteger(), sa.ForeignKey('consignment_reports.id')),
        sa.Column(
            'target_ledger_entry_id',
            sa.BigInteger(),
            sa.ForeignKey('consignment_ledger_entries.id'),
        ),
        sa.Column(
            'ledger_entry_id',
            sa.BigInteger(),
            sa.ForeignKey('consignment_ledger_entries.id'),
            nullable=False,
        ),
        sa.Column('adjustment_type', sa.String(40), nullable=False),
        sa.Column('direction', sa.String(12), nullable=False),
        sa.Column('amount', sa.Numeric(14, 2), nullable=False),
        sa.Column('effective_date', sa.Date(), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('internal_note', sa.Text()),
        sa.Column('original_calculated_amount', sa.Numeric(14, 2), nullable=False),
        sa.Column('prior_adjusted_amount', sa.Numeric(14, 2), nullable=False),
        sa.Column('resulting_adjusted_amount', sa.Numeric(14, 2), nullable=False),
        sa.Column('excess_credit_created', sa.Numeric(14, 2), nullable=False, server_default='0'),
        sa.Column('created_after_finalization', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            'reversed_adjustment_id',
            sa.BigInteger(),
            sa.ForeignKey('consignment_manual_adjustments.id'),
        ),
        sa.Column(
            'replacement_for_adjustment_id',
            sa.BigInteger(),
            sa.ForeignKey('consignment_manual_adjustments.id'),
        ),
        sa.Column(
            'created_by_principal_id',
            sa.BigInteger(),
            sa.ForeignKey('principals.id'),
            nullable=False,
        ),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "adjustment_type IN ('SHIPPING_CHARGE', 'TAX_CHARGE', 'VENDOR_FEE', "
            "'MISCELLANEOUS_CHARGE', 'VENDOR_CREDIT', 'DAMAGE_CREDIT', 'PROMOTIONAL_CREDIT', "
            "'MISCELLANEOUS_CREDIT', 'CORRECTION_REVERSAL')",
            name='consignment_manual_adjustments_type_ck',
        ),
        sa.CheckConstraint(
            "direction IN ('INCREASE', 'DECREASE')",
            name='consignment_manual_adjustments_direction_ck',
        ),
        sa.CheckConstraint('amount > 0', name='consignment_manual_adjustments_amount_ck'),
        sa.CheckConstraint(
            '(report_id IS NOT NULL AND target_ledger_entry_id IS NULL) OR '
            '(report_id IS NULL AND target_ledger_entry_id IS NOT NULL)',
            name='consignment_manual_adjustments_target_ck',
        ),
        sa.UniqueConstraint('ledger_entry_id', name='consignment_manual_adjustments_ledger_uniq'),
        sa.UniqueConstraint(
            'reversed_adjustment_id', name='consignment_manual_adjustments_reversal_uniq'
        ),
    )
    op.create_index(
        'idx_consignment_manual_adjustments_vendor_date',
        'consignment_manual_adjustments',
        ['vendor_id', 'effective_date', 'id'],
    )
    op.create_index(
        'idx_consignment_manual_adjustments_report',
        'consignment_manual_adjustments',
        ['report_id', 'created_at'],
    )


def downgrade() -> None:
    op.drop_table('consignment_manual_adjustments')
    op.drop_constraint(
        'consignment_ledger_entries_type_ck',
        'consignment_ledger_entries',
        type_='check',
    )
    op.create_check_constraint(
        'consignment_ledger_entries_type_ck',
        'consignment_ledger_entries',
        "entry_type IN ('COGS_GENERATED', 'REPLENISHMENT_RECEIVED', 'REPLENISHMENT_APPLIED', "
        "'REPLENISHMENT_CREDIT_CREATED', 'REPLENISHMENT_CREDIT_USED', 'VENDOR_RETURN', "
        "'INVENTORY_ADJUSTMENT', 'CASH_SETTLEMENT', 'APPROVED_CREDIT', 'MANUAL_CORRECTION', "
        "'VOID_REVERSAL')",
    )
