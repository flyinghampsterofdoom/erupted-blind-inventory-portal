import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.models import (
    Base,
    ConsignmentReturnFact,
    ConsignmentSaleFact,
    FundingAccount,
    FundingLedgerEntry,
    FundingPayment,
    FundingPaymentAllocation,
    FundingReport,
    FundingReportAdjustment,
    FundingReportExclusion,
    FundingReportFactLink,
    FundingReportLine,
    FundingSkuMapping,
    OrderingCatalogIdentity,
    OrderingCurrentInventory,
    Store,
)
from app.services.v2_funding_reports_service import (
    add_adjustment,
    account_summary,
    apr_estimate,
    bulk_assign_skus,
    calculate_report,
    finalize_report,
    normalize_sku,
    record_ledger_entry,
    record_inventory_purchase_for_order,
    record_payment,
    report_position,
    reverse_adjustment,
    reverse_ledger_entry,
    reverse_payment,
    tracked_balance,
    void_report,
)
from app.config import settings
from app.routers.v2_funding_reports import _action_gate, calculate_funding_report_action


TABLES = (
    'stores',
    'consignment_sale_facts', 'consignment_return_facts', 'funding_accounts',
    'funding_sku_mappings', 'funding_reports', 'funding_report_lines',
    'funding_report_fact_links', 'funding_report_exclusions',
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
        FundingAccount(id=1, account_type='CONSIGNMENT', vendor_id=10, display_name='Consignment A',
            is_active=True, created_by_principal_id=6, updated_by_principal_id=6),
        FundingAccount(id=2, account_type='CREDIT_CARD', payment_method_id=20, display_name='Card B',
            issuer='Issuer', last_four='1234', promotional_apr=Decimal('0'),
            promotional_start_date=date(2026, 1, 1), promotional_expiration_date=date(2026, 12, 31),
            standard_apr=Decimal('24'), is_active=True, created_by_principal_id=6,
            updated_by_principal_id=6),
        FundingAccount(id=3, account_type='CONSIGNMENT', vendor_id=11, display_name='Consignment B',
            is_active=True, created_by_principal_id=6, updated_by_principal_id=6),
    ])
    session.commit()
    yield session
    session.close()


def _sale(db, *, fact_id=1, sku='AB12', day=date(2026, 7, 1), quantity='3', store_id=1,
          product='Exact Product'):
    row = ConsignmentSaleFact(id=fact_id, square_order_id=f'ORDER-{fact_id}',
        square_line_item_uid=f'LINE-{fact_id}', square_variation_id='VAR-EXACT',
        square_location_id=f'LOC-{store_id}', store_id=store_id, business_date=day,
        transacted_at=datetime(day.year, day.month, day.day, 20, tzinfo=timezone.utc),
        quantity_sold=Decimal(quantity), gross_sales_amount=Decimal('30'), discount_amount=0,
        tax_amount=0, net_sales_amount=Decimal('30'), currency='USD',
        product_name_snapshot=product, variation_name_snapshot='Blue', sku_snapshot=sku,
        attribution_status='NON_CONSIGNMENT', attribution_source='SOURCE',
        source_synchronized_at=datetime.now(timezone.utc))
    db.add(row); db.flush(); return row


def _return(db, sale, *, fact_id=1, sku='AB12', day=date(2026, 7, 2), quantity='1', store_id=1):
    row = ConsignmentReturnFact(id=fact_id, square_return_order_id=f'RETURN-{fact_id}',
        square_return_uid=f'RET-{fact_id}', square_return_line_uid=f'RET-LINE-{fact_id}',
        original_square_order_id=sale.square_order_id, original_square_line_uid=sale.square_line_item_uid,
        square_variation_id='VAR-EXACT', square_location_id=f'LOC-{store_id}', store_id=store_id,
        business_date=day, returned_at=datetime(day.year, day.month, day.day, 20, tzinfo=timezone.utc),
        quantity_returned=Decimal(quantity), refund_amount=Decimal('10'), currency='USD',
        product_name_snapshot='Exact Product', variation_name_snapshot='Blue', sku_snapshot=sku,
        attribution_status='UNMATCHED_RETURN', source_synchronized_at=datetime.now(timezone.utc))
    db.add(row); db.flush(); return row


def _map(db, *, account_id=1, sku='AB12', cost='4', start=date(2026, 1, 1)):
    identities = db.scalars(select(OrderingCatalogIdentity).where(
        OrderingCatalogIdentity.sku.is_not(None))).all()
    if not any(normalize_sku(row.sku) == normalize_sku(sku) for row in identities):
        normalized = normalize_sku(sku)
        db.add(OrderingCatalogIdentity(square_variation_id=f'VAR-{normalized}', sku=sku,
            item_name=f'Product {normalized}', variation_name='Default', product_name=f'Product {normalized}',
            square_is_deleted=False, last_seen_at=datetime.now(timezone.utc)))
        db.flush()
    return bulk_assign_skus(db, account_id=account_id, skus=[sku], effective_date=start,
        unit_cost=Decimal(cost), reason='Owner verified account and cost.', actor_id=6)[0]


def _report(db, *, account_id=1, acknowledged=False, start=date(2026, 7, 1), end=date(2026, 7, 2)):
    return calculate_report(db, account_id=account_id, start_date=start, end_date=end,
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
    assert report.inventory_units_snapshot == 5
    assert report.inventory_value_snapshot == Decimal('20.00')


def test_same_sku_across_stores_stays_store_itemized(db):
    _map(db); _sale(db, fact_id=1, store_id=1); _sale(db, fact_id=2, store_id=2, quantity='2')
    report = _report(db)
    lines = db.scalars(select(FundingReportLine).where(FundingReportLine.report_id == report.id)
        .order_by(FundingReportLine.store_id)).all()
    assert [(row.store_id, row.units_sold) for row in lines] == [(1, 3), (2, 2)]


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


def test_empty_or_expired_mapping_set_fails_closed_without_report_lines(db):
    _sale(db, sku='AB12')
    with pytest.raises(ValueError, match='No SKUs are mapped'):
        _report(db, account_id=1)
    expired = _map(db, account_id=1, start=date(2026, 1, 1))
    expired.effective_end_date = date(2026, 6, 30); db.flush()
    with pytest.raises(ValueError, match='No SKUs are mapped'):
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


def test_mapping_effective_dates_restrict_each_sale_date(db):
    _map(db, account_id=1, sku='DATED', start=date(2026, 7, 2))
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


def test_optional_product_filter_only_narrows_exact_sku_attribution(db):
    _map(db); _sale(db, product='Exact Product')
    report = calculate_report(db, account_id=1, start_date=date(2026, 7, 1), end_date=date(2026, 7, 2),
        store_ids=[], sku_filter='Exact Product', internal_note='', overlap_acknowledged=False, actor_id=6)
    assert report.calculated_cogs == Decimal('12.00')
    assert db.scalar(select(FundingReportLine).where(FundingReportLine.report_id == report.id)).normalized_sku == 'AB12'


def test_conflicting_active_mappings_block_report_creation(db):
    _map(db, account_id=1)
    db.add(FundingSkuMapping(account_id=3, normalized_sku='AB12', sku_snapshot='AB12',
        square_variation_id='VAR-EXACT', product_name_snapshot='Exact Product',
        variation_name_snapshot='Blue', effective_start_date=date(2026, 1, 1), unit_cost=Decimal('5'),
        status='ACTIVE', reason='Conflicting fixture', created_by_principal_id=6))
    db.flush(); _sale(db)
    with pytest.raises(ValueError, match='multiple funding accounts'):
        _report(db, account_id=1)
    assert db.scalar(select(FundingReport.id)) is None


def test_mapping_without_effective_cost_blocks_report_creation(db):
    db.add(FundingSkuMapping(account_id=1, normalized_sku='AB12', sku_snapshot='AB12',
        square_variation_id='VAR-EXACT', product_name_snapshot='Exact Product',
        variation_name_snapshot='Blue', effective_start_date=date(2026, 1, 1), unit_cost=None,
        status='ACTIVE', reason='Imported mapping awaiting owner cost', created_by_principal_id=6))
    _sale(db)
    with pytest.raises(ValueError, match='effective cost'):
        _report(db)
    assert db.scalar(select(FundingReport.id)) is None


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
    charge = add_adjustment(db, report_id=report.id, adjustment_type='SHIPPING', direction='INCREASE',
        amount=Decimal('2'), effective_date=date(2026, 7, 3), reason='Freight', internal_note='',
        owner_confirmed=True, actor_id=6)
    assert report_position(db, report_id=report.id)['adjusted_amount'] == Decimal('14.00')
    reversal = reverse_adjustment(db, adjustment_id=charge.id, reason='Entered twice', actor_id=6)
    assert reversal.reversed_adjustment_id == charge.id
    assert db.scalar(select(FundingReportAdjustment).where(FundingReportAdjustment.id == charge.id)) is charge
    assert report_position(db, report_id=report.id)['adjusted_amount'] == Decimal('12.00')


def test_partial_multi_report_payment_and_excess_are_not_double_allocated(db):
    _map(db); _sale(db)
    first = _report(db); finalize_report(db, report_id=first.id, actor_id=6); db.commit()
    second = _report(db, acknowledged=True); finalize_report(db, report_id=second.id, actor_id=6); db.commit()
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
    summary = account_summary(db, account_id=1)
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
