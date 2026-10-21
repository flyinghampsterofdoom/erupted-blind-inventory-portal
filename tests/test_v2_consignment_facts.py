from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session

from app.models import (
    Base,
    ConsignmentEmailDelivery,
    ConsignmentInventorySnapshot,
    ConsignmentLedgerEntry,
    ConsignmentReport,
    ConsignmentReportFactLink,
    ConsignmentReportLine,
    ConsignmentReturnFact,
    ConsignmentSaleFact,
    ConsignmentSalesSyncState,
    OrderingCatalogIdentity,
    Store,
    Vendor,
    VendorPaymentSetting,
    VendorVariationAssignment,
    VendorVariationCost,
)
from app.services.v2_consignment_facts_service import (
    attribution_at,
    automatic_report_start_date,
    capture_test_email,
    create_assignment,
    create_cost,
    finalize_report,
    generate_report,
    import_square_orders,
    resolve_sale_fact,
    synchronize_square_facts,
    void_report,
)
from app.services.v2_square_data_service import square_data_status

TABLES = (
    'stores', 'vendors', 'ordering_catalog_identity', 'vendor_variation_assignments',
    'vendor_variation_costs', 'consignment_sale_facts', 'consignment_return_facts',
    'consignment_reports', 'consignment_report_lines', 'consignment_report_fact_links',
    'consignment_inventory_snapshots', 'consignment_sales_sync_state', 'consignment_email_deliveries',
    'vendor_payment_settings',
)


@pytest.fixture()
def db(monkeypatch):
    monkeypatch.setattr('app.services.v2_consignment_facts_service._audit', lambda *args, **kwargs: None)
    engine = create_engine('sqlite+pysqlite:///:memory:')

    @event.listens_for(engine, 'connect')
    def sqlite_functions(connection, _record):
        connection.create_function('char_length', 1, lambda value: len(value) if value is not None else None)

    Base.metadata.create_all(engine, tables=[Base.metadata.tables[name] for name in TABLES])
    with engine.begin() as connection:
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
                key = type(row).__name__; counters[key] = counters.get(key, 0) + 1; row.id = counters[key]

    session.add_all([
        Store(id=1, name='HWY99', square_location_id='LOC-1', active=True),
        Vendor(id=1, square_vendor_id='V-1', name='Consignment Vendor', active=True),
        Vendor(id=2, square_vendor_id='V-2', name='Other Vendor', active=True),
        OrderingCatalogIdentity(square_variation_id='VAR-1', square_item_id='ITEM-1', sku='SKU-1',
            item_name='Catalog item', variation_name='Blue', product_name='Original Product',
            square_is_deleted=False, last_seen_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
    ])
    session.commit()
    yield session
    session.close()


def _history(db, *, cost='4.2500'):
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    create_assignment(db, vendor_id=1, variation_id='VAR-1', is_consignment=True,
                      start_at=start, end_at=None, actor_id=99, notes='Contract start')
    create_cost(db, vendor_id=1, variation_id='VAR-1', unit_cost=Decimal(cost),
                start_at=start, end_at=None, actor_id=99, notes='Contract cost')
    db.commit()


def _sale_order(*, product='Sold Product', quantity='2'):
    return {'id': 'ORDER-1', 'location_id': 'LOC-1', 'state': 'COMPLETED',
        'closed_at': '2026-06-15T20:30:00Z', 'version': 3,
        'tenders': [{'payment_id': 'PAY-1'}], 'line_items': [{
            'uid': 'LINE-1', 'catalog_object_id': 'VAR-1', 'name': product,
            'variation_name': 'Blue', 'quantity': quantity,
            'gross_sales_money': {'amount': 2000, 'currency': 'USD'},
            'total_discount_money': {'amount': 200, 'currency': 'USD'},
            'total_tax_money': {'amount': 100, 'currency': 'USD'},
            'total_money': {'amount': 1900, 'currency': 'USD'},
        }]}


def test_effective_dated_lookup_and_missing_cost(db):
    create_assignment(db, vendor_id=1, variation_id='VAR-1', is_consignment=True,
        start_at=datetime(2025, 1, 1, tzinfo=timezone.utc), end_at=None, actor_id=1, notes='Contract')
    db.commit()
    missing = attribution_at(db, variation_id='VAR-1', transacted_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert missing.status == 'MISSING_COST'
    create_cost(db, vendor_id=1, variation_id='VAR-1', unit_cost=Decimal('4.25'),
        start_at=datetime(2025, 1, 1, tzinfo=timezone.utc), end_at=None, actor_id=1, notes='Contract')
    db.commit()
    attributed = attribution_at(db, variation_id='VAR-1', transacted_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert attributed.status == 'ATTRIBUTED'
    assert attributed.unit_cost == Decimal('4.2500')


def test_effective_period_overlap_is_rejected(db):
    _history(db)
    with pytest.raises(ValueError, match='cannot overlap'):
        create_cost(db, vendor_id=1, variation_id='VAR-1', unit_cost=Decimal('5'),
            start_at=datetime(2026, 1, 1, tzinfo=timezone.utc), end_at=None, actor_id=1, notes='Bad overlap')
    with pytest.raises(ValueError, match='cannot overlap'):
        create_assignment(db, vendor_id=2, variation_id='VAR-1', is_consignment=True,
            start_at=datetime(2026, 1, 1, tzinfo=timezone.utc), end_at=None, actor_id=1, notes='Ambiguous')


def test_sale_import_is_idempotent_and_snapshots_economic_identity(db):
    _history(db)
    first = import_square_orders(db, orders=[_sale_order()]); db.commit()
    second = import_square_orders(db, orders=[_sale_order(product='Renamed Later')]); db.commit()
    fact = db.scalar(select(ConsignmentSaleFact))
    assert (first.sales_created, second.sales_created, second.existing) == (1, 0, 1)
    assert fact.product_name_snapshot == 'Sold Product'
    assert fact.vendor_name_snapshot == 'Consignment Vendor'
    assert fact.unit_cost_snapshot == Decimal('4.2500')
    assert fact.extended_cogs_snapshot == Decimal('8.50')
    assert fact.store_id == 1


def test_sync_consumes_every_page_and_records_only_contiguous_run_coverage(db):
    _history(db)
    second = _sale_order(quantity='3')
    second['id'] = 'ORDER-2'
    second['line_items'][0]['uid'] = 'LINE-2'

    class Reader:
        def search(self, **values):
            self.values = values
            yield [_sale_order(quantity='2')]
            yield [second]

    reader = Reader()
    start_at = datetime(2026, 6, 1, 7, tzinfo=timezone.utc)
    end_at = datetime(2026, 6, 8, 7, tzinfo=timezone.utc)
    result = synchronize_square_facts(db, start_at=start_at, end_at=end_at,
        actor_id=1, reader=reader)
    state = db.get(ConsignmentSalesSyncState, 1)

    assert result.orders == 2
    assert result.sales_created == 2
    assert db.scalar(select(func.sum(ConsignmentSaleFact.quantity_sold))) == 5
    assert reader.values == {
        'location_ids': ['LOC-1'], 'start_at': start_at, 'end_at': end_at}
    assert state.last_successful_start_at == start_at
    assert state.last_successful_through_at == end_at


def test_sync_extends_only_overlapping_coverage_and_never_invents_a_gap(db):
    _history(db)

    class EmptyReader:
        def search(self, **_values):
            yield []

    state = ConsignmentSalesSyncState(
        id=1,
        last_successful_start_at=datetime(2026, 8, 2, 7, tzinfo=timezone.utc),
        last_successful_through_at=datetime(2026, 8, 4, 7, tzinfo=timezone.utc),
        last_successful_at=datetime(2026, 8, 4, 8, tzinfo=timezone.utc),
        last_result='COMPLETE',
        updated_by_principal_id=1,
    )
    db.add(state)
    db.flush()

    synchronize_square_facts(
        db,
        start_at=datetime(2026, 8, 3, 7, tzinfo=timezone.utc),
        end_at=datetime(2026, 8, 9, 7, tzinfo=timezone.utc),
        actor_id=1,
        reader=EmptyReader(),
    )
    assert state.last_successful_start_at == datetime(2026, 8, 2, 7, tzinfo=timezone.utc)
    assert state.last_successful_through_at == datetime(2026, 8, 9, 7, tzinfo=timezone.utc)

    synchronize_square_facts(
        db,
        start_at=datetime(2026, 8, 11, 7, tzinfo=timezone.utc),
        end_at=datetime(2026, 8, 12, 7, tzinfo=timezone.utc),
        actor_id=1,
        reader=EmptyReader(),
    )
    assert state.last_successful_start_at == datetime(2026, 8, 2, 7, tzinfo=timezone.utc)
    assert state.last_successful_through_at == datetime(2026, 8, 9, 7, tzinfo=timezone.utc)


def test_square_data_status_separates_freshness_from_coverage(db):
    state = ConsignmentSalesSyncState(
        id=1,
        last_successful_start_at=datetime(2026, 8, 2, 7, tzinfo=timezone.utc),
        last_successful_through_at=datetime(2026, 8, 3, 7, tzinfo=timezone.utc),
        last_successful_at=datetime(2026, 8, 10, 10, tzinfo=timezone.utc),
        last_result='COMPLETE',
        updated_by_principal_id=1,
    )
    db.add(state)
    db.flush()

    status = square_data_status(
        db, now=datetime(2026, 8, 10, 11, tzinfo=timezone.utc)
    )
    assert status['state'] == 'current'
    assert status['age_hours'] == 1
    assert status['coverage_through_at'] == datetime(2026, 8, 3, 7, tzinfo=timezone.utc)

    status = square_data_status(
        db, now=datetime(2026, 8, 11, 10, tzinfo=timezone.utc)
    )
    assert status['state'] == 'stale'

    status = square_data_status(
        db, now=datetime(2026, 8, 11, 11, tzinfo=timezone.utc)
    )
    assert status['state'] == 'stale'

    state.last_result = 'FAILED'
    state.last_error = 'safe failure'
    db.flush()
    status = square_data_status(
        db, now=datetime(2026, 8, 10, 11, tzinfo=timezone.utc)
    )
    assert status['state'] == 'failed'
    assert status['last_successful_at'] == datetime(2026, 8, 10, 10, tzinfo=timezone.utc)


def test_current_mapping_and_cost_changes_do_not_rewrite_sale_fact(db):
    _history(db)
    import_square_orders(db, orders=[_sale_order()]); db.commit()
    fact = db.scalar(select(ConsignmentSaleFact)); original = (fact.vendor_id_snapshot, fact.unit_cost_snapshot)
    db.query(VendorVariationAssignment).delete(); db.query(VendorVariationCost).delete(); db.commit()
    create_assignment(db, vendor_id=2, variation_id='VAR-1', is_consignment=True,
        start_at=datetime(2025, 1, 1, tzinfo=timezone.utc), end_at=None, actor_id=1, notes='Remap')
    create_cost(db, vendor_id=2, variation_id='VAR-1', unit_cost=Decimal('99'),
        start_at=datetime(2025, 1, 1, tzinfo=timezone.utc), end_at=None, actor_id=1, notes='New cost')
    import_square_orders(db, orders=[_sale_order()]); db.commit(); db.refresh(fact)
    assert (fact.vendor_id_snapshot, fact.unit_cost_snapshot) == original


def test_unresolved_backfill_enriches_after_effective_dated_history_is_added(db):
    import_square_orders(db, orders=[_sale_order()]); db.commit()
    fact = db.scalar(select(ConsignmentSaleFact)); assert fact.attribution_status == 'MISSING_VENDOR'
    _history(db)
    import_square_orders(db, orders=[_sale_order()]); db.commit(); db.refresh(fact)
    assert fact.attribution_status == 'ATTRIBUTED'
    assert fact.attribution_source == 'EFFECTIVE_DATED_ASSIGNMENT_AND_COST'
    assert fact.extended_cogs_snapshot == Decimal('8.50')


def test_partial_return_matches_original_and_uses_original_cost(db):
    _history(db)
    sale = _sale_order()
    returned = {'id': 'RETURN-ORDER', 'location_id': 'LOC-1', 'state': 'COMPLETED',
        'closed_at': '2026-06-20T18:00:00Z', 'line_items': [], 'returns': [{
            'uid': 'RET-1', 'source_order_id': 'ORDER-1', 'return_line_items': [{
                'uid': 'RET-LINE-1', 'source_line_item_uid': 'LINE-1', 'catalog_object_id': 'VAR-1',
                'quantity': '0.5', 'total_money': {'amount': 475, 'currency': 'USD'},
            }]}]}
    import_square_orders(db, orders=[sale, returned]); db.commit()
    fact = db.scalar(select(ConsignmentReturnFact))
    assert fact.attribution_status == 'ATTRIBUTED'
    assert fact.match_method == 'SOURCE_ORDER_AND_LINE_UID'
    assert fact.quantity_returned == Decimal('0.500')
    assert fact.unit_cost_snapshot == Decimal('4.2500')
    assert fact.extended_cogs_reversal == Decimal('2.13')


def test_multiple_partial_returns_cannot_exceed_original_sale(db):
    _history(db)
    first_return = {'id': 'RETURN-1', 'location_id': 'LOC-1', 'state': 'COMPLETED',
        'closed_at': '2026-06-20T18:00:00Z', 'returns': [{'uid': 'RET-1',
        'source_order_id': 'ORDER-1', 'return_line_items': [{'uid': 'RET-LINE-1',
        'source_line_item_uid': 'LINE-1', 'quantity': '1.5',
        'total_money': {'amount': 1000, 'currency': 'USD'}}]}]}
    excessive_return = {'id': 'RETURN-2', 'location_id': 'LOC-1', 'state': 'COMPLETED',
        'closed_at': '2026-06-21T18:00:00Z', 'returns': [{'uid': 'RET-2',
        'source_order_id': 'ORDER-1', 'return_line_items': [{'uid': 'RET-LINE-2',
        'source_line_item_uid': 'LINE-1', 'quantity': '1',
        'total_money': {'amount': 500, 'currency': 'USD'}}]}]}
    import_square_orders(db, orders=[_sale_order(), first_return, excessive_return]); db.commit()
    returns = db.scalars(select(ConsignmentReturnFact).order_by(ConsignmentReturnFact.id)).all()
    assert returns[0].attribution_status == 'ATTRIBUTED'
    assert returns[0].extended_cogs_reversal == Decimal('6.38')
    assert returns[1].attribution_status == 'SOURCE_INCOMPLETE'
    assert returns[1].extended_cogs_reversal is None
    assert returns[1].attribution_reason == 'RETURN_QUANTITY_EXCEEDS_OR_INVALID_FOR_ORIGINAL_SALE'


def test_unmatched_return_and_unitemized_refund_remain_blocked(db):
    _history(db)
    unmatched = {'id': 'RETURN-X', 'location_id': 'LOC-1', 'state': 'COMPLETED',
        'closed_at': '2026-06-21T18:00:00Z', 'returns': [{'uid': 'RET-X',
            'source_order_id': 'MISSING', 'return_line_items': [{'uid': 'RL-X',
            'source_line_item_uid': 'NOPE', 'quantity': '1', 'total_money': {'amount': 100}}]}]}
    refund = {'id': 'REFUND-X', 'location_id': 'LOC-1', 'state': 'COMPLETED',
        'closed_at': '2026-06-22T18:00:00Z', 'refunds': [{'id': 'RF-1',
            'amount_money': {'amount': 500, 'currency': 'USD'}}]}
    result = import_square_orders(db, orders=[unmatched, refund]); db.commit()
    statuses = set(db.scalars(select(ConsignmentReturnFact.attribution_status)))
    assert result.unresolved == 2
    assert statuses == {'UNMATCHED_RETURN', 'SOURCE_INCOMPLETE'}


def test_report_reconciles_to_immutable_facts_finalizes_once_and_voids_with_reversal(db, monkeypatch):
    _history(db)
    import_square_orders(db, orders=[_sale_order()]); db.commit()
    through = datetime(2026, 7, 5, tzinfo=timezone.utc)
    db.add(ConsignmentSalesSyncState(id=1, last_successful_start_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        last_successful_through_at=through, last_successful_at=through, last_attempted_at=through,
        last_result='COMPLETE', updated_by_principal_id=1))
    monkeypatch.setattr('app.services.v2_consignment_facts_service.inventory_snapshot', lambda _db, vendor_id: (
        Decimal('3'), Decimal('12.75'), [{'variation_id': 'VAR-1', 'store_id': 1,
        'quantity': Decimal('3'), 'unit_cost': Decimal('4.25'), 'value': Decimal('12.75'),
        'product_name': 'Inventory Product', 'variation_name': 'Blue', 'sku': 'SKU-1',
        'refreshed_at': datetime(2026, 6, 30, tzinfo=timezone.utc)}], []))
    report = generate_report(db, vendor_id=1, start_date=datetime(2026, 6, 1).date(),
                             end_date=datetime(2026, 6, 30).date(), actor_id=1)
    db.commit()
    assert report.total_units == Decimal('2.000')
    assert report.total_cogs == Decimal('8.50')
    assert report.inventory_value_snapshot == Decimal('12.75')
    assert not report.data_integrity_blockers['codes']
    assert db.scalar(select(ConsignmentReportFactLink.cogs_amount_snapshot)) == Decimal('8.50')
    assert report.cash_settlements_period_snapshot == Decimal('0.00')
    assert report.approved_credits_period_snapshot == Decimal('0.00')
    assert report.void_reversals_period_snapshot == Decimal('0.00')
    finalize_report(db, report_id=report.id, actor_id=1)
    finalize_report(db, report_id=report.id, actor_id=1)
    db.commit()
    entries = db.scalars(select(ConsignmentLedgerEntry)).all()
    assert [entry.entry_type for entry in entries] == ['COGS_GENERATED']
    assert entries[0].amount == Decimal('8.50')
    with pytest.raises(ValueError, match='vendor profile'):
        capture_test_email(db, report_id=report.id, actor_id=1)
    db.add(VendorPaymentSetting(vendor_id=1, report_email='vendor@example.com',
        updated_by_principal_id=1))
    db.flush()
    delivery = capture_test_email(db, report_id=report.id, actor_id=1)
    db.commit()
    assert delivery.status == 'CAPTURED_TEST'
    assert delivery.recipient == 'vendor@example.com'
    assert delivery.subject.startswith('[TEST CAPTURE]')
    assert 'NO EXTERNAL EMAIL SENT' in delivery.body_snapshot
    assert 'Inventory snapshot at:' in delivery.body_snapshot
    assert 'Closing unreplenished COGS: $8.50' in delivery.body_snapshot
    assert 'Available replenishment credit: $0.00' in delivery.body_snapshot
    assert delivery.error_summary is None
    with pytest.raises(ValueError, match='cannot be rewritten'):
        resolve_sale_fact(db, fact_id=db.scalar(select(ConsignmentSaleFact.id)), vendor_id=1,
                          unit_cost=Decimal('9'), disposition='ATTRIBUTED', reason='Too late', actor_id=1)
    audits = []
    monkeypatch.setattr('app.services.v2_consignment_facts_service._audit',
        lambda *args, **kwargs: audits.append(kwargs))
    void_report(db, report_id=report.id, reason='Owner-approved correction', actor_id=1)
    db.commit()
    assert set(db.scalars(select(ConsignmentLedgerEntry.entry_type))) == {'COGS_GENERATED', 'VOID_REVERSAL'}
    assert report.status == 'VOIDED'
    assert audits[-1]['before']['original_ledger_entry_id']
    assert audits[-1]['after']['reversal_ledger_entry_id']


def test_report_uses_explicit_signed_period_ledger_components(db, monkeypatch):
    _history(db)
    import_square_orders(db, orders=[_sale_order()]); db.commit()
    through = datetime(2026, 7, 5, tzinfo=timezone.utc)
    db.add(ConsignmentSalesSyncState(id=1, last_successful_through_at=through,
        last_successful_at=through, last_attempted_at=through, last_result='COMPLETE'))
    db.add(ConsignmentLedgerEntry(vendor_id=1, entry_type='COGS_GENERATED',
        effective_at=datetime(2026, 5, 10, tzinfo=timezone.utc), amount=Decimal('20'),
        created_by_principal_id=1))
    for entry_type, amount in (
        ('REPLENISHMENT_APPLIED', '3'),
        ('CASH_SETTLEMENT', '2'),
        ('APPROVED_CREDIT', '1'),
        ('VOID_REVERSAL', '4'),
    ):
        db.add(ConsignmentLedgerEntry(vendor_id=1, entry_type=entry_type,
            effective_at=datetime(2026, 6, 10, tzinfo=timezone.utc), amount=Decimal(amount),
            created_by_principal_id=1))
    monkeypatch.setattr('app.services.v2_consignment_facts_service.inventory_snapshot',
        lambda _db, vendor_id: (Decimal('0'), Decimal('0'), [], []))
    report = generate_report(db, vendor_id=1, start_date=datetime(2026, 6, 1).date(),
        end_date=datetime(2026, 6, 30).date(), actor_id=1)
    assert report.prior_unreplenished_cogs_snapshot == Decimal('20.00')
    assert report.total_cogs == Decimal('8.50')
    assert report.replenishment_applied_period_snapshot == Decimal('3.00')
    assert report.cash_settlements_period_snapshot == Decimal('2.00')
    assert report.approved_credits_period_snapshot == Decimal('1.00')
    assert report.void_reversals_period_snapshot == Decimal('4.00')
    assert report.ending_unreplenished_cogs_snapshot == Decimal('18.50')


def test_regenerating_same_preview_replaces_draft_links_without_duplication(db, monkeypatch):
    _history(db)
    import_square_orders(db, orders=[_sale_order()]); db.commit()
    through = datetime(2026, 7, 5, tzinfo=timezone.utc)
    db.add(ConsignmentSalesSyncState(id=1, last_successful_through_at=through,
        last_successful_at=through, last_attempted_at=through, last_result='COMPLETE'))
    monkeypatch.setattr('app.services.v2_consignment_facts_service.inventory_snapshot',
        lambda _db, vendor_id: (Decimal('0'), Decimal('0'), [], []))
    first = generate_report(db, vendor_id=1, start_date=datetime(2026, 6, 1).date(),
        end_date=datetime(2026, 6, 30).date(), actor_id=1)
    db.commit()
    first_id = first.id
    second = generate_report(db, vendor_id=1, start_date=datetime(2026, 6, 1).date(),
        end_date=datetime(2026, 6, 30).date(), actor_id=1)
    db.commit()
    assert second.id != first_id
    assert db.scalar(select(func.count(ConsignmentReport.id))) == 1
    assert db.scalar(select(func.count(ConsignmentReportFactLink.id))) == 1


def test_unresolved_fact_is_visible_but_excluded_and_blocks_finalization(db, monkeypatch):
    import_square_orders(db, orders=[_sale_order()]); db.commit()
    through = datetime(2026, 7, 5, tzinfo=timezone.utc)
    db.add(ConsignmentSalesSyncState(id=1, last_successful_through_at=through,
        last_successful_at=through, last_attempted_at=through, last_result='COMPLETE'))
    monkeypatch.setattr('app.services.v2_consignment_facts_service.inventory_snapshot',
                        lambda _db, vendor_id: (Decimal('0'), Decimal('0'), [], []))
    report = generate_report(db, vendor_id=1, start_date=datetime(2026, 6, 1).date(),
                             end_date=datetime(2026, 6, 30).date(), actor_id=1)
    assert report.total_cogs == Decimal('0.00')
    assert 'SALE_MISSING_VENDOR' in report.data_integrity_blockers['codes']
    assert report.data_integrity_blockers['unresolved_sale_ids']
    with pytest.raises(ValueError, match='Resolve all blocking facts'):
        finalize_report(db, report_id=report.id, actor_id=1)


def test_automatic_start_begins_at_end_of_latest_finalized_period(db):
    assert automatic_report_start_date(db, vendor_id=1) is None
    db.add(ConsignmentReport(id=1, vendor_id=1, report_number='OLD',
        start_at=datetime(2026, 5, 1, 7, tzinfo=timezone.utc),
        end_at=datetime(2026, 6, 1, 7, tzinfo=timezone.utc), status='FINALIZED',
        total_units=0, total_cogs=0, inventory_quantity_snapshot=0, inventory_value_snapshot=0,
        data_integrity_blockers={}, created_by_principal_id=1))
    db.commit()
    assert automatic_report_start_date(db, vendor_id=1).isoformat() == '2026-06-01'
