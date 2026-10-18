"""Owner decisions: real isolated PostgreSQL and deterministic four-store Square reads."""
from dataclasses import replace
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
import copy
import pytest
from sqlalchemy import select,func,text
from sqlalchemy.orm import Session
from test_purchasing_consignment_inventory_value import InventoryGateway,inventory
from test_purchasing_monday_payments import monday_db,engine,receiving_db,database_template,NOW,MONDAY,rec
from app.models import (PurchasingOwnerDecision,AuditLog,VendorSkuConfig,PurchaseOrder,PurchaseOrderLine,OrderPayment,
    FundingPayment,FundingLedgerEntry,FundingAccount,PaymentMethod,PurchasingSessionLine)
from app.services import purchasing_attention_service as attention,purchasing_payments_service as payments,purchasing_session_service as purchasing


def change(row,**values):return {'kind':row['kind'],'key':row['key'],'version':row['version'],'token':row['token'],**values}
def save(db,g,changes,key='owner-request'):
    return attention.save(db,actor_id=1,request_key=key,changes=changes,gateway=g)

@pytest.fixture
def ambiguous(inventory):
    engine,g=inventory
    g.category_names={'ELIQ':'Eliquid'}
    g.products['BIG']=replace(g.products['BIG'],vendor_ids=('V1','V3'),category_ids=('ELIQ',))
    return engine,g


def test_all_36_can_be_shown_with_suggestions_and_bulk_decisions(ambiguous):
    engine,g=ambiguous
    for i in range(35):g.products[f'A{i}']=replace(g.products['BIG'],variation_id=f'A{i}',sku=f'A{i}',item_name=f'Juice Head {i}')
    with Session(engine) as db:
        v=attention.workspace(db,actor_id=1,gateway=g);rows=[r for r in v['rows'] if r['kind']=='FINANCING']
        assert len(rows)==36 and all(r['suggested_account_id']==11 for r in rows)
        assert db.scalar(select(func.count()).select_from(PurchasingOwnerDecision))==0
        changes=[change(r,account_id=11) for r in rows];save(db,g,changes);db.commit()
        assert not [r for r in attention.workspace(db,actor_id=1,gateway=g)['rows'] if r['kind']=='FINANCING']
        assert db.scalar(select(func.count()).select_from(PurchasingOwnerDecision))==36
        a=db.scalar(select(AuditLog).where(AuditLog.action=='PURCHASING_EXCEPTION_DECISIONS'))
        assert len(a.meta['decisions'])==36 and a.actor_principal_id==1
        assert save(db,g,changes)['count']==36;db.commit()
        assert db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action=='PURCHASING_EXCEPTION_DECISIONS'))==1
        with pytest.raises(attention.Conflict):save(db,g,[{**changes[0],'account_id':12}])


def test_individual_persistence_valuation_and_new_evidence_conflict(ambiguous):
    engine,g=ambiguous
    with Session(engine) as db:
        row=next(r for r in attention.workspace(db,actor_id=1,gateway=g)['rows'] if r['kind']=='FINANCING')
        assert not payments.consignment_inventory_values(db,gateway=g)['big']['complete']
        save(db,g,[change(row,account_id=11)]);db.commit()
    with Session(engine) as db:
        assert not any(r['kind']=='FINANCING' for r in attention.workspace(db,actor_id=1,gateway=g)['rows'])
        values=payments.consignment_inventory_values(db,gateway=g);assert values['big']['value']=='1600.00' and values['streamline']['value']=='370.00'
        # Unrelated product metadata refresh does not erase the owner choice.
        g.products['BIG']=replace(g.products['BIG'],item_name='Renamed product')
        assert not any(r['kind']=='FINANCING' for r in attention.workspace(db,actor_id=1,gateway=g)['rows'])
        g.products['BIG']=replace(g.products['BIG'],vendor_ids=('V3',))
        assert any(r['kind']=='FINANCING' for r in attention.workspace(db,actor_id=1,gateway=g)['rows'])
        assert not payments.consignment_inventory_values(db,gateway=g)['big']['complete']


def test_missing_cost_is_current_input_audited_without_rewriting_mapping(inventory):
    engine,g=inventory
    with Session(engine) as db:
        mapping=db.scalar(select(VendorSkuConfig).where(VendorSkuConfig.square_variation_id=='BIG'));mapping.unit_cost=None;db.commit()
        before=rec(db)['token'];row=next(r for r in attention.workspace(db,actor_id=1,gateway=g)['rows'] if r['kind']=='COST')
        assert not payments.consignment_inventory_values(db,gateway=g)['big']['complete']
        save(db,g,[change(row,unit_cost='5.0000')]);db.commit()
        assert db.get(VendorSkuConfig,mapping.id).unit_cost is None
        assert payments.consignment_inventory_values(db,gateway=g)['big']['value']=='1600.00'
        assert purchasing._cost(db,'BIG',1)[0]==5
        assert rec(db)['token']==before
        assert not any(r['kind']=='COST' for r in attention.workspace(db,actor_id=1,gateway=g)['rows'])
        mapping.unit_cost=6;db.commit()
        assert purchasing._cost(db,'BIG',1)[0] is None


def lot(db,*,vid,vendor=4,category='TERMS',treatment='INVOICE',num=100):
    po=PurchaseOrder(id=num,vendor_id=vendor,status='COMPLETED',created_by_principal_id=1)
    db.add(po);db.flush();db.add(PurchaseOrderLine(id=num,purchase_order_id=num,variation_id=vid,sku=vid,item_name=vid,variation_name='Regular',ordered_qty=20,received_qty_total=20,unit_cost=5))
    db.add(OrderPayment(purchase_order_id=num,vendor_id=vendor,payment_category_snapshot=category,financial_treatment=treatment,payment_method_label_snapshot=category,status='UNPAID',order_amount=100,order_cost_complete=True,paid_amount=0));db.flush()


def test_terms_only_and_terms_card_exclude_consignment_without_weekly_card_guess(inventory):
    engine,g=inventory
    with Session(engine) as db:
        lot(db,vid='ORDINARY');lot(db,vid='ORDINARY',category='CREDIT_CARD',num=101);db.commit()
        values=payments.consignment_inventory_values(db,gateway=g)
        assert values['big']['complete'] and values['streamline']['complete']
        assert not any(r['kind']=='FINANCING' for r in attention.workspace(db,actor_id=1,gateway=g)['rows'])
        assert payments._lot_categories(db,['ORDINARY'],NOW).get('ORDINARY')=={'unknown'} # no dated receipt evidence; no card fabrication
        assert db.scalar(select(func.count()).select_from(FundingPayment))==0


def test_mixed_consignment_terms_stays_incomplete_despite_prospective_owner_choice(inventory):
    engine,g=inventory
    with Session(engine) as db:
        lot(db,vid='BIG',vendor=1,category='CONSIGNMENT',treatment='REPLENISHMENT');lot(db,vid='BIG',num=101);db.commit()
        before=[dict(x) for x in db.execute(text('select * from order_payments order by id')).mappings()]
        row=next(r for r in attention.workspace(db,actor_id=1,gateway=g)['rows'] if r['kind']=='FINANCING')
        save(db,g,[change(row,account_id=11)]);db.commit()
        assert not payments.consignment_inventory_values(db,gateway=g)['big']['complete']
        assert payments.consignment_inventory_values(db,gateway=g)['streamline']['complete']
        assert any(r['kind']=='FINANCING' for r in attention.workspace(db,actor_id=1,gateway=g)['rows'])
        assert [dict(x) for x in db.execute(text('select * from order_payments order by id')).mappings()]==before


def test_stale_bulk_save_rejected_atomically_and_nonowner_denied(ambiguous):
    engine,g=ambiguous
    with Session(engine) as db:
        row=next(r for r in attention.workspace(db,actor_id=1,gateway=g)['rows'] if r['kind']=='FINANCING')
        with pytest.raises(PermissionError):attention.workspace(db,actor_id=2,gateway=g)
        g.products['BIG']=replace(g.products['BIG'],vendor_ids=('V3',))
        with pytest.raises(attention.Conflict):save(db,g,[change(row,account_id=11)])
        db.rollback();assert db.scalar(select(func.count()).select_from(PurchasingOwnerDecision))==0


def test_two_owners_requests_cannot_overwrite_decisions(ambiguous):
    engine,g=ambiguous
    with Session(engine) as db:row=next(r for r in attention.workspace(db,actor_id=1,gateway=g)['rows'] if r['kind']=='FINANCING')
    def worker(aid):
        with Session(engine) as db:
            try:save(db,g,[change(row,account_id=aid)],key=f'concurrent-{aid}');db.commit();return 'saved'
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


def test_future_decision_does_not_change_distinct_historical_lot(inventory):
    engine,g=inventory
    with Session(engine) as db:
        lot(db,vid='BIG',vendor=1,category='CONSIGNMENT',treatment='REPLENISHMENT');lot(db,vid='BIG',num=101);db.commit()
        row=next(r for r in attention.workspace(db,actor_id=1,gateway=g)['rows'] if r['kind']=='FINANCING')
        save(db,g,[change(row,account_id=12)]);db.commit()
        assert attention.financing_choice(db,'BIG',('V1',),{'big','other'})[0] is None
        assert attention.financing_choice(db,'BIG',('V1',),{'big','other'},prospective=True)[0].id==12
        assert not payments.consignment_inventory_values(db,gateway=g)['big']['complete']


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


def test_http_requires_owner_csrf_and_preserves_bulk_retry(ambiguous,monkeypatch):
    from test_purchasing_ui import app_client
    engine,g=ambiguous
    original=attention.facts;monkeypatch.setattr(attention,'facts',lambda db,gateway=None:original(db,g))
    with Session(engine) as db:
        client,app=app_client(db)
        page=client.get('/management/purchasing/attention');assert page.status_code==200 and 'attention-save' in page.text
        view=client.get('/management/purchasing/attention/workspace');assert view.status_code==200
        row=next(r for r in view.json()['rows'] if r['kind']=='FINANCING')
        payload={'request_key':'http','changes':[change(row,account_id=11)]}
        assert client.post('/management/purchasing/attention',json=payload).status_code==403
        result=client.post('/management/purchasing/attention',json=payload,headers={'X-CSRF-Token':'test'})
        assert result.status_code==200,result.text
        assert client.post('/management/purchasing/attention',json=payload,headers={'X-CSRF-Token':'test'}).json()==result.json()
        assert not any(r['kind']=='FINANCING' for r in client.get('/management/purchasing/attention/workspace').json()['rows'])


def test_existing_cash_exception_is_not_replaced_by_sku_assignment(ambiguous):
    from app.models import PurchasingProductAuthority
    engine,g=ambiguous
    with Session(engine) as db:
        row=next(r for r in attention.workspace(db,actor_id=1,gateway=g)['rows'] if r['kind']=='FINANCING')
        db.add(PaymentMethod(id=30,display_name='Cash 9671',category='WIRE',last_four='9671',is_active=True,created_by_principal_id=1,updated_by_principal_id=1));db.flush()
        db.get(PurchasingProductAuthority,'BIG').cash_payment_method_id=30;db.commit()
        assert not any(r['kind']=='FINANCING' for r in attention.workspace(db,actor_id=1,gateway=g)['rows'])
        with pytest.raises(attention.Conflict):save(db,g,[change(row,account_id=11)])


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


def test_decisions_preserve_financial_po_receiving_and_sales_history(ambiguous):
    engine,g=ambiguous
    tables=('funding_accounts','funding_reports','funding_report_lines','funding_payments','funding_payment_allocations',
        'funding_ledger_entries','funding_actual_activity','funding_balance_checkpoints','purchase_orders','purchase_order_lines',
        'purchase_order_store_allocations','purchase_order_receipts','purchase_order_receipt_lines','square_sync_events',
        'consignment_sale_facts','purchasing_payment_batches','purchasing_payment_batch_entries')
    def snapshot(db):return {t:db.execute(text(f'SELECT to_jsonb(x)::text FROM {t} x ORDER BY to_jsonb(x)::text')).scalars().all() for t in tables}
    with Session(engine) as db:
        before=snapshot(db);row=next(r for r in attention.workspace(db,actor_id=1,gateway=g)['rows'] if r['kind']=='FINANCING')
        save(db,g,[change(row,account_id=11)]);db.commit()
        assert snapshot(db)==before
        assert db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action=='PURCHASING_EXCEPTION_DECISIONS'))==1


def test_bulk_over_500_preserves_unmodified_and_lists_resolved(ambiguous):
    engine,g=ambiguous
    for i in range(501):
        g.products[f'BULK-{i:04}']=replace(g.products['BIG'],variation_id=f'BULK-{i:04}',sku=f'BULK-{i:04}',item_name=f'Bulk product {i:04}')
    with Session(engine) as db:
        rows=[r for r in attention.workspace(db,actor_id=1,gateway=g)['rows'] if r['kind']=='FINANCING']
        assert len(rows)==502
        selected=rows[:501]
        result=save(db,g,[change(r,account_id=11) for r in selected],key='bulk-501');db.commit()
        assert result['count']==501
        view=attention.workspace(db,actor_id=1,gateway=g)
        assert len(view['resolved_rows'])==501
        assert {r['key'] for r in view['rows'] if r['kind']=='FINANCING'}=={rows[-1]['key']}
        assert db.get(PurchasingOwnerDecision,('FINANCING',rows[-1]['key'])) is None
        assert all(r['saved_value']['account_id']==11 for r in view['resolved_rows'])
        audit=db.scalar(select(AuditLog).where(AuditLog.action=='PURCHASING_EXCEPTION_DECISIONS'))
        assert len(audit.meta['decisions'])==501


def test_resolved_edit_is_versioned_and_stale_identity_cannot_overwrite(ambiguous):
    engine,g=ambiguous
    with Session(engine) as db:
        row=next(r for r in attention.workspace(db,actor_id=1,gateway=g)['rows'] if r['kind']=='FINANCING')
        save(db,g,[change(row,account_id=11)],key='first');db.commit()
        resolved=attention.workspace(db,actor_id=1,gateway=g)['resolved_rows'][0]
        assert resolved['version']==1
        # Keep the old ORM identity loaded while another request commits.
        assert db.get(PurchasingOwnerDecision,('FINANCING',row['key'])).version==1
        with Session(engine) as other:
            save(other,g,[change(resolved,account_id=12)],key='second');other.commit()
        with pytest.raises(attention.Conflict):save(db,g,[change(resolved,account_id=11)],key='stale')
        db.rollback()
        updated=attention.workspace(db,actor_id=1,gateway=g)['resolved_rows'][0]
        assert updated['version']==2 and updated['saved_value']['account_id']==12


def test_mixed_current_lots_stay_unresolved_after_bulk_save(inventory):
    engine,g=inventory
    with Session(engine) as db:
        lot(db,vid='BIG',vendor=1,category='CONSIGNMENT',treatment='REPLENISHMENT')
        lot(db,vid='BIG',num=101);db.commit()
        row=next(r for r in attention.workspace(db,actor_id=1,gateway=g)['rows'] if r['kind']=='FINANCING')
        save(db,g,[change(row,account_id=12)],key='prospective');db.commit()
        view=attention.workspace(db,actor_id=1,gateway=g)
        assert not any(r['key']=='BIG' for r in view['resolved_rows'])
        current=next(r for r in view['rows'] if r['kind']=='FINANCING' and r['key']=='BIG')
        assert current['prospective_only'] and 'Incomplete' in current['current_assignment']


def test_bulk_invalid_account_rolls_back_entire_http_request(ambiguous,monkeypatch):
    from test_purchasing_ui import app_client
    engine,g=ambiguous
    g.products['SECOND']=replace(g.products['BIG'],variation_id='SECOND',sku='SECOND')
    original=attention.facts;monkeypatch.setattr(attention,'facts',lambda db,gateway=None:original(db,g))
    with Session(engine) as db:
        client,_=app_client(db)
        rows=[r for r in client.get('/management/purchasing/attention/workspace').json()['rows'] if r['kind']=='FINANCING']
        response=client.post('/management/purchasing/attention',json={'request_key':'invalid-bulk',
            'changes':[change(rows[0],account_id=11),change(rows[1],account_id=999)]},headers={'X-CSRF-Token':'test'})
        assert response.status_code==400
        assert db.scalar(select(func.count()).select_from(PurchasingOwnerDecision))==0
        assert db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action=='PURCHASING_EXCEPTION_DECISIONS'))==0
