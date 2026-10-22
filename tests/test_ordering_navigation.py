"""Inventory category navigation is a read-only projection of existing purchasing data."""
import copy
from dataclasses import replace
from decimal import Decimal
import json
from time import perf_counter
from pathlib import Path

from sqlalchemy import select, text, event
from sqlalchemy.orm import Session
from test_purchasing_ui import engine, receiving_db, database_template, BulkGateway, app_client, patch
from app.models import VendorSkuConfig, PurchasingSessionLine, PurchasingSessionGroup, PurchaseOrder
from app.services import purchasing_session_service as service
from app.services import purchasing_item_type_service as types
from app.services.v2_ordering_square_gateway import SquareOrderingReadGateway


class CategoryGateway(BulkGateway):
    category_names={'liquids':'E-Liquid','devices':'Devices','pods':'Pods'}
    def __init__(self):
        super().__init__()
        self.catalog={vid:replace(p,vendor_ids=('V1',),category_ids=('liquids','pods') if i%2 else ('devices',),
            reporting_category_id='liquids' if i%2 else 'devices') for i,(vid,p) in enumerate(self.catalog.items())}


def seed(db):
    gateway=CategoryGateway()
    for vid in gateway.catalog:
        if vid=='VAR1':continue
        db.add(VendorSkuConfig(vendor_id=1,sku=vid,square_variation_id=vid,
            unit_cost=Decimal(3),pack_size=1,min_order_qty=0,active=True,is_default_vendor=True))
    db.flush()
    session=service.generate(db,actor_id=1,request_key='categories',gateway=gateway);db.commit()
    return session


def test_navigation_search_counts_quantity_retention_and_no_data_writes(engine):
    with Session(engine) as db:
        session=seed(db);client,app=app_client(db);base=f'/management/purchasing/sessions/{session.id}'
        row=db.scalar(select(PurchasingSessionLine).where(PurchasingSessionLine.session_id==session.id))
        response=patch(client,session.id,session.version,{'quantity':17,'allocations':{'1':2,'2':3,'3':4,'4':8}},line_id=row.id)
        assert response.status_code==200,response.text
        before={table:[dict(r) for r in db.execute(text(f'SELECT * FROM {table} ORDER BY 1')).mappings()] for table in
            ('purchasing_session_lines','purchasing_sessions','purchase_orders','purchase_order_lines','vendor_sku_configs','ordering_product_lifecycle','audit_log')}
        queries=[]
        def record(conn,cursor,stmt,params,ctx,many):queries.append(stmt)
        event.listen(engine,'before_cursor_execute',record)
        started=perf_counter()
        page=client.get(base+'/workspace?supplier=1&page_size=20').json()
        latency=(perf_counter()-started)*1000
        event.remove(engine,'before_cursor_execute',record)
        assert len(page['rows'])==20
        assert sum(c['count'] for c in page['categories'])==500
        assert {c['name'] for c in page['categories']}=={'Devices','E-Liquid'}
        assert len(page['rows'])<sum(c['count'] for c in page['categories'])
        devices=next(c for c in page['categories'] if c['name']=='Devices')
        selected=client.get(base+'/workspace',params={'supplier':1,'category':devices['id'],'page_size':20}).json()
        assert selected['total']==250 and selected['pages']==13
        assert all(r['item_type']['name']=='Devices' for r in selected['rows'])
        searched=client.get(base+'/workspace',params={'supplier':1,'q':'Representative Product 0001'}).json()
        assert searched['total']==1 and searched['rows'][0]['item_type']['name']=='E-Liquid'
        empty=client.get(base+'/workspace',params={'supplier':1,'q':'DOES-NOT-EXIST'}).json()
        assert not empty['rows'] and empty['total']==0
        assert client.get(base+'/draft',params={'line_ids':row.id}).json()['lines'][str(row.id)]['quantity']==17
        after={table:[dict(r) for r in db.execute(text(f'SELECT * FROM {table} ORDER BY 1')).mappings()] for table in before}
        assert before==after
        assert not any(stmt.lstrip().split()[0].upper() in ('INSERT','UPDATE','DELETE') for stmt in queries)
        print(f'500 products: {len(queries)} SQL statements, {latency:.1f} ms workspace, 0 writes; category counts span 25 pages.')


def test_legacy_category_lookup_never_backfills_evidence_and_pdf_metadata(engine,monkeypatch):
    from test_purchasing_phase2 import Gateway
    with Session(engine) as db:
        session=service.generate(db,actor_id=1,request_key='legacy',gateway=Gateway());db.commit()
        row=db.scalar(select(PurchasingSessionLine).where(PurchasingSessionLine.session_id==session.id))
        saved=dict(row.accepted);saved.pop('inventory_item_type');row.accepted=saved;db.commit()
        before=copy.deepcopy(row.accepted)
        monkeypatch.setattr(types,'legacy_types',lambda:({'VAR1':{'id':'existing','name':'Existing inventory type','issue':None}},None))
        page=service.table_workspace(db,session_id=session.id,actor_id=1,supplier=1)
        assert page['rows'][0]['item_type']['name']=='Existing inventory type'
        db.refresh(row);assert row.accepted==before
        po=service.create_po(db,session_id=session.id,supplier_id=1,actor_id=1);db.commit()
        group=service.table_workspace(db,session_id=session.id,actor_id=1)['groups'][0]
        assert group['po_id']==po.id and group['po_status']=='DRAFT' and not group['pdf_available']
        # UI eligibility reflects a pre-existing original, without generating one on read.
        po.pdf_path='generated/purchase_orders/synthetic.pdf';db.commit()
        assert service.table_workspace(db,session_id=session.id,actor_id=1)['groups'][0]['pdf_available']


def test_classifications_preserve_reporting_identity_and_report_inconsistency():
    from types import SimpleNamespace as Product
    reporting=types.classify(Product(reporting_category_id='p',category_ids=('p','q')),{'p':'Pods','q':'Devices'})
    assert reporting['name']=='Pods' and reporting['issue'] is None
    ambiguous=types.classify(Product(category_ids=('p','q')),{'p':'Pods','q':'Devices'})
    assert ambiguous['name']=='Pods / Devices' and ambiguous['issue']
    assert types.classify(Product(category_ids=()),{})['id']=='missing'
    assert types.classify(Product(category_ids=('p',)),{})['issue']
    assert types.classify(Product(category_ids=('p',)),{'p':'Same name'})['id']!=types.classify(Product(category_ids=('q',)),{'q':'Same name'})['id']


def test_square_category_reads_use_existing_policy_and_detect_partial_pagination():
    calls=[]
    def post(path,payload):
        calls.append((path,payload))
        if 'cursor' not in payload:return {'objects':[{'id':'a','category_data':{'name':'Devices'}}],'cursor':'next'}
        return {'objects':[{'id':'b','category_data':{'name':'E-Liquid'}}]}
    gateway=SquareOrderingReadGateway(post)
    assert gateway.fetch_category_names()=={'a':'Devices','b':'E-Liquid'}
    assert len(calls)==2 and all(p=='/v2/catalog/search' for p,_ in calls)


def test_legacy_cache_reuses_one_bulk_read(monkeypatch):
    from types import SimpleNamespace
    calls=[]
    class Gateway:
        def fetch_active_catalog(self):calls.append('catalog');return {'v':SimpleNamespace(category_ids=('p',))}
        def fetch_category_names(self):calls.append('categories');return {'p':'Pods'}
    monkeypatch.setattr(types.settings,'square_access_token','synthetic-only')
    monkeypatch.setattr(types,'SquareOrderingReadGateway',Gateway)
    types._cache.clear()
    assert types.legacy_types()[0]['v']['name']=='Pods'
    assert types.legacy_types()[0]['v']['name']=='Pods'
    assert calls==['catalog','categories']
    types._cache.clear()
