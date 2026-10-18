"""Phase 1 contracts and actual authority. PostgreSQL tests require an explicitly disposable DB."""
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Event

import pytest
from sqlalchemy import create_engine, select, func, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from app.config import settings
from app.models import (Principal, PaymentMethod, FundingAccount, FundingActualActivity,
    FundingBalanceCheckpoint, FundingLedgerEntry, PurchasingProductAuthority, Vendor, AuditLog,
    PurchasingEvidenceRun, OrderingCurrentInventory)
from app.services.card_balance_service import actual_card_balance, adjust_balance, record_actual_activity
from app.services.purchasing_evidence_service import quantity, InventoryEvidence, SalesCoverage, sales_coverage_for, record_run, inventory_pair_verified
from app.services.purchasing_inventory_service import outstanding_quantity, inventory_position
from app.services.purchasing_classification_service import classify_product, square_vendor_ids
from app.services.square_request_policy import enforce_square_request_policy, SquareWriteBoundaryViolation

NOW = datetime.now(timezone.utc)


@pytest.fixture
def p1db(monkeypatch):
    url = os.getenv('P1_TEST_DATABASE_URL')
    if not url:
        pytest.skip('P1_TEST_DATABASE_URL must name an explicitly disposable migrated local database')
    parsed = make_url(url)
    assert parsed.database.startswith('erupted_purchasing_p1_') and parsed.host in {None, 'localhost', '127.0.0.1'}
    engine = create_engine(url)
    connection = engine.connect()
    transaction = connection.begin()
    db = Session(connection, autoflush=False)
    db.add(Principal(id=6001, username='phase1-owner', password_hash='not-a-production-hash', role='ADMIN', active=True))
    db.flush()
    db.add(PaymentMethod(id=6001, display_name='Test card', category='CREDIT_CARD', is_active=True,
        created_by_principal_id=6001, updated_by_principal_id=6001))
    db.flush()
    db.add(FundingAccount(id=6001, account_type='CREDIT_CARD', payment_method_id=6001, display_name='Test card',
        created_by_principal_id=6001, updated_by_principal_id=6001))
    db.flush()
    monkeypatch.setattr(settings, 'purchasing_v3_phase1_enabled', True)
    monkeypatch.setattr(settings, 'purchasing_owner_principal_id', 6001)
    try:
        yield db
    finally:
        db.close(); transaction.rollback(); connection.close(); engine.dispose()


def test_square_endpoint_policy_and_write_boundary():
    enforce_square_request_policy('POST', '/v2/inventory/counts/batch-retrieve', {})
    with pytest.raises(SquareWriteBoundaryViolation):
        enforce_square_request_policy('POST', '/v2/catalog/batch-upsert', {})


@pytest.mark.parametrize('value', [None, '', 'NaN', 'Infinity', True, 'broken'])
def test_missing_or_invalid_quantity_never_becomes_zero(value):
    assert quantity(value) is None


def test_inventory_evidence_and_supply_definitions():
    assert quantity('0') == 0
    assert InventoryEvidence(Decimal('-2'), NOW).usable(as_of=NOW) == -2
    assert InventoryEvidence(Decimal(5), NOW-timedelta(hours=25)).usable(as_of=NOW) is None
    assert outstanding_quantity(20, 8, closed=2) == 10
    assert outstanding_quantity(5, 8) == 0
    assert outstanding_quantity(5, 'NaN') is None
    assert inventory_position(10, 5, non_sellable=2) == 15
    assert inventory_position(None, 5) is None
    assert inventory_position(10, 5, committed_out=3, committed_in=1) == 15


def test_coverage_requires_complete_range_and_location():
    coverage = SalesCoverage(NOW-timedelta(days=28), NOW, ('L1',), True, NOW)
    assert coverage.covers(NOW-timedelta(days=7), NOW, ['L1'])
    assert not coverage.covers(NOW-timedelta(days=30), NOW, ['L1'])
    assert not coverage.covers(NOW-timedelta(days=7), NOW, ['L2'])


def test_checkpoint_ignores_legacy_estimates_and_replay(p1db):
    db = p1db
    db.add(FundingLedgerEntry(account_id=6001, entry_type='INVENTORY_PURCHASE', direction='INCREASE',
        amount=Decimal('9999'), effective_date=NOW.date(), reason='Historical assignment estimate', created_by_principal_id=6001))
    db.flush()
    assert actual_card_balance(db, account_id=6001) is None
    adjust_balance(db, account_id=6001, balance='1000', actor_id=6001, idempotency_key='baseline')
    charge = record_actual_activity(db, account_id=6001, kind='CHARGE', amount=50, actor_id=6001, idempotency_key='charge')
    assert actual_card_balance(db, account_id=6001) == 1050
    adjust_balance(db, account_id=6001, balance=800, actor_id=6001, idempotency_key='corrected')
    assert record_actual_activity(db, account_id=6001, kind='CHARGE', amount=50, actor_id=6001, idempotency_key='charge').id == charge.id
    assert actual_card_balance(db, account_id=6001) == 800
    record_actual_activity(db, account_id=6001, kind='PAYMENT', amount=100, actor_id=6001, idempotency_key='payment')
    assert actual_card_balance(db, account_id=6001) == 700
    assert db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == 'CARD_BALANCE_CHECKPOINT')) == 2
    with pytest.raises(ValueError, match='Historical'):
        record_actual_activity(db, account_id=6001, kind='CHARGE', amount=50, actor_id=6001, idempotency_key='old-import', historical=True)


def test_balance_correction_no_reason_permissions_and_negative_credit(p1db, monkeypatch):
    db = p1db
    adjust_balance(db, account_id=6001, balance=-20, actor_id=6001, idempotency_key='credit')
    assert actual_card_balance(db, account_id=6001) == -20
    with pytest.raises(ValueError):
        adjust_balance(db, account_id=6001, balance='NaN', actor_id=6001, idempotency_key='invalid')
    with pytest.raises(PermissionError):
        adjust_balance(db, account_id=6001, balance=2, actor_id=6002, idempotency_key='not-owner')
    monkeypatch.setattr(settings, 'purchasing_v3_phase1_enabled', False)
    with pytest.raises(PermissionError):
        adjust_balance(db, account_id=6001, balance=2, actor_id=6001, idempotency_key='disabled')


def test_activity_reversals_preserve_history(p1db):
    db = p1db
    adjust_balance(db, account_id=6001, balance=1000, actor_id=6001, idempotency_key='base')
    charge = record_actual_activity(db, account_id=6001, kind='CHARGE', amount=50, actor_id=6001, idempotency_key='charge')
    record_actual_activity(db, account_id=6001, kind='REVERSAL', amount=50, actor_id=6001, idempotency_key='reverse', original_activity_id=charge.id)
    assert actual_card_balance(db, account_id=6001) == 1000
    assert db.scalar(select(func.count()).select_from(FundingActualActivity)) == 2
    with pytest.raises(ValueError):
        record_actual_activity(db, account_id=6001, kind='REVERSAL', amount=50, actor_id=6001, idempotency_key='twice', original_activity_id=charge.id)
    adjust_balance(db, account_id=6001, balance=900, actor_id=6001, idempotency_key='fix')
    with pytest.raises(ValueError, match='covered'):
        record_actual_activity(db, account_id=6001, kind='REVERSAL', amount=50, actor_id=6001, idempotency_key='covered', original_activity_id=charge.id)


def test_canonical_classification_never_uses_purchasing_supplier(p1db, monkeypatch):
    db = p1db
    monkeypatch.setattr(settings, 'purchasing_big_square_vendor_id', 'BIG')
    monkeypatch.setattr(settings, 'purchasing_streamline_square_vendor_id', 'STREAM')
    db.add_all([Vendor(id=6001, square_vendor_id='BIG', name='Big', active=True),
                Vendor(id=6002, square_vendor_id='OTHER', name='Other', active=True)])
    row = PurchasingProductAuthority(square_variation_id='VAR1', square_vendor_ids=['BIG'], observed_at=NOW, source_complete=True)
    db.add(row); db.flush()
    assert classify_product(db, variation_id='VAR1', as_of=NOW).category == 'BIG'
    row.square_vendor_ids = ['BIG', 'OTHER']; db.flush()
    assert classify_product(db, variation_id='VAR1', as_of=NOW).category == 'UNKNOWN'
    row.square_vendor_ids = ['OTHER']; db.flush()
    assert classify_product(db, variation_id='VAR1', as_of=NOW).category == 'CREDIT_CARD_POOL'
    db.add(PaymentMethod(id=6002, display_name='9671', category='WIRE', last_four='9671', is_active=True,
        created_by_principal_id=6001, updated_by_principal_id=6001)); db.flush()
    row.cash_payment_method_id=6002; db.flush()
    assert classify_product(db, variation_id='VAR1', as_of=NOW).category == 'CASH_9671'
    assert square_vendor_ids({'item_variation_vendor_infos': [{'vendor_id':'BIG'}, {'vendor_id':'OTHER'}]}) == ('BIG', 'OTHER')


def test_durable_sales_coverage_empty_facts_are_not_zero(p1db):
    db=p1db
    assert not sales_coverage_for(db, start_at=NOW-timedelta(days=5), end_at=NOW, location_ids=['L1'], as_of=NOW)
    record_run(db, source='SALES', status='COMPLETE', started_at=NOW-timedelta(seconds=1), completed_at=NOW,
        scope={'start_at':(NOW-timedelta(days=7)).isoformat(), 'end_at':NOW.isoformat(), 'location_ids':['L1']}, evidence={'source_filter':'CLOSED_AT','source_complete':True,'coverage_contract':2})
    assert sales_coverage_for(db, start_at=NOW-timedelta(days=5), end_at=NOW, location_ids=['L1'], as_of=NOW)
    assert not sales_coverage_for(db, start_at=NOW-timedelta(days=5), end_at=NOW, location_ids=['L2'], as_of=NOW)


def test_concurrent_balance_correction_and_payment_are_serialized(p1db):
    # Separate committed test identities allow two actual PostgreSQL transactions.
    url=os.environ['P1_TEST_DATABASE_URL']; engine=create_engine(url)
    actor=6101
    with Session(engine) as db:
        db.add(Principal(id=actor, username='phase1-concurrent', password_hash='test', role='ADMIN', active=True)); db.flush()
        db.add(PaymentMethod(id=actor, display_name='Concurrent card', category='CREDIT_CARD', is_active=True,
            created_by_principal_id=actor, updated_by_principal_id=actor)); db.flush()
        db.add(FundingAccount(id=actor, account_type='CREDIT_CARD', payment_method_id=actor, display_name='Concurrent',
            created_by_principal_id=actor, updated_by_principal_id=actor)); db.commit()
    old_owner=settings.purchasing_owner_principal_id; settings.purchasing_owner_principal_id=actor
    locked, release, entered, done = Event(), Event(), Event(), Event()
    def correction():
        with Session(engine) as db:
            adjust_balance(db, account_id=actor, balance=500, actor_id=actor, idempotency_key='correction')
            locked.set(); assert release.wait(5); db.commit()
    def payment():
        assert locked.wait(5)
        with Session(engine) as db:
            entered.set()
            record_actual_activity(db, account_id=actor, kind='PAYMENT', amount=50, actor_id=actor, idempotency_key='payment')
            db.commit(); done.set()
    try:
        with ThreadPoolExecutor(2) as pool:
            a=pool.submit(correction); b=pool.submit(payment)
            assert entered.wait(5)
            assert not done.wait(.1)
            release.set(); a.result(5); b.result(5)
        with Session(engine) as db:
            assert actual_card_balance(db, account_id=actor) == 450
    finally:
        release.set(); settings.purchasing_owner_principal_id=old_owner
        with engine.begin() as con:
            for table in ('funding_actual_activity','funding_balance_checkpoints','funding_accounts'):
                con.execute(text(f'DELETE FROM {table} WHERE '+('id' if table=='funding_accounts' else 'account_id')+'=:id'), {'id':actor})
            con.execute(text('DELETE FROM audit_log WHERE actor_principal_id=:id'), {'id':actor})
            con.execute(text('DELETE FROM payment_methods WHERE id=:id'), {'id':actor})
            con.execute(text('DELETE FROM principals WHERE id=:id'), {'id':actor})
        engine.dispose()


def test_adjust_balance_http_owner_csrf_and_duplicate_request(p1db):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.auth import Principal as Actor, Role
    from app.db import get_db
    from app.routers.v2_funding_reports import router
    from app.routers.v2_order_payments import feature_access, owner_access
    app=FastAPI(); app.include_router(router)
    app.dependency_overrides[get_db]=lambda:p1db
    app.dependency_overrides[feature_access]=lambda:None
    app.dependency_overrides[owner_access]=lambda:Actor(6001,'owner',Role.ADMIN,None,True)
    client=TestClient(app,follow_redirects=False)
    url='/v2/funding-accounts/6001/balance'
    payload={'balance':'777.50','idempotency_key':'http-request'}
    assert client.post(url,data=payload).status_code==403
    client.cookies.set('csrf_token','test-token')
    headers={'X-CSRF-Token':'test-token'}
    assert client.post(url,data=payload,headers=headers).status_code==303
    assert client.post(url,data=payload,headers=headers).status_code==303
    assert p1db.scalar(select(func.count()).select_from(FundingBalanceCheckpoint))==1
    assert actual_card_balance(p1db,account_id=6001)==Decimal('777.50')
    app.dependency_overrides[owner_access]=lambda:Actor(6002,'manager',Role.MANAGER,None,True)
    assert client.post(url,data={'balance':'1','idempotency_key':'intruder'},headers=headers).status_code==404
    assert actual_card_balance(p1db,account_id=6001)==Decimal('777.50')


def test_gateway_missing_inventory_and_incomplete_sales_are_unknown():
    from app.services.v2_ordering_square_gateway import SquareOrderingReadGateway
    def read(path,payload):
        if path.endswith('/orders/search'): return {'errors':[{'code':'INTERNAL_ERROR'}]}
        return {}
    result=SquareOrderingReadGateway(read).fetch(location_by_store={1:'L1'},variation_ids=['VAR1'],as_of=NOW)
    row=result.by_store_variation[(1,'VAR1')]
    assert row.current_on_hand is None and not row.inventory_valid
    assert not next(item for item in row.required_sources if item.source=='sales').available


def test_inventory_gateway_repeated_cursor_does_not_manufacture_coverage():
    from app.services.v2_ordering_square_gateway import SquareOrderingReadGateway
    gateway=SquareOrderingReadGateway(lambda path,payload:{'counts':[],'cursor':'loop'})
    with pytest.raises(ValueError,match='Repeated'):
        gateway.fetch_current_inventory_counts(location_ids=['L1'],variation_ids=['VAR1'])


def test_latest_partial_inventory_run_does_not_refresh_omitted_pair(p1db):
    from types import SimpleNamespace
    observation=SimpleNamespace(counted_quantity=Decimal(5),refreshed_at=NOW-timedelta(minutes=1))
    record_run(p1db,source='INVENTORY',status='PARTIAL',started_at=NOW-timedelta(seconds=1),completed_at=NOW,
        scope={'variation_ids':['VAR1'],'store_ids':[1]},evidence={'observed_pairs':[]})
    assert not inventory_pair_verified(p1db,variation_id='VAR1',store_id=1,observation=observation,as_of=NOW)


def test_physical_count_history_is_not_silently_treated_as_adjustments_only():
    from app.services.v2_ordering_square_gateway import SquareOrderingReadGateway
    def read(path,payload):
        if '/counts/' in path:
            return {'counts':[{'catalog_object_id':'VAR1','location_id':'L1','state':'IN_STOCK','quantity':'5'}]}
        if '/changes/' in path:
            return {'changes':[{'type':'PHYSICAL_COUNT','physical_count':{'catalog_object_id':'VAR1','location_id':'L1','quantity':'5'}}]}
        return {}
    row=SquareOrderingReadGateway(read).fetch(location_by_store={1:'L1'},variation_ids=['VAR1'],as_of=NOW).by_store_variation[(1,'VAR1')]
    assert not next(item for item in row.required_sources if item.source=='stockout_history').complete


def test_partial_receipts_pending_square_and_cancelled_supply(p1db):
    from app.models import Store, PurchaseOrder, PurchaseOrderLine, PurchaseOrderStoreAllocation, SquareSyncEvent
    from app.services.purchasing_inventory_service import load_incoming_supply
    db=p1db
    db.add_all([Vendor(id=6001,square_vendor_id='INCOMING',name='Vendor',active=True),
                Store(id=6001,name='Store',square_location_id='L1',active=True)]);db.flush()
    po=PurchaseOrder(id=6001,vendor_id=6001,status='IN_TRANSIT',created_by_principal_id=6001)
    db.add(po);db.flush()
    line=PurchaseOrderLine(id=6001,purchase_order_id=6001,variation_id='VAR1',sku='SKU1',item_name='Product',variation_name='Blue',ordered_qty=20,received_qty_total=8,unit_cost=3)
    db.add(line);db.flush()
    db.add(PurchaseOrderStoreAllocation(id=6001,purchase_order_line_id=6001,store_id=6001,allocated_qty=20,store_received_qty=8));db.flush()
    assert load_incoming_supply(db)[0].quantity == 12
    event=SquareSyncEvent(id=6001,purchase_order_id=6001,purchase_order_line_id=6001,store_id=6001,
        sync_type='PURCHASE_ORDER_RECEIVE_ADD_STOCK',idempotency_key='incoming',status='SUCCESS',last_attempt_at=NOW,request_payload={'received_qty_singles':'8'})
    db.add(event);db.flush()
    assert load_incoming_supply(db)[0].quantity==12
    po.status='SENT_TO_STORES';db.flush()
    assert not load_incoming_supply(db)  # Legacy terminal orders are historical, not incoming.
    po.status='CANCELLED';db.flush()
    assert not load_incoming_supply(db)


def test_assignment_and_legacy_invoice_fields_never_record_actual_charge(p1db):
    from app.models import PurchaseOrder, PurchaseOrderLine, VendorPaymentClassification
    from app.services.v2_order_payments_service import initialize_order_payment
    from app.services.purchase_order_admin_service import save_purchase_order_invoice
    db=p1db
    db.add(Vendor(id=6001,square_vendor_id='ASSIGNMENT',name='Vendor',active=True));db.flush()
    po=PurchaseOrder(id=6001,vendor_id=6001,status='IN_TRANSIT',created_by_principal_id=6001)
    db.add(po);db.flush()
    db.add(PurchaseOrderLine(id=6001,purchase_order_id=6001,variation_id='VAR1',sku='SKU1',item_name='Product',variation_name='Blue',ordered_qty=20,unit_cost=3));db.flush()
    classification=VendorPaymentClassification(id=6001,vendor_id=6001,payment_method_id=6001,payment_category='CREDIT_CARD',
        payment_method_label_snapshot='Test card',is_consignment=False,effective_date=NOW.date(),is_current=True,created_by_principal_id=6001)
    db.add(classification);db.flush()
    adjust_balance(db,account_id=6001,balance=1000,actor_id=6001,idempotency_key='issuer')
    initialize_order_payment(db,order=po,classification=classification,actor_id=6001,event_note='Assignment only')
    save_purchase_order_invoice(db,purchase_order_id=6001,payment_status='PAID',paid_date=NOW.date().isoformat(),paid_amount='60',difference_note='')
    assert actual_card_balance(db,account_id=6001)==1000
    assert db.scalar(select(func.count()).select_from(FundingActualActivity))==0
    assert db.scalar(select(func.count()).select_from(FundingLedgerEntry))==0


def test_known_charge_compatibility_form_has_stable_idempotency(p1db):
    from app.services.v2_funding_reports_service import record_ledger_entry
    from app.services.v2_order_payments_service import portal_today
    db=p1db
    adjust_balance(db,account_id=6001,balance=100,actor_id=6001,idempotency_key='issuer')
    kwargs=dict(account_id=6001,entry_type='MANUAL_CHARGE',direction='INCREASE',amount=Decimal('25'),
        effective_date=portal_today(),reason='Known issuer charge',internal_note='',actor_id=6001,idempotency_key='charge-form')
    first=record_ledger_entry(db,**kwargs)
    second=record_ledger_entry(db,**kwargs)
    assert first.id==second.id
    assert actual_card_balance(db,account_id=6001)==125
    assert db.scalar(select(func.count()).select_from(FundingActualActivity))==1


def test_known_payment_compatibility_retry_records_one_actual_effect(p1db):
    from app.models import PurchaseOrder, OrderPayment
    from app.services.v2_funding_reports_service import record_payment
    from app.services.v2_order_payments_service import portal_today
    db=p1db
    db.add(Vendor(id=6001,square_vendor_id='PAYMENT',name='Vendor',active=True));db.flush()
    db.add(PurchaseOrder(id=6001,vendor_id=6001,status='IN_TRANSIT',created_by_principal_id=6001));db.flush()
    db.add(OrderPayment(id=6001,purchase_order_id=6001,vendor_id=6001,payment_method_id=6001,
        status='UNPAID',financial_treatment='INVOICE',order_amount=50,order_cost_complete=True));db.flush()
    adjust_balance(db,account_id=6001,balance=1000,actor_id=6001,idempotency_key='issuer')
    kwargs=dict(account_id=6001,vendor_id=6001,entry_type='PAYMENT',amount=50,payment_date=portal_today(),
        payment_source='',confirmation_number='',reason='Known card repayment',internal_note='',allocations={},
        actor_id=6001,idempotency_key='payment-form')
    first=record_payment(db,**kwargs); second=record_payment(db,**kwargs)
    assert first.id==second.id
    assert actual_card_balance(db,account_id=6001)==950
    assert db.scalar(select(func.count()).select_from(FundingActualActivity))==1


def test_pausing_writers_cannot_restore_legacy_balance_authority(p1db, monkeypatch):
    from app.services.v2_funding_reports_service import tracked_balance
    db=p1db
    db.add(FundingLedgerEntry(account_id=6001,entry_type='INVENTORY_PURCHASE',direction='INCREASE',
        amount=9999,effective_date=NOW.date(),reason='Historical planning amount',created_by_principal_id=6001));db.flush()
    adjust_balance(db,account_id=6001,balance=123,actor_id=6001,idempotency_key='issuer')
    monkeypatch.setattr(settings,'purchasing_v3_phase1_enabled',False)
    assert tracked_balance(db,account_id=6001)==123
    with pytest.raises(PermissionError):
        record_actual_activity(db,account_id=6001,kind='CHARGE',amount=10,actor_id=6001,idempotency_key='paused')
