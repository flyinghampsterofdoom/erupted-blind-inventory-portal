"""Owner-authorized extension: real disposable PostgreSQL, mocked Square only."""
import copy
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from threading import Event
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import create_engine, select, text, func
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (Principal, Vendor, Store, PurchaseOrder, PurchaseOrderLine,
    PurchaseOrderStoreAllocation, PurchaseOrderReceipt, PurchaseOrderReceiptAllocation,
    SquareSyncEvent, SquareSyncStatus, PurchaseOrderStatus, FundingLedgerEntry)
from app.schema_contract import _alembic_config
from app.services import purchase_order_admin_service as receiving
from app.services.purchasing_inventory_service import load_incoming_supply, inventory_position, purchasing_store_locations


@pytest.fixture(scope='module')
def database_template():
    raw = os.getenv('P1_TEST_DATABASE_URL')
    if not raw:
        pytest.skip('Requires explicit disposable local PostgreSQL')
    url = make_url(raw)
    assert url.database.startswith('erupted_purchasing_p1_') and url.host in {None,'localhost','127.0.0.1'}
    admin = create_engine(url.set(database='postgres'), isolation_level='AUTOCOMMIT')
    name = 'erupted_purchasing_p1_template_'+uuid4().hex
    with admin.connect() as con:
        con.execute(text(f'CREATE DATABASE "{name}"'))
    command.upgrade(_alembic_config(url.set(database=name).render_as_string(hide_password=False)), 'head')
    try:
        yield admin, url, name
    finally:
        with admin.connect() as con:
            con.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture
def receiving_db(database_template, monkeypatch):
    admin, url, template = database_template
    name = 'erupted_purchasing_p1_receipt_'+uuid4().hex
    with admin.connect() as con:
        con.execute(text(f'CREATE DATABASE "{name}" TEMPLATE "{template}"'))
    engine = create_engine(url.set(database=name))
    monkeypatch.setattr(settings, 'purchasing_owner_principal_id', 1)
    monkeypatch.setattr(settings, 'purchasing_v3_phase1_enabled', True)
    # Fail closed if an implementation accidentally reaches a real HTTP path.
    def unexpected(*args, **kwargs):
        raise AssertionError('An unmocked Square operation was attempted')
    monkeypatch.setattr(receiving, '_square_post', unexpected)
    with Session(engine) as db:
        db.add_all([Principal(id=1, username='owner', password_hash='test', role='ADMIN', active=True),
            Principal(id=2, username='admin', password_hash='test', role='ADMIN', active=True),
            Principal(id=3, username='manager', password_hash='test', role='MANAGER', active=True)])
        db.add(Vendor(id=1,name='Synthetic vendor',square_vendor_id='V1',active=True))
        db.add_all([Store(id=i,name=name,square_location_id=f'L{i}',active=True) for i,name in enumerate(
            ('Andresen','Hwy 99','SR503','Longview'),1)])
        db.flush()
        db.add(PurchaseOrder(id=1,vendor_id=1,status='IN_TRANSIT',created_by_principal_id=1))
        db.flush()
        db.add(PurchaseOrderLine(id=1,purchase_order_id=1,variation_id='VAR1',sku='SKU1',item_name='Product',
            variation_name='Blue',ordered_qty=80,received_qty_total=0,in_transit_qty=80))
        db.flush()
        db.add_all([PurchaseOrderStoreAllocation(id=i,purchase_order_line_id=1,store_id=i,
            expected_qty=20,allocated_qty=20,store_received_qty=0) for i in range(1,5)])
        for table in ('purchase_order_lines', 'purchase_order_store_allocations'):
            db.execute(text(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), 100, true)"))
        db.commit()
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as con:
            con.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))


def save(db, qty=8, store=1, key=None):
    return receiving.save_purchase_order_received_quantities(db,purchase_order_id=1,
        received_qty_by_line_store={(1,store):qty},actor_principal_id=1,request_key=key or uuid4().hex)


def push(db):
    return receiving.receive_purchase_order(db,purchase_order_id=1,actor_principal_id=1)


def root_id(db):
    return db.scalar(select(PurchaseOrderReceiptAllocation.id).where(PurchaseOrderReceiptAllocation.event_kind=='DELIVERY').order_by(PurchaseOrderReceiptAllocation.id))


def test_repeated_partial_deliveries_duplicate_protection_and_closure(receiving_db):
    with Session(receiving_db) as db:
        save(db,key='delivery-1'); save(db,key='delivery-1');save(db,3,key='delivery-2')
        assert db.get(PurchaseOrderStoreAllocation,1).store_received_qty==11
        assert db.scalar(select(func.count()).select_from(PurchaseOrderReceiptAllocation))==2
        assert next(x for x in load_incoming_supply(db) if x.store_id==1).quantity==9
        receiving.close_purchase_order_remainder(db,purchase_order_id=1,line_id=1,store_id=1,quantity=9,
            actor_principal_id=1,request_key='close',reason='')
        assert next(x for x in load_incoming_supply(db) if x.store_id==1).quantity==0
        assert db.scalar(select(func.count()).select_from(SquareSyncEvent))==0
        assert db.scalar(select(func.count()).select_from(FundingLedgerEntry))==0
        with pytest.raises(ValueError,match='different input'):
            save(db,9,key='delivery-1')


def test_explicit_push_keeps_partial_order_open_and_durable_payload(receiving_db,monkeypatch):
    sent=[]
    def post(path,payload,**kwargs):
        assert kwargs=={'inventory_quantity_write':True}
        with Session(receiving_db) as observer:
            op=observer.scalar(select(SquareSyncEvent))
            assert op.request_payload==payload and op.attempt_count==1
            assert observer.scalar(select(PurchaseOrderReceiptAllocation)).square_sync_event_id==op.id
        sent.append(copy.deepcopy(payload));return {'counts':[]}
    monkeypatch.setattr(receiving,'_square_post',post)
    with Session(receiving_db) as db:
        save(db)
        assert sent==[]
        assert push(db)['succeeded']==1
        assert push(db)['skipped_already_synced']==1
        assert len(sent)==1
        assert db.get(PurchaseOrder,1).status==PurchaseOrderStatus.IN_TRANSIT
        assert next(x for x in load_incoming_supply(db) if x.store_id==1).quantity==12
        assert receiving.receiving_history(db,1)[0]['posting_state']=='Successfully pushed'


def test_pushed_twenty_corrected_nineteen_sends_only_minus_one(receiving_db,monkeypatch):
    sent=[]
    monkeypatch.setattr(receiving,'_square_post',lambda path,payload,**kw:sent.append(copy.deepcopy(payload)) or {})
    with Session(receiving_db) as db:
        save(db,20);push(db)
        original=root_id(db)
        receiving.correct_purchase_order_receiving(db,purchase_order_id=1,original_allocation_id=original,
            corrected_quantity=19,actor_principal_id=1,request_key='correct')
        assert len(sent)==1
        push(db)
        adjustments=[p['changes'][0]['adjustment'] for p in sent]
        assert [x['quantity'] for x in adjustments]==['20','1']
        assert adjustments[1]['from_state']=='IN_STOCK' and adjustments[1]['to_state']=='NONE'
        events=db.scalars(select(PurchaseOrderReceiptAllocation).order_by(PurchaseOrderReceiptAllocation.id)).all()
        assert events[0].quantity_delta==20 and events[1].quantity_delta==-1
        assert events[1].corrected_quantity==19 and events[1].original_allocation_id==original
        assert db.get(PurchaseOrderStoreAllocation,1).store_received_qty==19


@pytest.mark.parametrize('corrected',[0,7,12])
def test_correction_before_first_push_nets_saved_events(receiving_db,monkeypatch,corrected):
    sent=[];monkeypatch.setattr(receiving,'_square_post',lambda path,payload,**kw:sent.append(payload) or {})
    with Session(receiving_db) as db:
        save(db,8)
        receiving.correct_purchase_order_receiving(db,purchase_order_id=1,original_allocation_id=root_id(db),
            corrected_quantity=corrected,actor_principal_id=1,request_key='correct')
        push(db)
        assert len(sent)==(1 if corrected else 0)
        if corrected:assert sent[0]['changes'][0]['adjustment']['quantity']==str(corrected)
        assert db.get(PurchaseOrderStoreAllocation,1).store_received_qty==corrected


def test_partial_success_unknown_outcome_and_restart_recovery(receiving_db,monkeypatch):
    sent=[]
    def first(path,payload,**kw):
        sent.append(copy.deepcopy(payload))
        if payload['changes'][0]['adjustment']['location_id']=='L2':raise TimeoutError('reply lost')
        return {}
    monkeypatch.setattr(receiving,'_square_post',first)
    with Session(receiving_db) as db:
        save(db,4,1);save(db,5,2)
        result=push(db)
        assert result['succeeded']==1 and result['uncertain']==1
        assert [x['posting_state'] for x in receiving.receiving_history(db,1)]==['Successfully pushed','Outcome uncertain']
    original=copy.deepcopy(sent[1]);sent.clear()
    monkeypatch.setattr(receiving,'_square_post',lambda path,payload,**kw:sent.append(copy.deepcopy(payload)) or {})
    with Session(receiving_db) as restarted:
        result=push(restarted)
        assert result['skipped_already_synced']==1 and result['succeeded']==1
        assert sent==[original]
        assert restarted.get(PurchaseOrderStoreAllocation,2).store_received_qty==5


def test_confirmed_failure_is_distinct_and_retry_is_identical(receiving_db,monkeypatch):
    sent=[]
    def reject(path,payload,**kw):sent.append(copy.deepcopy(payload));raise RuntimeError('Square API error 400: invalid')
    monkeypatch.setattr(receiving,'_square_post',reject)
    with Session(receiving_db) as db:
        save(db);assert push(db)['failed']==1
        assert receiving.receiving_history(db,1)[0]['posting_state']=='Failed'
    monkeypatch.setattr(receiving,'_square_post',lambda path,payload,**kw:sent.append(copy.deepcopy(payload)) or {})
    with Session(receiving_db) as db:assert push(db)['succeeded']==1
    assert sent[0]==sent[1]


@pytest.mark.parametrize('actor',[None,2,3])
def test_service_owner_boundary_all_mutations(receiving_db,actor):
    with Session(receiving_db) as db:
        mutations=[lambda:receiving.save_purchase_order_received_quantities(db,purchase_order_id=1,
            received_qty_by_line_store={(1,1):1},actor_principal_id=actor,request_key='x'),
            lambda:receiving.scan_purchase_order_barcode(db,purchase_order_id=1,barcode='SKU1',actor_principal_id=actor,request_key='x'),
            lambda:receiving.cancel_purchase_order_barcode_scan(db,purchase_order_id=1,line_id=1,store_id=1,actor_principal_id=actor),
            lambda:receiving.correct_purchase_order_receiving(db,purchase_order_id=1,original_allocation_id=1,corrected_quantity=0,actor_principal_id=actor),
            lambda:receiving.close_purchase_order_remainder(db,purchase_order_id=1,line_id=1,store_id=1,quantity=1,actor_principal_id=actor),
            lambda:receiving.receive_purchase_order(db,purchase_order_id=1,actor_principal_id=actor)]
        for mutation in mutations:
            with pytest.raises(PermissionError):mutation()
        assert db.scalar(select(func.count()).select_from(PurchaseOrderReceipt))==0


def test_route_owner_boundary_and_general_save_bypass(receiving_db):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.auth import Principal as Actor, Role
    from app.db import get_db
    from app.routers import management
    app=FastAPI();app.include_router(management.router)
    with Session(receiving_db) as db:
        app.dependency_overrides[get_db]=lambda:db
        app.dependency_overrides[management.ordering_access]=lambda:Actor(2,'admin',Role.ADMIN,None,True)
        client=TestClient(app,follow_redirects=False,headers={'x-forwarded-for':'127.0.0.1'});client.cookies.set('csrf_token','test')
        for suffix in ['received-quantities','scan-barcode','scan-barcode/cancel','receive','receive-retry-failed','receiving-correction','close-remainder']:
            assert client.post('/management/ordering-tool/orders/1/'+suffix,data={'csrf_token':'test'}).status_code==403
        result=client.post('/management/ordering-tool/orders/1/save',data={'csrf_token':'test','received__1__1':'4'})
        assert result.status_code==400
        assert db.get(PurchaseOrderStoreAllocation,1).store_received_qty==0


def test_concurrent_save_is_idempotent_and_close_serializes(receiving_db):
    def delivery():
        with Session(receiving_db) as db:save(db,8,key='same-delivery');db.commit()
    with ThreadPoolExecutor(2) as pool:
        a=pool.submit(delivery);b=pool.submit(delivery);a.result(10);b.result(10)
    def close():
        with Session(receiving_db) as db:
            try:
                receiving.close_purchase_order_remainder(db,purchase_order_id=1,line_id=1,store_id=1,quantity=12,
                    actor_principal_id=1,request_key=uuid4().hex);db.commit();return True
            except ValueError:db.rollback();return False
    with ThreadPoolExecutor(2) as pool:
        a=pool.submit(close);b=pool.submit(close);assert sorted([a.result(10),b.result(10)])==[False,True]
    with Session(receiving_db) as db:
        assert db.get(PurchaseOrderStoreAllocation,1).store_received_qty==8
        assert next(x for x in load_incoming_supply(db) if x.store_id==1).quantity==0


def test_concurrent_push_cannot_send_same_target_twice(receiving_db,monkeypatch):
    entered,release=Event(),Event();calls=[]
    def post(*args,**kw):
        calls.append(1);entered.set();assert release.wait(10);return {}
    monkeypatch.setattr(receiving,'_square_post',post)
    with Session(receiving_db) as db:save(db);db.commit()
    def worker():
        with Session(receiving_db) as db:return push(db)
    with ThreadPoolExecutor(2) as pool:
        first=pool.submit(worker);assert entered.wait(10)
        second=pool.submit(worker)
        try:
            with pytest.raises(ValueError,match='already running'):second.result(10)
        finally:release.set()
        assert first.result(10)['succeeded']==1
    assert calls==[1]


def test_database_guards_payload_and_receiving_history(receiving_db,monkeypatch):
    monkeypatch.setattr(receiving,'_square_post',lambda *a,**kw:{})
    with Session(receiving_db) as db:save(db);push(db)
    for statement in ["UPDATE square_sync_events SET request_payload='{}' WHERE sync_type='PURCHASING_RECEIPT_V3'",
            "UPDATE square_sync_events SET status='FAILED' WHERE sync_type='PURCHASING_RECEIPT_V3'",
            "DELETE FROM purchase_order_receipt_allocations", "UPDATE purchase_order_receipt_allocations SET quantity_delta=99",
            "DELETE FROM purchase_order_receipts", "UPDATE purchase_order_receipt_lines SET received_qty=99"]:
        with receiving_db.connect() as con:
            with pytest.raises(Exception,match='immutable'):con.execute(text(statement))
            con.rollback()


def test_legacy_totals_are_preserved_and_never_queued(receiving_db,monkeypatch):
    calls=[];monkeypatch.setattr(receiving,'_square_post',lambda *a,**kw:calls.append(1) or {})
    with Session(receiving_db) as db:
        db.get(PurchaseOrderStoreAllocation,1).store_received_qty=7
        db.get(PurchaseOrderLine,1).received_qty_total=7;db.commit()
        assert push(db)['attempted']==0 and calls==[]
        save(db,2);push(db)
        op=db.scalar(select(SquareSyncEvent))
        assert op.request_payload['changes'][0]['adjustment']['quantity']=='2'
        # Received orders retain the original commitment; use explicit shortage closure instead of removing it.
        with pytest.raises(ValueError,match='preserve the original'):
            receiving.save_purchase_order_lines(db,purchase_order_id=1,ordered_qty_by_line_id={},removed_line_ids={1},
                manual_par_by_line_id={},manual_par_by_line_store={},allocation_qty_by_line_store={})
        db.rollback()
        assert db.get(PurchaseOrderLine,1).ordered_qty==80
        assert db.get(PurchaseOrderStoreAllocation,1).store_received_qty==9
        assert db.get(PurchaseOrderLine,1).received_qty_total==9
        assert len(receiving.receiving_history(db,1))==1


def test_live_four_store_partial_coverage_and_receipt_independence(receiving_db,monkeypatch):
    from app.services import square_ordering_data_service as source
    with Session(receiving_db) as db:
        save(db,8);db.commit()
        assert len(purchasing_store_locations(db)[0])==4
        seen=[]
        def counts(locations,variations,**kw):
            seen.append((locations,variations));return {('L1','VAR1'): -2,('L2','VAR1'): 0,('L4','VAR1'): 11}
        monkeypatch.setattr(source,'_fetch_on_hand',counts)
        result=source.fetch_on_hand_by_store_variation(db,variation_ids=['VAR1'],store_ids=[1])
        assert result=={(1,'VAR1'):-2,(2,'VAR1'):0,(4,'VAR1'):11}
        assert set(seen[0][0])=={'L1','L2','L3','L4'}
        assert inventory_position(-2,12)==10
        assert inventory_position(None,12) is None


@pytest.mark.parametrize('status', ['IN_TRANSIT', 'RECEIVED_SPLIT_PENDING'])
def test_barcode_pack_scan_duplicate_and_undo_keep_original_evidence(receiving_db,status):
    from app.models import VendorSkuConfig
    with Session(receiving_db) as db:
        db.get(PurchaseOrder,1).status=status
        db.add(VendorSkuConfig(vendor_id=1,sku='SKU1',square_variation_id='VAR1',pack_size=5,
            min_order_qty=0,active=True,is_default_vendor=True));db.commit()
        result=receiving.scan_purchase_order_barcode(db,purchase_order_id=1,barcode='SKU1',actor_principal_id=1,request_key='pack')
        again=receiving.scan_purchase_order_barcode(db,purchase_order_id=1,barcode='SKU1',actor_principal_id=1,request_key='pack')
        assert result['scan_increment_qty']==5 and again['receipt_allocation_id']==result['receipt_allocation_id']
        receiving.cancel_purchase_order_barcode_scan(db,purchase_order_id=1,line_id=1,store_id=result['store_id'],
            original_allocation_id=result['receipt_allocation_id'],actor_principal_id=1,request_key='undo-pack')
        assert db.get(PurchaseOrderStoreAllocation,result['store_id']).store_received_qty==0
        assert db.get(PurchaseOrderReceiptAllocation,result['receipt_allocation_id']).quantity_delta==5
        assert db.scalar(select(PurchaseOrderReceiptAllocation).where(PurchaseOrderReceiptAllocation.event_kind=='CORRECTION')).quantity_delta==-5
        assert db.scalar(select(func.count()).select_from(SquareSyncEvent))==0


def test_interruption_after_transmit_recovers_same_operation_after_restart(receiving_db,monkeypatch):
    sent=[]
    def interrupted(path,payload,**kw):
        sent.append(copy.deepcopy(payload));raise SystemExit('simulated worker interruption')
    monkeypatch.setattr(receiving,'_square_post',interrupted)
    with Session(receiving_db) as db:
        save(db)
        with pytest.raises(SystemExit):push(db)
    monkeypatch.setattr(receiving,'_square_post',lambda path,payload,**kw:sent.append(copy.deepcopy(payload)) or {})
    with Session(receiving_db) as db:
        assert receiving.receiving_history(db,1)[0]['posting_state']=='Outcome uncertain'
        assert push(db)['succeeded']==1
    assert sent[0]==sent[1]


def test_concurrent_correction_and_closure_are_serialized(receiving_db):
    with Session(receiving_db) as db:save(db,8);original=root_id(db);db.commit()
    def correction():
        with Session(receiving_db) as db:
            receiving.correct_purchase_order_receiving(db,purchase_order_id=1,original_allocation_id=original,
                corrected_quantity=7,actor_principal_id=1,request_key='correct');db.commit()
    def closure():
        with Session(receiving_db) as db:
            receiving.close_purchase_order_remainder(db,purchase_order_id=1,line_id=1,store_id=1,quantity=12,
                actor_principal_id=1,request_key='close');db.commit()
    with ThreadPoolExecutor(2) as pool:
        a=pool.submit(correction);b=pool.submit(closure);a.result(10);b.result(10)
    with Session(receiving_db) as db:
        assert next(x for x in load_incoming_supply(db) if x.store_id==1).quantity==1
        assert db.get(PurchaseOrderStoreAllocation,1).store_received_qty==7


def test_owner_interface_shows_separate_actions_and_posting_evidence(receiving_db,monkeypatch,tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.main import app as application
    from app.auth import Principal as Actor, Role
    from app.db import get_db
    from app.routers import management
    monkeypatch.setattr(receiving,'fetch_on_hand_by_store_variation',lambda *a,**kw:{})
    monkeypatch.setattr(receiving,'fetch_sales_volume_by_variation',lambda *a,**kw:{})
    monkeypatch.setattr(receiving,'fetch_catalog_by_sku',lambda *a,**kw:{})
    app=FastAPI();app.state.templates=application.state.templates;app.include_router(management.router)
    with Session(receiving_db) as db:
        app.dependency_overrides[get_db]=lambda:db
        app.dependency_overrides[management.ordering_access]=lambda:Actor(1,'owner',Role.ADMIN,None,True)
        client=TestClient(app,follow_redirects=False,headers={'x-forwarded-for':'127.0.0.1'});client.cookies.set('csrf_token','test')
        url='/management/ordering-tool/orders/1'
        response=client.get(url)
        assert response.status_code==200
        for text_ in ['Originally Ordered','Previously Received','Outstanding','Received Today','Save Receiving','Push to Square']:
            assert text_ in response.text
        assert 'name="received__1__1"\n                value=""' in response.text
        saved=client.post(url+'/received-quantities',data={'csrf_token':'test','request_key':'http-delivery','received__1__1':'4'})
        assert saved.status_code==303
        response=client.get(url)
        assert 'Saved, not pushed' in response.text and 'Save Correction' in response.text and 'Close Remainder' in response.text
        assert db.get(PurchaseOrderStoreAllocation,1).store_received_qty==4
        evidence=os.getenv('P1_HTML_EVIDENCE_DIR')
        if evidence:
            from pathlib import Path
            path=Path(evidence);path.mkdir(parents=True,exist_ok=True);(path/'purchasing-receiving-owner.html').write_text(response.text)
        app.dependency_overrides[management.ordering_access]=lambda:Actor(2,'admin',Role.ADMIN,None,True)
        response=client.get(url)
        assert 'id="received-quantities-form"' not in response.text and 'Save Correction' not in response.text


def test_manual_product_uses_owner_delivery_and_explicit_push(receiving_db,monkeypatch):
    from types import SimpleNamespace
    from app.services.manual_ordering_service import add_catalog_product
    monkeypatch.setattr('app.services.manual_ordering_service.catalog_variations', lambda: {'MANUAL': SimpleNamespace(
        sku='MANUAL-SKU', gtin=None, item_name='Manual product', variation_name='Default', unit_price=None)})
    with Session(receiving_db) as db:
        po=db.get(PurchaseOrder,1);po.status=PurchaseOrderStatus.DRAFT
        db.commit()
        line=add_catalog_product(db,purchase_order_id=1,variation_id='MANUAL',initial_qty=3,unit_cost='2',actor_id=1)
        db.get(PurchaseOrder,1).status=PurchaseOrderStatus.IN_TRANSIT;db.flush()
        allocation=db.scalar(select(PurchaseOrderStoreAllocation).where(PurchaseOrderStoreAllocation.purchase_order_line_id==line.id))
        receiving.save_purchase_order_received_quantities(db,purchase_order_id=1,
            received_qty_by_line_store={(line.id,allocation.store_id):3},actor_principal_id=1,request_key='manual-delivery')
        sent=[];monkeypatch.setattr(receiving,'_square_post',lambda path,payload,**kw:sent.append(payload) or {})
        assert push(db)['succeeded']==1
        assert sent[0]['changes'][0]['adjustment']['catalog_object_id']=='MANUAL'
        assert sent[0]['changes'][0]['adjustment']['quantity']=='3'


def test_legacy_receiving_blocks_destructive_discard(receiving_db):
    with Session(receiving_db) as db:
        db.get(PurchaseOrderStoreAllocation,1).store_received_qty=4;db.commit()
        with pytest.raises(ValueError,match='cannot be discarded'):
            receiving.delete_draft_purchase_order(db,purchase_order_id=1)
        assert db.get(PurchaseOrder,1) is not None


def test_inventory_reader_preserves_valid_pairs_after_later_page_failure(receiving_db,monkeypatch):
    from app.services import square_ordering_data_service as source
    calls=[]
    def read(path,payload,**kw):
        calls.append(payload)
        if len(calls)==1:
            return {'counts':[{'catalog_object_id':'VAR1','location_id':'L1','state':'IN_STOCK','quantity':'4'}], 'cursor':'next'}
        raise RuntimeError('Square read temporarily unavailable')
    monkeypatch.setattr(source,'_square_post',read)
    with Session(receiving_db) as db:
        coverage={}
        values=source.fetch_on_hand_by_store_variation(db,variation_ids=['VAR1'],coverage=coverage)
        assert values=={(1,'VAR1'):4}
        assert coverage['missing_pairs']==[(2,'VAR1'),(3,'VAR1'),(4,'VAR1')]
        assert coverage['source']=='LIVE_SQUARE'


def test_ambiguous_store_mapping_stays_unknown_without_losing_other_stores(receiving_db):
    with Session(receiving_db) as db:
        db.add(Store(id=10,name='HWY99',square_location_id='OTHER',active=True));db.commit()
        locations,issues=purchasing_store_locations(db)
        assert set(locations)=={1,3,4} and 'UNKNOWN_STORE_MAPPING:hwy99' in issues


def test_standard_generation_preserves_known_store_recommendations(receiving_db):
    from app.models import VendorSkuConfig, ParLevel
    from app.services.purchase_order_generation_service import generate_vendor_scoped_recommendations
    with Session(receiving_db) as db:
        db.add(VendorSkuConfig(vendor_id=1,sku='SKU1',square_variation_id='VAR1',pack_size=1,
            min_order_qty=0,active=True,is_default_vendor=True));db.flush()
        for store in range(1,5):
            db.add(ParLevel(vendor_id=1,store_id=store,sku='SKU1',manual_par_level=5,manual_stock_up_level=10,par_source='MANUAL'))
        db.commit()
        seen=[]
        def inventory(store,sku):
            seen.append(store)
            if store==3:raise ValueError('Inventory UNKNOWN for store 3')
            return 0
        rows=generate_vendor_scoped_recommendations(db,vendor_ids=[1],history_loader=lambda *a:[0]*30,
            on_hand_loader=inventory,include_zero_qty=True)
        assert set(seen)=={1,2,3,4}
        assert {row.store_id for row in rows}=={1,2,4}


@pytest.mark.parametrize('hand,incoming,expected', [('-2',0,12),('2.5',2,6)])
def test_existing_standard_engine_uses_exact_inventory_position(hand,incoming,expected):
    from decimal import Decimal
    from app.services.purchase_order_math_service import LineMathInput, OrderingMathParams, compute_line_recommendation
    result=compute_line_recommendation(LineMathInput(sku='TEST',current_on_hand=Decimal(hand),in_transit_qty=incoming,
        history_daily_units=[Decimal(1)]*30,manual_level=5,manual_par=10,par_source='MANUAL'),
        OrderingMathParams(reorder_weeks=1,stock_up_weeks=2,history_lookback_days=30))
    assert result.rounded_recommended_qty==expected


def test_receiving_migration_refuses_to_discard_new_delivery_evidence(receiving_db):
    with Session(receiving_db) as db:save(db);db.commit()
    with pytest.raises(RuntimeError,match='Refusing to discard purchasing receiving evidence'):
        command.downgrade(_alembic_config(receiving_db.url.render_as_string(hide_password=False)), '20261017_0032')
    with receiving_db.connect() as con:
        from app.schema_contract import HEAD_REVISION
        assert con.scalar(text('SELECT version_num FROM alembic_version'))==HEAD_REVISION
        assert con.scalar(text('SELECT count(*) FROM purchase_order_receipt_allocations'))==1


def test_retry_recovers_existing_command_without_first_posting_new_deliveries(receiving_db,monkeypatch):
    sent=[]
    monkeypatch.setattr(receiving,'_square_post',lambda *a,**kw:(_ for _ in ()).throw(TimeoutError('reply lost')))
    with Session(receiving_db) as db:
        save(db,4,1);assert push(db)['uncertain']==1
        original=db.scalar(select(SquareSyncEvent)).request_payload
        save(db,5,2);db.commit()
        monkeypatch.setattr(receiving,'_square_post',lambda path,payload,**kw:sent.append(copy.deepcopy(payload)) or {})
        result=receiving.receive_purchase_order(db,purchase_order_id=1,actor_principal_id=1,retry_failed_only=True)
        assert result['succeeded']==1 and sent==[original]
        history=receiving.receiving_history(db,1)
        assert history[1]['posting_state']=='Saved, not pushed' and history[1]['event'].square_sync_event_id is None


def test_existing_consignment_receipt_hook_remains_idempotent_without_actual_charge(receiving_db,monkeypatch):
    from decimal import Decimal
    from app.models import PaymentMethod, VendorPaymentClassification, OrderPayment, ConsignmentReplenishment, FundingActualActivity
    from app.services.v2_order_payments_service import portal_today
    monkeypatch.setattr(receiving,'_square_post',lambda *a,**kw:{})
    with Session(receiving_db) as db:
        db.get(PurchaseOrderLine,1).unit_cost=Decimal(3)
        db.add(PaymentMethod(id=10,display_name='Consignment',category='CONSIGNMENT',is_active=True,
            created_by_principal_id=1,updated_by_principal_id=1));db.flush()
        db.add(VendorPaymentClassification(vendor_id=1,payment_method_id=10,payment_category='CONSIGNMENT',
            payment_method_label_snapshot='Consignment',is_consignment=True,effective_date=portal_today(),
            is_current=True,created_by_principal_id=1));db.commit()
        save(db,8);push(db);push(db)
        assert db.scalar(select(func.count()).select_from(OrderPayment))==1
        assert db.scalar(select(ConsignmentReplenishment)).received_cost_value==24
        assert db.scalar(select(func.count()).select_from(FundingActualActivity))==0
        assert db.scalar(select(func.count()).select_from(FundingLedgerEntry))==0


def test_receiving_refreshes_stale_session_after_acquiring_order_lock(receiving_db):
    with Session(receiving_db) as first:
        stale=first.get(PurchaseOrderStoreAllocation,1)
        assert stale.store_received_qty==0
        with Session(receiving_db) as second:save(second,3);second.commit()
        save(first,5);first.commit()
        assert stale.store_received_qty==8
        assert next(x for x in load_incoming_supply(first) if x.store_id==1).quantity==12
