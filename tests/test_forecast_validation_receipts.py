"""Receipt regressions: disposable PostgreSQL; Square is always offline."""
from datetime import datetime,timedelta,timezone
from decimal import Decimal
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from test_purchasing_receiving_extension import database_template,receiving_db
from app import models
from app.forecast_validation.capture import capture
from app.forecast_validation.contracts import TZ,validate
from app.forecast_validation.demand import calculate
from app.forecast_validation.policy import store_need
from app.services.v2_ordering_square_gateway import SquareProductMetadata,SquareInventoryCount,SquareInventoryCountReadResult

@pytest.fixture
def source(receiving_db):
    now=datetime.now(timezone.utc);first=now.astimezone(TZ).date()-timedelta(days=84)
    with Session(receiving_db) as db:
        for vid in ('VAR1','VAR2'):
            db.add(models.VendorSkuConfig(vendor_id=1,sku=vid,square_variation_id=vid,unit_cost=3,pack_size=5))
            for sid in range(1,5):
                for offset,q in [(-1,9),(0,1),(40,2),(83,3),(84,8)]:
                    day=first+timedelta(days=offset)
                    db.add(models.ConsignmentSaleFact(square_order_id=f'{vid}-{sid}-{offset}',square_line_item_uid='1',square_variation_id=vid,square_location_id=f'L{sid}',store_id=sid,business_date=day,transacted_at=datetime.combine(day,datetime.min.time(),TZ),quantity_sold=q,gross_sales_amount=1,discount_amount=0,tax_amount=0,net_sales_amount=1,currency='USD',product_name_snapshot=vid,attribution_status='MISSING_COST',attribution_source='SYNTHETIC',source_synchronized_at=now,source_order_version=1))
        for sid in range(1,5):
            for kind in ('SALES','MOVEMENTS'):db.add(models.PurchasingHistoryCoverage(source=kind,location_id=f'L{sid}',start_at=datetime.combine(first,datetime.min.time(),TZ),through_at=now,verified_at=now))
        db.commit()
    class Gateway:
        def fetch_active_catalog(self):return {vid:SquareProductMetadata(vid,vid,vid,'Test',now-timedelta(days=200),False,vendor_ids=('V1',)) for vid in ('VAR1','VAR2')}
        def fetch_current_inventory_counts(self,**kw):return SquareInventoryCountReadResult({(f'L{sid}',vid):SquareInventoryCount(f'L{sid}',vid,Decimal(10),now-timedelta(seconds=1)) for sid in range(1,5) for vid in ('VAR1','VAR2')})
    engine=create_engine(receiving_db.url,connect_args={'options':'-c default_transaction_read_only=on'})
    def collect():
        with Session(engine) as db:return capture(db,Gateway())
    def receipt(i,lead=2.5,*,ordered=True,received=True,vid='VAR1',sid=1,po_id=None,created_offset=0):
        pid=po_id or i+10;at=now-timedelta(days=2)
        with Session(receiving_db) as db:
            if db.get(models.PurchaseOrder,pid) is None:
                db.add(models.PurchaseOrder(id=pid,vendor_id=1,status='COMPLETED',created_by_principal_id=1,ordered_at=at-timedelta(days=lead) if ordered else None));db.flush()
            line=models.PurchaseOrderLine(id=1000+i,purchase_order_id=pid,variation_id=vid,sku=vid,item_name='Test',variation_name='Test',ordered_qty=4,received_qty_total=4);db.add(line);db.flush()
            r=models.PurchaseOrderReceipt(id=1000+i,purchase_order_id=pid,received_by_principal_id=1,received_at=at if received else None,status='SUBMITTED');db.add(r);db.flush()
            rl=models.PurchaseOrderReceiptLine(id=1000+i,receipt_id=r.id,purchase_order_line_id=line.id);db.add(rl);db.flush()
            db.add(models.PurchaseOrderReceiptAllocation(id=1000+i,receipt_line_id=rl.id,purchase_order_line_id=line.id,store_id=sid,square_variation_id=vid,event_kind='DELIVERY',quantity_delta=4,closed_quantity=0,created_by_principal_id=1,created_at=at+timedelta(days=created_offset)));db.commit()
    yield collect,receipt,first
    engine.dispose()

def assert_calendar(s,first):
    validate(s);assert len(s['rows'])==8
    expected=['0']*84
    for offset,q in [(0,'1'),(40,'2'),(83,'3')]:expected[offset]=q
    for row in s['rows']:
        assert row['history_start']==str(first) and row['sales']==expected
        assert len(row['availability'])==len(row['verified_availability'])==84
        assert row['incoming']==('20' if row['variation_id']=='VAR1' else '0')

def test_no_qualifying_receipts(source):
    collect,receipt,first=source;s=collect();assert_calendar(s,first)
    assert all(r['lead_time_days'] is None for r in s['rows'])

@pytest.mark.parametrize('leads,expected',[([2.5],None),([2.5,3.5],None),([2.25,3.5,4.75],4),([2.1,2.2,2.3],2),([-1,3,4],None),([181,3,4],None),([0,180,90],90)])
def test_delivery_receipts_preserve_calendar_and_policy(source,leads,expected):
    collect,receipt,first=source;before=collect()
    for i,lead in enumerate(leads):receipt(i+1,lead,vid='VAR1' if i%2==0 else 'VAR2',sid=i%4+1)
    after=collect();assert_calendar(after,first)
    assert all(r['lead_time_days']==expected for r in after['rows'])
    for a,b in zip(before['rows'],after['rows']):
        for m in ('A_RAW84','B_CURRENT','C_CONSERVATIVE','D_VERIFIED'):
            assert calculate(m,a)==calculate(m,b)
            if expected is None or expected<=28:assert store_need(calculate(m,a)['rate'],a)==store_need(calculate(m,b)['rate'],b)
    events=[e for e in after['observational_events'] if e['source']=='actual_receipt']
    assert len(events)==len(leads) and all(e['data']['received_at'] for e in events)

def test_missing_order_time_and_receipt_time(source):
    collect,receipt,first=source;receipt(1,ordered=False);receipt(2,received=False);receipt(3,3)
    s=collect();assert_calendar(s,first);assert all(r['lead_time_days'] is None for r in s['rows'])
    events={e['id']:e for e in s['observational_events'] if e['source']=='actual_receipt'}
    assert events['1001']['data']['ordered_at'] is None
    assert events['1002']['occurred_at']==events['1002']['known_at']
    assert events['1002']['data']['receipt_timestamp_source']=='allocation.created_at'

def test_multiple_store_sku_receipts_count_distinct_orders(source):
    collect,receipt,first=source
    receipt(1,2,po_id=11);receipt(2,2,po_id=11,vid='VAR2',sid=2)
    receipt(3,3,po_id=12,sid=3);receipt(4,4,po_id=13,vid='VAR2',sid=4)
    s=collect();assert_calendar(s,first);assert all(r['lead_time_days']==3 for r in s['rows'])

@pytest.mark.parametrize('field',['received_at','created_at','ordered_at'])
def test_future_receipt_information_fails_closed(source,receiving_db,field):
    from app.forecast_validation.contracts import EvidenceError
    collect,receipt,first=source;receipt(1,created_offset=4 if field=='created_at' else 0)
    if field=='created_at':
        with pytest.raises(EvidenceError,match='future'):collect()
        return
    with Session(receiving_db) as db:
        obj=db.get(models.PurchaseOrder,11) if field=='ordered_at' else db.get(models.PurchaseOrderReceipt,1001) if field=='received_at' else db.get(models.PurchaseOrderReceiptAllocation,1001)
        setattr(obj,field,datetime.now(timezone.utc)+timedelta(days=1));db.commit()
    with pytest.raises(EvidenceError,match='future'):collect()

def test_multiple_receipts_on_two_orders_do_not_satisfy_three_order_minimum(source):
    collect,receipt,first=source
    receipt(1,2,po_id=11);receipt(2,2,po_id=11,vid='VAR2',sid=2);receipt(3,3,po_id=12,sid=3)
    s=collect();assert_calendar(s,first);assert all(r['lead_time_days'] is None for r in s['rows'])

def test_receiving_reduces_only_matching_incoming_commitment(source,receiving_db):
    from test_purchasing_receiving_extension import save
    collect,receipt,first=source
    with Session(receiving_db) as db:
        db.get(models.PurchaseOrder,1).ordered_at=datetime.now(timezone.utc)-timedelta(days=3);db.commit()
        save(db,8,store=1,key='forecast-regression-only');db.commit()
    s=collect();validate(s)
    assert {(r['variation_id'],r['store_id']):r['incoming'] for r in s['rows']}=={(v,str(i)):('12' if i==1 else '20') if v=='VAR1' else '0' for v in ('VAR1','VAR2') for i in range(1,5)}
    assert all(len(r['sales'])==84 for r in s['rows'])
