"""Monday payments: real PostgreSQL transactions, existing financial writers."""
import copy
from datetime import datetime,date,timedelta,timezone
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
import pytest
from sqlalchemy import select,func,text
from sqlalchemy.orm import Session
from test_purchasing_phase2 import engine,receiving_db,database_template
from test_purchasing_ui import app_client
from app.config import settings
from app.models import (Vendor,FundingAccount,PaymentMethod,PurchasingProductAuthority,ConsignmentSaleFact,
    PurchasingPaymentBatch as Batch,PurchasingPaymentBatchEntry as Entry,FundingPayment,FundingReport,FundingLedgerEntry,
    OrderPayment,PurchaseOrderLine,PurchaseOrder,Principal,AuditLog)
from app.services import purchasing_payments_service as service
from app.services import v2_order_payments_service as finance
from app.services import v2_funding_reports_service as funding
from app.services.card_balance_service import adjust_balance,actual_card_balance
from app.services.purchasing_evidence_service import record_run,BUSINESS_TIMEZONE

NOW=datetime(2026,10,19,17,tzinfo=timezone.utc)
MONDAY='2026-10-19'

@pytest.fixture
def monday_db(engine,monkeypatch):
    from types import SimpleNamespace
    from app.services import v2_ordering_square_gateway
    monkeypatch.setattr(v2_ordering_square_gateway,'SquareOrderingReadGateway',lambda:SimpleNamespace(
        fetch_active_catalog=lambda:{},fetch_product_metadata=lambda ids:{},
        fetch_current_inventory_counts=lambda **kw:SimpleNamespace(counts={})))
    monkeypatch.setattr(settings,'purchasing_monday_payments_enabled',True)
    monkeypatch.setattr(settings,'purchasing_big_square_vendor_id','V1')
    monkeypatch.setattr(settings,'purchasing_streamline_square_vendor_id','V3')
    monkeypatch.setattr(settings,'purchasing_card1_account_id',10)
    monkeypatch.setattr(settings,'purchasing_card2_account_id',20)
    monkeypatch.setattr(finance,'utc_now',lambda:NOW)
    with Session(engine) as db:
        db.add_all([Vendor(id=3,name='Streamline',square_vendor_id='V3',active=True),Vendor(id=4,name='Ordinary',square_vendor_id='V4',active=True)])
        db.add(PaymentMethod(id=20,display_name='Card Two',category='CREDIT_CARD',is_active=True,created_by_principal_id=1,updated_by_principal_id=1));db.flush()
        db.add_all([FundingAccount(id=i,account_type='CONSIGNMENT',vendor_id=v,display_name=n,created_by_principal_id=1,updated_by_principal_id=1) for i,v,n in [(11,1,'Big Wholesale'),(12,3,'Streamline')]])
        db.add(FundingAccount(id=20,account_type='CREDIT_CARD',payment_method_id=20,display_name='Card Two',created_by_principal_id=1,updated_by_principal_id=1));db.flush()
        for i,(vid,vendor,qty,cost) in enumerate([('BIG','V1',10,5),('STREAM','V3',20,4),('ORDINARY','V4',30,3)],1):
            db.add(PurchasingProductAuthority(square_variation_id=vid,square_vendor_ids=[vendor],observed_at=NOW,source_complete=True))
            db.add(ConsignmentSaleFact(square_order_id=f'sale-{i}',square_line_item_uid='line',square_variation_id=vid,
                square_location_id='L1',store_id=1,business_date=date(2026,10,12),transacted_at=datetime(2026,10,12,20,tzinfo=timezone.utc),
                quantity_sold=qty,gross_sales_amount=qty*10,discount_amount=50,tax_amount=3,net_sales_amount=1,
                currency='USD',product_name_snapshot=vid,sku_snapshot=vid,unit_cost_snapshot=cost,extended_cogs_snapshot=qty*cost,
                attribution_status='ATTRIBUTED' if vendor!='V4' else 'NON_CONSIGNMENT',attribution_source='SQUARE',source_synchronized_at=NOW))
        start=datetime(2026,10,11,tzinfo=BUSINESS_TIMEZONE).astimezone(timezone.utc);end=datetime(2026,10,18,tzinfo=BUSINESS_TIMEZONE).astimezone(timezone.utc)
        record_run(db,source='SALES',status='COMPLETE',started_at=NOW-timedelta(minutes=1),completed_at=NOW,
            scope={'start_at':start.isoformat(),'end_at':end.isoformat(),'location_ids':['L1','L2','L3','L4']},
            evidence={'source_filter':'CLOSED_AT','source_complete':True,'coverage_contract':2})
        adjust_balance(db,account_id=10,balance=1000,actor_id=1,idempotency_key='issuer-1')
        adjust_balance(db,account_id=20,balance=2000,actor_id=1,idempotency_key='issuer-2');db.commit()
    yield engine


def rec(db):return service.recommendation(db,actor_id=1,monday=MONDAY)
def actuals(r):return {row['role']:row['recommended'] or '0' for row in r['rows']}
def save(db,r,key=None,values=None,corrects=None):
    return service.save(db,actor_id=1,monday=MONDAY,amounts=values or actuals(r),request_key=key or uuid4().hex,token=r['token'],corrects=corrects)


def test_calendar_and_gross_recommendations_not_discounts_or_outside_period(monday_db):
    with Session(monday_db) as db:
        r=rec(db);assert (r['sales_start'],r['sales_end'],r['monday'])==('2026-10-11','2026-10-17',MONDAY)
        assert actuals(r)=={'big':'50.00','streamline':'80.00','card1':'30.00','card2':'60.00'}
        assert service.period(date(2026,10,19))==(date(2026,10,11),date(2026,10,17),date(2026,10,19))
        f=db.scalar(select(ConsignmentSaleFact).where(ConsignmentSaleFact.square_variation_id=='BIG'))
        f.transacted_at=datetime(2026,10,18,7,tzinfo=timezone.utc);db.commit()
        assert rec(db)['rows'][0]['recommended']=='0.00'
        assert db.scalar(select(func.count()).select_from(FundingPayment))==0


def test_atomic_save_uses_existing_reports_payments_balance_and_immutable_history(monday_db):
    with Session(monday_db) as db:
        r=rec(db);b=save(db,r,key='one');db.commit()
        assert db.scalar(select(func.count()).select_from(Entry))==4
        assert db.scalar(select(func.count()).select_from(FundingPayment))==4
        assert db.scalar(select(func.count()).select_from(FundingReport))==4
        assert actual_card_balance(db,account_id=10)==970 and actual_card_balance(db,account_id=20)==1940
        assert save(db,r,key='one').id==b.id;db.commit()
        assert db.scalar(select(func.count()).select_from(FundingPayment))==4
        with pytest.raises(service.Conflict):save(db,r,key='different')
        db.rollback()
        with pytest.raises(service.Conflict):save(db,r,key='one',values={**actuals(r),'big':'1'})
        db.rollback()
        with pytest.raises(Exception):db.execute(text("UPDATE purchasing_payment_batch_entries SET actual=999 WHERE batch_id=:id"),{'id':b.id});db.commit()
        db.rollback()
        assert db.scalar(select(Entry.actual).where(Entry.batch_id==b.id,Entry.role=='big'))==50
        for report in db.scalars(select(FundingReport)):
            assert funding.report_position(db,report_id=report.id)['remaining_amount']==0
            with pytest.raises(ValueError,match='Monday payment correction'):funding.void_report(db,report_id=report.id,reason='wrong workflow',actor_id=1)
            with pytest.raises(ValueError,match='Monday payment correction'):funding.delete_report(db,account_id=report.account_id,report_id=report.id,expected_token='anything',actor_id=1)


def test_failure_rolls_back_every_account_and_retries_safely(monday_db,monkeypatch):
    original=funding.record_payment;calls=[]
    def fail(db,**kw):
        calls.append(kw['account_id'])
        if len(calls)==3:raise ValueError('controlled third-account failure')
        return original(db,**kw)
    with Session(monday_db) as db:
        r=rec(db);monkeypatch.setattr(funding,'record_payment',fail)
        with pytest.raises(ValueError,match='third-account'):save(db,r,key='retry')
        db.rollback()
        for model in (Batch,Entry,FundingPayment,FundingReport,FundingLedgerEntry):assert db.scalar(select(func.count()).select_from(model))==0
        assert actual_card_balance(db,account_id=10)==1000
        monkeypatch.setattr(funding,'record_payment',original);save(db,r,key='retry');db.commit()
        assert db.scalar(select(func.count()).select_from(FundingPayment))==4


def test_zero_and_explicit_append_only_correction(monday_db):
    with Session(monday_db) as db:
        r=rec(db);b=save(db,r);db.commit();old=copy.deepcopy(service.batch_view(db,b));entries=service._entries(db,b.id)
        with pytest.raises(ValueError,match='weekly payment correction'):funding.reverse_payment(db,payment_id=entries[0].payment_id,reason='standalone',actor_id=1)
        db.rollback()
        amounts={'big':'40','streamline':'0','card1':'35','card2':'65'}
        correction=save(db,r,key='correction',values=amounts,corrects=b.id);db.commit()
        assert correction.revision==2 and correction.supersedes_id==b.id
        assert service.batch_view(db,b)==old
        assert actual_card_balance(db,account_id=10)==965 and actual_card_balance(db,account_id=20)==1935
        assert save(db,r,key='correction',values=amounts,corrects=b.id).id==correction.id
        db.commit()
        assert db.scalar(select(func.count()).select_from(FundingPayment))==11  # 4 original, 4 reversal, 3 replacements.
        assert len(service.workspace(db,actor_id=1,monday=MONDAY)['history'])==2


def test_all_zero_batch_is_audited_without_money_records(monday_db):
    with Session(monday_db) as db:
        r=rec(db);save(db,r,values={r:'0' for r in service.ROLES});db.commit()
        assert db.scalar(select(func.count()).select_from(FundingPayment))==0
        assert db.scalar(select(func.count()).select_from(Entry))==4
        assert db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action=='MONDAY_PAYMENTS_SAVED'))==1


@pytest.mark.parametrize('same_key',[False,True])
def test_concurrent_submissions_have_one_weekly_batch(monday_db,same_key):
    with Session(monday_db) as db:r=rec(db)
    def worker(i):
        with Session(monday_db) as db:
            try:b=save(db,r,key='same' if same_key else f'key-{i}');db.commit();return b.id
            except service.Conflict:db.rollback();return None
    with ThreadPoolExecutor(2) as p:results=list(p.map(worker,[1,2]))
    assert len(set(x for x in results if x))==1
    with Session(monday_db) as db:
        assert db.scalar(select(func.count()).select_from(Batch))==1
        assert db.scalar(select(func.count()).select_from(FundingPayment))==4


@pytest.mark.parametrize('value',['-1','0.001','NaN','Infinity','10000000000',True])
def test_invalid_amounts_do_not_write(monday_db,value):
    with Session(monday_db) as db:
        r=rec(db)
        with pytest.raises(ValueError):save(db,r,values={**actuals(r),'big':value})
        db.rollback();assert db.scalar(select(func.count()).select_from(Batch))==0


def test_stale_recommendations_block_without_overwriting_owner_values(monday_db):
    with Session(monday_db) as db:
        r=rec(db);adjust_balance(db,account_id=10,balance=1500,actor_id=1,idempotency_key='new-balance');db.commit()
        with pytest.raises(service.Conflict,match='changed'):save(db,r,values={'big':'1','streamline':'2','card1':'3','card2':'4'})
        db.rollback();assert db.scalar(select(func.count()).select_from(Batch))==0


def test_missing_balances_and_ambiguous_financing_remain_unavailable(monday_db):
    with Session(monday_db) as db:
        settings.purchasing_card1_account_id=None
        assert rec(db)['rows'][2]['recommended'] is None


def test_http_owner_csrf_saved_history_and_existing_account_pages(monday_db):
    from app.routers import v2_funding_reports,management
    from app.auth import Principal as Actor,Role
    with Session(monday_db) as db:
        client,app=app_client(db);client.headers['x-forwarded-for']='127.0.0.1'
        app.include_router(v2_funding_reports.router)
        app.dependency_overrides[v2_funding_reports.feature_access]=lambda:Actor(1,'owner',Role.ADMIN,None,True)
        app.dependency_overrides[v2_funding_reports.owner_access]=lambda:Actor(1,'owner',Role.ADMIN,None,True)
        url='/management/purchasing/payments'
        page=client.get(url+'?monday='+MONDAY);assert page.status_code==200,page.text
        assert 'Save Payments' in page.text and 'Weekly payment history' in page.text
        r=client.get(url+'/workspace?monday='+MONDAY).json()['recommendation']
        payload={'monday':MONDAY,'amounts':actuals(r),'token':r['token'],'request_key':'http'}
        assert client.post(url,json=payload).status_code==403
        response=client.post(url,json=payload,headers={'X-CSRF-Token':'test'});assert response.status_code==200,response.text
        assert client.post(url,json=payload,headers={'X-CSRF-Token':'test'}).json()['batch_id']==response.json()['batch_id']
        for e in service._entries(db,response.json()['batch_id']):
            page=client.get(f'/v2/funding-accounts/{e.account_id}');assert page.status_code==200,page.text
            detail=client.get(f'/v2/funding-accounts/{e.account_id}/reports/{e.report_id}');assert detail.status_code==200,detail.text
            assert 'Weekly gross COGS dollar allocation' in detail.text and '<strong>Voided:</strong>' not in detail.text
            assert 'Void this report' not in detail.text
        app.dependency_overrides[management.ordering_access]=lambda:Actor(2,'other',Role.ADMIN,None,True)
        assert client.get(url).status_code==403
        assert client.post(url,json=payload,headers={'X-CSRF-Token':'test'}).status_code==403

@pytest.mark.parametrize('category,expected',[('WIRE',{'big':'50.00','streamline':'80.00','card1':'0.00','card2':'0.00'}),('CONSIGNMENT',{'big':'140.00','streamline':'80.00','card1':'0.00','card2':'0.00'})])
def test_saved_received_lot_financing_survives_supplier_substitution(monday_db,category,expected):
    from app.models import PurchaseOrderReceipt,PurchaseOrderReceiptLine
    with Session(monday_db) as db:
        po=db.get(PurchaseOrder,1);po.ordered_at=datetime(2026,10,10,tzinfo=timezone.utc);po.vendor_id=2
        line=db.get(PurchaseOrderLine,1);line.variation_id='ORDINARY';line.received_qty_total=30
        db.add(OrderPayment(purchase_order_id=1,vendor_id=1,payment_method_id=10,payment_category_snapshot=category,
            status='UNPAID',financial_treatment='REPLENISHMENT' if category=='CONSIGNMENT' else 'INVOICE',
            created_at=datetime(2026,10,10,tzinfo=timezone.utc)))
        receipt=PurchaseOrderReceipt(purchase_order_id=1,status='SUBMITTED',received_by_principal_id=1,received_at=datetime(2026,10,10,tzinfo=timezone.utc))
        db.add(receipt);db.flush();db.add(PurchaseOrderReceiptLine(receipt_id=receipt.id,purchase_order_line_id=1,received_qty=30));db.commit()
        assert actuals(rec(db))==expected
        po.vendor_id=3;db.commit()
        assert actuals(rec(db))==expected


def test_transaction_override_and_missing_evidence_are_explicit(monday_db):
    from app.models import PurchasingEvidenceRun
    with Session(monday_db) as db:
        fact=db.scalar(select(ConsignmentSaleFact).where(ConsignmentSaleFact.square_variation_id=='ORDINARY'))
        fact.attribution_source='OWNER_TRANSACTION_OVERRIDE';fact.attribution_status='ATTRIBUTED';fact.vendor_id_snapshot=1;db.commit()
        assert actuals(rec(db))=={'big':'140.00','streamline':'80.00','card1':'0.00','card2':'0.00'}
        db.query(PurchasingEvidenceRun).delete();db.commit()
        r=rec(db);assert all(row['recommended'] is None and 'coverage' in row['unavailable'] for row in r['rows'])


def test_nonconsignment_cost_uses_existing_local_configuration_and_freezes_evidence(monday_db):
    from app.models import VendorSkuConfig,FundingReportFactLink
    with Session(monday_db) as db:
        fact=db.scalar(select(ConsignmentSaleFact).where(ConsignmentSaleFact.square_variation_id=='ORDINARY'))
        fact.unit_cost_snapshot=None;fact.extended_cogs_snapshot=None
        db.add(VendorSkuConfig(vendor_id=4,sku='ORDINARY',square_variation_id='ORDINARY',unit_cost=3,active=True));db.commit()
        r=rec(db);assert actuals(r)['card1']=='30.00'
        assert r['facts'][-1]['cost_source'].startswith('LOCAL_CONFIG:')
        save(db,r);db.commit()
        assert db.scalar(select(func.count()).select_from(FundingReportFactLink))==4
        assert fact.unit_cost_snapshot is None  # No rewriting captured source facts.


def test_checkpoint_blocks_unsafe_correction_atomically(monday_db):
    with Session(monday_db) as db:
        r=rec(db);b=save(db,r);db.commit()
        adjust_balance(db,account_id=10,balance=900,actor_id=1,idempotency_key='later-checkpoint');db.commit()
        with pytest.raises(ValueError,match='checkpoint|reconcil|covered'):save(db,r,corrects=b.id,values={**actuals(r),'card1':'5'})
        db.rollback()
        assert db.scalar(select(func.count()).select_from(Batch))==1
        assert db.scalar(select(func.count()).select_from(FundingPayment))==4
        assert actual_card_balance(db,account_id=10)==900


def test_database_requires_complete_batch_and_retains_history_on_downgrade(monday_db):
    from alembic import command
    from app.schema_contract import _alembic_config
    with Session(monday_db) as db:
        r=rec(db)
        db.add(Batch(sales_start=date(2026,10,11),sales_end=date(2026,10,17),monday=date(2026,10,19),payment_date=date(2026,10,19),
            revision=1,request_key='incomplete',request_payload={},recommendations=r,actor_id=1))
        with pytest.raises(Exception,match='four account'):db.commit()
        db.rollback();save(db,r);db.commit()
    with pytest.raises(RuntimeError,match='Refusing to discard weekly'):
        command.downgrade(_alembic_config(monday_db.url.render_as_string(hide_password=False)),'20261017_0034')


def test_finalized_source_in_another_account_cannot_be_recommended_twice(monday_db):
    from app.models import FundingReportFactLink,FundingReportLine
    with Session(monday_db) as db:
        fact=db.scalar(select(ConsignmentSaleFact).where(ConsignmentSaleFact.square_variation_id=='BIG'))
        other=FundingAccount(account_type='CONSIGNMENT',vendor_id=4,display_name='Older account',created_by_principal_id=1,updated_by_principal_id=1)
        db.add(other);db.flush()
        report=FundingReport(account_id=other.id,vendor_id=4,report_number='OLDER',account_name_snapshot='Older',account_type_snapshot='CONSIGNMENT',sales_start_date=date(2026,10,11),sales_end_date=date(2026,10,17),status='FINALIZED',calculated_cogs=50,created_by_principal_id=1)
        db.add(report);db.flush()
        line=FundingReportLine(report_id=report.id,normalized_sku='BIG',sku_snapshot='BIG',product_name_snapshot='Big',units_sold=10,units_returned=0,net_units=10,source_transaction_count=1,extended_cogs=50,mapping_effective_date_snapshot=date(2026,10,11));db.add(line);db.flush()
        db.add(FundingReportFactLink(report_id=report.id,report_line_id=line.id,sale_fact_id=fact.id,cogs_amount_snapshot=50));db.commit()
        assert all(row['recommended'] is None and 'already represented' in row['unavailable'] for row in rec(db)['rows'])


def test_navigation_replaces_placeholder_for_owner_in_both_shells(monday_db,monkeypatch):
    from app.auth import Principal as Actor,Role
    from app.v2.navigation import build_navigation
    from app.v2.workspaces import DESTINATIONS
    from starlette.requests import Request
    request=Request({'type':'http','method':'GET','path':'/management/purchasing/payments','headers':[],'query_string':b''})
    request.state.permission_flags={'ordering.manage':True,'nav.inventory.all':True}
    request.state.principal=Actor(1,'owner',Role.ADMIN,None,True)
    def children():return [c for section in build_navigation(request) for c in section.children]
    item=next(c for c in children() if c.key=='inventory.order_payments')
    assert item.href=='/management/purchasing/payments' and item.active and item.available
    assert not next(c for c in children() if c.key=='inventory.purchasing').active
    native=next(d for d in DESTINATIONS if d.path=='/management/purchasing/payments')
    assert native.allowed(request) and not native.compatibility
    request.state.principal=Actor(2,'other',Role.ADMIN,None,True)
    assert not native.allowed(request)
    assert '/management/purchasing/payments' not in [c.href for c in children()]
    request.state.principal=Actor(1,'owner',Role.ADMIN,None,True)
    monkeypatch.setattr(settings,'purchasing_monday_payments_enabled',False)
    assert not native.allowed(request)
    assert '/management/purchasing/payments' not in [c.href for c in children()]


def test_0035_additive_migration_preserves_existing_financial_and_source_rows(monday_db):
    from alembic import command
    from app.schema_contract import _alembic_config
    config=_alembic_config(monday_db.url.render_as_string(hide_password=False))
    command.downgrade(config,'20261017_0034')
    tables=('funding_accounts','funding_reports','funding_payments','funding_payment_allocations','funding_ledger_entries',
        'funding_actual_activity','funding_balance_checkpoints','consignment_sale_facts','order_payments',
        'purchase_orders','purchase_order_lines','purchase_order_receipts','audit_log')
    def rows():
        with monday_db.connect() as con:
            return {table:con.execute(text(f'SELECT row_to_json(t)::text FROM {table} t ORDER BY id')).scalars().all() for table in tables}
    before=rows();command.upgrade(config,'20261017_0035');assert rows()==before
    with monday_db.connect() as con:
        assert con.scalar(text('SELECT count(*) FROM purchasing_payment_batches'))==0
        assert con.scalar(text('SELECT count(*) FROM funding_actual_activity'))==0
        assert con.scalar(text('SELECT count(*) FROM funding_balance_checkpoints'))==2
