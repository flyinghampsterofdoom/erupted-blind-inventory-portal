"""Owner decisions: real isolated PostgreSQL and deterministic four-store Square reads."""
from dataclasses import replace
from decimal import Decimal
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
import copy
import pytest
from sqlalchemy import select,func,text
from sqlalchemy.orm import Session
from test_purchasing_consignment_inventory_value import InventoryGateway,inventory
from test_purchasing_monday_payments import monday_db,engine,receiving_db,database_template,NOW,MONDAY,rec
from app.models import (PurchasingOwnerDecision,AuditLog,VendorSkuConfig,PurchaseOrder,PurchaseOrderLine,OrderPayment,
    FundingPayment,FundingLedgerEntry,FundingAccount,PaymentMethod,PurchasingSessionLine)
from app.services import purchasing_attention_service as attention,purchasing_payments_service as payments,purchasing_session_service as purchasing,purchasing_classification_service as classification


def change(row,**values):return {'kind':row['kind'],'key':row['key'],'version':row['version'],'token':row['token'],**values}
def save(db,g,changes,key='owner-request'):
    return attention.save(db,actor_id=1,request_key=key,changes=changes,gateway=g)

def seed_missing_costs(db,g):
    existing=set(db.scalars(select(VendorSkuConfig.square_variation_id).where(VendorSkuConfig.active.is_(True))))
    for vid,product in g.products.items():
        if vid not in existing:
            db.add(VendorSkuConfig(vendor_id=1,sku=product.sku,square_variation_id=vid,unit_cost=None,active=True))
    db.commit()


def assign(db,category,*,vid='BIG',key='owner-classification',version=0):
    from datetime import datetime
    from app.services.purchasing_evidence_service import BUSINESS_TIMEZONE
    return classification.save_classification(db,actor_id=1,variation_id=vid,category=category,scope='FORWARD',
        start_date=str(datetime.now(BUSINESS_TIMEZONE).date()),reason='Owner correction',version=version,request_key=key)


@pytest.fixture
def missing_cost(inventory):
    engine,g=inventory
    g.category_names={'ELIQ':'Eliquid'}
    g.products['BIG']=replace(g.products['BIG'],vendor_ids=('V1','V3'),category_ids=('ELIQ',))
    with Session(engine) as db:
        db.scalar(select(VendorSkuConfig).where(VendorSkuConfig.square_variation_id=='BIG',VendorSkuConfig.active.is_(True))).unit_cost=None;db.commit()
    return engine,g


def test_all_36_can_be_shown_with_suggestions_and_bulk_decisions(missing_cost):
    engine,g=missing_cost
    for i in range(35):g.products[f'A{i}']=replace(g.products['BIG'],variation_id=f'A{i}',sku=f'A{i}',item_name=f'Juice Head {i}')
    with Session(engine) as db:
        seed_missing_costs(db,g)
        v=attention.workspace(db,actor_id=1,gateway=g);rows=[r for r in v['rows'] if r['kind']=='COST']
        assert len(rows)==36 and all('Missing' in r['reason'] for r in rows)
        assert db.scalar(select(func.count()).select_from(PurchasingOwnerDecision))==0
        changes=[change(r,unit_cost='5') for r in rows];save(db,g,changes);db.commit()
        assert not [r for r in attention.workspace(db,actor_id=1,gateway=g)['rows'] if r['kind']=='COST']
        assert db.scalar(select(func.count()).select_from(PurchasingOwnerDecision))==36
        a=db.scalar(select(AuditLog).where(AuditLog.action=='PURCHASING_EXCEPTION_DECISIONS'))
        assert len(a.meta['decisions'])==36 and a.actor_principal_id==1
        assert save(db,g,changes)['count']==36;db.commit()
        assert db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action=='PURCHASING_EXCEPTION_DECISIONS'))==1
        with pytest.raises(attention.Conflict):save(db,g,[{**changes[0],'unit_cost':'6'}])


def test_individual_persistence_cost_and_new_evidence_conflict(missing_cost):
    engine,g=missing_cost
    with Session(engine) as db:
        row=next(r for r in attention.workspace(db,actor_id=1,gateway=g)['rows'] if r['kind']=='COST')
        assert not payments.consignment_inventory_values(db,gateway=g)['big']['complete']
        save(db,g,[change(row,unit_cost='5')]);db.commit()
    with Session(engine) as db:
        assert not any(r['kind']=='COST' for r in attention.workspace(db,actor_id=1,gateway=g)['rows'])
        assert purchasing._cost(db,'BIG',1)[0]==5
        # Prospective cost does not overwrite established current or historical accounting costs.
        assert not payments.consignment_inventory_values(db,gateway=g)['big']['complete']
        g.products['BIG']=replace(g.products['BIG'],item_name='Renamed product')
        assert not any(r['kind']=='COST' for r in attention.workspace(db,actor_id=1,gateway=g)['rows'])
        db.scalar(select(VendorSkuConfig).where(VendorSkuConfig.square_variation_id=='BIG',VendorSkuConfig.active.is_(True))).unit_cost=6;db.commit()
        assert any(r['kind']=='COST' for r in attention.workspace(db,actor_id=1,gateway=g)['rows'])
        assert payments.consignment_inventory_values(db,gateway=g)['big']['complete']


def test_missing_cost_is_current_input_audited_without_rewriting_mapping(inventory):
    engine,g=inventory
    with Session(engine) as db:
        mapping=db.scalar(select(VendorSkuConfig).where(VendorSkuConfig.square_variation_id=='BIG'));mapping.unit_cost=None;db.commit()
        before=rec(db)['token'];row=next(r for r in attention.workspace(db,actor_id=1,gateway=g)['rows'] if r['kind']=='COST')
        assert not payments.consignment_inventory_values(db,gateway=g)['big']['complete']
        save(db,g,[change(row,unit_cost='5.0000')]);db.commit()
        assert db.get(VendorSkuConfig,mapping.id).unit_cost is None
        # Prospective purchasing input cannot supply current consignment cost.
        current=payments.consignment_inventory_values(db,gateway=g)['big']
        assert current['value'] is None and not current['complete']
        assert purchasing._cost(db,'BIG',1)[0]==5
        assert rec(db)['token']==before
        assert not any(r['kind']=='COST' for r in attention.workspace(db,actor_id=1,gateway=g)['rows'])
        mapping.unit_cost=6;db.commit()
        assert purchasing._cost(db,'BIG',1)[0] is None


def lot(db,*,vid,vendor=4,category='TERMS',treatment='INVOICE',num=100):
    po=PurchaseOrder(id=num,vendor_id=vendor,status='COMPLETED',created_by_principal_id=1,created_at=NOW-timedelta(days=1))
    db.add(po);db.flush();db.add(PurchaseOrderLine(id=num,purchase_order_id=num,variation_id=vid,sku=vid,item_name=vid,variation_name='Regular',ordered_qty=20,received_qty_total=20,unit_cost=5))
    db.add(OrderPayment(purchase_order_id=num,vendor_id=vendor,payment_category_snapshot=category,financial_treatment=treatment,payment_method_label_snapshot=category,status='UNPAID',order_amount=100,order_cost_complete=True,paid_amount=0));db.flush()


def test_terms_only_and_terms_card_exclude_consignment_without_weekly_card_guess(inventory):
    engine,g=inventory
    with Session(engine) as db:
        lot(db,vid='ORDINARY');lot(db,vid='ORDINARY',category='CREDIT_CARD',num=101);db.commit()
        values=payments.consignment_inventory_values(db,gateway=g)
        assert values['big']['complete'] and values['streamline']['complete']
        assert not any(r['kind']=='COST' for r in attention.workspace(db,actor_id=1,gateway=g)['rows'])
        assert classification.classify_product(db,variation_id='ORDINARY',as_of=NOW).category=='CREDIT_CARD_POOL'
        assert db.scalar(select(func.count()).select_from(FundingPayment))==0


def test_mixed_terms_do_not_generate_financing_exceptions(inventory):
    engine,g=inventory
    with Session(engine) as db:
        lot(db,vid='BIG',vendor=1,category='CONSIGNMENT',treatment='REPLENISHMENT');lot(db,vid='BIG',num=101);db.commit()
        before=[dict(x) for x in db.execute(text('select * from order_payments order by id')).mappings()]
        assert not any(r['kind']=='FINANCING' for r in attention.workspace(db,actor_id=1,gateway=g)['rows'])
        assert classification.classify_product(db,variation_id='BIG',as_of=NOW).category=='BIG'
        assert payments.consignment_inventory_values(db,gateway=g)['big']['value']=='1600.00'
        assert [dict(x) for x in db.execute(text('select * from order_payments order by id')).mappings()]==before


def test_stale_bulk_save_rejected_atomically_and_nonowner_denied(missing_cost):
    engine,g=missing_cost
    with Session(engine) as db:
        row=next(r for r in attention.workspace(db,actor_id=1,gateway=g)['rows'] if r['kind']=='COST')
        with pytest.raises(PermissionError):attention.workspace(db,actor_id=2,gateway=g)
        db.scalar(select(VendorSkuConfig).where(VendorSkuConfig.square_variation_id=='BIG',VendorSkuConfig.active.is_(True))).unit_cost=6;db.commit()
        with pytest.raises(attention.Conflict):save(db,g,[change(row,unit_cost='5')])
        db.rollback();assert db.scalar(select(func.count()).select_from(PurchasingOwnerDecision))==0


def test_two_owners_requests_cannot_overwrite_decisions(missing_cost):
    engine,g=missing_cost
    with Session(engine) as db:row=next(r for r in attention.workspace(db,actor_id=1,gateway=g)['rows'] if r['kind']=='COST')
    def worker(aid):
        with Session(engine) as db:
            try:save(db,g,[change(row,unit_cost=str(aid))],key=f'concurrent-{aid}');db.commit();return 'saved'
            except attention.Conflict:db.rollback();return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:out=list(pool.map(worker,[11,12]))
    assert sorted(out)==['conflict','saved']


def test_card_mapping_uses_existing_accounts_without_money_and_prevents_duplicate_roles(inventory,monkeypatch):
    from app.config import settings
    engine,g=inventory
    monkeypatch.setattr(settings,'purchasing_card1_account_id',None)
    monkeypatch.setattr(settings,'purchasing_card2_account_id',None)
    with Session(engine) as db:
        rows=[r for r in attention.workspace(db,actor_id=1,gateway=g)['rows'] if r['kind']=='CARD']
        assert len(rows)==2
        before=db.scalar(select(func.count()).select_from(FundingPayment))
        with pytest.raises(ValueError,match='distinct'):save(db,g,[change(r,account_id=10) for r in rows])
        db.rollback();assert db.scalar(select(func.count()).select_from(PurchasingOwnerDecision))==0
        save(db,g,[change(r,account_id=10 if r['key']=='card1' else 20) for r in rows],key='two-card-roles');db.commit()
        assert payments.account_map(db)[0]['card1'].id==10 and payments.account_map(db)[0]['card2'].id==20
        assert not any(r['kind']=='CARD' for r in attention.workspace(db,actor_id=1,gateway=g)['rows'])
        assert db.scalar(select(func.count()).select_from(FundingPayment))==before


def test_future_classification_does_not_change_historical_lot(inventory):
    from datetime import datetime,timezone
    engine,g=inventory
    with Session(engine) as db:
        lot(db,vid='BIG',vendor=1,category='CONSIGNMENT',treatment='REPLENISHMENT');lot(db,vid='BIG',num=101);db.commit()
        before=[dict(x) for x in db.execute(text('select * from order_payments order by id')).mappings()]
        assign(db,'STREAMLINE');db.commit()
        assert classification.classify_product(db,variation_id='BIG',as_of=datetime.now(timezone.utc)).category=='STREAMLINE'
        assert classification.classify_product(db,variation_id='BIG',as_of=NOW).category=='BIG'
        assert attention.financing_choice(db,'BIG',('V1',),{'big','other'},prospective=True)[0].id==12
        assert payments.consignment_inventory_values(db,gateway=g)['big']['value']=='1600.00'
        assert [dict(x) for x in db.execute(text('select * from order_payments order by id')).mappings()]==before


def test_owner_cost_correction_does_not_rewrite_historical_received_cost(inventory):
    engine,g=inventory
    with Session(engine) as db:
        lot(db,vid='BIG',vendor=1,category='CONSIGNMENT',treatment='REPLENISHMENT')
        line=db.get(PurchaseOrderLine,100);line.unit_cost=None;db.commit()
        row=next(r for r in attention.workspace(db,actor_id=1,gateway=g)['rows'] if r['kind']=='COST')
        save(db,g,[change(row,unit_cost='5')]);db.commit()
        assert db.get(PurchaseOrderLine,100).unit_cost is None
        assert payments.consignment_inventory_values(db,gateway=g)['big']['value']=='1600.00'
        assert not any(r['kind']=='COST' for r in attention.workspace(db,actor_id=1,gateway=g)['rows'])


def test_http_requires_owner_csrf_and_preserves_bulk_retry(missing_cost,monkeypatch):
    from test_purchasing_ui import app_client
    engine,g=missing_cost
    original=attention.facts;monkeypatch.setattr(attention,'facts',lambda db,gateway=None:original(db,g))
    with Session(engine) as db:
        client,app=app_client(db)
        page=client.get('/management/purchasing/attention');assert page.status_code==200 and 'attention-save' in page.text
        view=client.get('/management/purchasing/attention/workspace');assert view.status_code==200
        row=next(r for r in view.json()['rows'] if r['kind']=='COST')
        payload={'request_key':'http','changes':[change(row,unit_cost='5')]}
        assert client.post('/management/purchasing/attention',json=payload).status_code==403
        result=client.post('/management/purchasing/attention',json=payload,headers={'X-CSRF-Token':'test'})
        assert result.status_code==200,result.text
        assert client.post('/management/purchasing/attention',json=payload,headers={'X-CSRF-Token':'test'}).json()==result.json()
        assert not any(r['kind']=='COST' for r in client.get('/management/purchasing/attention/workspace').json()['rows'])


def test_owner_classification_does_not_rewrite_existing_cash_authority(inventory):
    from app.models import PurchasingProductAuthority
    engine,g=inventory
    with Session(engine) as db:
        db.add(PaymentMethod(id=30,display_name='Cash 9671',category='WIRE',last_four='9671',is_active=True,created_by_principal_id=1,updated_by_principal_id=1));db.flush()
        db.get(PurchasingProductAuthority,'BIG').cash_payment_method_id=30;db.commit()
        assign(db,'STREAMLINE');db.commit()
        assert db.get(PurchasingProductAuthority,'BIG').cash_payment_method_id==30
        assert not any(r['kind']=='FINANCING' for r in attention.workspace(db,actor_id=1,gateway=g)['rows'])


def test_representative_108_terms_and_12_terms_card_cases_do_not_pollute_valuation(inventory):
    engine,g=inventory
    with Session(engine) as db:
        for i in range(120):
            vid=f'TERMS-{i}'
            g.products[vid]=replace(g.products['ORDINARY'],variation_id=vid,sku=vid)
            lot(db,vid=vid,num=2000+i*2)
            if i>=108:lot(db,vid=vid,category='CREDIT_CARD',num=2001+i*2)
        db.commit()
        values=payments.consignment_inventory_values(db,gateway=g)
        assert values['big']['value']=='1600.00' and values['streamline']['value']=='370.00'
        assert values['big']['complete'] and values['streamline']['complete']
        assert db.scalar(select(func.count()).select_from(OrderPayment))==132


def test_decisions_preserve_financial_po_receiving_and_sales_history(missing_cost):
    engine,g=missing_cost
    tables=('funding_accounts','funding_reports','funding_report_lines','funding_payments','funding_payment_allocations',
        'funding_ledger_entries','funding_actual_activity','funding_balance_checkpoints','purchase_orders','purchase_order_lines',
        'purchase_order_store_allocations','purchase_order_receipts','purchase_order_receipt_lines','square_sync_events',
        'consignment_sale_facts','purchasing_payment_batches','purchasing_payment_batch_entries')
    def snapshot(db):return {t:db.execute(text(f'SELECT to_jsonb(x)::text FROM {t} x ORDER BY to_jsonb(x)::text')).scalars().all() for t in tables}
    with Session(engine) as db:
        before=snapshot(db);row=next(r for r in attention.workspace(db,actor_id=1,gateway=g)['rows'] if r['kind']=='COST')
        save(db,g,[change(row,unit_cost='5')]);db.commit()
        assert snapshot(db)==before
        assert db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action=='PURCHASING_EXCEPTION_DECISIONS'))==1


def test_bulk_over_500_preserves_unmodified_and_lists_resolved(missing_cost):
    engine,g=missing_cost
    for i in range(501):
        g.products[f'BULK-{i:04}']=replace(g.products['BIG'],variation_id=f'BULK-{i:04}',sku=f'BULK-{i:04}',item_name=f'Bulk product {i:04}')
    with Session(engine) as db:
        seed_missing_costs(db,g)
        rows=[r for r in attention.workspace(db,actor_id=1,gateway=g)['rows'] if r['kind']=='COST']
        assert len(rows)==502
        selected=rows[:501]
        result=save(db,g,[change(r,unit_cost='5') for r in selected],key='bulk-501');db.commit()
        assert result['count']==501
        view=attention.workspace(db,actor_id=1,gateway=g)
        assert len(view['resolved_rows'])==501
        assert {r['key'] for r in view['rows'] if r['kind']=='COST'}=={rows[-1]['key']}
        assert db.get(PurchasingOwnerDecision,('COST',rows[-1]['key'])) is None
        assert all(r['saved_value']['unit_cost']=='5' for r in view['resolved_rows'])
        audit=db.scalar(select(AuditLog).where(AuditLog.action=='PURCHASING_EXCEPTION_DECISIONS'))
        assert len(audit.meta['decisions'])==501


def test_resolved_edit_is_versioned_and_stale_identity_cannot_overwrite(missing_cost):
    engine,g=missing_cost
    with Session(engine) as db:
        row=next(r for r in attention.workspace(db,actor_id=1,gateway=g)['rows'] if r['kind']=='COST')
        save(db,g,[change(row,unit_cost='5')],key='first');db.commit()
        resolved=attention.workspace(db,actor_id=1,gateway=g)['resolved_rows'][0]
        assert resolved['version']==1
        # Keep the old ORM identity loaded while another request commits.
        assert db.get(PurchasingOwnerDecision,('COST',row['key'])).version==1
        with Session(engine) as other:
            save(other,g,[change(resolved,unit_cost='6')],key='second');other.commit()
        with pytest.raises(attention.Conflict):save(db,g,[change(resolved,unit_cost='5')],key='stale')
        db.rollback()
        updated=attention.workspace(db,actor_id=1,gateway=g)['resolved_rows'][0]
        assert updated['version']==2 and updated['saved_value']['unit_cost']=='6'


def test_mixed_lots_do_not_invalidate_owner_classification(inventory):
    from datetime import datetime,timezone
    engine,g=inventory
    with Session(engine) as db:
        lot(db,vid='BIG',vendor=1,category='CONSIGNMENT',treatment='REPLENISHMENT');lot(db,vid='BIG',num=101);db.commit()
        assign(db,'STREAMLINE');db.commit()
        view=attention.workspace(db,actor_id=1,gateway=g)
        assert not any(r['kind']=='FINANCING' for r in view['rows'])
        assert classification.classify_product(db,variation_id='BIG',as_of=datetime.now(timezone.utc)).category=='STREAMLINE'
        assert classification.classify_product(db,variation_id='BIG',as_of=NOW).category=='BIG'


def test_bulk_invalid_account_rolls_back_entire_http_request(missing_cost,monkeypatch):
    from test_purchasing_ui import app_client
    engine,g=missing_cost
    g.products['SECOND']=replace(g.products['BIG'],variation_id='SECOND',sku='SECOND')
    original=attention.facts;monkeypatch.setattr(attention,'facts',lambda db,gateway=None:original(db,g))
    with Session(engine) as db:
        seed_missing_costs(db,g)
        client,_=app_client(db)
        rows=[r for r in client.get('/management/purchasing/attention/workspace').json()['rows'] if r['kind']=='COST']
        response=client.post('/management/purchasing/attention',json={'request_key':'invalid-bulk',
            'changes':[change(rows[0],unit_cost='5'),change(rows[1],unit_cost='-1')]},headers={'X-CSRF-Token':'test'})
        assert response.status_code==400
        assert db.scalar(select(func.count()).select_from(PurchasingOwnerDecision))==0
        assert db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action=='PURCHASING_EXCEPTION_DECISIONS'))==0
