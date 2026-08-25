import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    AuditLog,
    Base,
    ConsignmentReturnFact,
    ConsignmentSaleFact,
    ConsignmentSalesSyncState,
    FundingAccount,
    FundingLedgerEntry,
    FundingPayment,
    FundingPaymentAllocation,
    FundingReport,
    FundingReportAdjustment,
    FundingReportExclusion,
    FundingReportFactLink,
    FundingReportFifoException,
    FundingReportLine,
    FundingSkuMapping,
    OrderingCatalogIdentity,
    OrderingCurrentInventory,
    OrderPayment,
    PaymentMethod,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderReceipt,
    PurchaseOrderReceiptLine,
    PurchaseOrderReceiptStatus,
    PurchaseOrderStoreAllocation,
    PurchaseOrderStatus,
    Store,
    Vendor,
    VendorPaymentSetting,
)
from app.routers.v2_funding_reports import (
    _action_gate,
    _purchase_order_source_display_rows,
    _report_history_date,
    _report_history_rows,
    calculate_funding_report_action,
    owner_access,
)
from app.services.v2_funding_reports_service import (
    _credit_card_fifo_scope,
    account_summary,
    add_adjustment,
    apr_estimate,
    bulk_assign_skus,
    calculate_combined_report,
    calculate_report,
    combined_report_members,
    correct_funding_po_line_cost,
    credit_card_inventory_summary,
    delete_draft_report,
    delete_report,
    eligible_vendors_for_account,
    finalize_report,
    funding_account_vendor_memberships,
    funding_account_purchase_lines,
    funding_po_cost_correction_history,
    funding_report_source_readiness,
    funding_report_fifo_exceptions,
    is_combined_report,
    normalize_sku,
    overlapping_reports,
    record_compact_payment,
    record_inventory_purchase_for_order,
    record_ledger_entry,
    record_payment,
    report_position,
    reverse_adjustment,
    reverse_ledger_entry,
    reverse_payment,
    resolve_assigned_po_line_identities,
    resolve_funding_po_line_identity,
    resolve_funding_report_fifo_exception,
    tracked_balance,
    void_report,
)

TABLES = (
    'vendors', 'vendor_payment_settings',
    'stores',
    'purchase_orders', 'purchase_order_lines', 'purchase_order_store_allocations', 'purchase_order_receipts',
    'purchase_order_receipt_lines', 'order_payments',
    'consignment_sale_facts', 'consignment_return_facts', 'consignment_sales_sync_state',
    'funding_accounts',
    'funding_sku_mappings', 'funding_reports', 'funding_report_lines',
    'funding_report_fact_links', 'funding_report_exclusions',
    'funding_report_fifo_exceptions',
    'funding_report_adjustments', 'funding_payments', 'funding_payment_allocations',
    'funding_ledger_entries',
)


@pytest.fixture()
def db(monkeypatch):
    monkeypatch.setattr('app.services.v2_funding_reports_service._audit', lambda *args, **kwargs: None)
    engine = create_engine('sqlite+pysqlite:///:memory:')
    with engine.begin() as connection:
        connection.exec_driver_sql('''CREATE TABLE ordering_catalog_identity (
            square_variation_id TEXT PRIMARY KEY, square_item_id TEXT, sku TEXT,
            item_name TEXT, variation_name TEXT, product_name TEXT,
            square_is_deleted BOOLEAN NOT NULL DEFAULT 0, square_updated_at DATETIME,
            last_seen_at DATETIME NOT NULL, created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL)''')
        connection.exec_driver_sql('''CREATE TABLE ordering_current_inventory (
            square_variation_id TEXT NOT NULL, store_id BIGINT NOT NULL,
            square_location_id TEXT NOT NULL, counted_quantity NUMERIC(14,3) NOT NULL,
            source_calculated_at DATETIME, refreshed_at DATETIME NOT NULL,
            freshness_state VARCHAR(16) NOT NULL, refresh_run_id BIGINT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
            PRIMARY KEY (square_variation_id, store_id))''')
        connection.exec_driver_sql('''CREATE TABLE payment_methods (
            id BIGINT PRIMARY KEY, display_name TEXT NOT NULL, category VARCHAR(24) NOT NULL,
            institution_or_company_name TEXT, account_nickname TEXT, last_four VARCHAR(4),
            term_days INTEGER, consignment_cycle VARCHAR(64), is_active BOOLEAN NOT NULL DEFAULT 1,
            notes TEXT, created_by_principal_id BIGINT NOT NULL, updated_by_principal_id BIGINT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL)''')
        connection.exec_driver_sql('''CREATE TABLE audit_log (
            id BIGINT PRIMARY KEY, actor_principal_id BIGINT, action TEXT NOT NULL,
            session_id BIGINT, ip TEXT, metadata JSON NOT NULL DEFAULT '{}',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL)''')
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[name] for name in TABLES])
    session = Session(engine, autoflush=False)
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
        OrderingCatalogIdentity(square_variation_id='VAR-EXACT', sku=' ab 12 ', item_name='Exact Product',
            variation_name='Blue', product_name='Exact Product', square_is_deleted=False,
            last_seen_at=datetime.now(timezone.utc)),
        OrderingCatalogIdentity(square_variation_id='VAR-PARTIAL', sku='AB123', item_name='Partial Product',
            variation_name='Red', product_name='Partial Product', square_is_deleted=False,
            last_seen_at=datetime.now(timezone.utc)),
        OrderingCurrentInventory(square_variation_id='VAR-EXACT', store_id=1, square_location_id='LOC-1',
            counted_quantity=Decimal('5'), refreshed_at=datetime.now(timezone.utc),
            freshness_state='FRESH', refresh_run_id=1),
    ])
    session.flush()
    session.add_all([
        Vendor(id=10, square_vendor_id='V-10', name='Alpha Vendor', active=True),
        Vendor(id=11, square_vendor_id='V-11', name='Beta Vendor', active=True),
        PaymentMethod(id=20, display_name='Card B', category='CREDIT_CARD',
            is_active=True, created_by_principal_id=6, updated_by_principal_id=6),
        VendorPaymentSetting(vendor_id=10, default_payment_method_id=20,
            updated_by_principal_id=6),
        FundingAccount(id=1, account_type='CONSIGNMENT', vendor_id=10, display_name='Consignment A',
            is_active=True, created_by_principal_id=6, updated_by_principal_id=6),
        FundingAccount(id=2, account_type='CREDIT_CARD', payment_method_id=20, display_name='Card B',
            issuer='Issuer', last_four='1234', promotional_apr=Decimal('0'),
            promotional_start_date=date(2026, 1, 1), promotional_expiration_date=date(2026, 12, 31),
            standard_apr=Decimal('24'), is_active=True, created_by_principal_id=6,
            updated_by_principal_id=6),
        FundingAccount(id=3, account_type='CONSIGNMENT', vendor_id=11, display_name='Consignment B',
            is_active=True, created_by_principal_id=6, updated_by_principal_id=6),
        ConsignmentSalesSyncState(id=1,
            last_successful_start_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            last_successful_through_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
            last_successful_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
            last_result='COMPLETE', updated_by_principal_id=6),
    ])
    session.flush()
    _assign_card_po(session, vendor_id=10, order_id=200, create_line=True)
    session.commit()
    yield session
    session.close()


def _sale(db, *, fact_id=1, sku='AB12', day=date(2026, 7, 1), quantity='3', store_id=1,
          product='Exact Product', vendor_id=10, variation_id='VAR-EXACT'):
    row = ConsignmentSaleFact(id=fact_id, square_order_id=f'ORDER-{fact_id}',
        square_line_item_uid=f'LINE-{fact_id}', square_variation_id=variation_id,
        square_location_id=f'LOC-{store_id}', store_id=store_id, business_date=day,
        transacted_at=datetime(day.year, day.month, day.day, 20, tzinfo=timezone.utc),
        quantity_sold=Decimal(quantity), gross_sales_amount=Decimal('30'), discount_amount=0,
        tax_amount=0, net_sales_amount=Decimal('30'), currency='USD',
        product_name_snapshot=product, variation_name_snapshot='Blue', sku_snapshot=sku,
        vendor_id_snapshot=vendor_id,
        attribution_status='NON_CONSIGNMENT', attribution_source='SOURCE',
        source_synchronized_at=datetime.now(timezone.utc))
    db.add(row); db.flush(); return row


def _return(db, sale, *, fact_id=1, sku='AB12', day=date(2026, 7, 2), quantity='1', store_id=1):
    row = ConsignmentReturnFact(id=fact_id, square_return_order_id=f'RETURN-{fact_id}',
        square_return_uid=f'RET-{fact_id}', square_return_line_uid=f'RET-LINE-{fact_id}',
        original_square_order_id=sale.square_order_id, original_square_line_uid=sale.square_line_item_uid,
        square_variation_id=sale.square_variation_id, square_location_id=f'LOC-{store_id}', store_id=store_id,
        business_date=day, returned_at=datetime(day.year, day.month, day.day, 20, tzinfo=timezone.utc),
        quantity_returned=Decimal(quantity), refund_amount=Decimal('10'), currency='USD',
        product_name_snapshot='Exact Product', variation_name_snapshot='Blue', sku_snapshot=sku,
        vendor_id_snapshot=sale.vendor_id_snapshot,
        attribution_status='UNMATCHED_RETURN', source_synchronized_at=datetime.now(timezone.utc))
    row.original_sale_fact_id = sale.id
    db.add(row); db.flush(); return row


def _assign_order(db, *, account_id=1, sku='AB12', cost='4', original_vendor_id=99,
                  ordered_qty=10, order_day=date(2026, 6, 1), usable_sku=True):
    account = db.get(FundingAccount, account_id)
    order_id = int(db.scalar(select(PurchaseOrder.id).order_by(PurchaseOrder.id.desc())) or 0) + 1
    ordered_at = datetime(order_day.year, order_day.month, order_day.day, 18, tzinfo=timezone.utc)
    order = PurchaseOrder(id=order_id, vendor_id=original_vendor_id,
        status=PurchaseOrderStatus.SENT_TO_STORES, created_by_principal_id=6,
        ordered_at=ordered_at, submitted_at=ordered_at)
    db.add(order); db.flush()
    db.add(OrderPayment(purchase_order_id=order.id, vendor_id=account.vendor_id,
        payment_category_snapshot='CONSIGNMENT', payment_method_label_snapshot='Consignment',
        status='CONSIGNMENT_ORDERED', financial_treatment='REPLENISHMENT',
        order_amount=Decimal(str(cost)) * ordered_qty, order_cost_complete=True))
    line = PurchaseOrderLine(purchase_order_id=order.id, variation_id=f'PO-{order.id}-{sku}',
        sku=sku if usable_sku else None, item_name=f'Product {sku}', variation_name='Default',
        unit_cost=Decimal(str(cost)), ordered_qty=ordered_qty, suggested_qty=ordered_qty)
    db.add(line); db.flush()
    return order, line


def _map(db, *, account_id=1, sku='AB12', cost='4', start=date(2026, 1, 1), assign_order=True):
    identities = db.scalars(select(OrderingCatalogIdentity).where(
        OrderingCatalogIdentity.sku.is_not(None))).all()
    if not any(normalize_sku(row.sku) == normalize_sku(sku) for row in identities):
        normalized = normalize_sku(sku)
        db.add(OrderingCatalogIdentity(square_variation_id=f'VAR-{normalized}', sku=sku,
            item_name=f'Product {normalized}', variation_name='Default', product_name=f'Product {normalized}',
            square_is_deleted=False, last_seen_at=datetime.now(timezone.utc)))
        db.flush()
    mapping = bulk_assign_skus(db, account_id=account_id, skus=[sku], effective_date=start,
        unit_cost=Decimal(cost), reason='Owner verified account and cost.', actor_id=6)[0]
    if assign_order and db.get(FundingAccount, account_id).account_type == 'CONSIGNMENT':
        _assign_order(db, account_id=account_id, sku=sku, cost=cost)
    return mapping


def _report(db, *, account_id=1, acknowledged=False, start=date(2026, 7, 1), end=date(2026, 7, 2)):
    return calculate_report(db, account_id=account_id, start_date=start, end_date=end,
        vendor_id=10 if account_id == 2 else None,
        store_ids=[], sku_filter='', internal_note='', overlap_acknowledged=acknowledged, actor_id=6)


def test_normalized_sku_is_exact_and_never_uses_product_name_or_partial_match(db):
    assert normalize_sku(' ab 12 ') == 'AB12'
    _map(db); _sale(db, fact_id=1, sku='ab 12'); _sale(db, fact_id=2, sku='AB1', product='Exact Product')
    report = _report(db)
    lines = db.scalars(select(FundingReportLine).where(FundingReportLine.report_id == report.id)).all()
    exclusions = db.scalars(select(FundingReportExclusion).where(FundingReportExclusion.report_id == report.id)).all()
    assert len(lines) == 1 and lines[0].normalized_sku == 'AB12'
    assert report.calculated_cogs == Decimal('12.00')
    assert exclusions == []


def test_effective_mapping_can_differ_from_purchase_vendor_and_returns_reduce_cogs(db):
    mapping = _map(db, account_id=2)
    sale = _sale(db); _return(db, sale)
    report = _report(db, account_id=2)
    line = db.scalar(select(FundingReportLine).where(FundingReportLine.report_id == report.id))
    assert mapping.account_id == 2
    assert line.units_sold == 3 and line.units_returned == 1 and line.net_units == 2
    assert line.extended_cogs == Decimal('8.00')
    assert report.inventory_units_snapshot == 8
    assert report.inventory_value_snapshot == Decimal('32.00')


def test_same_sku_across_stores_stays_store_itemized(db):
    _map(db); _sale(db, fact_id=1, store_id=1); _sale(db, fact_id=2, store_id=2, quantity='2')
    report = _report(db)
    lines = db.scalars(select(FundingReportLine).where(FundingReportLine.report_id == report.id)
        .order_by(FundingReportLine.store_id)).all()
    assert [(row.store_id, row.units_sold) for row in lines] == [(1, 3), (2, 2)]


def test_incomplete_square_sync_cannot_be_reported_as_partial_sales(db):
    _map(db, sku='BIG-A', cost='4')
    _map(db, sku='BIG-B', cost='5')
    first = _sale(db, fact_id=1, sku='BIG-A', day=date(2026, 7, 1),
        quantity='3', store_id=1)
    state = db.get(ConsignmentSalesSyncState, 1)
    state.last_successful_through_at = datetime(2026, 7, 4, 7, tzinfo=timezone.utc)
    state.last_successful_at = datetime(2026, 7, 4, 8, tzinfo=timezone.utc)
    db.flush()

    with pytest.raises(ValueError, match='Square sales data is not complete'):
        calculate_report(db, account_id=1, start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 7), store_ids=[], sku_filter='', internal_note='',
            overlap_acknowledged=False, actor_id=6)
    assert db.scalar(select(FundingReport.id)) is None

    second = _sale(db, fact_id=2, sku='BIG-A', day=date(2026, 7, 7),
        quantity='2', store_id=2)
    _sale(db, fact_id=3, sku='BIG-B', day=date(2026, 7, 3),
        quantity='4', store_id=2)
    _return(db, second, fact_id=1, sku='BIG-A', day=date(2026, 7, 7),
        quantity='1', store_id=2)
    state.last_successful_through_at = datetime(2026, 7, 8, 7, tzinfo=timezone.utc)
    state.last_successful_at = datetime(2026, 7, 11, 8, tzinfo=timezone.utc)
    db.flush()

    report = calculate_report(db, account_id=1, start_date=date(2026, 7, 1),
        end_date=date(2026, 7, 7), store_ids=[], sku_filter='', internal_note='',
        overlap_acknowledged=False, actor_id=6)
    assert first.business_date == date(2026, 7, 1)
    assert report.units_sold == 9
    assert report.units_returned == 1
    assert report.net_units == 8
    assert report.calculated_cogs == Decimal('36.00')
    assert len(db.scalars(select(FundingReportFactLink).where(
        FundingReportFactLink.report_id == report.id)).all()) == 4
    assert report.warning_summary['square_source_readiness']['last_successful_at'] == (
        '2026-07-11T08:00:00+00:00')


def test_big_wholesale_period_uses_los_angeles_boundaries_and_detects_production_cutoff(db):
    state = db.get(ConsignmentSalesSyncState, 1)
    state.last_successful_start_at = datetime(2026, 7, 27, tzinfo=timezone.utc)
    state.last_successful_through_at = datetime(2026, 8, 4, tzinfo=timezone.utc)
    state.last_successful_at = datetime(2026, 8, 3, 18, 47, tzinfo=timezone.utc)
    db.flush()

    readiness = funding_report_source_readiness(
        db, start_date=date(2026, 8, 2), end_date=date(2026, 8, 8))

    assert readiness['period_start_at'] == datetime(2026, 8, 2, 7, tzinfo=timezone.utc)
    assert readiness['period_end_at'] == datetime(2026, 8, 9, 7, tzinfo=timezone.utc)
    assert readiness['blockers'] == ['SQUARE_SYNC_END_GAP']


def test_big_wholesale_route_refreshes_incomplete_coverage_before_calculation(
    db, monkeypatch
):
    _map(db, sku='BIG-WHOLESALE', cost='10.8870')
    _sale(
        db,
        fact_id=1,
        sku='BIG-WHOLESALE',
        day=date(2026, 8, 2),
        quantity='77',
    )
    state = db.get(ConsignmentSalesSyncState, 1)
    state.last_successful_start_at = datetime(2026, 8, 2, 7, tzinfo=timezone.utc)
    state.last_successful_through_at = datetime(2026, 8, 4, 7, tzinfo=timezone.utc)
    db.flush()
    calls = []

    def refresh(**values):
        calls.append(values)
        state.last_successful_through_at = values['end_at']
        state.last_successful_at = values['end_at']
        state.last_result = 'COMPLETE'
        db.commit()
        return type('Result', (), {'state': 'current', 'message': 'updated'})()

    monkeypatch.setattr(
        'app.routers.v2_funding_reports.refresh_square_sales_data', refresh
    )
    monkeypatch.setattr(settings, 'v2_consignment_cogs_actions_enabled', True)

    class Form(dict):
        def getlist(self, _key):
            return []

    class Request:
        headers = {}
        client = None

        async def form(self):
            return Form(
                account_id='1',
                start_date='2026-08-02',
                end_date='2026-08-08',
                sku_filter='',
                internal_note='',
            )

    owner = type('Owner', (), {'id': 6})()
    response = asyncio.run(
        calculate_funding_report_action(Request(), owner, owner, db, None)
    )

    report = db.scalar(select(FundingReport).order_by(FundingReport.id.desc()))
    assert response.status_code == 303
    assert len(calls) == 1
    assert calls[0]['start_at'] == datetime(2026, 8, 2, 7, tzinfo=timezone.utc)
    assert report.units_sold == 77
    assert report.calculated_cogs == Decimal('838.30')


def test_draft_must_be_recalculated_after_a_later_square_sync(db):
    _map(db)
    _sale(db)
    report = _report(db)
    state = db.get(ConsignmentSalesSyncState, 1)
    state.last_successful_at = datetime(2026, 8, 11, tzinfo=timezone.utc)
    db.flush()

    with pytest.raises(ValueError, match='synchronized after this draft'):
        finalize_report(db, report_id=report.id, actor_id=6)
    assert report.status == 'DRAFT'


def test_legacy_draft_without_source_readiness_cannot_be_finalized(db):
    _map(db)
    _sale(db)
    report = _report(db)
    report.warning_summary = {}
    db.flush()

    with pytest.raises(ValueError, match='predates Square source-readiness controls'):
        finalize_report(db, report_id=report.id, actor_id=6)
    assert report.status == 'DRAFT'


def test_later_owner_assignment_moves_sku_between_accounts_by_effective_date(db):
    first = _map(db, account_id=1, start=date(2026, 1, 1))
    second = _map(db, account_id=2, start=date(2026, 8, 1))
    assert first.effective_end_date == date(2026, 7, 31)
    assert second.effective_end_date is None


def test_account_a_report_includes_only_account_a_skus(db):
    _map(db, account_id=1, sku='A-ONLY'); _map(db, account_id=3, sku='B-ONLY')
    _sale(db, fact_id=1, sku='A-ONLY'); _sale(db, fact_id=2, sku='B-ONLY')
    report = _report(db, account_id=1)
    assert {row.normalized_sku for row in db.scalars(select(FundingReportLine).where(
        FundingReportLine.report_id == report.id)).all()} == {'A-ONLY'}


def test_account_b_report_includes_only_account_b_skus(db):
    _map(db, account_id=1, sku='A-ONLY'); _map(db, account_id=3, sku='B-ONLY')
    _sale(db, fact_id=1, sku='A-ONLY'); _sale(db, fact_id=2, sku='B-ONLY')
    report = _report(db, account_id=3)
    assert {row.normalized_sku for row in db.scalars(select(FundingReportLine).where(
        FundingReportLine.report_id == report.id)).all()} == {'B-ONLY'}


def test_credit_card_and_unmapped_skus_never_appear_in_consignment_report(db):
    _map(db, account_id=1, sku='CONSIGNMENT-ONLY'); _map(db, account_id=2, sku='CARD-ONLY')
    _sale(db, fact_id=1, sku='CONSIGNMENT-ONLY'); _sale(db, fact_id=2, sku='CARD-ONLY')
    _sale(db, fact_id=3, sku='UNMAPPED')
    report = _report(db, account_id=1)
    lines = db.scalars(select(FundingReportLine).where(FundingReportLine.report_id == report.id)).all()
    assert {row.normalized_sku for row in lines} == {'CONSIGNMENT-ONLY'}
    assert db.scalars(select(FundingReportExclusion).where(
        FundingReportExclusion.report_id == report.id)).all() == []


def test_no_assigned_orders_or_no_usable_purchase_order_skus_fails_closed(db):
    _sale(db, sku='AB12')
    with pytest.raises(ValueError, match='No purchase-order SKUs are assigned'):
        _report(db, account_id=1)
    _assign_order(db, account_id=1, usable_sku=False)
    with pytest.raises(ValueError, match='No purchase-order SKUs are assigned'):
        _report(db, account_id=1)
    assert db.scalar(select(FundingReportLine.id)) is None


def test_purchase_or_financial_vendor_context_alone_does_not_include_unmapped_sale(db):
    _map(db, account_id=1, sku='MAPPED')
    _sale(db, fact_id=1, sku='MAPPED')
    unrelated = _sale(db, fact_id=2, sku='VENDOR-CONTEXT-ONLY')
    unrelated.attribution_source = 'PURCHASE_ORDER_VENDOR_AND_FINANCIAL_VENDOR_MATCH'
    report = _report(db, account_id=1)
    links = db.scalars(select(FundingReportFactLink).where(
        FundingReportFactLink.report_id == report.id)).all()
    assert {row.sale_fact_id for row in links} == {1}


def test_purchase_order_cost_effective_date_restricts_each_sale_date(db):
    _map(db, account_id=1, sku='DATED', start=date(2026, 7, 2), assign_order=False)
    _assign_order(db, account_id=1, sku='DATED', order_day=date(2026, 7, 2))
    _sale(db, fact_id=1, sku='DATED', day=date(2026, 7, 1))
    _sale(db, fact_id=2, sku='DATED', day=date(2026, 7, 2), quantity='2')
    report = _report(db, account_id=1)
    line = db.scalar(select(FundingReportLine).where(FundingReportLine.report_id == report.id))
    assert line.units_sold == 2
    assert {row.sale_fact_id for row in db.scalars(select(FundingReportFactLink).where(
        FundingReportFactLink.report_id == report.id)).all()} == {2}


def test_returns_are_restricted_to_selected_accounts_mapped_skus(db):
    _map(db, account_id=1, sku='A-SKU'); _map(db, account_id=3, sku='B-SKU')
    sale_a = _sale(db, fact_id=1, sku='A-SKU'); sale_b = _sale(db, fact_id=2, sku='B-SKU')
    _return(db, sale_a, fact_id=1, sku='A-SKU'); _return(db, sale_b, fact_id=2, sku='B-SKU')
    report = _report(db, account_id=1)
    line = db.scalar(select(FundingReportLine).where(FundingReportLine.report_id == report.id))
    assert line.normalized_sku == 'A-SKU' and line.units_returned == 1
    assert {row.return_fact_id for row in db.scalars(select(FundingReportFactLink).where(
        FundingReportFactLink.report_id == report.id, FundingReportFactLink.return_fact_id.is_not(None))).all()} == {1}


def test_store_scope_is_applied_after_account_sku_boundary(db):
    _map(db, account_id=1, sku='SCOPED'); _map(db, account_id=3, sku='OTHER')
    _sale(db, fact_id=1, sku='SCOPED', store_id=1)
    _sale(db, fact_id=2, sku='SCOPED', store_id=2)
    _sale(db, fact_id=3, sku='OTHER', store_id=1)
    report = calculate_report(db, account_id=1, start_date=date(2026, 7, 1), end_date=date(2026, 7, 2),
        store_ids=[2], sku_filter='', internal_note='', overlap_acknowledged=False, actor_id=6)
    lines = db.scalars(select(FundingReportLine).where(FundingReportLine.report_id == report.id)).all()
    assert [(row.normalized_sku, row.store_id) for row in lines] == [('SCOPED', 2)]


def test_selected_account_id_is_required_by_service_and_route(db):
    with pytest.raises(ValueError, match='account ID is required'):
        calculate_report(db, account_id=None, start_date=date(2026, 7, 1), end_date=date(2026, 7, 2),
            store_ids=[], sku_filter='', internal_note='', overlap_acknowledged=False, actor_id=6)

    class Form(dict):
        def getlist(self, _key): return []
    class Request:
        async def form(self): return Form()
    class RouteDb:
        def rollback(self): pass
        def get(self, *_args): raise AssertionError('missing account ID must fail before lookup')
    owner = type('Owner', (), {'id': 6})()
    response = asyncio.run(calculate_funding_report_action(Request(), owner, owner, RouteDb(), None))
    assert response.status_code == 303 and 'Choose%20an%20account' in response.headers['location']


def test_stale_lines_from_another_account_are_never_reused(db):
    _map(db, account_id=1, sku='A-LINE'); _map(db, account_id=3, sku='B-LINE')
    _sale(db, fact_id=1, sku='A-LINE'); _sale(db, fact_id=2, sku='B-LINE')
    report_a = _report(db, account_id=1)
    report_b = _report(db, account_id=3)
    lines_b = db.scalars(select(FundingReportLine).where(FundingReportLine.report_id == report_b.id)).all()
    assert {row.normalized_sku for row in lines_b} == {'B-LINE'}
    assert all(row.report_id != report_a.id for row in lines_b)


def test_final_total_reconciles_exactly_to_restricted_lines(db):
    _map(db, account_id=1, sku='ONE', cost='2'); _map(db, account_id=1, sku='TWO', cost='5')
    _sale(db, fact_id=1, sku='ONE', quantity='3'); _sale(db, fact_id=2, sku='TWO', quantity='2')
    report = _report(db, account_id=1)
    lines = db.scalars(select(FundingReportLine).where(FundingReportLine.report_id == report.id)).all()
    assert report.calculated_cogs == sum((row.extended_cogs for row in lines), Decimal('0')) == Decimal('16.00')


def test_many_unrelated_sales_cannot_expand_two_sku_account_boundary(db):
    _map(db, account_id=1, sku='ONLY-ONE', cost='2'); _map(db, account_id=1, sku='ONLY-TWO', cost='3')
    _sale(db, fact_id=1, sku='ONLY-ONE'); _sale(db, fact_id=2, sku='ONLY-TWO')
    for fact_id in range(3, 53):
        _sale(db, fact_id=fact_id, sku=f'UNRELATED-{fact_id}')
    report = _report(db, account_id=1)
    lines = db.scalars(select(FundingReportLine).where(FundingReportLine.report_id == report.id)).all()
    assert {row.normalized_sku for row in lines} == {'ONLY-ONE', 'ONLY-TWO'}
    assert len(db.scalars(select(FundingReportFactLink).where(
        FundingReportFactLink.report_id == report.id)).all()) == 2


def test_original_purchase_order_vendor_may_differ_from_financial_account(db):
    _map(db, account_id=1, sku='VENDOR-DIFFERENT')
    _sale(db, sku='VENDOR-DIFFERENT')
    report = _report(db, account_id=1)
    source = report.warning_summary['purchase_order_scope']['source_lines'][0]
    assert source['original_vendor_id'] == 99
    assert source['financial_vendor_id'] == db.get(FundingAccount, 1).vendor_id == 10
    assert db.scalar(select(FundingReportLine).where(
        FundingReportLine.report_id == report.id)).normalized_sku == 'VENDOR-DIFFERENT'


def test_saved_purchase_order_cost_is_numeric_when_rendering_report_history():
    rows = _purchase_order_source_display_rows({
        'source_lines': [{
            'purchase_order_line_id': 41,
            'unit_cost': '9.65',
        }],
    }, {
        'PO_LINE:41': {
            'units_sold': Decimal('2'),
            'units_returned': Decimal('0'),
            'net_units': Decimal('2'),
            'calculated_cogs': Decimal('19.30'),
        },
    })
    assert rows[0]['unit_cost'] == Decimal('9.65')
    assert rows[0]['calculated_cogs'] == Decimal('19.30')


def test_credit_card_funded_and_unassigned_orders_do_not_contribute_skus(db):
    _map(db, account_id=1, sku='VALID')
    _assign_order(db, account_id=1, sku='CARD-FUNDED')
    card_payment = db.scalar(select(OrderPayment).order_by(OrderPayment.id.desc()))
    card_payment.financial_treatment = 'INVOICE'; card_payment.payment_category_snapshot = 'CREDIT_CARD'
    _assign_order(db, account_id=1, sku='UNASSIGNED')
    unassigned_payment = db.scalar(select(OrderPayment).order_by(OrderPayment.id.desc()))
    db.delete(unassigned_payment); db.flush()
    _sale(db, fact_id=1, sku='VALID'); _sale(db, fact_id=2, sku='CARD-FUNDED'); _sale(db, fact_id=3, sku='UNASSIGNED')
    report = _report(db, account_id=1)
    assert {row.normalized_sku for row in db.scalars(select(FundingReportLine).where(
        FundingReportLine.report_id == report.id)).all()} == {'VALID'}


def test_duplicate_sku_across_assigned_orders_does_not_duplicate_sales(db):
    _map(db, account_id=1, sku='REPEATED', cost='4')
    _assign_order(db, account_id=1, sku='REPEATED', cost='4', order_day=date(2026, 6, 15))
    _sale(db, sku='REPEATED', quantity='3')
    report = _report(db, account_id=1)
    lines = db.scalars(select(FundingReportLine).where(FundingReportLine.report_id == report.id)).all()
    assert len(lines) == 1 and lines[0].units_sold == 3 and report.calculated_cogs == Decimal('12.00')
    assert report.warning_summary['purchase_order_scope']['assigned_purchase_order_count'] == 2
    assert report.warning_summary['purchase_order_scope']['eligible_sku_count'] == 1


def test_financial_reassignment_changes_future_eligibility_only(db):
    _map(db, account_id=1, sku='TRANSFERRED')
    _sale(db, sku='TRANSFERRED')
    historical = _report(db, account_id=1)
    finalize_report(db, report_id=historical.id, actor_id=6)
    saved_snapshot = dict(historical.finalized_snapshot)
    payment = db.scalar(select(OrderPayment).where(OrderPayment.vendor_id == 10))
    payment.vendor_id = 11; db.flush()
    with pytest.raises(ValueError, match='No purchase-order SKUs are assigned'):
        _report(db, account_id=1)
    replacement = _report(db, account_id=3)
    assert {row.normalized_sku for row in db.scalars(select(FundingReportLine).where(
        FundingReportLine.report_id == replacement.id)).all()} == {'TRANSFERRED'}
    db.refresh(historical)
    assert historical.finalized_snapshot == saved_snapshot
    assert db.scalar(select(FundingReportLine).where(
        FundingReportLine.report_id == historical.id)).normalized_sku == 'TRANSFERRED'


def test_draft_deletion_removes_only_draft_records_and_preserves_audit_snapshot(db, monkeypatch):
    events = []
    monkeypatch.setattr('app.services.v2_funding_reports_service._audit',
        lambda _db, **values: events.append(values))
    _map(db, account_id=1, sku='DELETE-ME'); _sale(db, sku='DELETE-ME')
    draft = _report(db, account_id=1)
    line_ids = db.scalars(select(FundingReportLine.id).where(FundingReportLine.report_id == draft.id)).all()
    link_ids = db.scalars(select(FundingReportFactLink.id).where(FundingReportFactLink.report_id == draft.id)).all()
    ledger_before = len(db.scalars(select(FundingLedgerEntry)).all())
    snapshot = delete_draft_report(db, report_id=draft.id, actor_id=6, reason='Owner discarded preview')
    assert db.get(FundingReport, draft.id) is None
    assert db.scalars(select(FundingReportLine).where(FundingReportLine.id.in_(line_ids))).all() == []
    assert db.scalars(select(FundingReportFactLink).where(FundingReportFactLink.id.in_(link_ids))).all() == []
    assert len(db.scalars(select(FundingLedgerEntry)).all()) == ledger_before
    assert snapshot['account_id'] == 1 and snapshot['reason'] == 'Owner discarded preview'
    assert events[-1]['action'] == 'FUNDING_DRAFT_REPORT_DELETED'


def test_finalized_report_cannot_be_deleted_and_retains_void_path(db):
    _map(db); _sale(db); report = _report(db)
    finalize_report(db, report_id=report.id, actor_id=6)
    with pytest.raises(ValueError, match='Only an unfinalized draft'):
        delete_draft_report(db, report_id=report.id, actor_id=6)
    void_report(db, report_id=report.id, reason='Append-only correction', actor_id=6)
    assert report.status == 'VOIDED'


def test_deleting_one_accounts_draft_does_not_affect_another_account(db):
    _map(db, account_id=1, sku='ACCOUNT-A'); _map(db, account_id=3, sku='ACCOUNT-B')
    _sale(db, fact_id=1, sku='ACCOUNT-A'); _sale(db, fact_id=2, sku='ACCOUNT-B')
    draft_a = _report(db, account_id=1); draft_b = _report(db, account_id=3)
    delete_draft_report(db, report_id=draft_a.id, actor_id=6)
    assert db.get(FundingReport, draft_a.id) is None
    assert db.get(FundingReport, draft_b.id) is draft_b
    assert {row.normalized_sku for row in db.scalars(select(FundingReportLine).where(
        FundingReportLine.report_id == draft_b.id)).all()} == {'ACCOUNT-B'}


def test_draft_delete_ui_and_route_are_owner_csrf_protected():
    from app.main import app
    template = open('app/templates/v2/order_payments/funding_report_detail.html').read()
    assert 'Delete Report' in template and 'Delete Draft' in template
    assert "report.status == 'DRAFT'" in template and "report.status != 'VOIDED'" in template
    delete_route = next(route for route in app.routes
        if getattr(route, 'path', '').endswith('/reports/{report_id}/delete'))
    calls = [dependency.call for dependency in delete_route.dependant.dependencies]
    from app.routers.v2_order_payments import feature_access, owner_access
    from app.security.csrf import verify_csrf
    assert feature_access in calls and owner_access in calls and verify_csrf in calls
    store = type('StorePrincipal', (), {'role': __import__('app.auth', fromlist=['Role']).Role.STORE})()
    with pytest.raises(HTTPException) as denied:
        owner_access(store)
    assert denied.value.status_code == 404


def test_optional_product_filter_only_narrows_exact_sku_attribution(db):
    _map(db); _sale(db, product='Exact Product')
    report = calculate_report(db, account_id=1, start_date=date(2026, 7, 1), end_date=date(2026, 7, 2),
        store_ids=[], sku_filter='Exact Product', internal_note='', overlap_acknowledged=False, actor_id=6)
    assert report.calculated_cogs == Decimal('12.00')
    assert db.scalar(select(FundingReportLine).where(FundingReportLine.report_id == report.id)).normalized_sku == 'AB12'


def test_credit_card_fifo_ignores_conflicting_legacy_mappings(db):
    _map(db, account_id=2)
    db.add(FundingSkuMapping(account_id=3, normalized_sku='AB12', sku_snapshot='AB12',
        square_variation_id='VAR-EXACT', product_name_snapshot='Exact Product',
        variation_name_snapshot='Blue', effective_start_date=date(2026, 1, 1), unit_cost=Decimal('5'),
        status='ACTIVE', reason='Conflicting fixture', created_by_principal_id=6))
    db.flush(); _sale(db)
    report = _report(db, account_id=2)
    assert report.units_sold == 3
    assert report.calculated_cogs == Decimal('12.00')


def test_credit_card_fifo_ignores_mapping_without_effective_cost(db):
    db.add(FundingSkuMapping(account_id=2, normalized_sku='AB12', sku_snapshot='AB12',
        square_variation_id='VAR-EXACT', product_name_snapshot='Exact Product',
        variation_name_snapshot='Blue', effective_start_date=date(2026, 1, 1), unit_cost=None,
        status='ACTIVE', reason='Imported mapping awaiting owner cost', created_by_principal_id=6))
    _sale(db)
    report = _report(db, account_id=2)
    assert report.units_sold == 3
    assert report.calculated_cogs == Decimal('12.00')


def test_overlapping_ranges_warn_then_include_the_same_sales_when_acknowledged(db):
    _map(db); _sale(db)
    first = _report(db); finalize_report(db, report_id=first.id, actor_id=6); db.commit()
    with pytest.raises(ValueError, match='OVERLAP_ACKNOWLEDGEMENT_REQUIRED'):
        _report(db)
    db.rollback()
    second = _report(db, acknowledged=True)
    assert second.overlap_acknowledged is True
    assert second.overlapping_report_ids == [first.id]
    assert second.calculated_cogs == first.calculated_cogs == Decimal('12.00')
    assert db.scalar(select(FundingReportFactLink).where(
        FundingReportFactLink.report_id == second.id)).sale_fact_id == 1


def test_finalized_report_stays_reproducible_after_new_mapping_cost(db):
    _map(db, cost='4'); _sale(db)
    report = _report(db); finalize_report(db, report_id=report.id, actor_id=6); db.commit()
    original = dict(report.finalized_snapshot)
    _map(db, cost='9', start=date(2026, 8, 1)); db.commit()
    db.refresh(report)
    assert report.calculated_cogs == Decimal('12.00')
    assert report.finalized_snapshot == original
    assert original['adjusted_amount'] == '12.00'


def test_adjustments_are_append_only_and_reversed_with_opposite_entry(db):
    _map(db); _sale(db); report = _report(db)
    finalize_report(db, report_id=report.id, actor_id=6)
    charge = add_adjustment(db, report_id=report.id, adjustment_type='SHIPPING', direction='INCREASE',
        amount=Decimal('2'), effective_date=date(2026, 7, 3), reason='Freight', internal_note='',
        owner_confirmed=True, actor_id=6)
    assert report_position(db, report_id=report.id)['adjusted_amount'] == Decimal('14.00')
    reversal = reverse_adjustment(db, adjustment_id=charge.id, reason='Entered twice', actor_id=6)
    assert reversal.reversed_adjustment_id == charge.id
    assert db.scalar(select(FundingReportAdjustment).where(FundingReportAdjustment.id == charge.id)) is charge
    assert report_position(db, report_id=report.id)['adjusted_amount'] == Decimal('12.00')


def test_partial_multi_report_payment_and_excess_are_not_double_allocated(db):
    _map(db); _sale(db); _sale(db, fact_id=2, day=date(2026, 7, 3))
    first = _report(db); finalize_report(db, report_id=first.id, actor_id=6); db.commit()
    second = _report(db, start=date(2026, 7, 3), end=date(2026, 7, 4))
    finalize_report(db, report_id=second.id, actor_id=6); db.commit()
    payment = record_payment(db, account_id=1, entry_type='PAYMENT', amount=Decimal('30'),
        payment_date=date(2026, 7, 4), payment_source='Owner', confirmation_number='TEST',
        reason='Safe test settlement', internal_note='', allocations={first.id: Decimal('8'), second.id: Decimal('20')},
        actor_id=6)
    allocations = db.scalars(select(FundingPaymentAllocation).where(
        FundingPaymentAllocation.payment_id == payment.id).order_by(FundingPaymentAllocation.report_id)).all()
    assert [row.amount for row in allocations] == [Decimal('8.00'), Decimal('12.00')]
    assert report_position(db, report_id=first.id)['remaining_amount'] == Decimal('4.00')
    assert report_position(db, report_id=second.id)['remaining_amount'] == Decimal('0.00')
    assert payment.amount - sum((row.amount for row in allocations), Decimal('0')) == Decimal('10.00')
    assert account_summary(db, account_id=1)['unallocated_payment'] == Decimal('10.00')


def test_consignment_replenishment_and_cash_settlement_stay_distinct(db):
    _map(db); _sale(db); report = _report(db); finalize_report(db, report_id=report.id, actor_id=6)
    record_payment(db, account_id=1, entry_type='REPLENISHMENT', amount=Decimal('15'),
        payment_date=date(2026, 7, 4), payment_source='Inventory', confirmation_number='',
        reason='Replacement inventory received', internal_note='', allocations={report.id: Decimal('7')}, actor_id=6)
    record_payment(db, account_id=1, entry_type='PAYMENT', amount=Decimal('2'),
        payment_date=date(2026, 7, 4), payment_source='Bank', confirmation_number='',
        reason='Exceptional cash settlement', internal_note='', allocations={report.id: Decimal('2')}, actor_id=6)
    position = report_position(db, report_id=report.id)
    summary = account_summary(
        db, account_id=1, include_purchase_order_lines=True)
    assert position['replenishment_applied'] == Decimal('7.00')
    assert position['cash_settlement'] == Decimal('2.00')
    assert position['remaining_amount'] == Decimal('3.00')
    assert summary['available_replenishment_credit'] == Decimal('8.00')


def test_credit_card_balance_opening_payment_interest_and_zero_percent_estimates(db):
    account = db.get(FundingAccount, 2)
    opening = record_ledger_entry(db, account_id=2, entry_type='OPENING_BALANCE', direction='INCREASE',
        amount=Decimal('1000'), effective_date=date(2026, 1, 1), reason='Owner opening balance',
        internal_note='', actor_id=6, inventory_backed_estimate=Decimal('600'))
    record_ledger_entry(db, account_id=2, entry_type='INTEREST', direction='INCREASE',
        amount=Decimal('25'), effective_date=date(2026, 7, 1), reason='Actual statement interest',
        internal_note='', actor_id=6)
    record_payment(db, account_id=2, entry_type='PAYMENT', amount=Decimal('100'),
        vendor_id=10,
        payment_date=date(2026, 7, 2), payment_source='', confirmation_number='',
        reason='Card payment', internal_note='', allocations={}, actor_id=6)
    balance = tracked_balance(db, account_id=2)
    estimate = apr_estimate(account, balance, today=date(2026, 7, 3))
    assert balance == Decimal('925.00')
    assert estimate.promotional_active is True and estimate.annual_cost == Decimal('0.00')
    assert estimate.post_promotion_annual_cost == Decimal('222.00')
    assert estimate.post_promotion_monthly_cost == Decimal('18.50')
    assert opening.inventory_backed_estimate == Decimal('600.00')
    assert {row.entry_type for row in db.scalars(select(FundingLedgerEntry)).all()} == {
        'OPENING_BALANCE', 'INTEREST', 'PAYMENT'
    }


def test_tracked_balance_formula_preserves_an_account_credit(db):
    record_payment(db, account_id=2, entry_type='PAYMENT', amount=Decimal('25'),
        vendor_id=10,
        payment_date=date(2026, 7, 2), payment_source='', confirmation_number='',
        reason='General card payment', internal_note='', allocations={}, actor_id=6)
    assert tracked_balance(db, account_id=2) == Decimal('-25.00')


def test_owner_confirmed_credit_card_purchase_posts_once_and_get_reads_post_nothing(db):
    entry = record_inventory_purchase_for_order(db, payment_method_id=20, order_payment_id=99,
        amount=Decimal('125.50'), effective_date=date(2026, 7, 1), actor_id=6)
    duplicate = record_inventory_purchase_for_order(db, payment_method_id=20, order_payment_id=99,
        amount=Decimal('999'), effective_date=date(2026, 7, 2), actor_id=6)
    assert duplicate.id == entry.id and entry.entry_type == 'INVENTORY_PURCHASE'
    assert tracked_balance(db, account_id=2) == Decimal('125.50')
    assert len(db.scalars(select(FundingLedgerEntry)).all()) == 1


def test_opening_balance_and_payment_reversals_are_append_only(db):
    opening = record_ledger_entry(db, account_id=2, entry_type='OPENING_BALANCE', direction='INCREASE',
        amount=Decimal('100'), effective_date=date(2026, 7, 1), reason='Owner opening balance',
        internal_note='', actor_id=6)
    ledger_reversal = reverse_ledger_entry(db, entry_id=opening.id, reason='Replace opening balance', actor_id=6)
    payment = record_payment(db, account_id=2, entry_type='PAYMENT', amount=Decimal('20'),
        vendor_id=10,
        payment_date=date(2026, 7, 2), payment_source='', confirmation_number='',
        reason='Card payment', internal_note='', allocations={}, actor_id=6)
    payment_reversal = reverse_payment(db, payment_id=payment.id, reason='Payment reversed by bank', actor_id=6)
    assert ledger_reversal.original_entry_id == opening.id and ledger_reversal.direction == 'DECREASE'
    assert payment_reversal.reversed_payment_id == payment.id
    assert tracked_balance(db, account_id=2) == Decimal('0.00')
    assert db.get(FundingLedgerEntry, opening.id) is opening
    assert db.get(FundingPayment, payment.id) is payment
    assert payment.id in account_summary(db, account_id=2)['reversed_payment_ids']


def test_void_report_preserves_calculation_and_removes_it_from_active_work(db):
    _map(db); _sale(db); report = _report(db); finalize_report(db, report_id=report.id, actor_id=6)
    original = report.calculated_cogs
    void_report(db, report_id=report.id, reason='Owner replaced the period report', actor_id=6)
    assert report.status == 'VOIDED' and report.void_reason == 'Owner replaced the period report'
    assert report.calculated_cogs == original
    assert db.scalar(select(FundingReportLine).where(FundingReportLine.report_id == report.id)) is not None
    assert account_summary(db, account_id=1)['open_report_amount'] == Decimal('0.00')


def test_report_creation_is_only_explicit_and_get_style_reads_do_not_mutate(db):
    _map(db); _sale(db); before = db.scalar(select(FundingReport.id))
    assert before is None
    assert db.scalars(select(FundingSkuMapping)).all()
    assert db.scalar(select(FundingReport.id)) is None


def test_each_account_type_has_an_independent_default_off_action_gate(db, monkeypatch):
    monkeypatch.setattr(settings, 'v2_consignment_cogs_actions_enabled', False)
    monkeypatch.setattr(settings, 'v2_credit_card_cogs_actions_enabled', False)
    with pytest.raises(HTTPException) as consignment_denied:
        _action_gate(db.get(FundingAccount, 1))
    with pytest.raises(HTTPException) as card_denied:
        _action_gate(db.get(FundingAccount, 2))
    assert consignment_denied.value.status_code == card_denied.value.status_code == 403
    monkeypatch.setattr(settings, 'v2_consignment_cogs_actions_enabled', True)
    _action_gate(db.get(FundingAccount, 1))
    with pytest.raises(HTTPException):
        _action_gate(db.get(FundingAccount, 2))


def _permanently_delete(db, report, *, account_id=None, expected_token=None):
    row = next(item for item in _report_history_rows(
        account_summary(db, account_id=report.account_id)) if item['report'].id == report.id)
    return delete_report(
        db,
        account_id=account_id or report.account_id,
        report_id=report.id,
        expected_token=expected_token or row['version_token'],
        reason='Owner-confirmed permanent deletion',
        actor_id=6,
    )


def test_permanent_report_deletion_is_atomic_and_preserves_shared_sources(db, monkeypatch):
    events = []
    monkeypatch.setattr('app.services.v2_funding_reports_service._audit',
        lambda _db, **values: events.append(values))
    _map(db); sale = _sale(db); report = _report(db)
    finalize_report(db, report_id=report.id, actor_id=6)
    payment = record_payment(db, account_id=1, entry_type='PAYMENT', amount=Decimal('5'),
        payment_date=date(2026, 7, 4), payment_source='Bank', confirmation_number='',
        reason='Partial payment', internal_note='', allocations={report.id: Decimal('5')}, actor_id=6)
    add_adjustment(db, report_id=report.id, adjustment_type='SHIPPING', direction='INCREASE',
        amount=Decimal('2'), effective_date=date(2026, 7, 3), reason='Freight', internal_note='',
        owner_confirmed=True, actor_id=6)
    report_id = report.id

    snapshot = _permanently_delete(db, report)

    assert db.get(FundingReport, report_id) is None
    assert db.scalar(select(FundingPaymentAllocation).where(
        FundingPaymentAllocation.report_id == report_id)) is None
    assert db.scalar(select(FundingReportAdjustment).where(
        FundingReportAdjustment.report_id == report_id)) is None
    assert db.scalar(select(FundingReportLine).where(
        FundingReportLine.report_id == report_id)) is None
    assert db.scalar(select(FundingReportFactLink).where(
        FundingReportFactLink.report_id == report_id)) is None
    assert db.get(FundingPayment, payment.id) is payment
    assert db.get(ConsignmentSaleFact, sale.id) is sale
    assert db.scalar(select(PurchaseOrder)) is not None
    assert snapshot['dependent_records_deleted']['payment_allocations'] == 1
    assert events[-1]['action'] == 'FUNDING_REPORT_DELETED'
    assert events[-1]['after'] == snapshot


def test_permanent_deletion_rejects_stale_cross_account_and_shared_ledger_links(db):
    _map(db); _sale(db); report = _report(db)
    with pytest.raises(ValueError, match='changed'):
        _permanently_delete(db, report, expected_token='stale')
    with pytest.raises(ValueError, match='does not belong'):
        _permanently_delete(db, report, account_id=3)

    owned = FundingLedgerEntry(account_id=1, entry_type='CORRECTION', direction='INCREASE',
        amount=Decimal('1'), effective_date=date(2026, 7, 4), report_id=report.id,
        reason='Report-owned entry', created_by_principal_id=6)
    db.add(owned); db.flush()
    db.add(FundingLedgerEntry(account_id=1, entry_type='REVERSAL', direction='DECREASE',
        amount=Decimal('1'), effective_date=date(2026, 7, 5), original_entry_id=owned.id,
        reason='Shared reversal', created_by_principal_id=6))
    db.flush()
    with pytest.raises(ValueError, match='shared accounting entry'):
        _permanently_delete(db, report)
    assert db.get(FundingReport, report.id) is report


def test_report_history_has_exact_compact_columns_and_accessible_delete_dialog():
    history = open('app/templates/v2/order_payments/funding_account_detail.html').read()
    table = history.split('v2-report-history__table', 1)[1].split('</table>', 1)[0]
    headers = ['Sales Period', 'COGS', 'Paid', 'Created', 'Delete']
    assert [table.index(f'>{header}</th>') for header in headers] == sorted(
        table.index(f'>{header}</th>') for header in headers)
    assert table.count('</th>') == 6  # Vendor is conditionally rendered for credit-card accounts.
    assert 'v2-button--danger v2-button--compact' in table
    assert 'Permanently delete this report?' in table
    assert 'This action cannot be undone.' in table
    assert 'csrf_token' in table and 'expected_token' in table
    assert 'No funding reports have been created.' in table

    css = open('app/static/v2/v2.css').read()
    script = open('app/static/v2/v2.js').read()
    assert 'overflow-x: scroll' in css and 'overflow-y: auto' in css
    assert 'scrollbar-gutter: stable' in css and 'overscroll-behavior: contain' in css
    assert 'font-variant-numeric: tabular-nums' in css
    assert '.v2-report-history__cogs { text-align: right' in css
    assert '.v2-report-history__paid, .v2-report-history__delete { text-align: center' in css
    assert "dialog.addEventListener('close'" in script and 'dialog._v2Opener?.focus()' in script
    assert "openDialog.close('cancel')" in script and 'confirmButton.click()' in script


def test_report_history_values_and_newest_first_ordering(db):
    _map(db); _sale(db)
    older = _report(db)
    newer = _report(db, acknowledged=True)
    older.created_at = datetime(2026, 7, 5, 18, tzinfo=timezone.utc)
    newer.created_at = datetime(2026, 7, 6, 18, tzinfo=timezone.utc)
    db.flush()
    rows = _report_history_rows(account_summary(db, account_id=1))
    assert [row['report'].id for row in rows] == [newer.id, older.id]
    assert rows[0]['effective_cogs'] == Decimal('12.00') and rows[0]['paid'] is False
    assert _report_history_date(newer.sales_start_date) == '07/01/2026'

    finalize_report(db, report_id=newer.id, actor_id=6)
    add_adjustment(db, report_id=newer.id, adjustment_type='VENDOR_CREDIT',
        direction='DECREASE', amount=Decimal('12'), effective_date=date(2026, 7, 3),
        reason='Full credit', internal_note='', owner_confirmed=True, actor_id=6)
    zero = _report_history_rows(account_summary(db, account_id=1))[0]
    assert zero['effective_cogs'] == Decimal('0.00') and zero['paid'] is False

    _permanently_delete(db, newer)
    assert [row.id for row in account_summary(db, account_id=1)['reports']] == [older.id]
    db.expire_all()
    assert [row.id for row in account_summary(db, account_id=1)['reports']] == [older.id]


def test_report_history_paid_uses_actual_fully_settled_state(db):
    _map(db); _sale(db); report = _report(db)
    finalize_report(db, report_id=report.id, actor_id=6)
    record_payment(db, account_id=1, entry_type='PAYMENT', amount=Decimal('12'),
        payment_date=date(2026, 7, 4), payment_source='Bank', confirmation_number='paid',
        reason='Paid in full', internal_note='', allocations={report.id: Decimal('12')}, actor_id=6)
    assert _report_history_rows(account_summary(db, account_id=1))[0]['paid'] is True


def _assign_card_po(db, *, vendor_id, order_id=None, payment_method_id=20,
                    create_line=False, variation_id='VAR-EXACT', sku='AB12',
                    cost='4', quantity=10, order_day=date(2026, 6, 1)):
    order_id = order_id or int(
        db.scalar(select(PurchaseOrder.id).order_by(PurchaseOrder.id.desc())) or 0
    ) + 1
    ordered_at = datetime(
        order_day.year, order_day.month, order_day.day, 18, tzinfo=timezone.utc
    )
    order = PurchaseOrder(
        id=order_id,
        vendor_id=vendor_id,
        status=PurchaseOrderStatus.SENT_TO_STORES,
        created_by_principal_id=6,
        ordered_at=ordered_at,
        submitted_at=ordered_at,
    )
    db.add(order)
    db.flush()
    payment = OrderPayment(
        purchase_order_id=order.id,
        vendor_id=vendor_id,
        payment_method_id=payment_method_id,
        payment_category_snapshot='CREDIT_CARD',
        payment_method_label_snapshot=f'Card {payment_method_id}',
        status='UNPAID',
        financial_treatment='INVOICE',
        order_amount=Decimal('0'),
        order_cost_complete=True,
    )
    db.add(payment)
    db.flush()
    if create_line:
        db.add(PurchaseOrderLine(
            purchase_order_id=order.id,
            variation_id=variation_id,
            sku=sku,
            item_name='Exact Product',
            variation_name='Blue',
            unit_cost=Decimal(str(cost)),
            ordered_qty=quantity,
            received_qty_total=quantity,
            suggested_qty=quantity,
        ))
        db.flush()
    return order, payment


def _add_card_vendor(db, *, vendor_id=12, name='Zulu Vendor', active=True, assign_order=True):
    vendor = Vendor(id=vendor_id, square_vendor_id=f'V-{vendor_id}', name=name, active=active)
    db.add(vendor); db.flush()
    db.add(VendorPaymentSetting(vendor_id=vendor.id, default_payment_method_id=20,
        updated_by_principal_id=6)); db.flush()
    if assign_order:
        _assign_card_po(db, vendor_id=vendor.id)
    return vendor


def test_credit_card_vendor_membership_is_po_assigned_deduplicated_and_zero_obligation_safe(db):
    _add_card_vendor(db, vendor_id=12, name='Zulu Vendor')
    _assign_card_po(db, vendor_id=12)
    _add_card_vendor(db, vendor_id=13, name='Dormant Vendor', active=False, assign_order=False)

    memberships = funding_account_vendor_memberships(
        db, account=db.get(FundingAccount, 2)
    )
    assert [
        (row.vendor.id, row.vendor.name, row.assigned_po_count)
        for row in memberships
    ] == [(10, 'Alpha Vendor', 1), (12, 'Zulu Vendor', 2)]
    options = eligible_vendors_for_account(db, account=db.get(FundingAccount, 2))
    assert [(row.id, row.name) for row in options] == [(10, 'Alpha Vendor'), (12, 'Zulu Vendor')]

    # Visibility is independent of vendor defaults, FundingSkuMapping, sales
    # snapshots, and a non-zero calculated obligation.
    for setting in db.scalars(select(VendorPaymentSetting)).all():
        db.delete(setting)
    db.get(PaymentMethod, 20).is_active = False
    db.flush()
    assert db.scalar(select(FundingSkuMapping.id)) is None
    assert db.scalar(select(ConsignmentSaleFact.id)) is None
    assert [(row.id, row.name) for row in eligible_vendors_for_account(
        db, account=db.get(FundingAccount, 2)
    )] == [(10, 'Alpha Vendor'), (12, 'Zulu Vendor')]


def test_credit_card_vendor_membership_moves_immediately_with_po_reassignment(db):
    zulu = _add_card_vendor(db, vendor_id=12, name='Zulu Vendor')
    db.add_all([
        PaymentMethod(id=21, display_name='Other Card', category='CREDIT_CARD',
            is_active=True, created_by_principal_id=6, updated_by_principal_id=6),
        FundingAccount(id=4, account_type='CREDIT_CARD', payment_method_id=21,
            display_name='Other Card', is_active=True, created_by_principal_id=6,
            updated_by_principal_id=6),
    ])
    db.flush()
    zulu_payment = db.scalar(select(OrderPayment).join(
        PurchaseOrder, PurchaseOrder.id == OrderPayment.purchase_order_id
    ).where(PurchaseOrder.vendor_id == zulu.id))
    zulu_payment.payment_method_id = 21
    db.flush()

    assert [row.vendor.id for row in funding_account_vendor_memberships(
        db, account=db.get(FundingAccount, 2)
    )] == [10]
    assert [row.vendor.id for row in funding_account_vendor_memberships(
        db, account=db.get(FundingAccount, 4)
    )] == [12]

    beta = _add_card_vendor(db, vendor_id=13, name='Beta Card Vendor', assign_order=False)
    _assign_card_po(db, vendor_id=beta.id)
    db.flush()
    assert [row.vendor.id for row in funding_account_vendor_memberships(
        db, account=db.get(FundingAccount, 2)
    )] == [10, 13]


def test_credit_card_report_requires_valid_vendor_and_ignores_vendor_snapshots(db):
    _add_card_vendor(db); _map(db, account_id=2)
    own = _sale(db, fact_id=1, vendor_id=10); other = _sale(db, fact_id=2, vendor_id=12)
    with pytest.raises(ValueError, match='Select a vendor'):
        calculate_report(db, account_id=2, start_date=date(2026, 7, 1), end_date=date(2026, 7, 2),
            store_ids=[], sku_filter='', internal_note='', overlap_acknowledged=False, actor_id=6)
    with pytest.raises(ValueError, match='no purchase order assigned'):
        calculate_report(db, account_id=2, vendor_id=999, start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 2), store_ids=[], sku_filter='', internal_note='',
            overlap_acknowledged=False, actor_id=6)
    alpha = _report(db, account_id=2)
    linked = {row.sale_fact_id for row in db.scalars(select(FundingReportFactLink).where(
        FundingReportFactLink.report_id == alpha.id)).all()}
    assert alpha.vendor_id == 10 and linked == {own.id, other.id}
    assert overlapping_reports(db, account_id=2, vendor_id=12,
        start_date=date(2026, 7, 1), end_date=date(2026, 7, 2)) == []
    assert overlapping_reports(db, account_id=2, vendor_id=10,
        start_date=date(2026, 7, 1), end_date=date(2026, 7, 2)) == [alpha]


def test_credit_card_po_variation_sales_need_no_mapping_or_vendor_snapshot(db):
    assert db.scalar(select(FundingSkuMapping.id)) is None
    sale = _sale(db, vendor_id=None, sku=None)

    report = _report(db, account_id=2)

    line = db.scalar(select(FundingReportLine).where(
        FundingReportLine.report_id == report.id
    ))
    link = db.scalar(select(FundingReportFactLink).where(
        FundingReportFactLink.report_id == report.id
    ))
    assert sale.vendor_id_snapshot is None and sale.sku_snapshot is None
    assert report.units_sold == 3 and report.calculated_cogs == Decimal('12.00')
    assert line.purchase_order_line_id is not None
    assert link.sale_fact_id == sale.id and link.allocated_quantity == 3


def test_credit_card_fifo_uses_store_receipts_when_line_received_total_is_stale(db):
    line = db.scalar(select(PurchaseOrderLine).where(
        PurchaseOrderLine.purchase_order_id == 200
    ))
    line.received_qty_total = 0
    db.add_all([
        PurchaseOrderStoreAllocation(
            purchase_order_line_id=line.id, store_id=1,
            expected_qty=5, allocated_qty=5, store_received_qty=5, variance_qty=0,
            updated_at=datetime(2026, 6, 2, 18, tzinfo=timezone.utc),
        ),
        PurchaseOrderStoreAllocation(
            purchase_order_line_id=line.id, store_id=2,
            expected_qty=5, allocated_qty=5, store_received_qty=5, variance_qty=0,
            updated_at=datetime(2026, 6, 2, 18, tzinfo=timezone.utc),
        ),
    ])
    db.flush()
    sale = _sale(db, vendor_id=None, sku=None)

    inventory = credit_card_inventory_summary(db, account=db.get(FundingAccount, 2))
    scope = _credit_card_fifo_scope(
        db, account=db.get(FundingAccount, 2), vendor=db.get(Vendor, 10)
    )
    report = _report(db, account_id=2)

    assert inventory['original_units'] == 10
    assert [(lot.order.id, lot.line.id, lot.quantity) for lot in scope['lots']] == [
        (200, line.id, Decimal('10'))
    ]
    assert report.units_sold == 3 and report.calculated_cogs == Decimal('12.00')
    assert {row.sale_fact_id for row in db.scalars(select(FundingReportFactLink).where(
        FundingReportFactLink.report_id == report.id
    )).all()} == {sale.id}


def test_credit_card_fifo_keeps_legacy_date_when_store_receipts_match_line_total(db):
    line = db.scalar(select(PurchaseOrderLine).where(
        PurchaseOrderLine.purchase_order_id == 200
    ))
    db.add(PurchaseOrderStoreAllocation(
        purchase_order_line_id=line.id, store_id=1,
        expected_qty=10, allocated_qty=10, store_received_qty=10, variance_qty=0,
        updated_at=datetime(2026, 7, 2, 18, tzinfo=timezone.utc),
    ))
    db.flush()
    _sale(db, day=date(2026, 7, 1), vendor_id=None, sku=None)

    scope = _credit_card_fifo_scope(
        db, account=db.get(FundingAccount, 2), vendor=db.get(Vendor, 10)
    )
    report = _report(db, account_id=2)

    assert scope['lots'][0].received_at == datetime(2026, 6, 1, 18, tzinfo=timezone.utc)
    assert report.units_sold == 3 and report.calculated_cogs == Decimal('12.00')


def test_credit_card_fifo_consumes_older_unassigned_inventory_before_funded_lot(db):
    _assign_card_po(
        db,
        vendor_id=10,
        order_id=201,
        payment_method_id=None,
        create_line=True,
        cost='2',
        quantity=5,
        order_day=date(2026, 5, 1),
    )
    sale = _sale(db, quantity='6')

    report = _report(db, account_id=2)

    links = db.scalars(select(FundingReportFactLink).where(
        FundingReportFactLink.report_id == report.id
    )).all()
    assert report.units_sold == 1
    assert report.calculated_cogs == Decimal('4.00')
    assert [(row.sale_fact_id, row.allocated_quantity) for row in links] == [(sale.id, 1)]
    assert report.warning_summary['purchase_order_scope']['allocation_method'] == 'FIFO'


def test_credit_card_fifo_splits_one_sale_across_multiple_funded_po_lots(db):
    fixture_line = db.scalar(select(PurchaseOrderLine).where(
        PurchaseOrderLine.purchase_order_id == 200
    ))
    fixture_line.ordered_qty = 5
    fixture_line.received_qty_total = 5
    _assign_card_po(
        db,
        vendor_id=10,
        order_id=201,
        create_line=True,
        cost='5',
        quantity=5,
        order_day=date(2026, 6, 15),
    )
    sale = _sale(db, quantity='7')

    report = _report(db, account_id=2)

    lines = db.scalars(select(FundingReportLine).where(
        FundingReportLine.report_id == report.id
    ).order_by(FundingReportLine.unit_cost_snapshot)).all()
    links = db.scalars(select(FundingReportFactLink).where(
        FundingReportFactLink.report_id == report.id
    ).order_by(FundingReportFactLink.id)).all()
    assert [(row.units_sold, row.unit_cost_snapshot) for row in lines] == [
        (5, Decimal('4.0000')), (2, Decimal('5.0000')),
    ]
    assert [row.sale_fact_id for row in links] == [sale.id, sale.id]
    assert [row.allocated_quantity for row in links] == [5, 2]
    assert report.units_sold == 7 and report.calculated_cogs == Decimal('30.00')


def test_credit_card_fifo_gap_creates_human_readable_pending_exception(db):
    sale = _sale(
        db, quantity='12', product='Stale Square Snapshot', sku='stale-sku'
    )

    report = _report(db, account_id=2)
    exceptions = funding_report_fifo_exceptions(db, report_id=report.id)

    assert report.status == 'DRAFT'
    assert report.units_sold == 10 and report.calculated_cogs == Decimal('40.00')
    assert len(exceptions) == 1
    exception = exceptions[0]
    assert exception.sale_fact_id == sale.id
    assert exception.product_name_snapshot == 'Exact Product'
    assert exception.variation_name_snapshot == 'Blue'
    assert exception.sku_snapshot == 'ab 12'
    assert exception.quantity_affected == Decimal('2.000')
    assert exception.sale_transacted_at.replace(tzinfo=timezone.utc) == datetime(
        2026, 7, 1, 20, tzinfo=timezone.utc
    )
    assert exception.sold_through_quantity == Decimal('12.000')
    assert exception.received_through_quantity == Decimal('10.000')
    assert exception.status == 'PENDING'
    with pytest.raises(ValueError, match='pending FIFO report exception'):
        finalize_report(db, report_id=report.id, actor_id=6)


def test_credit_card_fifo_gap_handles_missing_sku_and_multiple_sales(db):
    catalog = db.get(OrderingCatalogIdentity, 'VAR-EXACT')
    catalog.sku = None
    line = db.scalar(select(PurchaseOrderLine).where(
        PurchaseOrderLine.purchase_order_id == 200
    ))
    line.sku = None
    _sale(db, fact_id=1, quantity='11', sku=None)
    _sale(db, fact_id=2, quantity='2', sku=None, day=date(2026, 7, 2))

    report = _report(db, account_id=2)
    exceptions = funding_report_fifo_exceptions(db, report_id=report.id)

    assert len(exceptions) == 2
    assert [row.quantity_affected for row in exceptions] == [Decimal('1.000'), Decimal('2.000')]
    assert [row.sold_through_quantity for row in exceptions] == [Decimal('11.000'), Decimal('13.000')]
    assert all(row.product_name_snapshot == 'Exact Product' for row in exceptions)
    assert all(row.variation_name_snapshot == 'Blue' for row in exceptions)
    assert all(row.sku_snapshot is None for row in exceptions)


def test_fifo_ignore_excludes_gap_and_is_audited(db, monkeypatch):
    audits = []
    monkeypatch.setattr(
        'app.services.v2_funding_reports_service._audit',
        lambda *args, **kwargs: audits.append(kwargs),
    )
    sale = _sale(db, quantity='12')
    report = _report(db, account_id=2)
    exception = funding_report_fifo_exceptions(db, report_id=report.id)[0]

    resolve_funding_report_fifo_exception(
        db, report_id=report.id, exception_id=exception.id,
        action='IGNORE', reason='Opening inventory is still being researched.', actor_id=6,
    )

    assert exception.status == 'IGNORED'
    assert exception.resolved_by_principal_id == 6 and exception.resolved_at is not None
    assert exception.resolution_reason == 'Opening inventory is still being researched.'
    assert report.units_sold == 10 and report.calculated_cogs == Decimal('40.00')
    exclusion = db.scalar(select(FundingReportExclusion).where(
        FundingReportExclusion.report_id == report.id,
        FundingReportExclusion.source_id == sale.id,
        FundingReportExclusion.reason_code == 'FIFO_EXCEPTION_OWNER_IGNORED',
    ))
    assert exclusion.quantity_snapshot == Decimal('2.000')
    assert audits[-1]['action'] == 'FUNDING_FIFO_EXCEPTION_IGNORED'
    finalize_report(db, report_id=report.id, actor_id=6)
    assert report.finalized_snapshot['fifo_exceptions'][0]['status'] == 'IGNORED'


def test_fifo_include_uses_manual_cost_without_consuming_future_or_unreceived_po(db, monkeypatch):
    audits = []
    monkeypatch.setattr(
        'app.services.v2_funding_reports_service._audit',
        lambda *args, **kwargs: audits.append(kwargs),
    )
    future_order, _future_payment = _assign_card_po(
        db, vendor_id=10, order_id=201, create_line=True, quantity=20,
        order_day=date(2026, 7, 2),
    )
    future_line = db.scalar(select(PurchaseOrderLine).where(
        PurchaseOrderLine.purchase_order_id == future_order.id
    ))
    future_line.received_qty_total = 0
    future_order.status = PurchaseOrderStatus.IN_TRANSIT
    sale = _sale(db, quantity='12', day=date(2026, 7, 1))
    report = _report(db, account_id=2)
    exception = funding_report_fifo_exceptions(db, report_id=report.id)[0]
    prior_future_received = future_line.received_qty_total

    resolve_funding_report_fifo_exception(
        db, report_id=report.id, exception_id=exception.id,
        action='INCLUDE', unit_cost=Decimal('6.25'),
        reason='Owner supplied documented historical cost.', actor_id=6,
    )

    override = db.scalar(select(FundingReportLine).where(
        FundingReportLine.report_id == report.id,
        FundingReportLine.warning_state == f'FIFO_OVERRIDE:{exception.id}',
    ))
    link = db.scalar(select(FundingReportFactLink).where(
        FundingReportFactLink.report_line_id == override.id
    ))
    assert exception.status == 'INCLUDED'
    assert exception.cost_basis == 'OWNER_ENTERED_UNIT_COST'
    assert exception.unit_cost_snapshot == Decimal('6.2500')
    assert exception.resolved_by_principal_id == 6 and exception.resolved_at is not None
    assert exception.resolution_reason == 'Owner supplied documented historical cost.'
    assert override.purchase_order_line_id is None
    assert override.purchase_order_receipt_line_id is None
    assert override.units_sold == Decimal('2.000')
    assert link.sale_fact_id == sale.id and link.allocated_quantity == Decimal('2.000')
    assert report.units_sold == 12 and report.calculated_cogs == Decimal('52.50')
    assert future_line.received_qty_total == prior_future_received == 0
    assert audits[-1]['action'] == 'FUNDING_FIFO_EXCEPTION_INCLUDED'


def test_discard_fifo_exception_draft_preserves_source_inventory_and_sales(db):
    sale = _sale(db, quantity='12')
    line = db.scalar(select(PurchaseOrderLine).where(
        PurchaseOrderLine.purchase_order_id == 200
    ))
    received_before = line.received_qty_total
    report = _report(db, account_id=2)
    assert db.scalar(select(FundingReportFifoException.id).where(
        FundingReportFifoException.report_id == report.id
    )) is not None

    delete_draft_report(
        db, report_id=report.id, actor_id=6, reason='Owner discarded exception draft.'
    )

    assert db.get(FundingReport, report.id) is None
    assert db.get(ConsignmentSaleFact, sale.id) is sale
    assert db.get(PurchaseOrderLine, line.id).received_qty_total == received_before


def test_fifo_exception_owner_ui_contract():
    template = open('app/templates/v2/order_payments/funding_report_detail.html').read()
    assert 'Inventory history needs an owner decision' in template
    assert 'Quantity affected' in template and 'Sale date' in template
    assert "exception.sku_snapshot or 'No SKU'" in template
    assert 'This item was sold' in template
    assert '>Ignore for This Report<' in template and '>Include Anyway<' in template
    assert (
        'This excludes the unmatched quantity from this report only. It does not repair the '
        'inventory history, and this sale may appear as an exception again in a future report.'
        in template
    )
    assert '>Discard Report<' in template
    assert '<summary>Technical details</summary>' in template
    assert 'Square variation ID:' in template


def test_fifo_exception_actions_are_owner_feature_and_csrf_protected():
    from app.routers.v2_funding_reports import router
    from app.routers.v2_order_payments import feature_access, owner_access
    from app.security.csrf import verify_csrf

    routes = [
        route for route in router.routes
        if 'fifo-exceptions' in route.path or route.path.endswith('/discard')
    ]
    assert len(routes) == 2
    for route in routes:
        assert route.methods == {'POST'}
        dependencies = {row.call for row in route.dependant.dependencies}
        assert {feature_access, owner_access, verify_csrf} <= dependencies


def test_credit_card_inventory_aggregates_three_assigned_pos_without_collapsing_lots(db):
    _assign_card_po(
        db, vendor_id=10, order_id=201, create_line=True,
        variation_id='VAR-EXACT', sku='AB12', cost='5', quantity=5,
        order_day=date(2026, 6, 15),
    )
    _assign_card_po(
        db, vendor_id=10, order_id=202, create_line=True,
        variation_id='VAR-SECOND', sku='SECOND', cost='7', quantity=4,
        order_day=date(2026, 6, 20),
    )
    db.flush()

    account = db.get(FundingAccount, 2)
    vendor = db.get(Vendor, 10)
    membership = next(
        row for row in funding_account_vendor_memberships(db, account=account)
        if row.vendor.id == vendor.id
    )
    inventory = credit_card_inventory_summary(db, account=account)
    vendor_rows = [row for row in inventory['lines'] if row['vendor'].id == vendor.id]
    scope = _credit_card_fifo_scope(db, account=account, vendor=vendor)

    assert membership.assigned_po_count == 3
    assert {row['purchase_order_id'] for row in vendor_rows} == {200, 201, 202}
    assert sum(row['received_units'] for row in vendor_rows) == 19
    assert sum(row['original_value'] for row in vendor_rows) == Decimal('93.00')
    assert sorted(scope['assigned_orders']) == [200, 201, 202]
    exact_lots = [
        lot for lot in scope['lots']
        if lot.account_id == account.id and lot.line.variation_id == 'VAR-EXACT'
    ]
    assert [(lot.order.id, lot.quantity, lot.line.unit_cost) for lot in exact_lots] == [
        (200, Decimal('10'), Decimal('4.0000')),
        (201, Decimal('5'), Decimal('5.0000')),
    ]
    assert len({lot.line.id for lot in exact_lots}) == 2

    exact_sale = _sale(db, fact_id=101, vendor_id=None, sku=None, quantity='12')
    second_sale = _sale(
        db, fact_id=102, vendor_id=None, sku=None, quantity='3',
        variation_id='VAR-SECOND', product='Second Product',
    )
    report = calculate_report(
        db, account_id=2, vendor_id=10,
        start_date=date(2026, 7, 1), end_date=date(2026, 7, 2),
        store_ids=[], sku_filter='', internal_note='',
        overlap_acknowledged=False, actor_id=6,
    )

    assert report.units_sold == 15
    assert report.calculated_cogs == Decimal('71.00')
    assert report.warning_summary['purchase_order_scope']['purchase_order_ids'] == [200, 201, 202]
    assert {row.sale_fact_id for row in db.scalars(select(FundingReportFactLink).where(
        FundingReportFactLink.report_id == report.id
    )).all()} == {exact_sale.id, second_sale.id}
    assert db.scalar(select(FundingSkuMapping.id)) is None
    assert exact_sale.vendor_id_snapshot is None and second_sale.vendor_id_snapshot is None


def test_credit_card_multi_vendor_po_sets_move_one_assignment_at_a_time(db):
    _assign_card_po(db, vendor_id=10, order_id=201, create_line=True, quantity=5)
    zulu = _add_card_vendor(db, vendor_id=12, name='Zulu Vendor', assign_order=False)
    _assign_card_po(
        db, vendor_id=zulu.id, order_id=301, create_line=True,
        variation_id='VAR-ZULU-1', sku='ZULU1', quantity=6,
    )
    _assign_card_po(
        db, vendor_id=zulu.id, order_id=302, create_line=True,
        variation_id='VAR-ZULU-2', sku='ZULU2', quantity=7,
    )
    db.add_all([
        PaymentMethod(
            id=21, display_name='Other Card', category='CREDIT_CARD', is_active=True,
            created_by_principal_id=6, updated_by_principal_id=6,
        ),
        FundingAccount(
            id=4, account_type='CREDIT_CARD', payment_method_id=21,
            display_name='Other Card', is_active=True,
            created_by_principal_id=6, updated_by_principal_id=6,
        ),
    ])
    db.flush()

    account = db.get(FundingAccount, 2)
    assert {
        row.vendor.id: row.assigned_po_count
        for row in funding_account_vendor_memberships(db, account=account)
    } == {10: 2, 12: 2}

    moved_alpha = db.scalar(select(OrderPayment).where(OrderPayment.purchase_order_id == 201))
    moved_alpha.payment_method_id = 21
    db.flush()
    assert {
        row.vendor.id: row.assigned_po_count
        for row in funding_account_vendor_memberships(db, account=account)
    } == {10: 1, 12: 2}
    assert {row['purchase_order_id'] for row in credit_card_inventory_summary(
        db, account=account
    )['lines']} == {200, 301, 302}

    moved_zulu = db.scalar(select(OrderPayment).where(OrderPayment.purchase_order_id == 301))
    moved_zulu.payment_method_id = 21
    db.flush()
    assert {
        row.vendor.id: row.assigned_po_count
        for row in funding_account_vendor_memberships(db, account=account)
    } == {10: 1, 12: 1}
    assert {row['purchase_order_id'] for row in credit_card_inventory_summary(
        db, account=account
    )['lines']} == {200, 302}
    assert db.scalar(select(FundingSkuMapping.id)) is None


def test_credit_card_fifo_keeps_vendors_isolated_by_assigned_po_variation(db):
    _add_card_vendor(db, assign_order=False)
    _assign_card_po(
        db, vendor_id=12, create_line=True, variation_id='VAR-ZULU',
        sku='ZULU', cost='9', quantity=10,
    )
    alpha_sale = _sale(db, fact_id=1, vendor_id=None)
    zulu_sale = _sale(
        db, fact_id=2, vendor_id=None, variation_id='VAR-ZULU',
        sku=None, quantity='2',
    )

    alpha = _report(db, account_id=2)
    zulu = calculate_report(
        db, account_id=2, vendor_id=12,
        start_date=date(2026, 7, 1), end_date=date(2026, 7, 2),
        store_ids=[], sku_filter='', internal_note='',
        overlap_acknowledged=False, actor_id=6,
    )

    assert alpha.units_sold == 3 and alpha.calculated_cogs == Decimal('12.00')
    assert zulu.units_sold == 2 and zulu.calculated_cogs == Decimal('18.00')
    assert {row.sale_fact_id for row in db.scalars(select(FundingReportFactLink).where(
        FundingReportFactLink.report_id == alpha.id
    )).all()} == {alpha_sale.id}
    assert {row.sale_fact_id for row in db.scalars(select(FundingReportFactLink).where(
        FundingReportFactLink.report_id == zulu.id
    )).all()} == {zulu_sale.id}


def test_credit_card_fifo_return_recredits_original_lot(db):
    sale = _sale(db, quantity='3')
    returned = _return(db, sale, quantity='2')

    report = _report(db, account_id=2)

    links = db.scalars(select(FundingReportFactLink).where(
        FundingReportFactLink.report_id == report.id
    ).order_by(FundingReportFactLink.id)).all()
    assert report.units_sold == 3 and report.units_returned == 2
    assert report.net_units == 1 and report.calculated_cogs == Decimal('4.00')
    assert [(row.sale_fact_id, row.return_fact_id, row.allocated_quantity,
             row.cogs_amount_snapshot) for row in links] == [
        (sale.id, None, Decimal('3.000'), Decimal('12.00')),
        (None, returned.id, Decimal('2.000'), Decimal('-8.00')),
    ]


def test_credit_card_fifo_requires_square_coverage_back_to_earliest_lot(db):
    state = db.get(ConsignmentSalesSyncState, 1)
    state.last_successful_start_at = datetime(2026, 6, 15, 7, tzinfo=timezone.utc)
    _sale(db)

    with pytest.raises(ValueError, match='Square sales data is not complete'):
        _report(db, account_id=2)

    assert db.scalar(select(FundingReport.id)) is None


def test_credit_card_route_refreshes_square_back_to_fifo_history_start(db, monkeypatch):
    _sale(db)
    state = db.get(ConsignmentSalesSyncState, 1)
    state.last_successful_start_at = datetime(2026, 6, 15, 7, tzinfo=timezone.utc)
    calls = []

    def refresh(**values):
        calls.append(values)
        state.last_successful_start_at = values['start_at']
        state.last_successful_through_at = values['end_at']
        state.last_successful_at = values['end_at']
        state.last_result = 'COMPLETE'
        db.commit()
        return type('Result', (), {'state': 'current', 'message': 'updated'})()

    monkeypatch.setattr(
        'app.routers.v2_funding_reports.refresh_square_sales_data', refresh
    )
    monkeypatch.setattr(settings, 'v2_credit_card_cogs_actions_enabled', True)

    class Form(dict):
        def getlist(self, _key):
            return []

    class Request:
        headers = {}
        client = None

        async def form(self):
            return Form(
                account_id='2', vendor_id='10', start_date='2026-07-01',
                end_date='2026-07-02', sku_filter='', internal_note='',
            )

    owner = type('Owner', (), {'id': 6})()
    response = asyncio.run(
        calculate_funding_report_action(Request(), owner, owner, db, None)
    )

    report = db.scalar(select(FundingReport).order_by(FundingReport.id.desc()))
    assert response.status_code == 303
    assert calls[0]['start_at'] == datetime(2026, 6, 1, 7, tzinfo=timezone.utc)
    assert report.units_sold == 3 and report.calculated_cogs == Decimal('12.00')


def test_credit_card_fifo_fails_closed_for_missing_po_variation_identity(db):
    line = db.scalar(select(PurchaseOrderLine).where(
        PurchaseOrderLine.purchase_order_id == 200
    ))
    line.variation_id = ''
    line.sku = 'NO-CATALOG-MATCH'
    _sale(db)

    with pytest.raises(ValueError, match='missing Square identity or cost'):
        _report(db, account_id=2)

    assert db.scalar(select(FundingReport.id)) is None


def test_credit_card_inventory_is_po_derived_without_funding_mapping(db):
    assert db.scalar(select(FundingSkuMapping.id)) is None
    _sale(db, quantity='3')

    inventory = credit_card_inventory_summary(
        db, account=db.get(FundingAccount, 2)
    )
    summary = account_summary(db, account_id=2)

    assert inventory['assigned_po_count'] == 1
    assert inventory['original_units'] == 10
    assert inventory['original_value'] == Decimal('40.00')
    assert inventory['remaining_units'] == 7
    assert inventory['remaining_value'] == Decimal('28.00')
    assert inventory['sold_units'] == 3
    assert inventory['sold_cogs'] == Decimal('12.00')
    assert summary['inventory_units'] == 7
    assert summary['inventory_value'] == Decimal('28.00')


def test_credit_card_inventory_assignment_moves_without_sku_remapping(db):
    db.add_all([
        PaymentMethod(
            id=21, display_name='Card C', category='CREDIT_CARD', is_active=True,
            created_by_principal_id=6, updated_by_principal_id=6,
        ),
        FundingAccount(
            id=4, account_type='CREDIT_CARD', payment_method_id=21,
            display_name='Card C', is_active=True, created_by_principal_id=6,
            updated_by_principal_id=6,
        ),
    ])
    payment = db.scalar(select(OrderPayment).where(
        OrderPayment.purchase_order_id == 200
    ))
    payment.payment_method_id = 21
    db.flush()

    prior = credit_card_inventory_summary(db, account=db.get(FundingAccount, 2))
    current = credit_card_inventory_summary(db, account=db.get(FundingAccount, 4))

    assert prior['assigned_po_count'] == 0 and prior['original_units'] == 0
    assert current['assigned_po_count'] == 1 and current['original_units'] == 10
    assert db.scalar(select(FundingSkuMapping.id)) is None


def test_unique_missing_po_identity_is_persisted_and_auditable_by_service(db):
    line = db.scalar(select(PurchaseOrderLine).where(
        PurchaseOrderLine.purchase_order_id == 200
    ))
    line.variation_id = ''

    resolved = resolve_assigned_po_line_identities(
        db, account=db.get(FundingAccount, 2), vendor=db.get(Vendor, 10),
        actor_id=6,
    )

    assert resolved == [line]
    assert line.variation_id == 'VAR-EXACT'


def test_ambiguous_po_identity_is_visible_until_owner_resolves_it(db):
    line = db.scalar(select(PurchaseOrderLine).where(
        PurchaseOrderLine.purchase_order_id == 200
    ))
    line.variation_id = ''
    db.add(OrderingCatalogIdentity(
        square_variation_id='VAR-EXACT-SECOND', sku='AB12',
        item_name='Exact Product', variation_name='Second',
        product_name='Exact Product', square_is_deleted=False,
        last_seen_at=datetime.now(timezone.utc),
    ))
    db.flush()

    inventory = credit_card_inventory_summary(
        db, account=db.get(FundingAccount, 2)
    )

    assert resolve_assigned_po_line_identities(
        db, account=db.get(FundingAccount, 2), vendor=db.get(Vendor, 10),
        actor_id=6,
    ) == []
    assert inventory['remaining_units'] is None
    assert inventory['issues'][0]['issue'] == 'Product identity unresolved'
    assert {row.square_variation_id for row in inventory['issues'][0]['resolution_candidates']} == {
        'VAR-EXACT', 'VAR-EXACT-SECOND'
    }

    resolve_funding_po_line_identity(
        db, account_id=2, purchase_order_line_id=line.id,
        square_variation_id='VAR-EXACT-SECOND', reason='Owner verified recreated variation.',
        actor_id=6,
    )
    assert line.variation_id == 'VAR-EXACT-SECOND'


def test_credit_card_fifo_recalculation_uses_current_po_assignment_without_mutating_final(db):
    _sale(db)
    historical = _report(db, account_id=2)
    finalize_report(db, report_id=historical.id, actor_id=6)
    db.add_all([
        PaymentMethod(
            id=21, display_name='Card C', category='CREDIT_CARD', is_active=True,
            created_by_principal_id=6, updated_by_principal_id=6,
        ),
        FundingAccount(
            id=4, account_type='CREDIT_CARD', payment_method_id=21,
            display_name='Card C', is_active=True,
            created_by_principal_id=6, updated_by_principal_id=6,
        ),
    ])
    payment = db.scalar(select(OrderPayment).where(
        OrderPayment.purchase_order_id == 200
    ))
    payment.payment_method_id = 21
    db.flush()

    regenerated = calculate_report(
        db, account_id=4, vendor_id=10,
        start_date=date(2026, 7, 1), end_date=date(2026, 7, 2),
        store_ids=[], sku_filter='', internal_note='',
        overlap_acknowledged=False, actor_id=6,
    )

    assert historical.status == 'FINALIZED'
    assert historical.account_id == 2 and historical.calculated_cogs == Decimal('12.00')
    assert regenerated.account_id == 4 and regenerated.calculated_cogs == Decimal('12.00')


def test_credit_card_payments_reject_cross_vendor_and_ambiguous_legacy_reports(db):
    _add_card_vendor(db); _map(db, account_id=2); _sale(db, vendor_id=10)
    alpha = _report(db, account_id=2); finalize_report(db, report_id=alpha.id, actor_id=6)
    legacy = FundingReport(account_id=2, vendor_id=None, report_number='LEGACY-AMBIGUOUS',
        account_name_snapshot='Card B', account_type_snapshot='CREDIT_CARD',
        sales_start_date=date(2026, 6, 1), sales_end_date=date(2026, 6, 2),
        status='FINALIZED', created_by_principal_id=6)
    db.add(legacy); db.commit()
    with pytest.raises(ValueError, match='across vendors'):
        record_payment(db, account_id=2, vendor_id=12, entry_type='PAYMENT', amount=Decimal('1'),
            payment_date=date(2026, 7, 4), payment_source='Bank', confirmation_number='',
            reason='Wrong vendor', internal_note='', allocations={alpha.id: Decimal('1')}, actor_id=6)
    with pytest.raises(ValueError, match='Legacy reports'):
        record_payment(db, account_id=2, vendor_id=10, entry_type='PAYMENT', amount=Decimal('1'),
            payment_date=date(2026, 7, 4), payment_source='Bank', confirmation_number='',
            reason='Legacy allocation', internal_note='', allocations={legacy.id: Decimal('1')}, actor_id=6)
    payment = record_payment(db, account_id=2, entry_type='PAYMENT', amount=Decimal('1'),
        payment_date=date(2026, 7, 4), payment_source='Bank', confirmation_number='',
        reason='Inherited vendor', internal_note='', allocations={alpha.id: Decimal('1')}, actor_id=6)
    assert payment.vendor_id == alpha.vendor_id == 10


def test_credit_card_history_uses_current_vendor_name_and_legacy_label(db):
    report = FundingReport(account_id=2, vendor_id=10, report_number='CURRENT-NAME',
        account_name_snapshot='Card B', account_type_snapshot='CREDIT_CARD',
        sales_start_date=date(2026, 7, 1), sales_end_date=date(2026, 7, 2),
        status='DRAFT', created_by_principal_id=6)
    legacy = FundingReport(account_id=2, vendor_id=None, report_number='UNKNOWN-LEGACY',
        account_name_snapshot='Card B', account_type_snapshot='CREDIT_CARD',
        sales_start_date=date(2026, 6, 1), sales_end_date=date(2026, 6, 2),
        status='DRAFT', created_by_principal_id=6)
    db.add_all([report, legacy]); db.flush(); db.get(Vendor, 10).name = 'Renamed Vendor'
    rows = _report_history_rows(account_summary(db, account_id=2), {10: db.get(Vendor, 10)})
    assert {row['report'].report_number: row['vendor_name'] for row in rows} == {
        'CURRENT-NAME': 'Renamed Vendor', 'UNKNOWN-LEGACY': 'Unknown/Legacy'}


def test_report_vendor_has_no_mutation_route_after_creation():
    from app.main import app
    paths = {(getattr(route, 'path', ''), frozenset(getattr(route, 'methods', set())))
        for route in app.routes}
    assert not any('/reports/{report_id}/edit' in path or '/reports/{report_id}/vendor' in path
        for path, _methods in paths)


def test_vendor_scoped_delete_preserves_other_vendor_report_and_source_facts(db):
    _add_card_vendor(db); _map(db, account_id=2)
    _assign_card_po(
        db, vendor_id=12, create_line=True, variation_id='VAR-ZULU', sku='ZULU'
    )
    alpha_fact = _sale(db, fact_id=1, vendor_id=10)
    zulu_fact = _sale(
        db, fact_id=2, vendor_id=12, variation_id='VAR-ZULU', sku='ZULU'
    )
    alpha = _report(db, account_id=2)
    zulu = calculate_report(db, account_id=2, vendor_id=12,
        start_date=date(2026, 7, 1), end_date=date(2026, 7, 2), store_ids=[],
        sku_filter='', internal_note='', overlap_acknowledged=False, actor_id=6)
    alpha_id = alpha.id
    snapshot = _permanently_delete(db, alpha)
    assert snapshot['vendor_id'] == 10
    assert db.get(FundingReport, alpha_id) is None and db.get(FundingReport, zulu.id) is zulu
    assert db.get(ConsignmentSaleFact, alpha_fact.id) is alpha_fact
    assert db.get(ConsignmentSaleFact, zulu_fact.id) is zulu_fact


def test_credit_card_report_order_evidence_excludes_other_vendor_assignments(db):
    _add_card_vendor(db); _map(db, account_id=2); _sale(db, vendor_id=10)
    for order_id, vendor_id in ((101, 10), (102, 12)):
        ordered_at = datetime(2026, 6, 1, 18, tzinfo=timezone.utc)
        db.add(PurchaseOrder(id=order_id, vendor_id=vendor_id,
            status=PurchaseOrderStatus.IN_TRANSIT, created_by_principal_id=6,
            ordered_at=ordered_at, submitted_at=ordered_at))
        db.flush()
        db.add(OrderPayment(purchase_order_id=order_id, vendor_id=vendor_id,
            payment_method_id=20, payment_category_snapshot='CREDIT_CARD',
            payment_method_label_snapshot='Card B', status='PAID', financial_treatment='INVOICE',
            order_amount=Decimal('10'), order_cost_complete=True))
    db.flush()
    report = _report(db, account_id=2)
    assert report.warning_summary['vendor_purchase_order_ids'] == [101, 200]


def test_vendor_specific_ui_states_and_report_payment_inheritance_are_explicit():
    account_page = open('app/templates/v2/order_payments/funding_account_detail.html').read()
    accounts_page = open('app/templates/v2/order_payments/funding_accounts.html').read()
    legacy_mapping_page = open('app/templates/v2/order_payments/funding_mappings.html').read()
    report_page = open('app/templates/v2/order_payments/funding_report_detail.html').read()
    assert 'No purchase orders are assigned to this credit card account.' in account_page
    assert 'Assigned Vendors' in account_page and 'membership.assigned_po_count' in account_page
    assert '<option value="">Select a vendor</option>' in account_page
    assert "eligible_vendors|length == 1" in account_page
    assert "account.account_type == 'CREDIT_CARD'" in account_page
    assert 'Automatic PO association' in account_page
    assert 'No routine SKU mapping is required.' in account_page
    assert 'summary.derived_inventory.history_blockers' in account_page
    assert '/po-lines/{{ row.purchase_order_line_id }}/resolve' in account_page
    assert '/v2/funding-accounts/mappings' not in account_page
    assert '/v2/funding-accounts/mappings' not in accounts_page
    assert 'Legacy exception tool' in legacy_mapping_page
    assert '?payment_vendor_id={{ report.vendor_id }}#record-settlement' in report_page


def test_credit_card_combined_report_composes_three_vendor_reports_and_zero_activity(db):
    _add_card_vendor(db, vendor_id=12, name='Zulu Vendor')
    _assign_card_po(db, vendor_id=12, create_line=True,
                    variation_id='VAR-ZULU', sku='ZULU', cost='5')
    _add_card_vendor(db, vendor_id=13, name='Zero Vendor')
    _assign_card_po(db, vendor_id=13, create_line=True,
                    variation_id='VAR-ZERO', sku='ZERO', cost='7')
    _sale(db, fact_id=1, vendor_id=None, sku=None, quantity='3')
    _sale(db, fact_id=2, vendor_id=None, sku='ZULU', quantity='2',
          variation_id='VAR-ZULU')

    standalone = _report(db, account_id=2)
    combined = calculate_combined_report(
        db, account_id=2, start_date=date(2026, 7, 1), end_date=date(2026, 7, 2),
        store_ids=[], sku_filter='', internal_note='', actor_id=6,
        overlap_acknowledged=True,
    )
    members = combined_report_members(db, report=combined)

    assert is_combined_report(combined)
    assert [row.vendor_id for row in members] == [10, 13, 12]
    assert members[0].id == standalone.id
    assert [(row.units_sold, row.calculated_cogs) for row in members] == [
        (Decimal('3'), Decimal('12.00')),
        (Decimal('0'), Decimal('0.00')),
        (Decimal('2'), Decimal('10.00')),
    ]
    assert combined.units_sold == 5
    assert combined.calculated_cogs == Decimal('22.00')


def test_combined_report_finalizes_children_and_vendor_payment_reduces_combined_balance(db):
    _map(db, account_id=1)
    _sale(db, quantity='3')
    combined = calculate_combined_report(
        db, account_id=1, start_date=date(2026, 7, 1), end_date=date(2026, 7, 2),
        store_ids=[], sku_filter='', internal_note='', actor_id=6,
    )
    child = combined_report_members(db, report=combined)[0]

    finalize_report(db, report_id=combined.id, actor_id=6)
    record_payment(
        db, account_id=1, vendor_id=10, entry_type='REPLENISHMENT',
        amount=Decimal('5'), payment_date=date(2026, 7, 3), payment_source='Bank',
        confirmation_number='part', reason='Partial vendor payment', internal_note='',
        allocations={child.id: Decimal('5')}, actor_id=6,
    )
    position = report_position(db, report_id=combined.id)

    assert combined.status == 'FINALIZED' and child.status == 'PARTIALLY_SETTLED'
    assert position['adjusted_amount'] == Decimal('12.00')
    assert position['settled_amount'] == Decimal('5.00')
    assert position['remaining_amount'] == Decimal('7.00')
    assert account_summary(db, account_id=1)['open_report_amount'] == Decimal('7.00')


def test_overlapping_vendor_reports_cannot_finalize_same_square_fact_twice(db):
    _sale(db, quantity='3')
    first = _report(db, account_id=2)
    finalize_report(db, report_id=first.id, actor_id=6)
    second = _report(db, account_id=2, acknowledged=True)

    with pytest.raises(ValueError, match='cannot be finalized twice'):
        finalize_report(db, report_id=second.id, actor_id=6)


def test_combined_report_fails_closed_when_one_vendor_needs_earlier_square_coverage(db):
    _add_card_vendor(db, vendor_id=12, name='Earlier Vendor')
    _assign_card_po(
        db, vendor_id=12, create_line=True, variation_id='VAR-EARLY', sku='EARLY',
        order_day=date(2026, 5, 1),
    )
    state = db.get(ConsignmentSalesSyncState, 1)
    state.last_successful_start_at = datetime(2026, 6, 1, 7, tzinfo=timezone.utc)
    db.flush()

    with pytest.raises(ValueError, match='Square sales data is not complete'):
        calculate_combined_report(
            db, account_id=2, start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 2), store_ids=[], sku_filter='',
            internal_note='', actor_id=6,
        )


def test_combined_report_reuses_finalized_vendor_truth_and_preserves_lineage(db):
    sale = _sale(db, quantity='3')
    standalone = _report(db, account_id=2)
    finalize_report(db, report_id=standalone.id, actor_id=6)

    combined = calculate_combined_report(
        db, account_id=2, start_date=date(2026, 7, 1), end_date=date(2026, 7, 2),
        store_ids=[], sku_filter='', internal_note='', actor_id=6,
        overlap_acknowledged=True,
    )
    member = combined_report_members(db, report=combined)[0]
    link = db.scalar(select(FundingReportFactLink).where(
        FundingReportFactLink.report_id == member.id
    ))

    assert member.id == standalone.id and standalone.status == 'FINALIZED'
    assert link.sale_fact_id == sale.id
    assert db.get(FundingReportLine, link.report_line_id).purchase_order_line_id is not None


def test_combined_report_ui_exposes_account_action_vendor_details_and_payments():
    account_page = open('app/templates/v2/order_payments/funding_account_detail.html').read()
    combined_page = open(
        'app/templates/v2/order_payments/funding_combined_report_detail.html'
    ).read()
    assert 'Create Combined Report' in account_page
    assert 'Independent vendor results' in combined_page
    assert 'row.purchase_order_ids' in combined_page
    assert 'View Detail' in combined_page and 'Record Payment' in combined_page


def test_owner_cost_correction_updates_authoritative_po_line_and_invalidates_draft(
    db, monkeypatch,
):
    captured = []
    monkeypatch.setattr(
        'app.services.v2_funding_reports_service._audit',
        lambda *args, **kwargs: captured.append(kwargs),
    )
    _map(db, account_id=1, cost='10.50')
    _sale(db, quantity='3')
    draft = _report(db, account_id=1)
    source_line_id = int(
        (draft.warning_summary['purchase_order_scope']['source_lines'][0])[
            'purchase_order_line_id'
        ]
    )

    result = correct_funding_po_line_cost(
        db, account_id=1, purchase_order_line_id=source_line_id,
        unit_cost=Decimal('13.08'), reason='Vendor invoice cost was entered incorrectly',
        actor_id=6,
    )

    assert db.get(PurchaseOrderLine, source_line_id).unit_cost == Decimal('13.0800')
    assert db.get(FundingReport, draft.id) is None
    assert result['invalidated_draft_report_ids'] == [draft.id]
    correction = next(row for row in captured if row['action'] == 'FUNDING_PO_LINE_COST_CORRECTED')
    assert correction['after']['purchase_order_line_id'] == source_line_id
    assert correction['after']['old_unit_cost'] == '10.5000'
    assert correction['after']['new_unit_cost'] == '13.0800'
    assert correction['after']['reason'] == 'Vendor invoice cost was entered incorrectly'
    summary = account_summary(
        db, account_id=1, include_purchase_order_lines=True)
    assert [(row['line'].id, row['line'].sku, row['line'].unit_cost)
            for row in summary['purchase_order_lines']] == [
                (source_line_id, 'AB12', Decimal('13.0800'))]
    assert summary['inventory_value'] == Decimal('65.40')


def test_cost_correction_preserves_finalized_report_and_payment_and_exposes_adjustment(
    db, monkeypatch,
):
    captured = []
    monkeypatch.setattr(
        'app.services.v2_funding_reports_service._audit',
        lambda *args, **kwargs: captured.append(kwargs),
    )
    _map(db, account_id=1, cost='10.50')
    _sale(db, quantity='3')
    posted = _report(db, account_id=1)
    finalize_report(db, report_id=posted.id, actor_id=6)
    payment = record_payment(
        db, account_id=1, vendor_id=10, entry_type='REPLENISHMENT',
        amount=Decimal('10'), payment_date=date(2026, 7, 3), payment_source='Bank',
        confirmation_number='paid', reason='Partial settlement', internal_note='',
        allocations={posted.id: Decimal('10')}, actor_id=6,
    )
    source_line_id = int(
        posted.warning_summary['purchase_order_scope']['source_lines'][0][
            'purchase_order_line_id'
        ]
    )

    result = correct_funding_po_line_cost(
        db, account_id=1, purchase_order_line_id=source_line_id,
        unit_cost=Decimal('13.08'), reason='Invoice correction', actor_id=6,
    )

    db.refresh(posted)
    assert posted.calculated_cogs == Decimal('31.50')
    assert posted.finalized_snapshot['calculated_cogs'] == '31.50'
    assert db.get(FundingPayment, payment.id).amount == Decimal('10.00')
    assert result['invalidated_draft_report_ids'] == []
    assert result['finalized_report_impacts'] == [{
        'report_id': posted.id,
        'report_number': posted.report_number,
        'status': 'PARTIALLY_SETTLED',
        'affected_units': '3.000',
        'posted_cost_difference': '7.74',
        'settled_amount': '10.00',
        'payment_history_preserved': True,
    }]
    replacement = _report(db, account_id=1, acknowledged=True)
    assert replacement.calculated_cogs == Decimal('39.24')


def test_cost_correction_rejects_line_outside_funding_account(db):
    _map(db, account_id=1, cost='10.50')
    line_id = funding_account_purchase_lines(
        db, account=db.get(FundingAccount, 1))[-1]['line'].id

    with pytest.raises(ValueError, match='not assigned to this Funding Account'):
        correct_funding_po_line_cost(
            db, account_id=3, purchase_order_line_id=line_id,
            unit_cost=Decimal('13.08'), reason='Wrong account attempt', actor_id=6,
        )
    assert db.get(PurchaseOrderLine, line_id).unit_cost == Decimal('10.5000')


def test_cost_correction_ui_and_route_remain_owner_and_csrf_protected():
    from app.routers.v2_funding_reports import router
    from app.security.csrf import verify_csrf

    template = open('app/templates/v2/order_payments/funding_account_detail.html').read()
    assert 'Funded PO Cost Basis' in template
    assert '/po-lines/{{ line.id }}/cost' in template
    assert 'Cost correction history' in template
    route = next(row for row in router.routes if row.path.endswith('/po-lines/{line_id}/cost'))
    dependencies = {dependency.call for dependency in route.dependant.dependencies}
    assert route.methods == {'POST'}
    assert owner_access in dependencies
    assert verify_csrf in dependencies


def test_cost_correction_history_restores_json_money_strings_to_decimals(db):
    db.add(AuditLog(
        actor_principal_id=6,
        action='FUNDING_PO_LINE_COST_CORRECTED',
        meta={'entity_type': 'purchase_order_line', 'entity_id': 41, 'after': {
            'funding_account_id': 1,
            'purchase_order_id': 98,
            'purchase_order_line_id': 41,
            'old_unit_cost': '10.5000',
            'new_unit_cost': '13.0800',
            'finalized_report_impacts': [{
                'report_id': 9,
                'affected_units': '3.000',
                'posted_cost_difference': '7.74',
                'settled_amount': '2.00',
            }],
        }},
    ))
    db.flush()

    correction = funding_po_cost_correction_history(db, account_id=1)[0]

    assert correction['old_unit_cost'] == Decimal('10.5000')
    assert correction['new_unit_cost'] == Decimal('13.0800')
    assert correction['finalized_report_impacts'][0]['posted_cost_difference'] == Decimal('7.74')


def test_cost_correction_downstream_failure_rolls_back_cost_drafts_and_audit(
    db, monkeypatch,
):
    _map(db, account_id=1, cost='10.50')
    _sale(db, quantity='3')
    draft = _report(db, account_id=1)
    line_id = int(draft.warning_summary['purchase_order_scope']['source_lines'][0][
        'purchase_order_line_id'])

    def fail_final_audit(*_args, **kwargs):
        if kwargs['action'] == 'FUNDING_PO_LINE_COST_CORRECTED':
            raise RuntimeError('forced downstream audit failure')

    monkeypatch.setattr(
        'app.services.v2_funding_reports_service._audit', fail_final_audit)

    with pytest.raises(RuntimeError, match='forced downstream audit failure'):
        correct_funding_po_line_cost(
            db, account_id=1, purchase_order_line_id=line_id,
            unit_cost=Decimal('13.08'), reason='Rollback proof', actor_id=6,
        )

    assert db.get(PurchaseOrderLine, line_id).unit_cost == Decimal('10.5000')
    assert db.get(FundingReport, draft.id) is not None
    assert db.scalar(select(FundingReportLine.id).where(
        FundingReportLine.report_id == draft.id)) is not None
    assert db.scalar(select(AuditLog.id).where(
        AuditLog.action == 'FUNDING_PO_LINE_COST_CORRECTED')) is None


def test_account_get_data_does_not_run_correction_impacts_and_batches_catalog(
    db, monkeypatch,
):
    for index in range(12):
        _assign_order(db, account_id=1, sku=f'BATCH-{index}', cost='10.50')
    monkeypatch.setattr(
        'app.services.v2_funding_reports_service._po_cost_report_impacts',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError('GET must not calculate correction impacts')),
    )
    statements = []
    engine = db.get_bind()

    def record_statement(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(engine, 'before_cursor_execute', record_statement)
    try:
        summary = account_summary(
            db, account_id=1, include_purchase_order_lines=True)
    finally:
        event.remove(engine, 'before_cursor_execute', record_statement)

    catalog_queries = [statement for statement in statements
        if 'ordering_catalog_identity' in statement.lower()]
    assert len(summary['purchase_order_lines']) == 12
    assert len(catalog_queries) == 2
    assert len(statements) < 30


def _combined_payment_obligations(db):
    _add_card_vendor(db, vendor_id=12, name='Beta Card Vendor')
    first = FundingReport(
        account_id=2, vendor_id=10, report_number='CARD-ALPHA',
        account_name_snapshot='Card B', account_type_snapshot='CREDIT_CARD',
        sales_start_date=date(2026, 7, 1), sales_end_date=date(2026, 7, 7),
        status='FINALIZED', calculated_cogs=Decimal('60'), created_by_principal_id=6,
    )
    second = FundingReport(
        account_id=2, vendor_id=12, report_number='CARD-BETA',
        account_name_snapshot='Card B', account_type_snapshot='CREDIT_CARD',
        sales_start_date=date(2026, 7, 8), sales_end_date=date(2026, 7, 14),
        status='FINALIZED', calculated_cogs=Decimal('40'), created_by_principal_id=6,
    )
    db.add_all([first, second]); db.flush()
    combined = FundingReport(
        account_id=2, vendor_id=None, report_number='CARD-ALL',
        account_name_snapshot='Card B', account_type_snapshot='CREDIT_CARD',
        sales_start_date=date(2026, 7, 1), sales_end_date=date(2026, 7, 14),
        status='FINALIZED', calculated_cogs=Decimal('100'), created_by_principal_id=6,
        warning_summary={'combined_report': {
            'version': 1, 'member_report_ids': [first.id, second.id], 'vendor_count': 2,
        }},
    )
    db.add(combined); db.flush()
    return first, second, combined


def test_compact_vendor_payment_is_authoritative_and_reconciles(db):
    first, _, _ = _combined_payment_obligations(db)
    record_compact_payment(
        db, account_id=2, report_id=first.id, amount=Decimal('15'),
        paid_in_full=False, payment_date=date(2026, 8, 17), actor_id=6,
    )
    payment = record_compact_payment(
        db, account_id=2, report_id=first.id, amount=Decimal('999999'),
        paid_in_full=True, payment_date=date(2026, 8, 18), actor_id=6,
    )
    assert payment.amount == Decimal('45.00')
    assert report_position(db, report_id=first.id)['settled_amount'] == Decimal('60.00')
    assert report_position(db, report_id=first.id)['remaining_amount'] == Decimal('0.00')


@pytest.mark.parametrize('amount', [Decimal('0'), Decimal('-1'), Decimal('60.01')])
def test_compact_vendor_payment_rejects_invalid_amounts(db, amount):
    first, _, _ = _combined_payment_obligations(db)
    with pytest.raises(ValueError):
        record_compact_payment(
            db, account_id=2, report_id=first.id, amount=amount,
            paid_in_full=False, payment_date=date(2026, 8, 17), actor_id=6,
        )


def test_account_vendor_payment_allocates_oldest_first(db):
    first, _, _ = _combined_payment_obligations(db)
    newer = FundingReport(
        account_id=2, vendor_id=10, report_number='CARD-ALPHA-NEW',
        account_name_snapshot='Card B', account_type_snapshot='CREDIT_CARD',
        sales_start_date=date(2026, 7, 15), sales_end_date=date(2026, 7, 21),
        status='FINALIZED', calculated_cogs=Decimal('30'), created_by_principal_id=6,
    )
    db.add(newer); db.flush()
    payment = record_compact_payment(
        db, account_id=2, vendor_id=10, amount=Decimal('75'), paid_in_full=False,
        payment_date=date(2026, 8, 17), actor_id=6,
    )
    allocations = db.scalars(select(FundingPaymentAllocation).where(
        FundingPaymentAllocation.payment_id == payment.id
    ).order_by(FundingPaymentAllocation.id)).all()
    assert [(row.report_id, row.amount) for row in allocations] == [
        (first.id, Decimal('60.00')), (newer.id, Decimal('15.00')),
    ]


def test_combined_payment_is_one_event_with_oldest_first_capped_allocations(db):
    first, second, combined = _combined_payment_obligations(db)
    payment = record_compact_payment(
        db, account_id=2, report_id=combined.id, amount=Decimal('75'),
        paid_in_full=False, combined=True, payment_date=date(2026, 8, 17), actor_id=6,
    )
    allocations = db.scalars(select(FundingPaymentAllocation).where(
        FundingPaymentAllocation.payment_id == payment.id
    ).order_by(FundingPaymentAllocation.id)).all()
    assert payment.vendor_id is None
    assert [(row.report_id, row.amount) for row in allocations] == [
        (first.id, Decimal('60.00')), (second.id, Decimal('15.00')),
    ]
    assert len(db.scalars(select(FundingPayment)).all()) == 1
    assert sum((row.amount for row in allocations), Decimal('0')) == payment.amount
    assert report_position(db, report_id=combined.id)['remaining_amount'] == Decimal('25.00')


def test_combined_paid_in_full_respects_prior_payment(db):
    first, second, combined = _combined_payment_obligations(db)
    record_compact_payment(
        db, account_id=2, report_id=first.id, amount=Decimal('20'),
        paid_in_full=False, payment_date=date(2026, 8, 16), actor_id=6,
    )
    payment = record_compact_payment(
        db, account_id=2, report_id=combined.id, amount=Decimal('0.01'),
        paid_in_full=True, combined=True, payment_date=date(2026, 8, 17), actor_id=6,
    )
    allocations = db.scalars(select(FundingPaymentAllocation).where(
        FundingPaymentAllocation.payment_id == payment.id
    ).order_by(FundingPaymentAllocation.id)).all()
    assert payment.amount == Decimal('80.00')
    assert [(row.report_id, row.amount) for row in allocations] == [
        (first.id, Decimal('40.00')), (second.id, Decimal('40.00')),
    ]
    assert report_position(db, report_id=combined.id)['remaining_amount'] == Decimal('0.00')


def test_overlap_and_inline_payment_ui_contracts():
    new_report = open('app/templates/v2/order_payments/funding_report_new.html').read()
    combined_new = open(
        'app/templates/v2/order_payments/funding_combined_report_new.html'
    ).read()
    combined_detail = open(
        'app/templates/v2/order_payments/funding_combined_report_detail.html'
    ).read()
    account_detail = open(
        'app/templates/v2/order_payments/funding_account_detail.html'
    ).read()
    for template in (new_report, combined_new):
        assert 'name="overlap_acknowledged"' in template
        assert 'I understand these reporting periods overlap and want to continue.' in template
        assert 'type="hidden" name="overlap_acknowledged"' not in template
    for template in (combined_detail, account_detail):
        assert 'data-inline-payment' in template
        assert 'data-paid-in-full' in template
    assert 'Record Combined Payment' in combined_detail
    assert 'Payment source' not in combined_detail and 'Internal note' not in combined_detail
    assert 'name="payment_date" value="{{ today }}"' in combined_detail


def test_combined_overlap_requires_acknowledgement_and_then_succeeds(db):
    _add_card_vendor(db, vendor_id=12, name='Zulu Vendor')
    _assign_card_po(
        db, vendor_id=12, create_line=True, variation_id='VAR-ZULU', sku='ZULU'
    )
    _sale(db, fact_id=1, vendor_id=10, day=date(2026, 7, 1))
    _sale(
        db, fact_id=2, vendor_id=12, day=date(2026, 7, 3),
        variation_id='VAR-ZULU', sku='ZULU',
    )
    prior = calculate_report(
        db, account_id=2, vendor_id=10, start_date=date(2026, 7, 1),
        end_date=date(2026, 7, 1), store_ids=[], sku_filter='', internal_note='',
        overlap_acknowledged=False, actor_id=6,
    )
    finalize_report(db, report_id=prior.id, actor_id=6)
    db.commit()
    with pytest.raises(ValueError, match='OVERLAP_ACKNOWLEDGEMENT_REQUIRED'):
        calculate_combined_report(
            db, account_id=2, start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 1), store_ids=[], sku_filter='',
            internal_note='', actor_id=6, overlap_acknowledged=False,
        )
    db.rollback()
    combined = calculate_combined_report(
        db, account_id=2, start_date=date(2026, 7, 1),
        end_date=date(2026, 7, 1), store_ids=[], sku_filter='', internal_note='',
        actor_id=6, overlap_acknowledged=True,
    )
    assert combined.overlap_acknowledged is True
    assert prior.id in combined.overlapping_report_ids
    assert len(combined_report_members(db, report=combined)) == 2


def test_compact_payment_routes_preserve_owner_csrf_guards():
    from app.main import app
    from app.routers.v2_order_payments import feature_access, owner_access
    from app.security.csrf import verify_csrf

    routes = [route for route in app.routes
        if 'vendor-payments' in getattr(route, 'path', '')
        or 'combined-payments' in getattr(route, 'path', '')]
    assert len(routes) == 3
    for route in routes:
        calls = [dependency.call for dependency in route.dependant.dependencies]
        assert feature_access in calls and owner_access in calls and verify_csrf in calls
