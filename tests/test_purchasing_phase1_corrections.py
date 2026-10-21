"""Corrective acceptance regressions. External traffic is always mocked."""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from threading import Barrier, Event
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.orm import Session

from test_purchasing_phase1 import p1db, database_template
from app.config import settings
from app.models import (Principal, PaymentMethod, FundingAccount, FundingActualActivity,
    FundingBalanceCheckpoint, Store, Vendor, PurchaseOrder, PurchaseOrderLine,
    PurchaseOrderStoreAllocation, SquareSyncEvent, ConsignmentSaleFact, PurchasingEvidenceRun)
from app.services.card_balance_service import actual_card_balance, adjust_balance, record_actual_activity
from app.services.purchasing_evidence_service import record_run, sales_coverage_for, inventory_pair_verified
from app.services.purchasing_inventory_service import (load_incoming_supply, receipt_observation_requirements,
    receipt_observation_verified)
from app.services.v2_consignment_facts_service import synchronize_square_facts, SquareOrdersReader

NOW = datetime.now(timezone.utc)


@pytest.fixture
def committed_card(p1db, monkeypatch):
    # p1db validates that the URL is an explicitly disposable local database.
    engine = create_engine(os.environ['P1_TEST_DATABASE_URL'])
    actor = 6201
    monkeypatch.setattr(settings, 'purchasing_owner_principal_id', actor)
    with Session(engine) as db:
        db.add(Principal(id=actor, username='correction-concurrency', password_hash='synthetic', role='ADMIN', active=True)); db.flush()
        db.add(PaymentMethod(id=actor, display_name='Concurrent card', category='CREDIT_CARD', is_active=True,
            created_by_principal_id=actor, updated_by_principal_id=actor)); db.flush()
        db.add(FundingAccount(id=actor, account_type='CREDIT_CARD', payment_method_id=actor, display_name='Concurrent card',
            created_by_principal_id=actor, updated_by_principal_id=actor)); db.flush()
        adjust_balance(db, account_id=actor, balance=100, actor_id=actor, idempotency_key='baseline'); db.commit()
    try:
        yield engine, actor
    finally:
        with engine.begin() as con:
            for table in ('funding_actual_activity', 'funding_balance_checkpoints', 'funding_accounts'):
                con.execute(text(f'DELETE FROM {table} WHERE '+('id' if table == 'funding_accounts' else 'account_id')+'=:id'), {'id': actor})
            con.execute(text('DELETE FROM audit_log WHERE actor_principal_id=:id'), {'id':actor})
            con.execute(text('DELETE FROM payment_methods WHERE id=:id'), {'id':actor})
            con.execute(text('DELETE FROM principals WHERE id=:id'), {'id':actor})
        engine.dispose()


@pytest.mark.parametrize('operation,readers,expected', [('checkpoint',1,500), ('charge',1,125), ('payment',1,65), ('mixed',3,490)])
def test_balance_read_uses_one_committed_snapshot(committed_card, operation, readers, expected):
    engine, actor = committed_card
    captured, release = Barrier(readers + 1), Event()
    statement_counts = []
    def pause_after_execute(conn, cursor, statement, parameters, context, executemany):
        if conn.info.get('balance_reader') and 'funding_balance_checkpoints' in statement:
            conn.info['balance_reader'] = False
            captured.wait(10)
            assert release.wait(10)
    event.listen(engine, 'after_cursor_execute', pause_after_execute)
    def reader():
        with Session(engine) as db:
            conn = db.connection(); conn.info['balance_reader'] = True
            calls = []
            def count(*args): calls.append(1)
            event.listen(conn, 'before_cursor_execute', count)
            try:
                result = actual_card_balance(db, account_id=actor)
                statement_counts.append(len(calls))
                return result
            finally:
                conn.info.pop('balance_reader', None)
    try:
        with ThreadPoolExecutor(readers) as pool:
            futures = [pool.submit(reader) for _ in range(readers)]
            captured.wait(10)  # Readers have executed, but have not consumed their result.
            try:
                with Session(engine) as db:
                    if operation in {'checkpoint', 'mixed'}:
                        adjust_balance(db, account_id=actor, balance=500, actor_id=actor, idempotency_key='new-baseline')
                    if operation in {'charge', 'mixed'}:
                        record_actual_activity(db, account_id=actor, kind='CHARGE', amount=25, actor_id=actor, idempotency_key='charge')
                    if operation in {'payment', 'mixed'}:
                        record_actual_activity(db, account_id=actor, kind='PAYMENT', amount=35, actor_id=actor, idempotency_key='payment')
                    db.commit()
            finally:
                release.set()
            assert [f.result(10) for f in futures] == [100] * readers
        assert statement_counts == [1] * readers
        with Session(engine) as db:
            assert actual_card_balance(db, account_id=actor) == expected
            adjust_balance(db, account_id=actor, balance=100, actor_id=actor, idempotency_key='baseline')
            assert actual_card_balance(db, account_id=actor) == expected  # Checkpoint replay cannot reset it.
    finally:
        release.set(); event.remove(engine, 'after_cursor_execute', pause_after_execute)


def test_multiple_readers_and_writers_serialize_without_mixed_balances(committed_card):
    engine, actor = committed_card
    locked, release, entered = Event(), Event(), Barrier(3)
    def checkpoint():
        with Session(engine) as db:
            adjust_balance(db, account_id=actor, balance=500, actor_id=actor, idempotency_key='checkpoint')
            locked.set(); assert release.wait(10); db.commit()
    def activity(kind, amount):
        assert locked.wait(10); entered.wait(10)
        with Session(engine) as db:
            record_actual_activity(db, account_id=actor, kind=kind, amount=amount, actor_id=actor, idempotency_key=kind)
            db.commit()
    def read():
        with Session(engine) as db: return actual_card_balance(db, account_id=actor)
    with ThreadPoolExecutor(6) as pool:
        a = pool.submit(checkpoint)
        b = pool.submit(activity, 'CHARGE', 5); c = pool.submit(activity, 'PAYMENT', 3)
        try:
            entered.wait(10)
            assert list(pool.map(lambda _: read(), range(3))) == [100]*3
        finally: release.set()
        a.result(10); b.result(10); c.result(10)
    with Session(engine) as db:
        assert actual_card_balance(db, account_id=actor) == 502
        assert db.scalar(select(func.count()).select_from(FundingActualActivity).where(FundingActualActivity.account_id==actor)) == 2


def _store(db):
    db.add(Store(id=6001, name='Andresen', square_location_id='L1', active=True)); db.flush()


def _coverage(db, start, end, evidence, status='COMPLETE'):
    record_run(db, source='SALES', status=status, started_at=NOW-timedelta(seconds=1), completed_at=NOW,
        scope={'start_at':start.isoformat(),'end_at':end.isoformat(),'location_ids':['L1']}, evidence=evidence)


@pytest.mark.parametrize('evidence', [{}, {'source_filter':'UPDATED_AT','source_complete':True,'coverage_contract':2},
    {'source_filter':'CLOSED_AT','source_complete':False,'coverage_contract':2}])
def test_update_sync_and_unknown_contract_never_prove_sales_period(p1db, evidence):
    start, end = NOW-timedelta(days=7), NOW-timedelta(days=1)
    _coverage(p1db,start,end,evidence)
    assert not sales_coverage_for(p1db,start_at=start,end_at=end,location_ids=['L1'],as_of=NOW)


def test_historical_coverage_gaps_partial_runs_and_boundaries(p1db):
    start, end = NOW-timedelta(days=7), NOW-timedelta(days=1)
    proof={'source_filter':'CLOSED_AT','source_complete':True,'coverage_contract':2}
    _coverage(p1db,start,start+timedelta(days=2),proof)
    _coverage(p1db,start+timedelta(days=3),end,proof)
    assert not sales_coverage_for(p1db,start_at=start,end_at=end,location_ids=['L1'],as_of=NOW)
    _coverage(p1db,start,end,proof,status='PARTIAL')
    assert not sales_coverage_for(p1db,start_at=start,end_at=end,location_ids=['L1'],as_of=NOW)
    _coverage(p1db,start+timedelta(days=2),start+timedelta(days=3),proof)
    assert sales_coverage_for(p1db,start_at=start,end_at=end,location_ids=['L1'],as_of=NOW)
    assert not sales_coverage_for(p1db,start_at=start-timedelta(microseconds=1),end_at=end,location_ids=['L1'],as_of=NOW)
    assert not sales_coverage_for(p1db,start_at=start,end_at=end+timedelta(microseconds=1),location_ids=['L1'],as_of=NOW)
    assert not sales_coverage_for(p1db,start_at=start,end_at=end,location_ids=['L2'],as_of=NOW)
    assert not sales_coverage_for(p1db,start_at=end,end_at=end,location_ids=['L1'],as_of=NOW)


@pytest.mark.parametrize('has_sale', [False, True])
def test_complete_closed_period_includes_later_updated_sale_and_true_empty(p1db, has_sale):
    _store(p1db)
    start, end = NOW-timedelta(days=7), NOW-timedelta(days=1)
    from app.services.v2_consignment_facts_service import create_assignment
    p1db.add(Vendor(id=6001,square_vendor_id='LATE-VENDOR',name='Vendor',active=True));p1db.flush()
    create_assignment(p1db,vendor_id=6001,variation_id='VAR1',is_consignment=False,start_at=start,
        end_at=None,actor_id=6001)
    calls=[]
    class Reader:
        def search(self, **kw):
            basis=kw.get('date_filter','UPDATED_AT'); calls.append(basis)
            if basis=='CLOSED_AT' and has_sale:
                yield [{'id':'later-update','state':'COMPLETED','version':1,'location_id':'L1','closed_at':(start+timedelta(days=1)).isoformat(),
                    'updated_at':NOW.isoformat(),'line_items':[{'uid':'line','catalog_object_id':'VAR1','name':'Product','quantity':'2'}]}]
            else: yield []
    synchronize_square_facts(p1db,start_at=start,end_at=end,actor_id=6001,reader=Reader());p1db.flush()
    assert calls==['UPDATED_AT','CLOSED_AT']
    assert sales_coverage_for(p1db,start_at=start,end_at=end,location_ids=['L1'],as_of=datetime.now(timezone.utc)) is True
    assert p1db.scalar(select(func.count()).select_from(ConsignmentSaleFact)) == int(has_sale)


@pytest.mark.parametrize('failure', ['interrupted','pagination'])
def test_failed_closed_period_never_certifies_or_commits_partial_facts(p1db, failure):
    _store(p1db);start,end=NOW-timedelta(days=7),NOW-timedelta(days=1)
    class Reader:
        def search(self, **kw):
            if kw.get('date_filter')=='CLOSED_AT':
                yield [{'id':'partial-sale','state':'COMPLETED','version':1,'location_id':'L1','closed_at':(start+timedelta(days=1)).isoformat(),
                    'line_items':[{'uid':'line','catalog_object_id':'VAR1','name':'Product','quantity':'2'}]}]
                raise RuntimeError(failure)
            yield []
    with pytest.raises(RuntimeError,match=failure):
        synchronize_square_facts(p1db,start_at=start,end_at=end,actor_id=6001,reader=Reader())
    assert not sales_coverage_for(p1db,start_at=start,end_at=end,location_ids=['L1'],as_of=datetime.now(timezone.utc))
    assert p1db.scalar(select(func.count()).select_from(PurchasingEvidenceRun)) == 0
    assert p1db.scalar(select(func.count()).select_from(ConsignmentSaleFact)) == 0


def test_square_reader_uses_matching_closed_filter_and_rejects_repeated_cursor(monkeypatch):
    monkeypatch.setattr(settings,'square_access_token','synthetic')
    payloads=[]
    class Response:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def read(self): return json.dumps({'orders':[],'cursor':'repeated'}).encode()
    def open_request(req, **kw): payloads.append(json.loads(req.data));return Response()
    with patch('app.services.square_ordering_data_service.urlopen',side_effect=open_request):
        with pytest.raises(RuntimeError,match='Repeated'):
            list(SquareOrdersReader().search(location_ids=['L1'],start_at=NOW-timedelta(days=2),end_at=NOW,date_filter='CLOSED_AT'))
    assert all(p['query']['sort']['sort_field']=='CLOSED_AT' and 'closed_at' in p['query']['filter']['date_time_filter'] for p in payloads)


def _receipt(db, quantities=(8,), statuses=None, timestamp=True, cancelled=False):
    _store(db)
    db.add(Vendor(id=6001,square_vendor_id='V1',name='Synthetic vendor',active=True));db.flush()
    db.add(PurchaseOrder(id=6001,vendor_id=6001,status='CANCELLED' if cancelled else 'IN_TRANSIT',created_by_principal_id=6001));db.flush()
    db.add(PurchaseOrderLine(id=6001,purchase_order_id=6001,variation_id='VAR1',sku='SKU1',item_name='Product',variation_name='Blue',ordered_qty=20,received_qty_total=8));db.flush()
    db.add(PurchaseOrderStoreAllocation(id=6001,purchase_order_line_id=6001,store_id=6001,allocated_qty=20,store_received_qty=8));db.flush()
    for n,qty in enumerate(quantities):
        db.add(SquareSyncEvent(purchase_order_id=6001,purchase_order_line_id=6001,store_id=6001,
            sync_type='PURCHASE_ORDER_RECEIVE_ADD_STOCK',idempotency_key=f'receipt-{n}',status=(statuses or ['SUCCESS']*len(quantities))[n],
            last_attempt_at=NOW+timedelta(seconds=n) if timestamp else None, request_payload={'received_qty_singles':str(qty)}))
    db.flush()
    return receipt_observation_requirements(db,variation_ids=['VAR1'],store_ids=[6001])


@pytest.mark.parametrize('case,expected', [(case,True) for case in ('before','after','cached','partial','failed','unknown','multiple','legacy','mismatch','cancelled')])
def test_live_count_is_independent_of_legacy_receipt_posting_history(p1db, case, expected):
    quantities=(3,5) if case in {'partial','multiple'} else (7,) if case=='mismatch' else (8,)
    statuses=['SUCCESS','FAILED'] if case=='partial' else ['FAILED'] if case=='failed' else ['PENDING'] if case=='unknown' else None
    requirements=_receipt(p1db,quantities,statuses,timestamp=case!='legacy',cancelled=case=='cancelled')
    calculated=NOW-timedelta(seconds=1) if case in {'before','cached','cancelled'} else NOW+timedelta(seconds=2)
    observed=NOW-timedelta(seconds=1) if case=='before' else NOW+timedelta(seconds=3)
    assert receipt_observation_verified(requirements,store_id=6001,variation_id='VAR1',observed_at=observed,calculated_at=calculated) is expected
    if case=='cancelled': assert not load_incoming_supply(p1db)
    elif case in {'after','multiple'}: assert load_incoming_supply(p1db)[0].quantity==12
    elif case in {'partial','failed','unknown','legacy','mismatch'}: assert load_incoming_supply(p1db)[0].quantity == 12
    observation=SimpleNamespace(counted_quantity=Decimal(18),refreshed_at=observed,source_calculated_at=calculated)
    run=SimpleNamespace(scope={'variation_ids':['VAR1'],'store_ids':[6001]},evidence={'observed_pairs':[['VAR1',6001]]},status='COMPLETE',completed_at=observed)
    assert inventory_pair_verified(p1db,variation_id='VAR1',store_id=6001,observation=observation,as_of=observed,
        runs=[run],receipt_requirements=requirements) is expected


@pytest.mark.parametrize('default_supplier', [False, True])
def test_new_supplier_does_not_hide_another_suppliers_physical_incoming(p1db, monkeypatch, default_supplier):
    from app.models import VendorSkuConfig, ParLevelSource
    from app.services import purchase_order_generation_service as generation
    from app.services.purchase_order_math_service import OrderingMathParams
    _store(p1db)
    p1db.add_all([Vendor(id=6001,square_vendor_id='OLD',name='Old supplier',active=True),
                 Vendor(id=6002,square_vendor_id='NEW',name='New supplier',active=True)]);p1db.flush()
    p1db.add(PurchaseOrder(id=6001,vendor_id=6001,status='IN_TRANSIT',created_by_principal_id=6001));p1db.flush()
    p1db.add(PurchaseOrderLine(id=6001,purchase_order_id=6001,variation_id='VAR1',sku='SKU1',item_name='Product',variation_name='Blue',ordered_qty=8));p1db.flush()
    p1db.add(PurchaseOrderStoreAllocation(purchase_order_line_id=6001,store_id=6001,allocated_qty=8,store_received_qty=0))
    p1db.add(VendorSkuConfig(vendor_id=6002,sku='SKU1',square_variation_id='VAR1',unit_cost=3,active=True,is_default_vendor=default_supplier,pack_size=1,min_order_qty=0));p1db.flush()
    par=SimpleNamespace(manual_par_level=20,manual_stock_up_level=20,par_source=ParLevelSource.MANUAL)
    monkeypatch.setattr(generation,'_par_levels_by_vendor_store_sku',lambda *a,**kw:{(6002,6001,'SKU1'):par})
    monkeypatch.setattr(generation,'resolve_effective_math_params',lambda *a,**kw:OrderingMathParams(1,2,30))
    rows=generation.generate_vendor_scoped_recommendations(p1db,vendor_ids=[6002],history_loader=lambda *a:[1]*30,
        on_hand_loader=lambda *a:Decimal(10),include_non_default_vendor_skus=True)
    assert len(rows)==1 and rows[0].result.rounded_recommended_qty==2


def test_velocity_and_stock_coverage_deduct_shared_incoming_once(p1db, monkeypatch):
    from app.services import inventory_velocity_report_service as velocity
    inventory={'VAR1':velocity.VelocityInventory('VAR1','SKU1','Product','Category','Vendor',Decimal(3),False,{6001:Decimal(0)},6001)}
    sales=[velocity.VelocitySale(NOW.date(),'VAR1','L1',Decimal(30),Decimal(90))]
    monkeypatch.setattr(velocity,'fetch_current_inventory',lambda *a,**kw:(inventory,[(6001,'Store')],{'L1':6001}))
    monkeypatch.setattr(velocity,'fetch_sales_data',lambda *a,**kw:sales)
    monkeypatch.setattr(velocity,'fetch_inventory_stock_events',lambda *a,**kw:[])
    supply=SimpleNamespace(variation_id='VAR1',store_id=6001,quantity=8,sku='SKU1')
    monkeypatch.setattr(velocity,'load_incoming_supply',lambda *a,**kw:(supply,))
    report=velocity.build_inventory_velocity_report(p1db,days=30,end_date=NOW.date(),target_days=30)
    assert report.rows[0].recommended_reorder_quantity==22
    assert report.rows[0].store_splits[0].recommended_purchase_quantity==22
    purchase=velocity.build_stock_coverage_purchase_report(p1db,days=30,end_date=NOW.date(),target_months=Decimal(1))
    assert purchase.rows[0].recommended_purchase_quantity==22
    supply.quantity=None
    with pytest.raises(ValueError,match='UNKNOWN'):
        velocity.build_inventory_velocity_report(p1db,days=30,end_date=NOW.date())


@pytest.mark.parametrize('with_template', [False,True])
def test_saved_po_generates_actual_pdf_with_quantities_template_and_removed_line_exclusion(p1db, monkeypatch, tmp_path, with_template):
    from app.models import VendorSkuConfig, PurchaseOrderPdfTemplate
    from app.services import purchase_order_admin_service as orders
    from reportlab.pdfgen.canvas import Canvas
    _store(p1db)
    p1db.add(Vendor(id=6001,square_vendor_id='PDF-VENDOR',name='Synthetic PDF Vendor',active=True));p1db.flush()
    p1db.add(PurchaseOrder(id=6001,vendor_id=6001,status='IN_TRANSIT',created_by_principal_id=6001));p1db.flush()
    p1db.add(VendorSkuConfig(vendor_id=6001,sku='PDF1',square_variation_id='PDF-V1',pack_size=5,min_order_qty=0,active=True,is_default_vendor=True))
    for i in range(35 if with_template else 1):
        p1db.add(PurchaseOrderLine(purchase_order_id=6001,variation_id=f'PDF-V{i+1}',sku=f'PDF{i+1}',
            item_name=f'Saved Product {i+1:02}',variation_name='Blue',ordered_qty=12,unit_cost=Decimal('3.25')))
    p1db.add(PurchaseOrderLine(purchase_order_id=6001,variation_id='REMOVED',sku='REMOVED',item_name='Removed Product',
        variation_name='Hidden',ordered_qty=99,removed=True))
    if with_template:
        p1db.add(PurchaseOrderPdfTemplate(name='Synthetic template',vendor_id=6001,active=True,
            legal_disclaimer='Synthetic terms: verify delivered quantities.',updated_by_principal_id=6001))
    p1db.flush()
    # Isolate the existing generator's filesystem root; no saved PDF is overwritten.
    fake_module=tmp_path/'app'/'services'/'purchase_order_admin_service.py'
    monkeypatch.setattr(orders,'Path',lambda value:fake_module if value==orders.__file__ else Path(value))
    # Compression changes storage only. All page drawing runs through real ReportLab.
    def uncompressed_canvas(*a,**kw): return Canvas(*a,**kw,pageCompression=0)
    with patch('reportlab.pdfgen.canvas.Canvas',side_effect=uncompressed_canvas):
        relative=orders._generate_purchase_order_pdf(p1db,purchase_order_id=6001)
    pdf=tmp_path/relative
    data=pdf.read_bytes()
    assert data.startswith(b'%PDF-')
    assert b'Synthetic PDF Vendor' in data and b'Purchase Order #: 6001' in data
    assert b'Saved Product 01' in data and b'Blue' in data
    assert b'12 \\(3 pk @ 5\\)' in data
    assert b'Removed Product' not in data
    assert (b'Synthetic terms:' in data) is with_template
    if with_template: assert b'Saved Product 35' in data
    evidence=os.getenv('P1_PDF_EVIDENCE_DIR')
    if evidence:
        destination=Path(evidence);destination.mkdir(parents=True,exist_ok=True)
        (destination/('synthetic-po-template.pdf' if with_template else 'synthetic-po-default.pdf')).write_bytes(data)


def test_financial_account_and_report_routes_render_with_actual_authority(p1db, monkeypatch, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.main import app as application
    from app.auth import Principal as Actor, Role
    from app.db import get_db
    from app.models import FundingLedgerEntry, FundingReport
    from app.routers import v2_funding_reports as routes
    from app.routers.v2_order_payments import feature_access, owner_access
    actor=Actor(6001,'owner',Role.ADMIN,None,True)
    monkeypatch.setattr('app.routers.v2_order_payments._visible_navigation',lambda request:[])
    app=FastAPI();app.state.templates=application.state.templates;app.include_router(routes.router)
    app.dependency_overrides[get_db]=lambda:p1db
    app.dependency_overrides[feature_access]=lambda:actor
    app.dependency_overrides[owner_access]=lambda:actor
    p1db.add(Vendor(id=6001,square_vendor_id='REPORT-VENDOR',name='Report vendor',active=True));p1db.flush()
    p1db.add(FundingLedgerEntry(account_id=6001,entry_type='INVENTORY_PURCHASE',direction='INCREASE',amount=99999,
        effective_date=NOW.date(),reason='Historical planning only',created_by_principal_id=6001))
    p1db.add(FundingReport(id=6001,account_id=6001,vendor_id=6001,report_number='LOCAL-ACCEPTANCE',
        account_name_snapshot='Test card',account_type_snapshot='CREDIT_CARD',sales_start_date=NOW.date()-timedelta(days=7),
        sales_end_date=NOW.date()-timedelta(days=1),status='FINALIZED',calculated_cogs=23,created_by_principal_id=6001,
        finalized_snapshot={'legacy':True}));p1db.flush()
    p1db.add(FundingReport(id=6002,account_id=6001,vendor_id=None,report_number='LOCAL-COMBINED',
        account_name_snapshot='Test card',account_type_snapshot='CREDIT_CARD',sales_start_date=NOW.date()-timedelta(days=7),
        sales_end_date=NOW.date()-timedelta(days=1),status='FINALIZED',calculated_cogs=23,created_by_principal_id=6001,
        warning_summary={'combined_report':{'member_report_ids':[6001]}},finalized_snapshot={'legacy':True}));p1db.flush()
    client=TestClient(app)
    before=client.get('/v2/funding-accounts/6001')
    assert before.status_code==200 and 'UNKNOWN' in before.text
    listing=client.get('/v2/funding-accounts')
    assert listing.status_code==200 and 'UNKNOWN' in listing.text
    adjust_balance(p1db,account_id=6001,balance='777.50',actor_id=6001,idempotency_key='render-baseline')
    for path in ['/v2/funding-accounts','/v2/funding-accounts/6001','/v2/funding-accounts/6001/reports/6001','/v2/funding-accounts/6001/reports/6002']:
        response=client.get(path)
        assert response.status_code==200
        assert 'Traceback' not in response.text
        if '/reports/' not in path:
            assert '$777.50' in response.text and 'Actual tracked card balance' in response.text
            assert '$99,999.00' not in response.text or 'Historical planning only' in response.text
        else: assert ('LOCAL-COMBINED' if path.endswith('/6002') else 'LOCAL-ACCEPTANCE') in response.text and '$23.00' in response.text
        evidence=os.getenv('P1_HTML_EVIDENCE_DIR')
        if evidence:
            output=Path(evidence);output.mkdir(parents=True,exist_ok=True)
            (output/(path.strip('/').replace('/','-')+'.html')).write_text(response.text)
    assert actual_card_balance(p1db,account_id=6001)==Decimal('777.50')
    assert p1db.scalar(select(func.count()).select_from(FundingActualActivity))==0


@pytest.mark.parametrize('after_receipt', [False, True])
def test_live_standard_and_velocity_inventory_reader_enforces_receipt_barrier(p1db, monkeypatch, after_receipt):
    from app.services import square_ordering_data_service as source
    _receipt(p1db)
    monkeypatch.setattr(source,'_now',lambda:NOW+timedelta(seconds=3))
    calculated=NOW+timedelta(seconds=2) if after_receipt else NOW-timedelta(seconds=1)
    monkeypatch.setattr(source,'_square_post',lambda *a,**kw:{'counts':[{'location_id':'L1','catalog_object_id':'VAR1',
        'state':'IN_STOCK','quantity':'18','calculated_at':calculated.isoformat()}]})
    proof={}
    assert source.fetch_on_hand_by_store_variation(p1db,variation_ids=['VAR1'],evidence_by_pair=proof)=={(6001,'VAR1'):18}
    assert proof[(6001,'VAR1')][1]==calculated


def test_standard_snapshot_is_rechecked_when_receiving_finishes_after_snapshot(p1db):
    from app.services.square_ordering_data_service import SquareOrderingSnapshot, SquareSkuMeta
    snapshot=SquareOrderingSnapshot({(6001,'SKU1'):SquareSkuMeta('VAR1','SKU1',None,'Product','Blue',Decimal(3),None)},
        {(6001,'SKU1'):Decimal(10)},{},inventory_observed_at=NOW-timedelta(seconds=1),
        calculated_at_by_store_sku={(6001,'SKU1'):NOW-timedelta(seconds=2)})
    _receipt(p1db)
    snapshot.reconcile_receipts(p1db)
    assert snapshot.on_hand_loader(6001,'SKU1') == 10


@pytest.mark.parametrize('after_receipt', [False,True])
def test_ordering_intelligence_consumes_receipt_barrier(p1db, after_receipt):
    from app.models import VendorSkuConfig
    from app.services.v2_ordering_data_coordinator import build_ordering_dashboard
    from app.services.v2_ordering_square_gateway import SquareStoreSkuData, SquareOrderingReadResult
    from app.services.v2_ordering_policy_service import DataSourceEvidence
    _receipt(p1db)
    p1db.add(VendorSkuConfig(vendor_id=6001,sku='SKU1',square_variation_id='VAR1',unit_cost=3,pack_size=1,
        min_order_qty=0,active=True,is_default_vendor=True));p1db.flush()
    observed=NOW+timedelta(seconds=3)
    calculated=NOW+timedelta(seconds=2) if after_receipt else NOW-timedelta(seconds=1)
    class Gateway:
        def fetch(self, **kw):
            remote=SquareStoreSkuData(6001,'VAR1',Decimal(18),True,(),(),
                (DataSourceEvidence('inventory',observed),DataSourceEvidence('sales',observed)),(),calculated)
            return SquareOrderingReadResult({}, {(6001,'VAR1'):remote})
    result=build_ordering_dashboard(p1db,store_ids=(6001,),gateway=Gateway(),as_of=observed).recommendations[0]
    assert result.current_on_hand==18
    assert result.incoming_supply==12



@pytest.mark.parametrize('after_receipt', [False,True])
def test_replenishment_reads_live_square_and_ignores_receipt_timestamp(p1db, after_receipt, monkeypatch):
    from app.models import VendorSkuConfig, OrderingCurrentInventory, OrderingInventoryRefreshRun
    from app.services.v2_replenishment_report_service import run_replenishment_report, build_replenishment_preview
    _receipt(p1db)
    observed=NOW+timedelta(seconds=3)
    calculated=NOW+timedelta(seconds=2) if after_receipt else NOW-timedelta(seconds=1)
    p1db.add(VendorSkuConfig(vendor_id=6001,sku='SKU1',square_variation_id='VAR1',unit_cost=3,pack_size=1,
        min_order_qty=0,active=True,is_default_vendor=True))
    refresh=OrderingInventoryRefreshRun(correlation_id='corrective-refresh',result='COMPLETE',
        expected_variation_count=1,active_store_count=1,expected_pair_count=1,covered_pair_count=1,
        missing_pair_count=0,square_request_count=1,started_at=observed,completed_at=observed,refreshed_by_principal_id=6001)
    # Use SQL column defaults for the established refresh-run shape.
    p1db.add(refresh);p1db.flush()
    p1db.add(OrderingCurrentInventory(square_variation_id='VAR1',store_id=6001,square_location_id='L1',counted_quantity=18,
        source_calculated_at=calculated,refreshed_at=observed,freshness_state='FRESH',refresh_run_id=refresh.id));p1db.flush()
    record_run(p1db,source='INVENTORY',status='COMPLETE',started_at=observed,completed_at=observed,
        scope={'variation_ids':['VAR1'],'store_ids':[6001]},evidence={'observed_pairs':[['VAR1',6001]]})
    record_run(p1db,source='SALES',status='COMPLETE',started_at=observed,completed_at=observed,
        scope={'start_at':(observed-timedelta(days=70)).isoformat(),'end_at':observed.isoformat(),'location_ids':['L1']},
        evidence={'source_filter':'CLOSED_AT','source_complete':True,'coverage_contract':2})
    from app.services import square_ordering_data_service as source
    monkeypatch.setattr(source, 'fetch_on_hand_by_store_variation', lambda *a, **kw: {(6001, 'VAR1'): Decimal(23)})
    from app.services import purchasing_inventory_service as inventory_source
    monkeypatch.setattr(inventory_source, 'purchasing_store_locations', lambda db: ({6001:'L1'}, ()))
    report=run_replenishment_report(p1db,start_date=NOW.date()-timedelta(days=7),end_date=NOW.date(),store_ids=[6001],
        exclude_no_recent_sales=False,now=observed,as_of=NOW.date())
    row=report.rows[0]
    assert row.current_on_hand==23
    assert row.incoming_quantity==12
    replacement=build_replenishment_preview(report,vendor_id=6001,mode='replace_sales')
    assert 'live inventory and qualified incoming are included' in replacement.lines[0].adjustment_note
    assert build_replenishment_preview(report,vendor_id=6001,mode='target_weeks',final_quantities={row.key:1})


def test_active_financial_readiness_rejects_legacy_updated_at_envelope(p1db):
    from app.models import ConsignmentSalesSyncState
    from app.services.v2_funding_reports_service import funding_report_source_readiness, PORTAL_TIMEZONE
    from datetime import time
    _store(p1db)
    first,last=NOW.date()-timedelta(days=14),NOW.date()-timedelta(days=7)
    start=datetime.combine(first,time.min,PORTAL_TIMEZONE).astimezone(timezone.utc)
    end=datetime.combine(last+timedelta(days=1),time.min,PORTAL_TIMEZONE).astimezone(timezone.utc)
    p1db.add(ConsignmentSalesSyncState(id=1,last_result='COMPLETE',last_successful_start_at=start,
        last_successful_through_at=end,last_successful_at=NOW,updated_by_principal_id=6001));p1db.flush()
    _coverage(p1db,start,end,{'source_filter':'UPDATED_AT','source_complete':True,'coverage_contract':2});p1db.flush()
    assert funding_report_source_readiness(p1db,start_date=first,end_date=last)['blockers']==['SALES_PERIOD_COVERAGE_UNVERIFIED']
    _coverage(p1db,start,end,{'source_filter':'CLOSED_AT','source_complete':True,'coverage_contract':2});p1db.flush()
    assert not funding_report_source_readiness(p1db,start_date=first,end_date=last)['blockers']
