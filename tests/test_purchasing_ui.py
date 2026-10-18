"""Native V2, bounded 500-SKU presentation and owner draft concurrency."""
from datetime import datetime,timedelta,timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
import copy
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select,func
from sqlalchemy.orm import Session
from test_purchasing_phase2 import engine,receiving_db,database_template,Gateway
from app.models import (PurchasingSession,PurchasingSessionLine,PurchaseOrder,PurchaseOrderLine,
    PurchaseOrderStoreAllocation,VendorSkuConfig,Store,FundingActualActivity,FundingLedgerEntry,SquareSyncEvent,
    PurchasingSessionGroup)
from app.services import purchasing_session_service as service
from app.services.v2_ordering_square_gateway import SquareProductMetadata,SquareStoreSkuData,SquareOrderingReadResult
from app.services.v2_ordering_normalization_service import DailyQuantity
from app.services.v2_ordering_policy_service import DataSourceEvidence
from app.services.purchasing_evidence_service import BUSINESS_TIMEZONE

class BulkGateway(Gateway):
    def __init__(self):
        super().__init__();stamp=datetime.now(timezone.utc)
        self.catalog={'VAR1':self.catalog['VAR1']}
        for i in range(1,500):
            self.catalog[f'BULK-{i:04d}']=SquareProductMetadata(f'BULK-{i:04d}',f'BULK-{i:04d}',
                f'Representative Product {i:04d}','Original',stamp-timedelta(days=240),False,vendor_ids=(f'V{1+i%2}',))
    def fetch(self,**kw):
        end=kw['as_of'].astimezone(BUSINESS_TIMEZONE).date()-timedelta(days=1);rows={}
        for sid in kw['location_by_store']:
            for vid in kw['variation_ids']:
                rows[(sid,vid)]=SquareStoreSkuData(sid,vid,Decimal(10+sid),True,
                    tuple(DailyQuantity(end-timedelta(days=i),Decimal(sid)) for i in range(kw['history_days'])),(),
                    (DataSourceEvidence('inventory',kw['as_of']),DataSourceEvidence('sales',kw['as_of']),
                     DataSourceEvidence('stockout_history',None,available=False,complete=False)),())
        return SquareOrderingReadResult(self.catalog,rows)


def app_client(db):
    from app.main import app as application
    from app.auth import Principal as Actor,Role
    from app.db import get_db
    from app.routers import management,purchasing
    app=FastAPI();app.state.templates=application.state.templates;app.include_router(purchasing.router)
    app.dependency_overrides[get_db]=lambda:db
    app.dependency_overrides[management.ordering_access]=lambda:Actor(1,'owner',Role.ADMIN,None,True)
    client=TestClient(app,follow_redirects=False);client.cookies.set('csrf_token','test')
    return client,app


def patch(client,sid,version,changes=None,**kw):
    return client.patch(f'/management/purchasing/sessions/{sid}/draft',json={
        'version':version,'kind':'line','changes':changes or {},**kw},headers={'X-CSRF-Token':'test'})


def test_500_sku_workspace_inline_decisions_allocations_and_conversion(engine):
    gateway=BulkGateway()
    with Session(engine) as db:
        for index,(vid,product) in enumerate(gateway.catalog.items()):
            if vid=='VAR1':continue
            for vendor in (1,2):db.add(VendorSkuConfig(vendor_id=vendor,sku=vid+f'-{vendor}',square_variation_id=vid,
                unit_cost=Decimal(3)+Decimal(index%10)/2,pack_size=(index%3)+3,min_order_qty=0,active=True,is_default_vendor=product.vendor_ids==(f'V{vendor}',)))
        db.flush()
        session=service.generate(db,actor_id=1,request_key='bulk500',gateway=gateway);db.commit()
        client,app=app_client(db);url=f'/management/purchasing/sessions/{session.id}'
        home=client.get(url);assert home.status_code==200,home.text
        assert '/v2-assets/v2.css' in home.text and 'v2-sidebar' in home.text
        assert 'data-supplier-groups' in home.text and '<form class="product"' not in home.text
        assert len(home.content)<35000  # No 500-row or evidence dump in initial HTML.
        meta=client.get(url+'/workspace').json()
        assert meta['matched_count']==500 and sum(g['product_count'] for g in meta['groups'])==500
        data=client.get(url+'/workspace?supplier=1&page_size=20').json()
        assert len(data['rows'])==20 and data['total']==250 and data['pages']==13
        assert len({row['id'] for row in data['rows']} & {row['id'] for row in client.get(url+'/workspace?supplier=1&page_size=20&page=2').json()['rows']})==0
        row=next(row for row in data['rows'] if row['variation_id']=='VAR1')
        assert {sid:forecast['raw_need'] for sid,forecast in row['forecasts'].items()}=={'1':59,'2':128,'3':197,'4':266}
        assert row['need']==650 and sum(row['allocations'].values())==row['quantity']
        assert row['allocations']!={sid:f['raw_need'] for sid,f in row['forecasts'].items()} or row['quantity']==650
        need=copy.deepcopy(row['forecasts']);version=meta['version'];old_units={g['id']:g['units'] for g in meta['groups']}
        response=patch(client,session.id,version,{'quantity':17,'supplier_id':2},line_id=row['id']);assert response.status_code==200,response.text
        version=response.json()['version']
        saved=client.get(url+'/draft?line_ids='+str(row['id'])).json()['lines'][str(row['id'])]
        assert saved['quantity']==17 and saved['supplier_id']==2 and sum(saved['allocations'].values())==17
        current=db.get(PurchasingSessionLine,row['id']);db.refresh(current)
        assert current.accepted['forecasts']==need
        meta=client.get(url+'/workspace').json();bygroup={g['id']:g for g in meta['groups']}
        assert bygroup[1]['product_count']==249 and bygroup[2]['product_count']==251
        assert bygroup[1]['units']==old_units[1]-650 and bygroup[2]['units']==old_units[2]+17
        # Old tab cannot overwrite the saved decision, and no blanket rebase occurs.
        stale=patch(client,session.id,version-1,{'quantity':99},line_id=row['id'])
        assert stale.status_code==409 and stale.json()['conflict']
        response=patch(client,session.id,version,{'quantity':17,'allocations':{'1':2,'2':3,'3':4,'4':8}},line_id=row['id'])
        assert response.status_code==200;version=response.json()['version']
        bad=patch(client,session.id,version,{'quantity':18},line_id=row['id']);assert bad.status_code==400
        assert db.get(PurchasingSessionLine,row['id']).quantity==17
        # Refresh reads new evidence but preserves all saved owner decisions.
        service.refresh(db,session_id=session.id,actor_id=1,request_key='refresh500',gateway=gateway);db.commit()
        db.refresh(current);assert current.quantity==17 and current.supplier_id==2 and current.allocations=={'1':2,'2':3,'3':4,'4':8}
        version=client.get(url+'/workspace').json()['version']
        r=patch(client,session.id,version,{'account_id':10},kind='account',supplier_id=2);assert r.status_code==200
        version=r.json()['version']
        result=client.post(url+'/groups/2/po',data={'csrf_token':'test','version':version});assert result.status_code==303,result.text
        po_id=int(result.headers['location'].split('/')[-1]);line=db.scalar(select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id==po_id,PurchaseOrderLine.variation_id=='VAR1'))
        assert line.ordered_qty==17
        allocations=dict(db.execute(select(PurchaseOrderStoreAllocation.store_id,PurchaseOrderStoreAllocation.allocated_qty).where(PurchaseOrderStoreAllocation.purchase_order_line_id==line.id)).all())
        assert allocations=={1:2,2:3,3:4,4:8}
        assert db.scalar(select(PurchasingSessionGroup).where(PurchasingSessionGroup.purchase_order_id==po_id)).intended_account_id==10
        assert client.post(url+'/groups/1/po',data={'csrf_token':'test','version':version}).status_code==400
        searched=client.get(url+'/workspace?supplier=1&q=BULK-0498').json();assert searched['total']==1
        costs=client.get(url+'/workspace?supplier=1&sort=cost_desc').json()['rows'];assert [Decimal(r['cost']) for r in costs]==sorted([Decimal(r['cost']) for r in costs],reverse=True)
        assert client.get(url+'/workspace?store=4&supplier=1').json()['total']==249
        assert client.get(url+'/workspace?supplier=1&page_size=1000000').status_code==400
        assert client.get(url+'/workspace?supplier=1&sort=arbitrary').status_code==400
        for model in (FundingActualActivity,FundingLedgerEntry,SquareSyncEvent):assert db.scalar(select(func.count()).select_from(model))==0


def test_dynamic_store_columns_preserve_saved_allocation_destinations(engine):
    with Session(engine) as db:
        session=service.generate(db,actor_id=1,request_key='stores',gateway=Gateway());db.commit()
        db.add(Store(id=5,name='Future Store',square_location_id='L5',active=True));db.get(Store,4).active=False;db.commit()
        view=service.table_workspace(db,session_id=session.id,actor_id=1,supplier=1)
        assert {s['id'] for s in view['stores']}=={'1','2','3','5'}
        assert {s['id'] for s in view['allocation_stores']}=={'1','2','3','4'}
        assert '5' not in view['rows'][0]['forecasts']  # New columns remain unknown, never invented.
        assert '4' in view['rows'][0]['allocations']


def test_inline_csrf_permissions_remove_undo_and_invalid_fields(engine):
    from app.auth import Principal as Actor,Role
    from app.routers import management
    from app.config import settings
    with Session(engine) as db:
        session=service.generate(db,actor_id=1,request_key='inline',gateway=Gateway());db.commit()
        row=db.scalar(select(PurchasingSessionLine).where(PurchasingSessionLine.session_id==session.id))
        client,app=app_client(db);url=f'/management/purchasing/sessions/{session.id}'
        assert client.patch(url+'/draft',json={'version':session.version}).status_code==403
        assert patch(client,session.id,session.version,{'unexpected':3},line_id=row.id).status_code==400
        assert patch(client,session.id,session.version,{'quantity':True},line_id=row.id).status_code==400
        assert client.patch(url+'/draft',json={},headers={'X-CSRF-Token':'test'}).status_code==400
        response=patch(client,session.id,session.version,kind='remove',line_id=row.id);assert response.status_code==200
        assert client.get(url+'/workspace?supplier=1&state=removed').json()['total']==1
        response=patch(client,session.id,response.json()['version'],kind='undo',line_id=row.id);assert response.status_code==200
        assert client.get(url+'/workspace?supplier=1').json()['total']==1
        app.dependency_overrides[management.ordering_access]=lambda:Actor(2,'other',Role.ADMIN,None,True)
        for suffix in ('/workspace','/draft','/catalog?q=Product'):assert client.get(url+suffix).status_code==403
        assert patch(client,session.id,session.version,{'quantity':7},line_id=row.id).status_code==403


def test_uncertainties_are_separate_from_purchasing_and_zero_owner_quantities_remain_saved(engine):
    gateway=Gateway();gateway.missing={(4,'VAR1')}
    with Session(engine) as db:
        session=service.generate(db,actor_id=1,request_key='review-ui',gateway=gateway);db.commit()
        client,app=app_client(db);url=f'/management/purchasing/sessions/{session.id}'
        primary=client.get(url+'/workspace?supplier=1').json()
        review=client.get(url+'/workspace?supplier=1&state=review').json()
        assert primary['total']==0 and review['total']==1 and review['review_count']==1
        row=review['rows'][0];assert row['forecasts']['4']['raw_need'] is None
        response=patch(client,session.id,review['version'],{'acknowledge':True},line_id=row['id']);assert response.status_code==200
        assert client.get(url+'/workspace?supplier=1').json()['total']==1
        response=patch(client,session.id,response.json()['version'],{'quantity':0},line_id=row['id']);assert response.status_code==200
        assert client.get(url+'/workspace?supplier=1').json()['total']==0
        all_saved=client.get(url+'/workspace?supplier=1&state=all').json()
        assert all_saved['total']==1 and all_saved['rows'][0]['quantity']==0 and all_saved['rows'][0]['need']>0


def test_native_purchasing_navigation_is_feature_and_owner_scoped(engine,monkeypatch):
    from app.auth import Principal as Actor,Role
    from app.config import settings
    from app.v2.navigation import build_navigation
    from starlette.requests import Request
    request=Request({'type':'http','method':'GET','path':'/management/purchasing','headers':[],'query_string':b''})
    request.state.permission_flags={'ordering.manage':True,'nav.inventory.all':True}
    def links():return [item.href for section in build_navigation(request) for item in section.children if item.available]
    request.state.principal=Actor(1,'owner',Role.ADMIN,None,True)
    assert '/management/purchasing' in links()
    request.state.principal=Actor(2,'other',Role.ADMIN,None,True)
    assert '/management/purchasing' not in links()
    request.state.principal=Actor(1,'owner',Role.ADMIN,None,True)
    monkeypatch.setattr(settings,'purchasing_v3_phase2_enabled',False)
    assert '/management/purchasing' not in links()


def test_inline_store_plan_sum_validation_refresh_and_supplier_preservation(engine):
    """The UI sends the complete edited plan and its sum atomically, using existing decisions."""
    with Session(engine) as db:
        session=service.generate(db,actor_id=1,request_key='store-plan',gateway=Gateway());db.commit()
        client,_=app_client(db);url=f'/management/purchasing/sessions/{session.id}'
        meta=client.get(url+'/workspace?supplier=1').json();row=meta['rows'][0]
        original=copy.deepcopy(db.get(PurchasingSessionLine,row['id']).accepted)
        plan={'1':60,'2':75,'3':90,'4':76}
        r=patch(client,session.id,meta['version'],{'quantity':sum(plan.values()),'allocations':plan},line_id=row['id']);assert r.status_code==200,r.text
        version=r.json()['version']
        assert client.get(url+'/draft?line_ids='+str(row['id'])).json()['lines'][str(row['id'])]['quantity']==301
        plan.update({'1':0,'2':12,'3':13,'4':14})
        r=patch(client,session.id,version,{'quantity':39,'allocations':plan,'supplier_id':2},line_id=row['id']);assert r.status_code==200,r.text
        version=r.json()['version']
        for invalid in ({'1':-1,'2':12,'3':13,'4':15},{'1':0.5,'2':12,'3':13,'4':13.5},{'1':True,'2':12,'3':13,'4':13},{'1':'','2':12,'3':13,'4':14}):
            r=patch(client,session.id,version,{'quantity':39,'allocations':invalid},line_id=row['id']);assert r.status_code==400,r.text
        service.refresh(db,session_id=session.id,actor_id=1,request_key='store-plan-refresh',gateway=Gateway());db.commit()
        current=db.get(PurchasingSessionLine,row['id']);db.refresh(current)
        assert current.quantity==39 and current.allocations==plan and current.supplier_id==2
        assert current.accepted==original
        saved=client.get(url+'/draft?line_ids='+str(row['id'])).json()['lines'][str(row['id'])]
        assert saved['allocations']==plan and saved['quantity']==39
        version=client.get(url+'/workspace').json()['version']
        # The last allocation change immediately precedes PO creation; stale versions are refused.
        plan['4']=15
        r=patch(client,session.id,version,{'quantity':40,'allocations':plan},line_id=row['id']);assert r.status_code==200
        assert client.post(url+'/groups/2/po',data={'csrf_token':'test','version':version}).status_code==400
        created=client.post(url+'/groups/2/po',data={'csrf_token':'test','version':r.json()['version']});assert created.status_code==303,created.text
        po_id=int(created.headers['location'].split('/')[-1])
        line=db.scalar(select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id==po_id,PurchaseOrderLine.variation_id==row['variation_id']))
        assert line.ordered_qty==40
        assert dict(db.execute(select(PurchaseOrderStoreAllocation.store_id,PurchaseOrderStoreAllocation.allocated_qty).where(PurchaseOrderStoreAllocation.purchase_order_line_id==line.id)).all())=={int(k):v for k,v in plan.items()}
        for model in (FundingActualActivity,FundingLedgerEntry,SquareSyncEvent):assert db.scalar(select(func.count()).select_from(model))==0


def test_v3_allocations_connect_to_existing_partial_destination_receiving(engine,monkeypatch):
    """Prove the existing destination-receipt connection, without claiming a central transfer workflow."""
    from app.services import purchase_order_admin_service as receiving
    from app.services.purchasing_inventory_service import load_incoming_supply
    from app.models import PurchaseOrderStatus,PurchaseOrderReceiptAllocation
    sent=[]
    monkeypatch.setattr(receiving,'_square_post',lambda path,payload,**kw:sent.append(copy.deepcopy(payload)) or {'counts':[]})
    monkeypatch.setattr('app.services.v2_order_payments_service.initialize_new_order_if_configured',lambda *a,**kw:None)
    with Session(engine) as db:
        session=service.generate(db,actor_id=1,request_key='v3-receipt',gateway=Gateway());db.commit()
        client,_=app_client(db);url=f'/management/purchasing/sessions/{session.id}'
        row=client.get(url+'/workspace?supplier=1').json()['rows'][0]
        response=patch(client,session.id,session.version,{'quantity':17,'allocations':{'1':2,'2':3,'3':4,'4':8}},line_id=row['id']);assert response.status_code==200
        created=client.post(url+'/groups/1/po',data={'csrf_token':'test','version':response.json()['version']});assert created.status_code==303
        po_id=int(created.headers['location'].split('/')[-1]);po=db.get(PurchaseOrder,po_id)
        line=db.scalar(select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id==po_id))
        # Local fixture transition only. Production requires the existing order-submission workflow.
        po.status=PurchaseOrderStatus.IN_TRANSIT;db.commit()
        receiving.save_purchase_order_received_quantities(db,purchase_order_id=po_id,
            received_qty_by_line_store={(line.id,1):1},actor_principal_id=1,request_key='v3-partial');db.commit()
        assert line.ordered_qty==17 and line.received_qty_total==1 and line.in_transit_qty==16
        assert dict(db.execute(select(PurchaseOrderStoreAllocation.store_id,PurchaseOrderStoreAllocation.allocated_qty).where(PurchaseOrderStoreAllocation.purchase_order_line_id==line.id)).all())=={1:2,2:3,3:4,4:8}
        assert db.scalar(select(PurchaseOrderReceiptAllocation).where(PurchaseOrderReceiptAllocation.purchase_order_line_id==line.id)).quantity_delta==1
        assert sent==[] and db.scalar(select(func.count()).select_from(SquareSyncEvent))==0
        assert sum(x.quantity for x in load_incoming_supply(db) if x.purchase_order_id==po_id)==16
        result=receiving.receive_purchase_order(db,purchase_order_id=po_id,actor_principal_id=1)
        assert result['succeeded']==1 and len(sent)==1
        adjustment=sent[0]['changes'][0]['adjustment'];assert adjustment['quantity']=='1' and adjustment['location_id']=='L1'
        assert receiving.receive_purchase_order(db,purchase_order_id=po_id,actor_principal_id=1)['skipped_already_synced']==1
        assert len(sent)==1 and po.status==PurchaseOrderStatus.IN_TRANSIT
        assert db.scalar(select(func.count()).select_from(FundingActualActivity))==0
