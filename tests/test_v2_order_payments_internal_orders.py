from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session

from app.models import (
    Base,
    ConsignmentAllocation,
    ConsignmentLedgerEntry,
    ConsignmentReceiptAllocation,
    ConsignmentReplenishment,
    ConsignmentReplenishmentReceipt,
    ConsignmentReplenishmentReceiptLine,
    ConsignmentReport,
    OrderPayment,
    OrderPaymentEvent,
    PaymentMethod,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderStatus,
    PurchaseOrderStoreAllocation,
    Store,
    Vendor,
    VendorPaymentSetting,
)
from app.services.v2_order_payments_service import (
    backfill_placed_order_payments,
    purchase_order_scope_labels,
    sync_consignment_replenishment,
    update_order_payment,
)


TABLES = (
    'stores', 'vendors', 'purchase_orders', 'purchase_order_lines',
    'purchase_order_store_allocations', 'vendor_payment_settings',
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


def test_existing_v1_order_initializes_once_with_default_terms_and_never_mutates_v1(db):
    method = _method(db, method_id=1, category='TERMS', term_days=30)
    db.add(VendorPaymentSetting(vendor_id=1, default_payment_method_id=method.id,
        updated_by_principal_id=99))
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

    assert backfill_placed_order_payments(db, actor_id=99) == 1
    db.commit()
    assert backfill_placed_order_payments(db, actor_id=99) == 0
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
    db.add(VendorPaymentSetting(vendor_id=1, default_payment_method_id=method.id,
        updated_by_principal_id=99))
    order, _line = _order(db, order_id=11, vendor_id=1)
    db.commit()
    backfill_placed_order_payments(db, actor_id=99); db.commit()
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
    db.add(VendorPaymentSetting(vendor_id=1, default_payment_method_id=method.id,
        updated_by_principal_id=99))
    _order_row, _line = _order(db, order_id=12, vendor_id=1, unit_cost=None)
    db.commit()
    backfill_placed_order_payments(db, actor_id=99); db.commit()
    payment = db.scalar(select(OrderPayment))
    assert payment.order_cost_complete is False
    with pytest.raises(ValueError, match='line-cost snapshot is incomplete'):
        update_order_payment(db, order_payment_id=payment.id, payment_method_id=method.id,
            status='PAID', paid_date=None, actor_id=99)


def test_partial_v1_receipt_allocates_only_received_value_with_lineage_and_credit(db):
    method = _method(db, method_id=2, category='CONSIGNMENT')
    db.add(VendorPaymentSetting(vendor_id=2, default_payment_method_id=method.id,
        updated_by_principal_id=99))
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
    backfill_placed_order_payments(db, actor_id=99); db.commit()
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


def test_received_quantity_decrease_blocks_silent_revaluation(db):
    method = _method(db, method_id=2, category='CONSIGNMENT')
    db.add(VendorPaymentSetting(vendor_id=2, default_payment_method_id=method.id,
        updated_by_principal_id=99))
    _order_row, line = _order(db, order_id=21, vendor_id=2, unit_cost='4.00', ordered_qty=5)
    allocation = PurchaseOrderStoreAllocation(purchase_order_line_id=line.id, store_id=1,
        expected_qty=5, allocated_qty=5, store_received_qty=2, variance_qty=0)
    line.received_qty_total = 2
    db.add(allocation); db.commit()
    backfill_placed_order_payments(db, actor_id=99); db.commit()
    replenishment = db.scalar(select(ConsignmentReplenishment))
    sync_consignment_replenishment(db, replenishment=replenishment, actor_id=99); db.commit()
    allocation.store_received_qty = 1; line.received_qty_total = 1
    sync_consignment_replenishment(db, replenishment=replenishment, actor_id=99)
    assert 'typed correction is required' in replenishment.integrity_warning
    assert replenishment.received_cost_value == Decimal('8.00')


def test_aggregate_receipt_without_canonical_store_allocation_is_blocked(db):
    method = _method(db, method_id=2, category='CONSIGNMENT')
    db.add(VendorPaymentSetting(vendor_id=2, default_payment_method_id=method.id,
        updated_by_principal_id=99))
    _order_row, line = _order(db, order_id=22, vendor_id=2, unit_cost='4.00', ordered_qty=5)
    line.received_qty_total = 2
    line.in_transit_qty = 3
    db.commit()
    backfill_placed_order_payments(db, actor_id=99); db.commit()
    replenishment = db.scalar(select(ConsignmentReplenishment))
    sync_consignment_replenishment(db, replenishment=replenishment, actor_id=99)
    assert 'no canonical V1 store-allocation receipt rows' in replenishment.integrity_warning
    assert replenishment.received_cost_value == Decimal('0.00')
    assert db.scalar(select(func.count(ConsignmentReplenishmentReceipt.id))) == 0
    assert db.scalar(select(func.count(ConsignmentLedgerEntry.id))) == 0


def test_direct_v1_order_detail_lazily_initializes_and_renders_saved_snapshot(db):
    from starlette.requests import Request

    from app.auth import Principal, Role
    from app.main import app
    from app.routers.v2_order_payments import order_payment_detail_page

    method = _method(db, method_id=1, category='TERMS', term_days=30)
    db.add(VendorPaymentSetting(vendor_id=1, default_payment_method_id=method.id,
        updated_by_principal_id=99))
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

    response = order_payment_detail_page(
        30, request, _feature=owner, principal=owner, db=db
    )
    body = response.body.decode()
    assert 'Internal item 30' in body
    assert 'North: 2 / 4' in body
    assert '$6.25' in body
    assert 'Partially received' in body
    assert db.scalar(select(func.count(OrderPayment.id))) == 1


def test_order_payment_source_has_no_square_purchase_order_dependency():
    source = (Path(__file__).resolve().parents[1] / 'app/services/v2_order_payments_service.py').read_text()
    assert 'SquareOrdersReader' not in source
    assert '/v2/orders/search' not in source
    assert 'received_qty_total' in source
    assert 'store_received_qty' in source
