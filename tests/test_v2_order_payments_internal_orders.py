from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session

from app.models import (
    Base,
    ConsignmentAllocation,
    ConsignmentLedgerEntry,
    ConsignmentManualAdjustment,
    ConsignmentReceiptAllocation,
    ConsignmentReplenishment,
    ConsignmentReplenishmentReceipt,
    ConsignmentReplenishmentReceiptLine,
    ConsignmentReport,
    OrderPayment,
    OrderPaymentBackfillOperation,
    OrderPaymentBackfillResult,
    OrderPaymentEvent,
    PaymentMethod,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderStatus,
    PurchaseOrderStoreAllocation,
    Store,
    Vendor,
    VendorPaymentClassification,
    VendorPaymentSetting,
)
from app.services.v2_order_payments_service import (
    confirm_historical_backfill,
    consignment_balance,
    classification_correction_preview,
    confirm_classification_correction,
    historical_backfill_preview,
    initialize_new_order_if_configured,
    order_payment_list_rows,
    purchase_order_scope_labels,
    save_vendor_settings,
    sync_consignment_replenishment,
    update_order_payment,
    update_payment_method,
    create_consignment_adjustment,
    reverse_consignment_adjustment,
)


TABLES = (
    'stores', 'vendors', 'purchase_orders', 'purchase_order_lines',
    'purchase_order_store_allocations', 'vendor_payment_settings',
    'vendor_payment_classifications', 'order_payment_backfill_operations',
    'order_payment_backfill_results',
    'order_payments', 'order_payment_events', 'consignment_reports',
    'consignment_replenishments', 'consignment_allocations',
    'consignment_replenishment_receipts', 'consignment_replenishment_receipt_lines',
    'consignment_receipt_allocations',
)


@pytest.fixture()
def db(monkeypatch):
    monkeypatch.setattr('app.services.v2_order_payments_service._audit', lambda *args, **kwargs: None)
    engine = create_engine('sqlite+pysqlite:///:memory:')
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[name] for name in TABLES])
    with engine.begin() as connection:
        # These two PostgreSQL models use constraints/index predicates that SQLite
        # cannot parse faithfully.  The focused service tests only need their
        # portable column contract.
        connection.exec_driver_sql('''CREATE TABLE payment_methods (
            id BIGINT PRIMARY KEY, display_name TEXT NOT NULL, category VARCHAR(24) NOT NULL,
            institution_or_company_name TEXT, account_nickname TEXT, last_four VARCHAR(4),
            term_days INTEGER, consignment_cycle VARCHAR(64), is_active BOOLEAN NOT NULL DEFAULT 1,
            notes TEXT, created_by_principal_id BIGINT NOT NULL, updated_by_principal_id BIGINT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL)''')
        connection.exec_driver_sql('''CREATE TABLE consignment_ledger_entries (
            id BIGINT PRIMARY KEY, vendor_id BIGINT NOT NULL, entry_type VARCHAR(40) NOT NULL,
            effective_at DATETIME NOT NULL, amount NUMERIC(14,2) NOT NULL, quantity NUMERIC(14,3),
            square_variation_id TEXT, report_id BIGINT, purchase_order_id BIGINT,
            payment_method_id BIGINT, note TEXT, created_by_principal_id BIGINT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL)''')
        connection.exec_driver_sql('''CREATE TABLE consignment_manual_adjustments (
            id BIGINT PRIMARY KEY, vendor_id BIGINT NOT NULL, report_id BIGINT,
            target_ledger_entry_id BIGINT, ledger_entry_id BIGINT NOT NULL UNIQUE,
            adjustment_type VARCHAR(40) NOT NULL, direction VARCHAR(12) NOT NULL,
            amount NUMERIC(14,2) NOT NULL, effective_date DATE NOT NULL, reason TEXT NOT NULL,
            internal_note TEXT, original_calculated_amount NUMERIC(14,2) NOT NULL,
            prior_adjusted_amount NUMERIC(14,2) NOT NULL,
            resulting_adjusted_amount NUMERIC(14,2) NOT NULL,
            excess_credit_created NUMERIC(14,2) NOT NULL DEFAULT 0,
            created_after_finalization BOOLEAN NOT NULL DEFAULT 0,
            reversed_adjustment_id BIGINT UNIQUE, replacement_for_adjustment_id BIGINT,
            created_by_principal_id BIGINT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL)''')
    session = Session(engine)
    counters = {}

    @event.listens_for(session, 'before_flush')
    def assign_bigint_ids(_session, _context, _instances):
        for row in session.new:
            if hasattr(row, 'id') and row.id is None:
                key = type(row).__name__
                counters[key] = counters.get(key, 0) + 1
                row.id = counters[key]

    session.add_all([
        Store(id=1, name='North', square_location_id='LOC-1', active=True),
        Store(id=2, name='South', square_location_id='LOC-2', active=True),
        Vendor(id=1, square_vendor_id='V-1', name='Invoice Vendor', active=True),
        Vendor(id=2, square_vendor_id='V-2', name='Consignment Vendor', active=True),
    ])
    session.commit()
    yield session
    session.close()


def _method(db, *, method_id, category, term_days=None):
    method = PaymentMethod(
        id=method_id,
        display_name='Net 30' if category == 'TERMS' else 'Rolling consignment',
        category=category,
        term_days=term_days,
        consignment_cycle='SINCE_LAST_FINALIZED_REPORT' if category == 'CONSIGNMENT' else None,
        is_active=True,
        created_by_principal_id=99,
        updated_by_principal_id=99,
    )
    db.add(method)
    return method


def _order(db, *, order_id, vendor_id, unit_cost='4.00', ordered_qty=10):
    ordered_at = datetime(2026, 7, 1, 18, tzinfo=timezone.utc)
    order = PurchaseOrder(
        id=order_id,
        vendor_id=vendor_id,
        status=PurchaseOrderStatus.IN_TRANSIT,
        created_by_principal_id=99,
        ordered_at=ordered_at,
        submitted_at=ordered_at,
        invoice_payment_status='UNPAID',
    )
    line = PurchaseOrderLine(
        id=order_id,
        purchase_order_id=order_id,
        variation_id=f'VAR-{order_id}',
        sku=f'SKU-{order_id}',
        item_name=f'Internal item {order_id}',
        variation_name='Default',
        unit_cost=Decimal(unit_cost) if unit_cost is not None else None,
        ordered_qty=ordered_qty,
        received_qty_total=0,
        in_transit_qty=ordered_qty,
        removed=False,
    )
    db.add_all([order, line])
    db.flush()
    return order, line


def _configure(db, *, vendor_id, method):
    db.add(VendorPaymentSetting(
        vendor_id=vendor_id,
        default_payment_method_id=method.id,
        updated_by_principal_id=99,
    ))
    classification = VendorPaymentClassification(
        vendor_id=vendor_id,
        payment_method_id=method.id,
        payment_category=method.category,
        payment_method_label_snapshot=method.display_name,
        term_days_snapshot=method.term_days if method.category == 'TERMS' else None,
        is_consignment=method.category == 'CONSIGNMENT',
        effective_date=datetime(2026, 1, 1, tzinfo=timezone.utc).date(),
        is_current=True,
        created_by_principal_id=99,
    )
    db.add(classification)
    return classification


def _backfill(db, *, vendor_id, method_id, order_ids):
    return confirm_historical_backfill(
        db,
        vendor_id=vendor_id,
        payment_method_id=method_id,
        scope_type='SELECTED',
        effective_from=None,
        selected_order_ids=order_ids,
        confirmation_note='Focused test backfill.',
        actor_id=99,
    )


def test_existing_v1_order_initializes_once_with_default_terms_and_never_mutates_v1(db):
    method = _method(db, method_id=1, category='TERMS', term_days=30)
    _configure(db, vendor_id=1, method=method)
    order, line = _order(db, order_id=10, vendor_id=1, unit_cost='3.25', ordered_qty=8)
    db.add_all([
        PurchaseOrderStoreAllocation(purchase_order_line_id=line.id, store_id=1,
            expected_qty=4, allocated_qty=4, store_received_qty=0, variance_qty=0),
        PurchaseOrderStoreAllocation(purchase_order_line_id=line.id, store_id=2,
            expected_qty=4, allocated_qty=4, store_received_qty=0, variance_qty=0),
    ])
    db.commit()
    original_v1 = (
        order.status, order.vendor_id, order.ordered_at, line.ordered_qty, line.unit_cost,
        order.invoice_payment_status, order.invoice_paid_date, order.invoice_paid_amount,
    )

    preview = historical_backfill_preview(
        db, vendor_id=1, payment_method_id=method.id,
        scope_type='SELECTED', selected_order_ids=[order.id]
    )
    assert preview['actionable_count'] == 1
    assert db.scalar(select(func.count(OrderPayment.id))) == 0
    operation = _backfill(db, vendor_id=1, method_id=method.id, order_ids=[order.id])
    assert operation.created_count == 1
    db.commit()
    duplicate = _backfill(db, vendor_id=1, method_id=method.id, order_ids=[order.id])
    assert duplicate.created_count == 0 and duplicate.skipped_count == 1
    db.commit()

    payment = db.scalar(select(OrderPayment))
    assert payment.purchase_order_id == order.id
    assert payment.status == 'UNPAID'
    assert payment.order_amount == Decimal('26.00')
    assert payment.payment_method_id == method.id
    assert payment.term_days_snapshot == 30
    assert payment.due_date.isoformat() == '2026-07-31'
    assert db.scalar(select(func.count(OrderPayment.id))) == 1
    assert db.scalar(select(func.count(OrderPaymentEvent.id))) == 1
    assert purchase_order_scope_labels(db, order_ids=[order.id]) == {order.id: 'North, South'}
    assert (
        order.status, order.vendor_id, order.ordered_at, line.ordered_qty, line.unit_cost,
        order.invoice_payment_status, order.invoice_paid_date, order.invoice_paid_amount,
    ) == original_v1


def test_ordinary_paid_unpaid_workflow_changes_only_v2_records(db):
    method = _method(db, method_id=1, category='TERMS', term_days=30)
    _configure(db, vendor_id=1, method=method)
    order, _line = _order(db, order_id=11, vendor_id=1)
    db.commit()
    _backfill(db, vendor_id=1, method_id=method.id, order_ids=[order.id]); db.commit()
    payment = db.scalar(select(OrderPayment))

    update_order_payment(db, order_payment_id=payment.id, payment_method_id=method.id,
        status='PAID', paid_date=None, actor_id=99)
    db.commit()
    assert payment.status == 'PAID'
    assert payment.paid_amount == Decimal('40.00')
    assert order.invoice_payment_status == 'UNPAID'
    assert order.invoice_paid_date is None
    assert order.invoice_paid_amount is None
    update_order_payment(db, order_payment_id=payment.id, payment_method_id=method.id,
        status='UNPAID', paid_date=None, actor_id=99)
    db.commit()
    assert payment.status == 'UNPAID'
    assert db.scalar(select(func.count(OrderPaymentEvent.id))) == 3


def test_incomplete_v1_cost_snapshot_cannot_be_marked_paid(db):
    method = _method(db, method_id=1, category='TERMS', term_days=30)
    _configure(db, vendor_id=1, method=method)
    _order_row, _line = _order(db, order_id=12, vendor_id=1, unit_cost=None)
    db.commit()
    preview = historical_backfill_preview(
        db, vendor_id=1, payment_method_id=method.id,
        scope_type='SELECTED', selected_order_ids=[12]
    )
    assert preview['rows'][0]['action'] == 'BLOCKED'
    operation = _backfill(db, vendor_id=1, method_id=method.id, order_ids=[12]); db.commit()
    assert operation.blocked_count == 1
    assert db.scalar(select(func.count(OrderPayment.id))) == 0


def test_partial_v1_receipt_allocates_only_received_value_with_lineage_and_credit(db):
    method = _method(db, method_id=2, category='CONSIGNMENT')
    _configure(db, vendor_id=2, method=method)
    order, line = _order(db, order_id=20, vendor_id=2, unit_cost='4.00', ordered_qty=10)
    north = PurchaseOrderStoreAllocation(purchase_order_line_id=line.id, store_id=1,
        expected_qty=5, allocated_qty=5, store_received_qty=3, variance_qty=0)
    south = PurchaseOrderStoreAllocation(purchase_order_line_id=line.id, store_id=2,
        expected_qty=5, allocated_qty=5, store_received_qty=2, variance_qty=0)
    line.received_qty_total = 5
    line.in_transit_qty = 5
    db.add_all([north, south])
    report = ConsignmentReport(id=1, vendor_id=2, report_number='COGS-V2-1',
        start_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        end_at=datetime(2026, 7, 1, tzinfo=timezone.utc), status='FINALIZED',
        total_units=3, total_cogs=Decimal('15.00'), inventory_quantity_snapshot=0,
        inventory_value_snapshot=0, data_integrity_blockers={}, created_by_principal_id=99)
    db.add(report); db.flush()
    db.add(ConsignmentLedgerEntry(vendor_id=2, entry_type='COGS_GENERATED',
        effective_at=report.end_at, amount=Decimal('15.00'), report_id=report.id,
        created_by_principal_id=99))
    db.commit()
    _backfill(db, vendor_id=2, method_id=method.id, order_ids=[order.id]); db.commit()
    replenishment = db.scalar(select(ConsignmentReplenishment))

    sync_consignment_replenishment(db, replenishment=replenishment, actor_id=99)
    db.commit()
    payment = db.scalar(select(OrderPayment))
    assert replenishment.ordered_cost_value == Decimal('40.00')
    assert replenishment.received_cost_value == Decimal('20.00')
    assert replenishment.amount_applied == Decimal('15.00')
    assert replenishment.excess_credit_created == Decimal('5.00')
    assert replenishment.status == 'PARTIALLY_APPLIED'
    assert payment.status == 'CONSIGNMENT_PARTIALLY_APPLIED'
    assert payment.paid_date is None and payment.paid_amount is None
    assert db.scalar(select(func.count(ConsignmentReplenishmentReceipt.id))) == 1
    receipt_lines = db.scalars(select(ConsignmentReplenishmentReceiptLine).order_by(
        ConsignmentReplenishmentReceiptLine.store_id)).all()
    assert [(row.store_id, row.received_qty_delta, row.received_value_delta) for row in receipt_lines] == [
        (1, 3, Decimal('12.00')), (2, 2, Decimal('8.00')),
    ]
    assert sum(db.scalars(select(ConsignmentReceiptAllocation.amount_applied)), Decimal('0')) == Decimal('15.00')
    assert db.scalar(select(func.count(ConsignmentLedgerEntry.id)).where(
        ConsignmentLedgerEntry.entry_type == 'REPLENISHMENT_RECEIVED')) == 1
    assert db.scalar(select(func.count(ConsignmentLedgerEntry.id)).where(
        ConsignmentLedgerEntry.entry_type == 'REPLENISHMENT_APPLIED')) == 2
    assert db.scalar(select(func.sum(ConsignmentLedgerEntry.amount)).where(
        ConsignmentLedgerEntry.entry_type == 'REPLENISHMENT_CREDIT_CREATED')) == Decimal('5.00')

    sync_consignment_replenishment(db, replenishment=replenishment, actor_id=99)
    db.commit()
    assert db.scalar(select(func.count(ConsignmentReplenishmentReceipt.id))) == 1
    assert db.scalar(select(func.count(ConsignmentLedgerEntry.id))) == 5


def test_consignment_waits_for_first_canonical_receipt(db):
    method = _method(db, method_id=2, category='CONSIGNMENT')
    _configure(db, vendor_id=2, method=method)
    order, line = _order(db, order_id=23, vendor_id=2, unit_cost='4.00', ordered_qty=5)
    allocation = PurchaseOrderStoreAllocation(
        purchase_order_line_id=line.id,
        store_id=1,
        expected_qty=5,
        allocated_qty=5,
        store_received_qty=0,
        variance_qty=0,
    )
    db.add(allocation)
    db.commit()

    assert initialize_new_order_if_configured(db, order=order, actor_id=99) is None
    preview = historical_backfill_preview(
        db,
        vendor_id=2,
        payment_method_id=method.id,
        scope_type='SELECTED',
        selected_order_ids=[order.id],
    )
    assert preview['rows'][0]['action'] == 'BLOCKED'
    assert 'begins only when inventory is received' in preview['rows'][0]['reason']
    list_row = next(row for row in order_payment_list_rows(db) if row['order'].id == order.id)
    assert list_row['display_state'] == 'UNINITIALIZED'
    assert list_row['reason'] == 'Waiting for a canonical V1 receipt before entering consignment.'
    assert db.scalar(select(func.count(OrderPayment.id))) == 0
    assert db.scalar(select(func.count(ConsignmentReplenishment.id))) == 0

    allocation.store_received_qty = 2
    line.received_qty_total = 2
    line.in_transit_qty = 3
    payment = initialize_new_order_if_configured(db, order=order, actor_id=99)
    db.commit()

    assert payment is not None
    assert payment.status == 'CONSIGNMENT_PARTIALLY_APPLIED'
    assert payment.paid_date is None and payment.paid_amount is None
    replenishment = db.scalar(select(ConsignmentReplenishment))
    assert replenishment.received_cost_value == Decimal('8.00')
    assert db.scalar(select(func.count(ConsignmentReplenishmentReceipt.id))) == 1


def test_received_quantity_decrease_blocks_silent_revaluation(db):
    method = _method(db, method_id=2, category='CONSIGNMENT')
    _configure(db, vendor_id=2, method=method)
    _order_row, line = _order(db, order_id=21, vendor_id=2, unit_cost='4.00', ordered_qty=5)
    allocation = PurchaseOrderStoreAllocation(purchase_order_line_id=line.id, store_id=1,
        expected_qty=5, allocated_qty=5, store_received_qty=2, variance_qty=0)
    line.received_qty_total = 2
    db.add(allocation); db.commit()
    _backfill(db, vendor_id=2, method_id=method.id, order_ids=[21]); db.commit()
    replenishment = db.scalar(select(ConsignmentReplenishment))
    sync_consignment_replenishment(db, replenishment=replenishment, actor_id=99); db.commit()
    allocation.store_received_qty = 1; line.received_qty_total = 1
    sync_consignment_replenishment(db, replenishment=replenishment, actor_id=99)
    assert 'typed correction is required' in replenishment.integrity_warning
    assert replenishment.received_cost_value == Decimal('8.00')


def test_aggregate_receipt_without_canonical_store_allocation_is_blocked(db):
    method = _method(db, method_id=2, category='CONSIGNMENT')
    _configure(db, vendor_id=2, method=method)
    _order_row, line = _order(db, order_id=22, vendor_id=2, unit_cost='4.00', ordered_qty=5)
    line.received_qty_total = 2
    line.in_transit_qty = 3
    db.commit()
    operation = _backfill(db, vendor_id=2, method_id=method.id, order_ids=[22]); db.commit()
    assert operation.blocked_count == 1
    assert db.scalar(select(ConsignmentReplenishment)) is None
    assert db.scalar(select(func.count(ConsignmentReplenishmentReceipt.id))) == 0
    assert db.scalar(select(func.count(ConsignmentLedgerEntry.id))) == 0


def test_read_only_list_and_direct_detail_never_initialize(db):
    from starlette.requests import Request

    from app.auth import Principal, Role
    from app.main import app
    from app.routers.v2_order_payments import order_payment_detail_page

    method = _method(db, method_id=1, category='TERMS', term_days=30)
    _configure(db, vendor_id=1, method=method)
    _order_row, line = _order(db, order_id=30, vendor_id=1, unit_cost='6.25', ordered_qty=4)
    db.add(PurchaseOrderStoreAllocation(purchase_order_line_id=line.id, store_id=1,
        expected_qty=4, allocated_qty=4, store_received_qty=2, variance_qty=0))
    line.received_qty_total = 2
    line.in_transit_qty = 2
    db.commit()
    owner = Principal(id=99, username='owner', role=Role.ADMIN, store_id=None, active=True)
    request = Request({
        'type': 'http', 'method': 'GET', 'path': '/v2/order-payments/30',
        'headers': [], 'query_string': b'', 'app': app,
    })
    request.state.principal = owner
    request.state.permission_flags = {}

    before = (
        db.scalar(select(func.count(OrderPayment.id))),
        db.scalar(select(func.count(OrderPaymentEvent.id))),
    )
    first = order_payment_list_rows(db)
    second = order_payment_list_rows(db)
    assert first[0]['display_state'] == 'UNINITIALIZED'
    assert second[0]['display_state'] == 'UNINITIALIZED'
    with pytest.raises(HTTPException) as missing:
        order_payment_detail_page(30, request, _feature=owner, principal=owner, db=db)
    assert missing.value.status_code == 404
    assert (
        db.scalar(select(func.count(OrderPayment.id))),
        db.scalar(select(func.count(OrderPaymentEvent.id))),
    ) == before


def test_order_payments_list_get_is_non_mutating_on_repeated_requests(db):
    from starlette.requests import Request

    from app.auth import Principal, Role
    from app.main import app
    from app.routers.v2_order_payments import order_payments_page

    method = _method(db, method_id=1, category='TERMS', term_days=30)
    _configure(db, vendor_id=1, method=method)
    _order(db, order_id=40, vendor_id=1)
    db.commit()
    owner = Principal(id=99, username='owner', role=Role.ADMIN, store_id=None, active=True)
    request = Request({
        'type': 'http', 'method': 'GET', 'path': '/v2/order-payments',
        'headers': [], 'query_string': b'', 'app': app,
    })
    request.state.principal = owner
    request.state.permission_flags = {}
    before = (
        db.scalar(select(func.count(OrderPayment.id))),
        db.scalar(select(func.count(OrderPaymentEvent.id))),
        db.scalar(select(func.count(OrderPaymentBackfillOperation.id))),
    )
    first = order_payments_page(request, _feature=owner, principal=owner, db=db)
    second = order_payments_page(request, _feature=owner, principal=owner, db=db)
    assert b'Setup required' in first.body
    assert b'Setup required' in second.body
    assert (
        db.scalar(select(func.count(OrderPayment.id))),
        db.scalar(select(func.count(OrderPaymentEvent.id))),
        db.scalar(select(func.count(OrderPaymentBackfillOperation.id))),
    ) == before == (0, 0, 0)


def test_unconfigured_vendor_stays_visible_and_uninitialized(db):
    method = _method(db, method_id=1, category='WIRE')
    _order(db, order_id=41, vendor_id=1)
    db.commit()
    preview = historical_backfill_preview(
        db,
        vendor_id=1,
        payment_method_id=method.id,
        scope_type='SELECTED',
        selected_order_ids=[41],
    )
    assert preview['rows'][0]['action'] == 'BLOCKED'
    assert 'UNCONFIGURED' in preview['rows'][0]['reason']
    assert order_payment_list_rows(db)[0]['display_state'] == 'UNINITIALIZED'
    assert db.scalar(select(func.count(OrderPayment.id))) == 0


def test_vendor_default_change_affects_future_orders_not_initialized_history(db):
    terms = _method(db, method_id=1, category='TERMS', term_days=30)
    _configure(db, vendor_id=1, method=terms)
    historical, _line = _order(db, order_id=42, vendor_id=1)
    db.commit()
    _backfill(db, vendor_id=1, method_id=terms.id, order_ids=[historical.id])
    db.commit()
    payment = db.scalar(select(OrderPayment).where(OrderPayment.purchase_order_id == historical.id))
    original = (
        payment.payment_method_id,
        payment.payment_category_snapshot,
        payment.term_days_snapshot,
        payment.due_date,
    )
    wire = _method(db, method_id=2, category='WIRE')
    db.flush()
    save_vendor_settings(
        db,
        vendor_id=1,
        default_payment_method_id=wire.id,
        report_email='',
        payment_notes='Future orders use wire.',
        effective_date=datetime(2026, 7, 2, tzinfo=timezone.utc).date(),
        actor_id=99,
    )
    future, _future_line = _order(db, order_id=43, vendor_id=1)
    future.ordered_at = datetime(2026, 7, 3, tzinfo=timezone.utc)
    future.submitted_at = future.ordered_at
    created = initialize_new_order_if_configured(db, order=future, actor_id=99)
    db.commit()
    assert (
        payment.payment_method_id,
        payment.payment_category_snapshot,
        payment.term_days_snapshot,
        payment.due_date,
    ) == original
    assert created is not None
    assert created.payment_method_id == wire.id
    assert created.payment_category_snapshot == 'WIRE'


def test_unsafe_invoice_to_consignment_inline_conversion_is_blocked(db):
    terms = _method(db, method_id=1, category='TERMS', term_days=30)
    consignment = _method(db, method_id=2, category='CONSIGNMENT')
    _configure(db, vendor_id=1, method=terms)
    order, _line = _order(db, order_id=44, vendor_id=1)
    db.commit()
    _backfill(db, vendor_id=1, method_id=terms.id, order_ids=[order.id]); db.commit()
    payment = db.scalar(select(OrderPayment))
    with pytest.raises(ValueError, match='cannot be converted to consignment'):
        update_order_payment(
            db,
            order_payment_id=payment.id,
            payment_method_id=consignment.id,
            status='UNPAID',
            paid_date=None,
            actor_id=99,
        )


def test_controlled_classification_correction_requires_clean_downstream_state_and_reason(db):
    terms = _method(db, method_id=1, category='TERMS', term_days=30)
    consignment = _method(db, method_id=2, category='CONSIGNMENT')
    _configure(db, vendor_id=1, method=terms)
    order, _line = _order(db, order_id=45, vendor_id=1)
    db.commit()
    _backfill(db, vendor_id=1, method_id=terms.id, order_ids=[order.id]); db.commit()
    payment = db.scalar(select(OrderPayment))
    preview = classification_correction_preview(
        db, order_payment_id=payment.id, payment_method_id=consignment.id
    )
    assert preview['allowed'] is True
    with pytest.raises(ValueError, match='reason is required'):
        confirm_classification_correction(
            db,
            order_payment_id=payment.id,
            payment_method_id=consignment.id,
            reason='',
            actor_id=99,
        )
    corrected = confirm_classification_correction(
        db,
        order_payment_id=payment.id,
        payment_method_id=consignment.id,
        reason='Owner verified the original classification was incorrect.',
        actor_id=99,
    )
    db.commit()
    assert corrected.financial_treatment == 'REPLENISHMENT'
    assert corrected.status == 'CONSIGNMENT_ORDERED'
    assert corrected.paid_date is None and corrected.paid_amount is None
    assert db.scalar(select(ConsignmentReplenishment)) is not None
    blocked = classification_correction_preview(
        db, order_payment_id=payment.id, payment_method_id=terms.id
    )
    assert blocked['allowed'] is False
    assert any('transition history' in reason for reason in blocked['blockers'])


def test_order_payment_source_has_no_square_purchase_order_dependency():
    source = (Path(__file__).resolve().parents[1] / 'app/services/v2_order_payments_service.py').read_text()
    assert 'SquareOrdersReader' not in source
    assert '/v2/orders/search' not in source
    assert 'received_qty_total' in source
    assert 'store_received_qty' in source


def test_payment_method_rename_preserves_order_snapshot_and_rejects_used_type_change(db):
    method = _method(db, method_id=1, category='TERMS', term_days=30)
    _configure(db, vendor_id=1, method=method)
    order, _line = _order(db, order_id=50, vendor_id=1)
    db.commit()
    _backfill(db, vendor_id=1, method_id=method.id, order_ids=[order.id])
    db.commit()
    payment = db.scalar(select(OrderPayment).where(OrderPayment.purchase_order_id == order.id))
    captured_label = payment.payment_method_label_snapshot

    update_payment_method(
        db, method_id=method.id, actor_id=99, display_name='USA Vape Lab Net 30',
        category='TERMS', institution='', account_nickname='', last_four='',
        term_days=30, notes='Owner-friendly name.',
    )
    db.commit()
    assert payment.payment_method_label_snapshot == captured_label
    assert db.scalar(select(VendorPaymentClassification).where(
        VendorPaymentClassification.vendor_id == 1,
        VendorPaymentClassification.is_current.is_(True),
    )).payment_method_label_snapshot == 'USA Vape Lab Net 30'
    with pytest.raises(ValueError, match='type cannot change'):
        update_payment_method(
            db, method_id=method.id, actor_id=99, display_name='USA Vape Lab Net 30',
            category='DEBIT_CARD', institution='Bank', account_nickname='Operating',
            last_four='1234', term_days=None, notes='',
        )


def test_manual_consignment_adjustments_create_credit_excess_and_append_only_reversal(db):
    cogs = ConsignmentLedgerEntry(
        vendor_id=2, entry_type='COGS_GENERATED', effective_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        amount=Decimal('100.00'), created_by_principal_id=99,
    )
    db.add(cogs)
    db.flush()
    charge = create_consignment_adjustment(
        db, vendor_id=2, report_id=None, target_ledger_entry_id=cogs.id,
        adjustment_type='SHIPPING_CHARGE', direction='INCREASE', amount=Decimal('20.00'),
        effective_date=date(2026, 7, 2), reason='Freight charged by vendor.', internal_note='Invoice 200',
        actor_id=99,
    )
    credit = create_consignment_adjustment(
        db, vendor_id=2, report_id=None, target_ledger_entry_id=cogs.id,
        adjustment_type='VENDOR_CREDIT', direction='DECREASE', amount=Decimal('150.00'),
        effective_date=date(2026, 7, 3), reason='Promotional credit memo.', internal_note=None,
        actor_id=99,
    )
    db.commit()
    balance = consignment_balance(db, vendor_id=2)
    assert balance.unreplenished_cogs == Decimal('0.00')
    assert balance.available_replenishment_credit == Decimal('30.00')
    assert credit.excess_credit_created == Decimal('30.00')

    reversal = reverse_consignment_adjustment(
        db, adjustment_id=credit.id, reason='Credit memo was assigned to the wrong vendor.', actor_id=99,
    )
    db.commit()
    assert reversal.reversed_adjustment_id == credit.id
    assert reversal.direction == 'INCREASE'
    assert db.scalar(select(func.count(ConsignmentManualAdjustment.id))) == 3
    assert db.get(ConsignmentManualAdjustment, charge.id).reason == 'Freight charged by vendor.'
    with pytest.raises(ValueError, match='already been reversed'):
        reverse_consignment_adjustment(
            db, adjustment_id=credit.id, reason='Duplicate reversal attempt.', actor_id=99,
        )


def test_finalized_report_adjustment_does_not_rewrite_report_total(db):
    report = ConsignmentReport(
        id=7, vendor_id=2, report_number='STREAMLINE-2026-07',
        start_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        end_at=datetime(2026, 8, 1, tzinfo=timezone.utc), status='FINALIZED',
        finalized_at=datetime(2026, 8, 2, tzinfo=timezone.utc), total_units=12,
        total_cogs=Decimal('400.00'), inventory_quantity_snapshot=24,
        inventory_value_snapshot=Decimal('600.00'), data_integrity_blockers={},
        created_by_principal_id=99,
    )
    db.add(report)
    db.flush()
    adjustment = create_consignment_adjustment(
        db, vendor_id=2, report_id=report.id, target_ledger_entry_id=None,
        adjustment_type='VENDOR_FEE', direction='INCREASE', amount=Decimal('15.00'),
        effective_date=date(2026, 8, 3), reason='Monthly portal fee.', internal_note=None,
        actor_id=99,
    )
    db.commit()
    assert report.total_cogs == Decimal('400.00')
    assert adjustment.prior_adjusted_amount == Decimal('400.00')
    assert adjustment.resulting_adjusted_amount == Decimal('415.00')
    assert adjustment.created_after_finalization is True
