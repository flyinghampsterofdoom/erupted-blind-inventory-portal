"""Phase 2 owner workflow: disposable PostgreSQL and deterministic Square mocks."""
import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select,func,text
from sqlalchemy.orm import Session

from test_purchasing_receiving_extension import database_template,receiving_db
from app.config import settings
from app.models import (PurchaseOrder,PurchaseOrderLine,PurchaseOrderStoreAllocation,Vendor,VendorSkuConfig,
    PurchasingSession,PurchasingSessionRevision,PurchasingSessionLine,PurchasingSessionGroup,
    FundingLedgerEntry,FundingActualActivity,FundingAccount,PaymentMethod,PurchaseOrderStatus,SquareSyncEvent,
    PurchasingProductAuthority)
from app.services import purchasing_session_service as service
from app.services.purchase_order_math_service import DemandEvidence,forecast_demand,allocate_purchase
from app.services.purchasing_evidence_service import BUSINESS_TIMEZONE
from app.services.v2_ordering_square_gateway import SquareProductMetadata,SquareStoreSkuData,SquareOrderingReadResult
from app.services.v2_ordering_normalization_service import DailyQuantity
from app.services.v2_ordering_policy_service import DataSourceEvidence

NOW=datetime.now(timezone.utc)


class Gateway:
    def __init__(self):
        self.hand=Decimal(0);self.units=Decimal(1);self.calls=[];self.missing=set();self.sales_complete=True;self.hook=None
        self.catalog={'VAR1':SquareProductMetadata('VAR1','SKU1','Everyday Product','Blue',NOW-timedelta(days=180),False,vendor_ids=('V1',)),
            'VAR2':SquareProductMetadata('VAR2','SEARCH-ONLY','Searchable Special','Original',NOW-timedelta(days=180),False,vendor_ids=('V1',))}
    def fetch_active_catalog(self):self.calls.append(('catalog',));return self.catalog
    def fetch(self,**kw):
        self.calls.append(('evidence',copy.deepcopy(kw)))
        if self.hook:self.hook();self.hook=None
        end=kw['as_of'].astimezone(BUSINESS_TIMEZONE).date()-timedelta(days=1)
        rows={}
        for sid in kw['location_by_store']:
            for vid in kw['variation_ids']:
                hand=None if (sid,vid) in self.missing else Decimal(20) if vid=='VAR2' else self.hand
                points=tuple(DailyQuantity(end-timedelta(days=i),self.units if vid=='VAR1' else Decimal(0)) for i in range(kw['history_days']-1,-1,-1))
                rows[(sid,vid)]=SquareStoreSkuData(sid,vid,hand,hand is not None,points,(),
                    (DataSourceEvidence('inventory',NOW,available=hand is not None),
                     DataSourceEvidence('sales',NOW,available=self.sales_complete,complete=self.sales_complete),
                     DataSourceEvidence('stockout_history',None,available=False,complete=False)),())
        return SquareOrderingReadResult(self.catalog,rows)


@pytest.fixture
def engine(receiving_db,monkeypatch):
    monkeypatch.setattr(settings,'purchasing_v3_phase2_enabled',True)
    def denied(*args,**kwargs):raise AssertionError('Purchasing preparation invoked a financial writer')
    monkeypatch.setattr('app.services.v2_order_payments_service.initialize_new_order_if_configured',denied)
    with Session(receiving_db) as db:
        db.get(PurchaseOrder,1).status=PurchaseOrderStatus.DRAFT
        db.add(Vendor(id=2,square_vendor_id='V2',name='Alternative Supplier',active=True))
        db.add_all([VendorSkuConfig(vendor_id=1,sku='SKU1',square_variation_id='VAR1',unit_cost=3,pack_size=1,min_order_qty=0,active=True,is_default_vendor=True),
            VendorSkuConfig(vendor_id=2,sku='SKU1-ALT',square_variation_id='VAR1',unit_cost=4,pack_size=3,min_order_qty=0,active=True,is_default_vendor=False),
            VendorSkuConfig(vendor_id=1,sku='SEARCH-ONLY',square_variation_id='VAR2',unit_cost=2,pack_size=1,min_order_qty=0,active=True,is_default_vendor=True)])
        db.add(PaymentMethod(id=10,display_name='Card',category='CREDIT_CARD',is_active=True,created_by_principal_id=1,updated_by_principal_id=1));db.flush()
        db.add(FundingAccount(id=10,account_type='CREDIT_CARD',payment_method_id=10,display_name='Intended Card',created_by_principal_id=1,updated_by_principal_id=1))
        db.execute(text("SELECT setval(pg_get_serial_sequence('purchase_orders','id'),100,true)"));db.commit()
    yield receiving_db


def generate(db,gateway=None,key=None):return service.generate(db,actor_id=1,request_key=key or uuid4().hex,gateway=gateway or Gateway())

def line(db,session):return db.scalar(select(PurchasingSessionLine).where(PurchasingSessionLine.session_id==session.id,PurchasingSessionLine.variation_id=='VAR1'))


def test_generate_live_four_store_snapshot_and_continue_without_read(engine):
    gateway=Gateway()
    with Session(engine) as db:
        session=generate(db,gateway,key='generation');db.commit();row=line(db,session)
        assert row.quantity==280 and sum(row.allocations.values())==280
        assert row.allocations=={'1':70,'2':70,'3':70,'4':70}
        assert gateway.calls[1][1]['location_by_store']=={1:'L1',2:'L2',3:'L3',4:'L4'}
        assert set(gateway.calls[1][1]['variation_ids'])=={'VAR1','VAR2'}
        again=generate(db,gateway,key='generation');assert again.id==session.id and len(gateway.calls)==2
        assert service.view_session(db,session_id=session.id,actor_id=1)['groups'][0]['product_count']==1
        assert len(gateway.calls)==2
        revision=db.scalar(select(PurchasingSessionRevision))
        assert revision.evidence['products']['VAR1']['stores']['1']['current_inventory']=='0'
        assert db.scalar(select(func.count()).select_from(PurchaseOrder))==1


def test_refresh_preserves_owner_quantity_supplier_allocations_and_proposes_changes(engine):
    gateway=Gateway()
    with Session(engine) as db:
        session=generate(db,gateway);row=line(db,session)
        service.update_line(db,session_id=session.id,line_id=row.id,actor_id=1,quantity=11,supplier_id=2,
            allocations={'1':2,'2':3,'3':4,'4':2});db.commit()
        gateway.units=Decimal(2);gateway.hand=Decimal(5)
        service.refresh(db,session_id=session.id,actor_id=1,request_key='refresh',gateway=gateway);db.commit();db.refresh(row)
        assert row.quantity==11 and row.supplier_id==2 and row.allocations=={'1':2,'2':3,'3':4,'4':2}
        assert row.proposed['quantity']==540 and row.accepted['quantity']==280
        assert len(gateway.calls)==4
        service.update_line(db,session_id=session.id,line_id=row.id,actor_id=1,accept_proposal=True,acknowledge=True)
        assert row.quantity==540 and sum(row.allocations.values())==540 and row.proposed is None


def test_supplier_substitution_regroups_without_po_or_global_vendor_changes(engine):
    with Session(engine) as db:
        session=generate(db);row=line(db,session);before=copy.deepcopy(row.accepted)
        service.update_line(db,session_id=session.id,line_id=row.id,actor_id=1,supplier_id=2)
        assert row.quantity==282 and row.unit_cost==4 and row.accepted==before
        assert row.canonical_vendor_id==1 and row.supplier_id==2
        assert db.scalar(select(VendorSkuConfig).where(VendorSkuConfig.vendor_id==1,VendorSkuConfig.sku=='SKU1')).is_default_vendor
        assert db.scalar(select(func.count()).select_from(PurchaseOrder))==1
        service.update_line(db,session_id=session.id,line_id=row.id,actor_id=1,quantity=1,supplier_id=2)
        assert row.quantity==1  # Owner overrides are not rounded to a pack.


def test_manual_search_all_catalog_add_remove_undo_and_discard_completion(engine):
    with Session(engine) as db:
        session=generate(db)
        assert service.catalog_search(db,session_id=session.id,actor_id=1,query='searchable')[0]['variation_id']=='VAR2'
        assert service.catalog_search(db,session_id=session.id,actor_id=1,query='SEARCH-ONLY')[0]['variation_id']=='VAR2'
        added=service.add_product(db,session_id=session.id,actor_id=1,variation_id='VAR2',quantity=5,supplier_id=2,cost='2.5')
        assert added.quantity==5 and added.supplier_id==2
        service.remove_line(db,session_id=session.id,line_id=added.id,actor_id=1)
        service.remove_line(db,session_id=session.id,line_id=added.id,actor_id=1,undo=True)
        assert not added.removed
        gateway=Gateway();gateway.units=Decimal(2)
        service.refresh(db,session_id=session.id,actor_id=1,request_key='refresh',gateway=gateway)
        assert added.quantity==5 and added.supplier_id==2
        service.discard_group(db,session_id=session.id,supplier_id=1,actor_id=1)
        assert session.status=='ACTIVE'
        service.discard_group(db,session_id=session.id,supplier_id=2,actor_id=1)
        assert session.status=='COMPLETE'
        assert db.scalar(select(func.count()).select_from(PurchaseOrder))==1


def test_conversion_is_idempotent_populates_cost_account_allocations_and_no_writers(engine):
    with Session(engine) as db:
        session=generate(db);row=line(db,session)
        service.set_account(db,session_id=session.id,supplier_id=1,actor_id=1,account_id=10)
        po=service.create_po(db,session_id=session.id,supplier_id=1,actor_id=1)
        repeated=service.create_po(db,session_id=session.id,supplier_id=1,actor_id=1)
        assert repeated.id==po.id and session.status=='COMPLETE' and po.status==PurchaseOrderStatus.DRAFT
        pol=db.scalar(select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id==po.id))
        assert pol.variation_id=='VAR1' and pol.unit_cost==3 and pol.ordered_qty==280
        allocations=db.scalars(select(PurchaseOrderStoreAllocation).where(PurchaseOrderStoreAllocation.purchase_order_line_id==pol.id)).all()
        assert sum(a.allocated_qty for a in allocations)==280 and all(a.store_received_qty==0 for a in allocations)
        assert db.scalar(select(PurchasingSessionGroup)).intended_account_id==10
        for model in [FundingActualActivity,FundingLedgerEntry,SquareSyncEvent]:assert db.scalar(select(func.count()).select_from(model))==0
        with pytest.raises(ValueError,match='complete'):service.update_line(db,session_id=session.id,line_id=row.id,actor_id=1,quantity=1)


def test_partial_source_keeps_valid_observations_and_requires_owner_review(engine):
    gateway=Gateway();gateway.missing={(4,'VAR1')}
    with Session(engine) as db:
        session=generate(db,gateway);row=line(db,session)
        assert row.quantity==210 and row.accepted['forecasts']['4']['quantity'] is None
        assert row.accepted['stores']['4']['current_inventory'] is None
        with pytest.raises(ValueError,match='Review uncertain'):service.create_po(db,session_id=session.id,supplier_id=1,actor_id=1)
        service.update_line(db,session_id=session.id,line_id=row.id,actor_id=1,quantity=210,acknowledge=True)
        assert service.create_po(db,session_id=session.id,supplier_id=1,actor_id=1).id


def test_incoming_reduces_need_and_receipts_never_replace_live_counts(engine):
    gateway=Gateway();gateway.hand=Decimal(5)
    with Session(engine) as db:
        db.get(PurchaseOrder,1).status=PurchaseOrderStatus.IN_TRANSIT
        db.get(PurchaseOrderStoreAllocation,1).store_received_qty=8;db.get(PurchaseOrderLine,1).received_qty_total=8;db.commit()
        session=generate(db,gateway);row=line(db,session)
        assert row.accepted['stores']['1']['current_inventory']=='5' and row.accepted['stores']['1']['incoming']==12
        assert row.accepted['forecasts']['1']['quantity']==53
        assert row.accepted['forecasts']['2']['quantity']==45


def test_incomplete_sales_remains_unknown_and_manual_choices_are_explicit(engine):
    gateway=Gateway();gateway.sales_complete=False
    with Session(engine) as db:
        session=generate(db,gateway);row=line(db,session)
        assert row.quantity==0 and all(value['quantity'] is None for value in row.accepted['forecasts'].values())
        assert service.view_session(db,session_id=session.id,actor_id=1)['review']
        service.update_line(db,session_id=session.id,line_id=row.id,actor_id=1,quantity=7,acknowledge=True)
        po=service.create_po(db,session_id=session.id,supplier_id=1,actor_id=1)
        assert db.scalar(select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id==po.id)).ordered_qty==7


def test_concurrent_conversion_and_stale_session_cannot_duplicate_po(engine):
    with Session(engine) as db:session=generate(db);sid=session.id;db.commit()
    def convert():
        with Session(engine) as db:po=service.create_po(db,session_id=sid,supplier_id=1,actor_id=1);pid=po.id;db.commit();return pid
    with ThreadPoolExecutor(2) as pool:
        a=pool.submit(convert);b=pool.submit(convert);assert a.result(10)==b.result(10)
    with Session(engine) as db:assert db.scalar(select(func.count()).select_from(PurchaseOrder))==2


def test_refresh_merges_decision_committed_during_external_read(engine):
    gateway=Gateway()
    with Session(engine) as db:session=generate(db);sid=session.id;lid=line(db,session).id;db.commit()
    def edit():
        with Session(engine) as other:
            service.update_line(other,session_id=sid,line_id=lid,actor_id=1,quantity=17);other.commit()
    gateway.hook=edit;gateway.units=Decimal(2)
    with Session(engine) as db:
        service.refresh(db,session_id=sid,actor_id=1,request_key='concurrent-refresh',gateway=gateway);db.commit()
        row=db.get(PurchasingSessionLine,lid)
        assert row.quantity==17 and row.proposed['quantity']==560


@pytest.mark.parametrize('actor',[None,2,3])
def test_owner_only_sessions_at_service_boundary(engine,actor):
    with Session(engine) as db:
        with pytest.raises(PermissionError):service.generate(db,actor_id=actor,request_key='x',gateway=Gateway())
        with pytest.raises(PermissionError):service.create_po(db,session_id=1,supplier_id=1,actor_id=actor)
        assert db.scalar(select(func.count()).select_from(PurchasingSession))==0


def test_source_revisions_and_conversion_lineage_are_immutable(engine):
    with Session(engine) as db:
        session=generate(db);service.create_po(db,session_id=session.id,supplier_id=1,actor_id=1);db.commit()
    for sql in ["UPDATE purchasing_session_revisions SET evidence='{}'",'DELETE FROM purchasing_session_revisions',
                'UPDATE purchasing_session_groups SET purchase_order_id=NULL']:
        with engine.connect() as con:
            with pytest.raises(Exception,match='immutable'):con.execute(text(sql))
            con.rollback()


def test_owner_http_acceptance_flow_and_existing_pdf(engine,monkeypatch,tmp_path):
    """The thirteen owner acceptance actions, rendered through real routes."""
    import os
    from pathlib import Path
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.main import app as application
    from app.auth import Principal as Actor,Role
    from app.db import get_db
    from app.routers import management,purchasing
    from app.services import purchase_order_admin_service as admin
    gateway=Gateway()
    monkeypatch.setattr(service,'SquareOrderingReadGateway',lambda:gateway)
    for name in ('fetch_on_hand_by_store_variation','fetch_sales_volume_by_variation','fetch_catalog_by_sku'):
        monkeypatch.setattr(admin,name,lambda *a,**kw:{})
    app=FastAPI();app.state.templates=application.state.templates
    app.include_router(purchasing.router);app.include_router(management.router)
    saved=Path(os.getenv('P2_HTML_EVIDENCE_DIR',str(tmp_path)));saved.mkdir(parents=True,exist_ok=True)
    with Session(engine) as db:
        app.dependency_overrides[get_db]=lambda:db
        app.dependency_overrides[management.ordering_access]=lambda:Actor(1,'owner',Role.ADMIN,None,True)
        client=TestClient(app,follow_redirects=False,headers={'x-forwarded-for':'127.0.0.1'})
        client.cookies.set('csrf_token','test')
        def post(url,**data):
            response=client.post(url,data={'csrf_token':'test',**data})
            assert response.status_code==303,response.text
            return response
        response=client.get('/management/purchasing');assert response.status_code==200
        (saved/'01-generate.html').write_text(response.text)
        assert 'value="5"' in response.text and 'value="10"' in response.text
        assert client.post('/management/purchasing/generate',data={'request_key':'no-csrf'}).status_code==403
        response=post('/management/purchasing/generate',request_key='http-generate',reorder_weeks=5,target_weeks=10,history_days=84)
        url=response.headers['location'];sid=int(url.rsplit('/',1)[1]);line=db.scalar(select(PurchasingSessionLine).where(PurchasingSessionLine.session_id==sid))
        response=client.get(url);assert response.status_code==200,response.text
        assert 'data-supplier-groups' in response.text and '/v2-assets/purchasing.js' in response.text
        (saved/'02-collapsed-groups.html').write_text(response.text)
        version=db.get(PurchasingSession,sid).version
        post(f'{url}/lines/{line.id}',quantity=11,supplier_id=2,version=version)
        assert db.get(PurchasingSessionLine,line.id).quantity==11
        response=client.get(url+'?q=SEARCH-ONLY');assert 'Searchable Special' in response.text
        post(url+'/add',variation_id='VAR2',quantity=3,supplier_id=1)
        other=db.scalar(select(PurchasingSessionLine).where(PurchasingSessionLine.session_id==sid,PurchasingSessionLine.variation_id=='VAR2'))
        post(f'{url}/lines/{other.id}/remove');assert other.removed
        post(f'{url}/lines/{other.id}/remove',undo=1);assert not other.removed
        gateway.units=Decimal(2)
        post(url+'/refresh',request_key='http-refresh')
        assert db.get(PurchasingSessionLine,line.id).quantity==11
        response=client.get(url);(saved/'03-review-refresh.html').write_text(response.text)
        assert 'Updated recommendation' in response.text
        post(url+'/groups/2/account',account_id=10)
        result=post(url+'/groups/2/po');po_url=result.headers['location']
        po_id=int(po_url.rsplit('/',1)[1])
        response=client.get(po_url);assert response.status_code==200,response.text
        assert 'Return to Purchasing Session' in response.text and 'Intended Card' in response.text
        (saved/'04-existing-po-editor.html').write_text(response.text)
        quantities=db.scalars(select(PurchaseOrderStoreAllocation.allocated_qty).join(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id==po_id)).all()
        assert sum(quantities)==11
        # Publish via the existing explicit Submit action; GET never creates originals.
        monkeypatch.setattr('app.services.v2_order_payments_service.initialize_new_order_if_configured', lambda *a,**kw:None)
        post(po_url+'/submit')
        pdf=admin.ensure_current_purchase_order_pdf(db,purchase_order_id=po_id)
        assert pdf.read_bytes().startswith(b'%PDF')
        response=client.get(po_url+'/pdf');assert response.status_code==200 and response.content.startswith(b'%PDF')
        (saved/'05-created-po.pdf').write_bytes(response.content)
        assert client.get(url).status_code==200
        assert client.post(po_url+'/delete',data={'csrf_token':'test'}).status_code==400
        assert db.scalar(select(func.count()).select_from(FundingActualActivity))==0
        assert db.scalar(select(func.count()).select_from(SquareSyncEvent))==0
        app.dependency_overrides[management.ordering_access]=lambda:Actor(2,'admin',Role.ADMIN,None,True)
        assert client.get(url).status_code==403
        assert client.post(url+'/refresh',data={'csrf_token':'test','request_key':'denied'}).status_code==403


def test_additive_0034_preserves_every_existing_table_and_modern_receiving(engine):
    import hashlib,json
    from alembic import command
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlalchemy import inspect
    from app.schema_contract import _alembic_config
    from app.models import Base
    from app.services import purchase_order_admin_service as receiving
    config=_alembic_config(engine.url.render_as_string(hide_password=False))
    command.downgrade(config,'20261017_0033')
    with Session(engine) as db:
        db.get(PurchaseOrder,1).status=PurchaseOrderStatus.IN_TRANSIT;db.commit()
        receiving.save_purchase_order_received_quantities(db,purchase_order_id=1,received_qty_by_line_store={(1,1):8},
            actor_principal_id=1,request_key='phase2-preserved-receipt');db.commit()
    tables=[name for name in inspect(engine).get_table_names() if name!='alembic_version']
    def fingerprint():
        with engine.connect() as con:
            return {name:hashlib.sha256(json.dumps(sorted(con.execute(text(
                f'SELECT row_to_json(t)::text FROM "{name}" t')).scalars().all())).encode()).hexdigest() for name in tables}
    before=fingerprint();command.upgrade(config,'head');assert fingerprint()==before
    names={'purchasing_sessions','purchasing_session_revisions','purchasing_session_lines','purchasing_session_groups'}
    with engine.connect() as con:
        def include(obj,name,type_,reflected,compare_to):
            table=obj if type_=='table' else getattr(obj,'table',None)
            return table is not None and table.name in names
        assert compare_metadata(MigrationContext.configure(con,opts={'include_object':include}),Base.metadata)==[]
        assert all(con.scalar(text(f'SELECT count(*) FROM {name}'))==0 for name in names)
    with Session(engine) as db:
        generate(db);db.commit()
    with pytest.raises(RuntimeError,match='Refusing to discard purchasing session history'):
        command.downgrade(config,'20261017_0033')
    after=fingerprint()
    assert all(after[name]==before[name] for name in tables if name!="audit_log")


def test_stale_editor_cannot_overwrite_a_later_owner_decision(engine):
    with Session(engine) as db:
        session=generate(db);row=line(db,session);version=session.version
        service.update_line(db,session_id=session.id,line_id=row.id,actor_id=1,quantity=17,expected_version=version)
        with pytest.raises(ValueError,match='reload'):
            service.update_line(db,session_id=session.id,line_id=row.id,actor_id=1,quantity=99,expected_version=version)
        assert row.quantity==17


def test_discarded_and_converted_groups_are_not_reopened_by_refresh(engine):
    gateway=Gateway()
    with Session(engine) as db:
        session=generate(db,gateway);row=line(db,session)
        service.add_product(db,session_id=session.id,actor_id=1,variation_id='VAR2',quantity=2,supplier_id=2,cost=2)
        service.discard_group(db,session_id=session.id,supplier_id=2,actor_id=1)
        gateway.units=Decimal(3)
        service.refresh(db,session_id=session.id,actor_id=1,request_key='after-discard',gateway=gateway)
        special=db.scalar(select(PurchasingSessionLine).where(PurchasingSessionLine.session_id==session.id,PurchasingSessionLine.variation_id=='VAR2'))
        assert special.removed
        assert service._group(db,session,2).status=='DISCARDED'
        service.create_po(db,session_id=session.id,supplier_id=1,actor_id=1)
        assert session.status=='COMPLETE'
        with pytest.raises(ValueError,match='complete'):
            service.refresh(db,session_id=session.id,actor_id=1,request_key='closed',gateway=gateway)


def test_historical_cost_is_provisional_and_missing_cost_is_never_zero(engine):
    with Session(engine) as db:
        cfg=db.scalar(select(VendorSkuConfig).where(VendorSkuConfig.vendor_id==1,VendorSkuConfig.square_variation_id=='VAR1'))
        cfg.unit_cost=None;db.flush()
        session=generate(db);row=line(db,session)
        assert row.unit_cost is None and row.cost_source=='UNKNOWN'
        with pytest.raises(ValueError,match='Review uncertain'):
            service.create_po(db,session_id=session.id,supplier_id=1,actor_id=1)
        service.update_line(db,session_id=session.id,line_id=row.id,actor_id=1,quantity=9,cost='3.25',acknowledge=True)
        assert row.cost_source=='OWNER_CONFIRMED' and cfg.unit_cost is None
        po=service.create_po(db,session_id=session.id,supplier_id=1,actor_id=1)
        assert db.scalar(select(PurchaseOrderLine.unit_cost).where(PurchaseOrderLine.purchase_order_id==po.id))==Decimal('3.25')


def test_real_gateway_requests_four_live_locations_and_never_fills_missing_count(engine):
    from app.services.v2_ordering_square_gateway import SquareOrderingReadGateway
    calls=[]
    def post(path,payload):
        calls.append((path,copy.deepcopy(payload)))
        if path.endswith('search-catalog-items'):
            return {'items':[{'id':'ITEM','created_at':'2026-01-01T00:00:00Z','item_data':{'name':'Live product','variations':[
                {'id':'VAR1','item_variation_data':{'sku':'SKU1','name':'Original','item_variation_vendor_infos':[
                    {'item_variation_vendor_info_data':{'vendor_id':'V1'}}]}}]}}]}
        if path.endswith('counts/batch-retrieve'):
            assert payload['location_ids']==['L1','L2','L3','L4']
            return {'counts':[{'catalog_object_id':'VAR1','location_id':sid,'state':'IN_STOCK','quantity':'4'} for sid in ('L1','L2','L3')]}
        if path.endswith('orders/search'):return {'orders':[]}
        if path.endswith('changes/batch-retrieve'):return {'changes':[]}
        raise AssertionError(path)
    with Session(engine) as db:
        session=generate(db,SquareOrderingReadGateway(post))
        revision=db.scalar(select(PurchasingSessionRevision).where(PurchasingSessionRevision.session_id==session.id))
        product=revision.evidence['products']['VAR1']
        assert product['stores']['1']['current_inventory']=='4'
        assert product['stores']['4']['current_inventory'] is None
        assert product['forecasts']['4']['quantity'] is None
        assert len(calls)==4
        assert db.scalar(select(func.count()).select_from(SquareSyncEvent))==0


def test_concurrent_generation_uses_database_uniqueness(engine):
    from threading import Barrier
    barrier=Barrier(2)
    def worker():
        gateway=Gateway();gateway.hook=lambda:barrier.wait(10)
        with Session(engine) as db:
            session=generate(db,gateway,key='same-concurrent-request');db.commit();return session.id
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(worker);second=pool.submit(worker)
        assert first.result(20)==second.result(20)
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(PurchasingSession))==1
        assert db.scalar(select(func.count()).select_from(PurchasingSessionRevision))==1


def test_historical_purchase_cost_requires_review_and_cannot_become_configuration(engine):
    with Session(engine) as db:
        db.get(PurchaseOrder,1).status=PurchaseOrderStatus.COMPLETED
        db.get(PurchaseOrderLine,1).unit_cost=Decimal('4.25')
        config=db.scalar(select(VendorSkuConfig).where(VendorSkuConfig.vendor_id==1,VendorSkuConfig.square_variation_id=='VAR1'))
        config.unit_cost=None;db.flush()
        session=generate(db);row=line(db,session)
        assert row.unit_cost==Decimal('4.25') and row.cost_source=='HISTORICAL_REVIEW'
        with pytest.raises(ValueError,match='Review uncertain'):
            service.create_po(db,session_id=session.id,supplier_id=1,actor_id=1)
        service.update_line(db,session_id=session.id,line_id=row.id,actor_id=1,cost='4.25',acknowledge=True)
        service.create_po(db,session_id=session.id,supplier_id=1,actor_id=1)
        assert config.unit_cost is None


def test_confirmed_zero_need_missing_cost_does_not_clutter_review(engine):
    with Session(engine) as db:
        config=db.scalar(select(VendorSkuConfig).where(VendorSkuConfig.square_variation_id=='VAR2'))
        config.unit_cost=None;db.flush()
        session=generate(db)
        assert db.scalar(select(func.count()).select_from(PurchasingSessionLine).where(PurchasingSessionLine.session_id==session.id))==1
        assert service.catalog_search(db,session_id=session.id,actor_id=1,query='SEARCH-ONLY')


def test_ambiguous_canonical_identity_is_reviewed_without_changing_cogs(engine):
    from dataclasses import replace
    gateway=Gateway();gateway.catalog['VAR1']=replace(gateway.catalog['VAR1'],vendor_ids=('V1','V2'))
    with Session(engine) as db:
        session=generate(db,gateway);row=line(db,session)
        assert row.canonical_vendor_id is None and row.supplier_id is None
        assert any('Canonical' in message for message in row.accepted['review'])
        service.update_line(db,session_id=session.id,line_id=row.id,actor_id=1,supplier_id=2,quantity=5,acknowledge=True)
        service.create_po(db,session_id=session.id,supplier_id=2,actor_id=1)
        assert row.canonical_vendor_id is None and row.accepted['square_vendor_ids']==['V1','V2']
        assert db.scalar(select(func.count()).select_from(PurchasingProductAuthority))==0


def test_stale_evidence_requires_explicit_refresh_before_draft_conversion(engine,monkeypatch):
    with Session(engine) as db:
        session=generate(db);stamp=service.now()
        monkeypatch.setattr(service,'now',lambda:stamp+timedelta(hours=25))
        with pytest.raises(ValueError,match='Refresh the source evidence'):
            service.create_po(db,session_id=session.id,supplier_id=1,actor_id=1)
        assert db.scalar(select(func.count()).select_from(PurchaseOrder))==1


def test_invalid_historical_or_configured_cost_remains_unknown(engine):
    with Session(engine) as db:
        config=db.scalar(select(VendorSkuConfig).where(VendorSkuConfig.vendor_id==1,VendorSkuConfig.square_variation_id=='VAR1'))
        config.unit_cost=Decimal('NaN');db.flush()
        session=generate(db);row=line(db,session)
        assert row.unit_cost is None and row.cost_source=='INVALID'
        with pytest.raises(ValueError,match='Review uncertain'):
            service.create_po(db,session_id=session.id,supplier_id=1,actor_id=1)
