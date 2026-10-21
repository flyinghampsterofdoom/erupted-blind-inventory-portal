"""Add guarded historical order-payment backfill and remove defect snapshots.

Revision ID: 20260728_0012
Revises: 20260728_0011
Create Date: 2026-07-28
"""

from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op


revision = '20260728_0012'
down_revision = '20260728_0011'
branch_labels = None
depends_on = None


_DEFECT_PAYMENT_SOURCE = {
    1: (29, 15), 2: (30, 9), 3: (31, 2), 4: (41, 14), 5: (43, 13),
    6: (45, 13), 7: (47, 13), 8: (49, 2), 9: (50, 9), 10: (51, 13),
    11: (52, 33), 12: (53, 31), 13: (54, 13), 14: (65, 13), 15: (66, 15),
    16: (67, 9), 17: (69, 2), 18: (72, 9), 19: (73, 33), 20: (75, 13),
    21: (77, 14), 22: (78, 2), 23: (80, 13), 24: (85, 2), 25: (86, 9),
    26: (87, 2), 27: (90, 13), 28: (92, 31), 29: (93, 14), 30: (95, 15),
    31: (96, 33), 32: (97, 2), 33: (98, 32), 34: (99, 13), 35: (100, 36),
    36: (101, 9), 37: (102, 38), 38: (107, 32), 39: (109, 33),
}
_DEFECT_CREATED_AT = datetime(2026, 7, 28, 18, 13, 46, 242973, tzinfo=timezone.utc)
_DEFECT_NOTE = 'Safe V2 initialization; no paid state inferred.'


def _cleanup_defect_snapshots() -> None:
    bind = op.get_bind()
    ids = tuple(_DEFECT_PAYMENT_SOURCE)
    payments = bind.execute(
        sa.text(
            "SELECT id, purchase_order_id, vendor_id, status, financial_treatment, payment_method_id, "
            "payment_category_snapshot, term_days_snapshot, paid_amount, paid_date, marked_paid_at, "
            "marked_paid_by_principal_id, created_at FROM order_payments WHERE id IN :ids ORDER BY id"
        ).bindparams(sa.bindparam('ids', expanding=True)),
        {'ids': ids},
    ).mappings().all()
    if not payments:
        return
    if len(payments) != len(ids):
        raise RuntimeError('Defect cleanup refused: only part of the exact payment ID set exists.')
    for row in payments:
        if (
            _DEFECT_PAYMENT_SOURCE.get(int(row['id'])) != (
                int(row['purchase_order_id']), int(row['vendor_id'])
            )
            or row['status'] != 'UNPAID'
            or row['financial_treatment'] != 'INVOICE'
            or any(row[key] is not None for key in (
                'payment_method_id', 'payment_category_snapshot', 'term_days_snapshot',
                'paid_amount', 'paid_date', 'marked_paid_at', 'marked_paid_by_principal_id',
            ))
            or row['created_at'] != _DEFECT_CREATED_AT
        ):
            raise RuntimeError(f"Defect cleanup refused: payment {row['id']} no longer matches provenance.")

    events = bind.execute(
        sa.text(
            "SELECT id, order_payment_id, prior_status, new_status, prior_payment_method_id, "
            "new_payment_method_id, effective_date, note, actor_principal_id, created_at "
            "FROM order_payment_events WHERE order_payment_id IN :ids ORDER BY order_payment_id, id"
        ).bindparams(sa.bindparam('ids', expanding=True)),
        {'ids': ids},
    ).mappings().all()
    if len(events) != len(ids):
        raise RuntimeError('Defect cleanup refused: expected exactly one initialization event per payment.')
    for event in events:
        if (
            int(event['id']) != int(event['order_payment_id'])
            or int(event['order_payment_id']) not in _DEFECT_PAYMENT_SOURCE
            or event['prior_status'] is not None
            or event['new_status'] != 'UNPAID'
            or event['prior_payment_method_id'] is not None
            or event['new_payment_method_id'] is not None
            or event['effective_date'] is not None
            or event['note'] != _DEFECT_NOTE
            or int(event['actor_principal_id']) != 6
            or event['created_at'] != _DEFECT_CREATED_AT
        ):
            raise RuntimeError(
                f"Defect cleanup refused: event {event['id']} no longer matches initialization provenance."
            )

    order_ids = tuple(value[0] for value in _DEFECT_PAYMENT_SOURCE.values())
    reference_count = bind.execute(
        sa.text(
            "SELECT "
            "(SELECT count(*) FROM consignment_replenishments WHERE purchase_order_id IN :order_ids) + "
            "(SELECT count(*) FROM consignment_ledger_entries WHERE purchase_order_id IN :order_ids) + "
            "(SELECT count(*) FROM consignment_replenishment_receipts WHERE purchase_order_id IN :order_ids)"
        ).bindparams(sa.bindparam('order_ids', expanding=True)),
        {'order_ids': order_ids},
    ).scalar_one()
    if int(reference_count) != 0:
        raise RuntimeError('Defect cleanup refused: a targeted order has downstream financial references.')
    audit_count = bind.execute(
        sa.text(
            "SELECT count(*) FROM audit_log WHERE metadata->>'domain' = 'ORDER_PAYMENTS_V2' "
            "AND metadata->>'entity_type' = 'order_payment' "
            "AND (metadata->>'entity_id')::bigint IN :ids"
        ).bindparams(sa.bindparam('ids', expanding=True)),
        {'ids': ids},
    ).scalar_one()
    if int(audit_count) != 0:
        raise RuntimeError('Defect cleanup refused: a targeted payment has an additional audit reference.')
    if int(bind.execute(sa.text('SELECT count(*) FROM vendor_payment_settings')).scalar_one()) != 0:
        raise RuntimeError('Defect cleanup refused: vendor defaults now exist.')

    bind.execute(
        sa.text('DELETE FROM order_payment_events WHERE order_payment_id IN :ids').bindparams(
            sa.bindparam('ids', expanding=True)
        ),
        {'ids': ids},
    )
    deleted = bind.execute(
        sa.text('DELETE FROM order_payments WHERE id IN :ids').bindparams(
            sa.bindparam('ids', expanding=True)
        ),
        {'ids': ids},
    ).rowcount
    if deleted != len(ids):
        raise RuntimeError('Defect cleanup failed to delete the exact payment set atomically.')


def upgrade() -> None:
    op.create_table(
        'vendor_payment_classifications',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('vendor_id', sa.BigInteger(), sa.ForeignKey('vendors.id', ondelete='CASCADE'), nullable=False),
        sa.Column('payment_method_id', sa.BigInteger(), sa.ForeignKey('payment_methods.id', ondelete='RESTRICT')),
        sa.Column('payment_category', sa.String(24), nullable=False),
        sa.Column('payment_method_label_snapshot', sa.Text()),
        sa.Column('term_days_snapshot', sa.Integer()),
        sa.Column('is_consignment', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('effective_date', sa.Date(), nullable=False),
        sa.Column('internal_note', sa.Text()),
        sa.Column('is_current', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('superseded_at', sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "payment_category IN ('UNCONFIGURED', 'WIRE', 'CREDIT_CARD', 'DEBIT_CARD', 'TERMS', 'CONSIGNMENT')",
            name='vendor_payment_classifications_category_ck',
        ),
        sa.CheckConstraint(
            "(payment_category = 'UNCONFIGURED' AND payment_method_id IS NULL) OR "
            "(payment_category <> 'UNCONFIGURED' AND payment_method_id IS NOT NULL)",
            name='vendor_payment_classifications_method_ck',
        ),
        sa.CheckConstraint(
            "(payment_category = 'TERMS' AND term_days_snapshot IS NOT NULL AND term_days_snapshot > 0) OR "
            "(payment_category <> 'TERMS' AND term_days_snapshot IS NULL)",
            name='vendor_payment_classifications_terms_ck',
        ),
    )
    op.create_index(
        'idx_vendor_payment_classifications_vendor_effective',
        'vendor_payment_classifications', ['vendor_id', 'effective_date', 'id'],
    )
    op.create_index(
        'vendor_payment_classifications_current_vendor_uniq',
        'vendor_payment_classifications', ['vendor_id'], unique=True,
        postgresql_where=sa.text('is_current'),
    )

    op.create_table(
        'order_payment_backfill_operations',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('vendor_id', sa.BigInteger(), sa.ForeignKey('vendors.id'), nullable=False),
        sa.Column(
            'vendor_classification_id', sa.BigInteger(),
            sa.ForeignKey('vendor_payment_classifications.id', ondelete='RESTRICT'), nullable=False,
        ),
        sa.Column('scope_type', sa.String(24), nullable=False),
        sa.Column('effective_from', sa.Date()),
        sa.Column('selected_order_ids', sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('created_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('skipped_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('blocked_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('confirmation_note', sa.Text()),
        sa.Column('created_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('completed_at', sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "scope_type IN ('ALL_ELIGIBLE', 'FROM_DATE', 'SELECTED')",
            name='order_payment_backfill_operations_scope_ck',
        ),
        sa.CheckConstraint(
            "status IN ('CONFIRMED', 'COMPLETED', 'COMPLETED_WITH_BLOCKS')",
            name='order_payment_backfill_operations_status_ck',
        ),
    )
    op.create_index(
        'idx_order_payment_backfill_operations_vendor_created',
        'order_payment_backfill_operations', ['vendor_id', 'created_at'],
    )

    op.create_table(
        'order_payment_backfill_results',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column(
            'operation_id', sa.BigInteger(),
            sa.ForeignKey('order_payment_backfill_operations.id', ondelete='CASCADE'), nullable=False,
        ),
        sa.Column(
            'purchase_order_id', sa.BigInteger(),
            sa.ForeignKey('purchase_orders.id', ondelete='RESTRICT'), nullable=False,
        ),
        sa.Column('order_payment_id', sa.BigInteger(), sa.ForeignKey('order_payments.id', ondelete='RESTRICT')),
        sa.Column('outcome', sa.String(16), nullable=False),
        sa.Column('reason', sa.Text()),
        sa.Column('proposed_state', sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            'operation_id', 'purchase_order_id',
            name='order_payment_backfill_results_operation_order_uniq',
        ),
        sa.CheckConstraint(
            "outcome IN ('CREATED', 'SKIPPED', 'BLOCKED')",
            name='order_payment_backfill_results_outcome_ck',
        ),
    )
    op.create_index(
        'idx_order_payment_backfill_results_operation',
        'order_payment_backfill_results', ['operation_id', 'outcome'],
    )

    _cleanup_defect_snapshots()


def downgrade() -> None:
    op.drop_index('idx_order_payment_backfill_results_operation', table_name='order_payment_backfill_results')
    op.drop_table('order_payment_backfill_results')
    op.drop_index(
        'idx_order_payment_backfill_operations_vendor_created',
        table_name='order_payment_backfill_operations',
    )
    op.drop_table('order_payment_backfill_operations')
    op.drop_index(
        'vendor_payment_classifications_current_vendor_uniq',
        table_name='vendor_payment_classifications',
    )
    op.drop_index(
        'idx_vendor_payment_classifications_vendor_effective',
        table_name='vendor_payment_classifications',
    )
    op.drop_table('vendor_payment_classifications')
