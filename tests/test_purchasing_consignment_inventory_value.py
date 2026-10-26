"""Current Square stock valuation; real PostgreSQL, isolated read-only Square fixture."""
import copy
from datetime import datetime,timezone,date
from decimal import Decimal
from types import SimpleNamespace
import pytest
from sqlalchemy import select,func,text
from sqlalchemy.orm import Session
from test_purchasing_monday_payments import monday_db,engine,receiving_db,database_template,NOW,MONDAY,rec,save,actuals
from app.models import (VendorSkuConfig,PurchaseOrder,PurchaseOrderLine,OrderPayment,FundingReportAdjustment,
    FundingPayment,FundingReport,FundingLedgerEntry,FundingActualActivity,AuditLog,PurchasingProductAuthority)
from app.services import purchasing_payments_service as service
from app.services.v2_ordering_square_gateway import SquareProductMetadata,SquareInventoryCount

class InventoryGateway:
    def __init__(self):
        self.products={};self.counts={};self.requests=[]
        for vid,sku,vendor,quantities in [('BIG','Product A','V1',[25,25,25,25]),
            ('BIG-B','Product B','V1',[20,20,20,15]),('BIG-C','Product C','V1',[10,10,15,15]),
            ('STREAM','Product D','V3',[10,10,10,10]),('STREAM-E','Product E','V3',[5,10,5,10]),
            ('ORDINARY','Card Product','V4',[100,100,100,100])]:
            self.products[vid]=SquareProductMetadata(vid,sku,sku,'Regular',NOW,False,vendor_ids=(vendor,))
            for i,qty in enumerate(quantities,1):self.counts[(f'L{i}',vid)]=SquareInventoryCount(f'L{i}',vid,Decimal(qty),NOW)
    def fetch_active_catalog(self):return dict(self.products)
    def fetch_product_metadata(self,ids):return {vid:p for vid,p in self.products.items() if vid in ids}
    def fetch_current_inventory_counts(self,**kw):
        self.requests.append(kw)
        return SimpleNamespace(counts={k:v for k,v in self.counts.items() if k[0] in kw['location_ids'] and k[1] in kw['variation_ids']})

@pytest.fixture
def inventory(monday_db,monkeypatch):
    with Session(monday_db) as db:
        for existing in db.scalars(select(VendorSkuConfig)):existing.active=False
        for vendor,vid,cost in [(1,'BIG',5),(1,'BIG-B',8),(1,'BIG-C',10),(3,'STREAM',4),(3,'STREAM-E',7),(4,'ORDINARY',3)]:
            db.add(VendorSkuConfig(vendor_id=vendor,sku={'BIG':'Product A','BIG-B':'Product B','BIG-C':'Product C','STREAM':'Product D','STREAM-E':'Product E'}.get(vid,vid),square_variation_id=vid,unit_cost=cost,active=True))
        db.commit()
    gateway=InventoryGateway()
    from app.services import v2_ordering_square_gateway
    monkeypatch.setattr(v2_ordering_square_gateway,'SquareOrderingReadGateway',lambda:gateway)
    return monday_db,gateway

def valuation(db,gateway):return service.consignment_inventory_values(db,as_of=NOW,gateway=gateway)

def fingerprint(db):
    tables=('funding_reports','funding_payments','funding_payment_allocations','funding_ledger_entries',
        'funding_actual_activity','funding_balance_checkpoints','purchase_orders','purchase_order_lines','audit_log')
    return {table:db.execute(text(f'SELECT row_to_json(t)::text FROM {table} t ORDER BY id')).scalars().all() for table in tables}

def test_multiple_skus_four_stores_independent_values_and_read_only(inventory):
    engine,gateway=inventory
    with Session(engine) as db:
        before=fingerprint(db);r=valuation(db,gateway)
        assert r['big']['complete'] and r['big']['value']=='1600.00'
        assert r['streamline']['complete'] and r['streamline']['value']=='370.00'
        assert [Decimal(i['value']) for i in r['big']['items']]==[Decimal(500),Decimal(600),Decimal(500)]
        assert [Decimal(i['on_hand']) for i in r['big']['items']]==[100,75,50]
        assert all(len(i['stores'])==4 for i in r['big']['items'])
        assert 'ORDINARY' not in gateway.requests[-1]['variation_ids']
        submitted=[v for request in gateway.requests for v in request['variation_ids']]
        assert len(submitted)==len(set(submitted))==5
        assert fingerprint(db)==before


def test_zero_is_explicit_and_missing_count_is_incomplete(inventory):
    engine,gateway=inventory
    for key,c in list(gateway.counts.items()):gateway.counts[key]=SquareInventoryCount(c.location_id,c.variation_id,Decimal(0),NOW)
    with Session(engine) as db:
        assert valuation(db,gateway)['big']['value']=='0.00'
        del gateway.counts[('L4','BIG-B')]
        r=valuation(db,gateway);assert r['big']['value'] is None and not r['big']['complete']
        assert any(i['sku']=='Product B' and 'L4' in i['message'] for i in r['big']['issues'])
        assert r['streamline']['complete']


def test_missing_cost_flags_exact_sku_and_keeps_known_subtotal(inventory):
    engine,gateway=inventory
    with Session(engine) as db:
        db.scalar(select(VendorSkuConfig).where(VendorSkuConfig.square_variation_id=='BIG-B')).unit_cost=None;db.commit()
        r=valuation(db,gateway);assert r['big']['value'] is None and r['big']['known_value']=='1000.00'
        assert any(i['sku']=='Product B' and 'cost' in i['message'] for i in r['big']['issues'])
        assert r['streamline']['value']=='370.00'


def add_lot(db,*,poid,vid='BIG',vendor=1,category='CONSIGNMENT',cost=5):
    po=PurchaseOrder(id=poid,vendor_id=2,status='COMPLETED',created_by_principal_id=1,
        ordered_at=datetime(2026,10,10,tzinfo=timezone.utc));db.add(po);db.flush()
    line=PurchaseOrderLine(purchase_order_id=poid,variation_id=vid,sku=vid,item_name=vid,variation_name='Regular',ordered_qty=10000,received_qty_total=10000,unit_cost=cost)
    db.add(line);db.add(OrderPayment(purchase_order_id=poid,vendor_id=vendor,payment_method_id=10,payment_category_snapshot=category,
        financial_treatment='REPLENISHMENT' if category=='CONSIGNMENT' else 'INVOICE',status='UNPAID'));db.flush()
    return po,line


def test_supplier_changes_cannot_reassign_stock_and_po_quantities_not_stock(inventory):
    engine,gateway=inventory
    with Session(engine) as db:
        po,line=add_lot(db,poid=20);db.commit()
        r=valuation(db,gateway);assert r['big']['value']=='1600.00'
        item=next(i for i in r['big']['items'] if i['variation_id']=='BIG');assert item['on_hand']=='100' and item['cost_source'].startswith('VendorSkuConfig:')
        po.vendor_id=3;line.ordered_qty=90000;line.received_qty_total=80000;db.commit()
        assert valuation(db,gateway)==r
        gateway.products['BIG']=SquareProductMetadata('BIG','Product A','Product A','Regular',NOW,False,vendor_ids=('V3',))
        r=valuation(db,gateway);assert r['big']['value']=='1600.00' and r['streamline']['value']=='370.00'


def test_mixed_financing_lots_do_not_override_current_vendor_assignment(inventory):
    engine,gateway=inventory
    with Session(engine) as db:
        add_lot(db,poid=20);add_lot(db,poid=21,vendor=4,category='CREDIT_CARD');db.commit()
        before=fingerprint(db);r=valuation(db,gateway)
        assert r['big']['value']=='1600.00' and r['streamline']['value']=='370.00'
        assert sum(i['variation_id']=='BIG' for i in r['big']['items'])==1
        assert fingerprint(db)==before


def test_big_and_streamline_historical_lots_do_not_duplicate_current_inventory(inventory):
    engine,gateway=inventory
    with Session(engine) as db:
        add_lot(db,poid=20);add_lot(db,poid=21,vendor=3);db.commit()
        r=valuation(db,gateway)
        assert r['big']['value']=='1600.00' and r['streamline']['value']=='370.00'
        assert all(i['variation_id']!='BIG' for i in r['streamline']['items'])


def test_historical_card_and_bank_financing_do_not_override_current_assigned_vendor(inventory):
    engine,gateway=inventory
    with Session(engine) as db:
        add_lot(db,poid=20,category='CREDIT_CARD');add_lot(db,poid=21,vid='BIG-B',category='WIRE');db.commit()
        r=valuation(db,gateway);assert r['big']['value']=='1600.00' and r['big']['complete']


def test_square_return_changes_value_without_replaying_sales_or_receipts(inventory):
    engine,gateway=inventory
    with Session(engine) as db:
        before=valuation(db,gateway);assert before['big']['value']=='1600.00'
        old=gateway.counts[('L2','BIG')]
        gateway.counts[('L2','BIG')]=SquareInventoryCount('L2','BIG',old.quantity+2,NOW)
        assert valuation(db,gateway)['big']['value']=='1610.00'
        # A return/credit without a changed Square count has no further effect.
        assert valuation(db,gateway)['big']['value']=='1610.00'


def test_monday_payments_vendor_credits_and_history_do_not_change_inventory(inventory):
    engine,gateway=inventory
    with Session(engine) as db:
        r=rec(db);before=valuation(db,gateway);batch=save(db,r);db.commit()
        assert valuation(db,gateway)==before
        report=db.scalar(select(FundingReport).where(FundingReport.account_id==11))
        db.add(FundingReportAdjustment(report_id=report.id,adjustment_type='VENDOR_CREDIT',direction='DECREASE',amount=99999,
            effective_date=date(2026,10,19),reason='Synthetic vendor credit',owner_confirmed=True,created_by_principal_id=1));db.commit()
        assert valuation(db,gateway)==before
        correction=save(db,r,corrects=batch.id,values={**actuals(r),'big':'0'});db.commit()
        assert valuation(db,gateway)==before and correction.supersedes_id==batch.id
        assert rec(db)['rows'][0]['recommended']==r['rows'][0]['recommended']


def test_inventory_movements_do_not_change_payment_recommendation_or_save_token(inventory):
    engine,gateway=inventory
    with Session(engine) as db:
        r=rec(db);before=fingerprint(db)
        gateway.counts[('L1','BIG')]=SquareInventoryCount('L1','BIG',Decimal(5000),NOW)
        assert valuation(db,gateway)['big']['value']=='26475.00'
        assert rec(db)==r and fingerprint(db)==before
        save(db,r);db.commit()


def test_missing_catalog_cost_identity_and_store_mapping_do_not_become_zero(inventory):
    engine,gateway=inventory
    with Session(engine) as db:
        del gateway.products['BIG-B'];r=valuation(db,gateway)
        assert r['big']['complete'] and r['big']['value']=='1600.00'  # Configured identity already established.
        from app.models import Store
        db.get(Store,4).square_location_id=None;db.commit()
        assert not valuation(db,gateway)['streamline']['complete']


def test_round_total_after_full_precision_sku_calculations(inventory):
    engine,gateway=inventory
    with Session(engine) as db:
        for mapping in db.scalars(select(VendorSkuConfig).where(VendorSkuConfig.vendor_id==1)):mapping.unit_cost=Decimal('.005')
        db.commit()
        for vid in ('BIG','BIG-B','BIG-C'):
            for sid in range(1,5):gateway.counts[(f'L{sid}',vid)]=SquareInventoryCount(f'L{sid}',vid,Decimal(1 if sid==1 else 0),NOW)
        assert valuation(db,gateway)['big']['value']=='0.02'


def test_same_account_multiple_lots_count_square_once_and_pending_po_not_stock(inventory):
    engine,gateway=inventory
    with Session(engine) as db:
        add_lot(db,poid=20);add_lot(db,poid=21);po,line=add_lot(db,poid=22,category='CREDIT_CARD');line.received_qty_total=0;db.commit()
        r=valuation(db,gateway);assert r['big']['value']=='1600.00'
        assert sum(i['variation_id']=='BIG' for i in r['big']['items'])==1
        assert sum(request['variation_ids'].count('BIG') for request in gateway.requests)==1


def test_missing_historical_cost_does_not_replace_configured_vendor_cost(inventory):
    engine,gateway=inventory
    with Session(engine) as db:
        add_lot(db,poid=20,cost=None);db.commit()
        r=valuation(db,gateway);assert r['big']['value']=='1600.00'
        item=next(i for i in r['big']['items'] if i['variation_id']=='BIG')
        assert Decimal(item['unit_cost'])==5 and item['cost_source'].startswith('VendorSkuConfig:')


def test_square_supplier_metadata_does_not_override_current_configured_assignment(inventory):
    engine,gateway=inventory
    gateway.products['BIG']=SquareProductMetadata('BIG','Product A','Product A','Regular',NOW,False,vendor_ids=('V1','V3'))
    with Session(engine) as db:
        r=valuation(db,gateway)
        assert r['big']['value']=='1600.00' and r['streamline']['value']=='370.00'
        del gateway.counts[('L1','BIG')]
        assert valuation(db,gateway)['big']['value'] is None


def test_http_live_inventory_separate_from_frozen_week_and_payment_token(inventory):
    from test_purchasing_ui import app_client
    engine,gateway=inventory
    with Session(engine) as db:
        client,_=app_client(db);url='/management/purchasing/payments';before=fingerprint(db)
        response=client.get(url+'?monday='+MONDAY);assert response.status_code==200,response.text
        assert 'Consignment Inventory Value' in response.text and 'Actual issuer balance' in response.text
        assert 'Outstanding (recorded)' not in response.text
        data=client.get(url+'/workspace?monday='+MONDAY).json();r=data['recommendation']
        assert data['inventory_values']['big']['value']=='1600.00'
        gateway.counts[('L1','BIG')]=SquareInventoryCount('L1','BIG',Decimal(27),NOW)
        refreshed=client.get(url+'/workspace?monday='+MONDAY).json()
        assert refreshed['inventory_values']['big']['value']=='1610.00'
        assert refreshed['recommendation']==r and fingerprint(db)==before
        response=client.post(url,json={'monday':MONDAY,'amounts':actuals(r),'request_key':'inventory-independent','token':r['token']},headers={'X-CSRF-Token':'test'})
        assert response.status_code==200,response.text
        data=client.get(url+'/workspace?monday='+MONDAY).json()
        assert data['inventory_values']['big']['value']=='1610.00' and data['latest']['id']==response.json()['batch_id']


def test_explicit_current_primary_vendor_change_updates_valuation_without_rewriting_history(inventory):
    engine,gateway=inventory
    with Session(engine) as db:
        before=valuation(db,gateway)
        original=db.scalar(select(VendorSkuConfig).where(VendorSkuConfig.square_variation_id=='BIG'))
        original.is_default_vendor=False
        db.add(VendorSkuConfig(vendor_id=4,sku='Alternative-BIG',square_variation_id='BIG',unit_cost=2,active=True,is_default_vendor=True));db.commit()
        after=valuation(db,gateway)
        assert after['big']['value']=='1100.00' and after['streamline']['value']==before['streamline']['value']


def test_secondary_supplier_cost_does_not_override_primary_vendor_cost(inventory):
    engine,gateway=inventory
    with Session(engine) as db:
        db.add(VendorSkuConfig(vendor_id=1,sku='Conflicting-BIG',square_variation_id='BIG',unit_cost=99,active=True,is_default_vendor=False));db.commit()
        r=valuation(db,gateway)
        assert r['big']['complete'] and r['big']['value']=='1600.00'
        assert r['streamline']['value']=='370.00'
