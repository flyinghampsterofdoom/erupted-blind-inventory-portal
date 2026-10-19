from datetime import date,datetime,timezone
from decimal import Decimal as D
from types import SimpleNamespace as Obj
import pytest
from sqlalchemy import create_engine,event,select
from sqlalchemy.orm import Session
from app.models import Base,Vendor,VendorSkuConfig,OrderingCatalogIdentity,Store
from app.services import consignment_reporting_service as service

@pytest.fixture
def db():
    engine=create_engine('sqlite:///:memory:')
    @event.listens_for(engine,'connect')
    def functions(c,_):c.create_function('char_length',1,len)
    Base.metadata.create_all(engine,tables=[Store.__table__,Vendor.__table__,VendorSkuConfig.__table__,OrderingCatalogIdentity.__table__])
    with Session(engine) as db:
        db.add_all([Vendor(id=10,square_vendor_id='SV10',name='Streamline',active=True),Vendor(id=11,square_vendor_id='SV11',name='BIG Wholesale',active=True)])
        for i in range(1,5):db.add(Store(id=i,name=f'Store {i}',active=True,square_location_id=f'L{i}'))
        for i,(vid,sku,cost,vendor) in enumerate([('A','Q904802','2.50',10),('B','B','1.20',10),('C','OTHER','4',11)],1):
            db.add(OrderingCatalogIdentity(square_variation_id=vid,sku=sku,product_name=f'Product {sku}',variation_name='Blue',square_is_deleted=False,last_seen_at=datetime.now(timezone.utc)))
            db.add(VendorSkuConfig(id=i,vendor_id=vendor,sku=sku,square_variation_id=vid,unit_cost=D(cost),active=True,is_default_vendor=True))
        db.commit();yield db
    engine.dispose()

class Gateway:
    def __init__(self,values=None):self.values=values
    def fetch_current_inventory_counts(self,**kw):
        values=self.values or {('L'+str(i),v):Obj(quantity=D(q),calculated_at=datetime.now(timezone.utc)) for v,qs in [('A',['10','-3','0','5']),('B',['4']*4)] for i,q in enumerate(qs,1)}
        return Obj(counts=values)

class Reader:
    def __init__(self,orders):self.orders=orders;self.calls=[]
    def search(self,**kw):self.calls.append(kw);yield self.orders

def order(oid,qty='5',*,vid='A',location='L1',stamp='2025-10-12T12:00:00Z',**extra):
    return dict(id=oid,location_id=location,state='COMPLETED',closed_at=stamp,updated_at=stamp,line_items=[{'uid':'line','catalog_object_id':vid,'quantity':qty}],**extra)

def report(db,orders,**kw):return service.sales_report(db,vendor_id=10,start_date=date(2025,10,11),end_date=date(2025,10,17),reader=Reader(orders),**kw)

def test_positive_inventory_across_four_stores_and_negative_is_only_flagged(db):
    result=service.inventory_value(db,vendor_id=10,gateway=Gateway())
    assert result['assigned_skus']==2 and result['positive_units']==31
    assert result['value']==D('56.70') and result['complete']
    assert len(result['stores'])==4
    assert result['negative_observations']==[{'sku':'Q904802','store':'Store 2','quantity':D('-3')}]
    assert next(r for r in result['rows'] if r['sku']=='Q904802' and r['store_id']=='2')['value']==0

def test_missing_cost_and_unknown_quantity_are_not_zero(db):
    db.get(VendorSkuConfig,2).unit_cost=None;db.flush()
    result=service.inventory_value(db,vendor_id=10,gateway=Gateway({('L1','A'):Obj(quantity=D('0'),calculated_at=None),('L1','B'):Obj(quantity=D('3'),calculated_at=None)}))
    assert not result['complete'] and result['value'] is None
    assert {'Q904802','B'}=={r['sku'] for r in result['issues']}
    assert any(r['reason'].startswith('Current Square quantity unknown') for r in result['issues'])

def test_q904802_sales_ignore_inventory_payments_and_financing(db,monkeypatch):
    monkeypatch.setattr(service,'inventory_value',lambda *a,**k:pytest.fail('Sales must not query inventory'))
    result=report(db,[order(str(i),location=f'L{i}') for i in range(1,5)]+[order('unrelated','999',vid='C')])
    assert result['net_units']==20 and result['cogs']==50 and len(result['stores'])==4
    assert result['complete'] and not db.new and not db.dirty

def test_inclusive_local_dates_and_completed_only(db):
    included=[order('start',stamp='2025-10-11T07:00:00Z'),order('end',stamp='2025-10-18T06:59:59.999999Z')]
    before=order('before',stamp='2025-10-11T06:59:59Z');after=order('after',stamp='2025-10-18T07:00:00Z');cancelled=order('cancel');cancelled['state']='CANCELED'
    result=report(db,included+[before,after,cancelled])
    assert result['net_units']==10 and result['cogs']==25
    assert result['end_exclusive'].isoformat()=='2025-10-18T07:00:00+00:00'

def test_itemized_returns_refunds_and_original_sale_reference(db):
    returned=order('return','0');returned['line_items']=[]
    returned['returns']=[{'uid':'r','source_order_id':'older','return_line_items':[{'uid':'r1','source_line_item_uid':'line','quantity':'2'}]}]
    returned['refunds']=[{'id':'refund','status':'COMPLETED'}]
    result=report(db,[order('sale','5'),returned],retrieve_order=lambda oid:order('older','5'))
    assert result['sold']==5 and result['returned']==2 and result['net_units']==3 and result['cogs']==D('7.50')
    assert result['amount_only_refunds']==0

def test_amount_only_refunds_never_invent_returned_quantities(db):
    source=order('sale','5');source['refunds']=[{'status':'COMPLETED','amount_money':{'amount':123}}]
    result=report(db,[source]);assert result['net_units']==5 and result['cogs']==D('12.50') and result['amount_only_refunds']==1

def test_zero_sales_is_valid_but_missing_cost_on_activity_is_incomplete(db):
    db.get(VendorSkuConfig,1).unit_cost=None;db.flush()
    zero=report(db,[]);assert zero['complete'] and zero['cogs']==zero['net_units']==0
    missing=report(db,[order('sale')]);assert not missing['complete'] and missing['cogs'] is None
    assert missing['issues']==[{'sku':'Q904802','reason':'Configured vendor unit cost missing, zero or invalid'}]

def test_duplicate_pages_corrections_and_delayed_versions_are_idempotent(db):
    older=order('same','5');newer=order('same','8');newer['updated_at']='2025-10-13T12:00:00Z'
    result=report(db,[newer,older,newer]);assert result['net_units']==8 and result['cogs']==20

def test_vendor_current_assignment_overrides_history_without_modifying_mapping(db):
    mapping=db.get(VendorSkuConfig,1);mapping.vendor_id=11;db.flush()
    assert report(db,[order('sale')])['net_units']==0
    db.rollback()
    assert report(db,[order('sale')])['net_units']==5

def test_pagination_search_assignment_store_status_and_sort(db):
    for i in range(4,80):
        db.add(VendorSkuConfig(id=i,vendor_id=10,sku=f'SKU{i}',square_variation_id=f'V{i}',unit_cost=D(1),active=True,is_default_vendor=True))
    db.flush();view=service.inventory_value(db,vendor_id=10,gateway=Gateway())
    table=service.inventory_table(view);assert len(table['rows'])==25 and table['pages']==4
    second=service.inventory_table(view,page=2);assert {r['mapping_id'] for r in table['rows']}.isdisjoint({r['mapping_id'] for r in second['rows']})
    assert len(service.inventory_table(view,query='q904',store='2',status='negative')['rows'])==1
    assert len(service.inventory_table(view,query='product q904 blue')['rows'])==1
    assert service.inventory_table(view,page_size=50)['page_size']==50
    assert service.inventory_table(view,status='unknown')['total']==76
    with pytest.raises(ValueError):service.inventory_table(view,page_size=1000)


def test_incomplete_filter_includes_missing_cost_without_hiding_known_units(db):
    db.get(VendorSkuConfig,2).unit_cost=None;db.flush()
    view=service.inventory_value(db,vendor_id=10,gateway=Gateway())
    rows=service.inventory_table(view,status='unknown')['rows']
    assert len(rows)==1 and rows[0]['sku']=='B' and rows[0]['positive_units']==16
