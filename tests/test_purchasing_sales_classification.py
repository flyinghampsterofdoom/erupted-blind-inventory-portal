"""Authoritative Square-sales model: receiving-independent COGS and audited overrides."""
from datetime import datetime,date,timezone,timedelta
from decimal import Decimal
import copy
import pytest
from sqlalchemy import select,func
from sqlalchemy.orm import Session
from test_purchasing_monday_payments import (database_template,receiving_db,engine,monday_db,NOW,MONDAY,rec,save,legacy_lot)
from test_purchasing_ui import app_client
from app.models import (ConsignmentSaleFact,ConsignmentReturnFact,ConsignmentSalesSyncState,FundingReport,FundingReportLine,
    FundingReportFactLink,FundingPayment,PurchaseOrderLine,OrderPayment,PurchasingProductAuthority,VendorSkuConfig,AuditLog,PurchasingOwnerDecision,Vendor,OrderingCatalogIdentity)
from app.services import purchasing_classification_service as classification,purchasing_sales_finance_service as sales,purchasing_payments_service as monday,v2_funding_reports_service as funding

START=date(2026,10,11);END=date(2026,10,17)


def assign(db,category,*,vid='ORDINARY',scope='HISTORICAL',start='2026-10-11',end='2026-10-17',key=None,version=None):
    row=db.get(PurchasingOwnerDecision,('FINANCING',vid))
    return classification.save_classification(db,actor_id=1,variation_id=vid,category=category,scope=scope,start_date=start,end_date=end,
        reason='Owner reviewed sales classification',version=(row.version if row else 0) if version is None else version,request_key=key or category+'-'+vid+'-'+scope)


@pytest.fixture
def report_db(monday_db,monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return NOW if tz else NOW.replace(tzinfo=None)
    monkeypatch.setattr(funding,'datetime',Clock)
    with Session(monday_db) as db:
        db.add(ConsignmentSalesSyncState(id=1,last_result='COMPLETE',last_successful_start_at=datetime(2026,10,11,tzinfo=timezone.utc),
            last_successful_through_at=datetime(2026,10,19,tzinfo=timezone.utc),last_successful_at=NOW,updated_by_principal_id=1));db.commit()
    return monday_db


def report(db,account=10,**kw):
    return funding.calculate_report(db,account_id=account,start_date=START,end_date=END,store_ids=[],sku_filter='',internal_note='',overlap_acknowledged=False,actor_id=1,**kw)


def test_hierarchy_defaults_every_other_square_sku_to_pool_without_receipts(monday_db):
    with Session(monday_db) as db:
        context=classification.ClassificationContext(db)
        assert context.classify('UNSEEN-SQUARE-SKU',NOW).category=='CREDIT_CARD_POOL'
        assert context.classify('BIG',NOW).category=='BIG'
        assert context.classify('STREAM',NOW).category=='STREAMLINE'
        authority=db.get(PurchasingProductAuthority,'BIG');authority.square_vendor_ids=['V1','V3'];authority.source_complete=False;db.commit()
        assert classification.classify_product(db,variation_id='BIG',as_of=NOW).category=='STREAMLINE'
        authority.square_vendor_ids=[];db.commit()
        assert classification.classify_product(db,variation_id='BIG',as_of=NOW).category=='CREDIT_CARD_POOL'


@pytest.mark.parametrize('receipt',[False,True])
def test_identical_sales_cost_association_with_and_without_receiving_are_identical(monday_db,receipt):
    with Session(monday_db) as db:
        before=sales.calculate(db,start_date=START,end_date=END)
        before_monday=rec(db)
        legacy_lot(db,variation='ORDINARY',category='CONSIGNMENT',vendor=1,receipt=receipt);db.commit()
        after=sales.calculate(db,start_date=START,end_date=END);after_monday=rec(db)
        assert before==after
        assert before_monday==after_monday
        assert after['totals']=={'BIG':'50.00','STREAMLINE':'80.00','CREDIT_CARD_POOL':'90.00'}
        assert after['detected_sale_units']=='60.000'


def test_twenty_units_at_eight_dollars_require_no_po_or_receipt(report_db):
    from app.services.card_balance_service import adjust_balance
    with Session(report_db) as db:
        fact=db.scalar(select(ConsignmentSaleFact).where(ConsignmentSaleFact.square_variation_id=='ORDINARY'))
        fact.quantity_sold=20;fact.unit_cost_snapshot=8;fact.extended_cogs_snapshot=160
        db.delete(db.get(PurchasingProductAuthority,'ORDINARY'))
        adjust_balance(db,account_id=20,balance=0,actor_id=1,idempotency_key='zero-second-card');db.commit()
        assert rec(db)['rows'][2]['recommended']=='160.00'
        without_receipts=sales.calculate(db,start_date=START,end_date=END)
        legacy_lot(db,variation='ORDINARY',category='CONSIGNMENT',vendor=1,receipt=True);db.commit()
        assert sales.calculate(db,start_date=START,end_date=END)==without_receipts
        assert rec(db)['rows'][2]['recommended']=='160.00'
        r=report(db);db.commit()
        assert r.calculated_cogs==160 and r.units_sold==20
        assert r.vendor_id is None and r.warning_summary['sales_classification']['all_vendors']
        assert all(line.purchase_order_line_id is None for line in db.scalars(select(FundingReportLine).where(FundingReportLine.report_id==r.id)))


def test_owner_override_precedes_automatic_and_persists_without_receiving(report_db):
    with Session(report_db) as db:
        result=assign(db,'STREAMLINE');db.commit()
        assert result['saved']
    with Session(report_db) as db:
        r=rec(db);assert r['rows'][1]['recommended']=='170.00' and r['gross_card_pool']=='0.00'
        snapshot=report(db,account=12);db.commit();assert snapshot.calculated_cogs==170
        assert db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action=='SKU_FINANCING_CLASSIFIED'))==1
        assert db.scalar(select(func.count()).select_from(OrderPayment))==0


def test_missing_cost_retains_sale_and_does_not_invalidate_unrelated_accounts(report_db):
    with Session(report_db) as db:
        f=db.scalar(select(ConsignmentSaleFact).where(ConsignmentSaleFact.square_variation_id=='ORDINARY'));f.unit_cost_snapshot=None;f.extended_cogs_snapshot=None;db.commit()
        basis=sales.calculate(db,start_date=START,end_date=END);assert basis['detected_sale_units']=='60.000'
        assert basis['issues'][0]['quantity']=='30.000' and basis['issues'][0]['category']=='CREDIT_CARD_POOL'
        r=rec(db);assert r['rows'][0]['recommended']=='50.00' and r['rows'][1]['recommended']=='80.00' and r['rows'][2]['recommended'] is None
        card=report(db);db.commit();assert card.units_sold==30 and card.calculated_cogs is None
        line=db.scalar(select(FundingReportLine).where(FundingReportLine.report_id==card.id));assert line.units_sold==30 and line.extended_cogs is None
        with pytest.raises(ValueError,match='exceptions'):funding.finalize_report(db,report_id=card.id,actor_id=1)


def test_card_report_includes_all_vendors_and_missing_vendor_associations(report_db):
    with Session(report_db) as db:
        f=db.scalar(select(ConsignmentSaleFact).where(ConsignmentSaleFact.square_variation_id=='ORDINARY'));f.attribution_status='MISSING_VENDOR';f.vendor_id_snapshot=None
        db.delete(db.get(PurchasingProductAuthority,'ORDINARY'));db.commit()
        r=report(db,vendor_id=99999);db.commit()
        assert r.calculated_cogs==90 and r.units_sold==30 and r.vendor_id is None
        combined=funding.calculate_combined_report(db,account_id=20,start_date=START,end_date=END,store_ids=[],sku_filter='',internal_note='',actor_id=1)
        assert combined.calculated_cogs==90 and combined.vendor_id is None


def test_receiving_history_does_not_affect_card_reports(report_db):
    with Session(report_db) as db:
        first=report(db);db.commit()
        before=copy.deepcopy(first.warning_summary['sales_classification'])
        legacy_lot(db,variation='ORDINARY',category='CONSIGNMENT',vendor=1,receipt=False);db.commit()
        second=report(db,overlap_acknowledged=True) if False else funding.calculate_report(db,account_id=10,start_date=START,end_date=END,store_ids=[],sku_filter='',internal_note='',overlap_acknowledged=True,actor_id=1)
        assert before==second.warning_summary['sales_classification']
        assert first.calculated_cogs==second.calculated_cogs==90


def test_forward_change_does_not_rewrite_past_sales_and_historical_correction_is_explicit(monday_db):
    today=str(datetime.now(classification.BUSINESS_TIMEZONE).date())
    with Session(monday_db) as db:
        before=rec(db)
        assign(db,'BIG',scope='FORWARD',start=today,end=None,key='future');db.commit()
        assert rec(db)==before
        context=classification.ClassificationContext(db);assert context.classify('ORDINARY',datetime.now(timezone.utc)).category=='BIG'
        assign(db,'STREAMLINE',key='past');db.commit()
        assert rec(db)['rows'][1]['recommended']=='170.00'
        assert classification.ClassificationContext(db).classify('ORDINARY',datetime.now(timezone.utc)).category=='BIG'
        history=db.get(PurchasingOwnerDecision,('FINANCING','ORDINARY')).value['classification_history']
        assert [e['scope'] for e in history]==['FORWARD','HISTORICAL']


def test_classification_change_invalidates_draft_but_finalized_snapshot_stays_frozen(report_db):
    with Session(report_db) as db:
        draft=report(db);db.commit()
        finalized=funding.finalize_report(db,report_id=draft.id,actor_id=1);db.commit();frozen=copy.deepcopy(finalized.finalized_snapshot)
        other=report(db,account=20);db.commit()
        assign(db,'BIG');db.commit()
        assert db.get(FundingReport,draft.id).finalized_snapshot==frozen
        with pytest.raises(ValueError,match='changed'):funding.finalize_report(db,report_id=other.id,actor_id=1)
        assert rec(db)['rows'][0]['recommended']=='140.00'  # An informational pool snapshot never creates a second obligation.


def test_classification_save_owner_csrf_versions_retry_and_search_include_sold_only_skus(monday_db):
    from app.routers import management
    from app.auth import Principal as Actor,Role
    with Session(monday_db) as db:
        client,app=app_client(db)
        payload={'variation_id':'ORDINARY','category':'BIG','scope':'HISTORICAL','start_date':str(START),'end_date':str(END),'reason':'Owner correction','version':0,'request_key':'http-classification'}
        assert client.get('/management/purchasing/classifications?q=ORDINARY').status_code==200
        assert client.post('/management/purchasing/classifications',json=payload).status_code==403
        response=client.post('/management/purchasing/classifications',json=payload,headers={'X-CSRF-Token':'test'});assert response.status_code==200,response.text
        assert client.post('/management/purchasing/classifications',json=payload,headers={'X-CSRF-Token':'test'}).json()==response.json()
        assert client.post('/management/purchasing/classifications',json={**payload,'request_key':'new'},headers={'X-CSRF-Token':'test'}).status_code==400
        page=client.get('/management/purchasing/classifications?q=ORDINARY&as_of=2026-10-12');assert 'Owner assigned' in page.text
        app.dependency_overrides[management.ordering_access]=lambda:Actor(2,'admin',Role.ADMIN,None,True)
        assert client.post('/management/purchasing/classifications',json=payload,headers={'X-CSRF-Token':'test'}).status_code==403


def test_financial_attribution_is_not_a_sales_coverage_gate(monday_db):
    from app.models import PurchasingEvidenceRun
    with Session(monday_db) as db:
        run=db.scalar(select(PurchasingEvidenceRun).where(PurchasingEvidenceRun.source=='SALES'))
        run.status='PARTIAL';run.evidence={**run.evidence,'unresolved':100,'source_corrections':0};db.commit()
        assert rec(db)['coverage_complete'] is True
        run.evidence={**run.evidence,'source_corrections':1};db.commit()
        assert rec(db)['coverage_complete'] is False



def test_returns_retain_net_report_policy_and_do_not_reduce_monday_gross_basis(report_db):
    with Session(report_db) as db:
        original=db.scalar(select(ConsignmentSaleFact).where(ConsignmentSaleFact.square_variation_id=='ORDINARY'))
        db.add(ConsignmentReturnFact(square_return_order_id='return-1',square_return_uid='r1',square_return_line_uid='l1',
            square_variation_id='ORDINARY',square_location_id='L1',store_id=1,business_date=date(2026,10,16),
            returned_at=datetime(2026,10,16,20,tzinfo=timezone.utc),quantity_returned=2,refund_amount=10,currency='USD',
            product_name_snapshot='ORDINARY',sku_snapshot='ORDINARY',unit_cost_snapshot=3,extended_cogs_reversal=6,
            attribution_status='NON_CONSIGNMENT',original_sale_fact_id=original.id,source_synchronized_at=NOW));db.commit()
        r=report(db);assert r.calculated_cogs==84 and r.units_sold==30 and r.units_returned==2
        assert rec(db)['gross_card_pool']=='90.00'


def test_legacy_consignment_statement_uses_same_classifier_without_receiving(report_db,monkeypatch):
    from app.services import v2_consignment_facts_service as statements
    monkeypatch.setattr(statements,'inventory_snapshot',lambda *a,**kw:(Decimal(0),Decimal(0),[],[]))
    with Session(report_db) as db:
        assign(db,'STREAMLINE');db.commit()
        statement=statements.generate_report(db,vendor_id=3,start_date=START,end_date=END,actor_id=1);db.commit()
        assert statement.total_cogs==170 and statement.total_units==50
        assert statement.data_integrity_blockers['sales_classification']['category']=='STREAMLINE'
        assert 'SALE_MISSING_VENDOR' not in statement.data_integrity_blockers['codes']


def test_all_vendor_card_report_can_record_an_issuer_payment_through_existing_writer(report_db):
    with Session(report_db) as db:
        r=report(db);db.commit();funding.finalize_report(db,report_id=r.id,actor_id=1);db.commit()
        payment=funding.record_payment(db,account_id=10,entry_type='PAYMENT',amount=Decimal('10'),payment_date=NOW.date(),
            payment_source='Owner reported',confirmation_number='',reason='Test partial payment',internal_note='',allocations={},actor_id=1,idempotency_key='all-vendor-payment')
        db.commit();assert payment.vendor_id is None and payment.amount==10 and funding.report_position(db,report_id=r.id)['remaining_amount']==0


def test_all_vendor_report_form_does_not_require_po_membership(report_db):
    from app.routers import v2_funding_reports
    from app.auth import Principal as Actor,Role
    with Session(report_db) as db:
        client,app=app_client(db);app.include_router(v2_funding_reports.router)
        actor=Actor(1,'owner',Role.ADMIN,None,True)
        app.dependency_overrides[v2_funding_reports.feature_access]=lambda:actor
        app.dependency_overrides[v2_funding_reports.owner_access]=lambda:actor
        page=client.get('/v2/funding-accounts/reports/new?account_id=10');assert page.status_code==200,page.text
        assert 'All vendors' in page.text and 'name="vendor_id" required' not in page.text
        assert 'No purchase orders are assigned' not in page.text
        generated=report(db,account=12);db.commit()
        detail=client.get(f'/v2/funding-accounts/12/reports/{generated.id}')
        assert detail.status_code==200 and 'preserved legacy calculation' not in detail.text
        assert 'Sales-based financing calculation' in detail.text


def test_existing_owner_sku_account_mapping_is_respected_without_receiving(monday_db):
    from app.models import FundingSkuMapping
    with Session(monday_db) as db:
        db.add(FundingSkuMapping(account_id=10,normalized_sku='BIG',sku_snapshot='BIG',square_variation_id='BIG',
            effective_start_date=START,unit_cost=5,status='ACTIVE',reason='Explicit existing owner account association',created_by_principal_id=1));db.commit()
        assert classification.ClassificationContext(db).classify('BIG',NOW).category=='CREDIT_CARD_POOL'
        r=rec(db);assert r['rows'][0]['recommended']=='0.00' and r['gross_card_pool']=='140.00'


def test_unfinalized_drafts_do_not_invalidate_independent_monday_calculations(report_db):
    with Session(report_db) as db:
        before=rec(db);r=report(db);db.commit()
        assert rec(db)==before
        assign(db,'STREAMLINE');db.commit()
        updated=rec(db);assert updated['rows'][1]['recommended']=='170.00' and updated['rows'][2]['recommended']=='0.00'
        with pytest.raises(ValueError,match='changed'):funding.finalize_report(db,report_id=r.id,actor_id=1)


def test_historical_reclassification_cannot_double_post_a_finalized_source(report_db):
    with Session(report_db) as db:
        assign(db,'STREAMLINE');db.commit()
        original=report(db,account=12);db.commit();funding.finalize_report(db,report_id=original.id,actor_id=1);db.commit()
        frozen=copy.deepcopy(original.finalized_snapshot)
        assign(db,'BIG',key='financial-correction');db.commit()
        changed=report(db,account=11);db.commit()
        assert changed.calculated_cogs==140
        with pytest.raises(ValueError,match='already appear'):funding.finalize_report(db,report_id=changed.id,actor_id=1)
        db.rollback();assert db.get(FundingReport,original.id).finalized_snapshot==frozen


def test_combined_card_pool_can_finalize_only_once_across_cards(report_db):
    with Session(report_db) as db:
        first=report(db);second=report(db,account=20);db.commit()
        funding.finalize_report(db,report_id=first.id,actor_id=1);db.commit()
        with pytest.raises(ValueError,match='already appear'):funding.finalize_report(db,report_id=second.id,actor_id=1)
        assert first.calculated_cogs==second.calculated_cogs==90
        assert funding.report_position(db,report_id=first.id)['remaining_amount']==0


def test_direct_square_consignment_view_uses_owner_classification_not_receipts(report_db):
    from app.services import consignment_reporting_service as live
    with Session(report_db) as db:
        db.add(VendorSkuConfig(vendor_id=4,sku='ORDINARY',square_variation_id='ORDINARY',unit_cost=8,active=True,is_default_vendor=True));db.commit()
        class Reader:
            def search(self,**kw):
                yield [{'id':'live-square-sale','state':'COMPLETED','location_id':'L1','closed_at':'2026-10-12T20:00:00Z','line_items':[{'uid':'line','catalog_object_id':'ORDINARY','name':'SKU X','quantity':'20'}]}]
        before=live.sales_report(db,vendor_id=3,start_date=START,end_date=END,reader=Reader())
        assert before['cogs']==0
        assign(db,'STREAMLINE');db.commit()
        after=live.sales_report(db,vendor_id=3,start_date=START,end_date=END,reader=Reader())
        assert after['sold']==20 and after['cogs']==160 and after['complete']
        assert db.scalar(select(func.count()).select_from(OrderPayment))==0


def test_concurrent_owner_changes_reject_stale_version_without_losing_audit(monday_db):
    from concurrent.futures import ThreadPoolExecutor
    def writer(category):
        with Session(monday_db) as db:
            try:
                result=assign(db,category,version=0,key='concurrent-'+category);db.commit();return result['saved']
            except ValueError:db.rollback();return False
    with ThreadPoolExecutor(2) as executor:results=list(executor.map(writer,['BIG','STREAMLINE']))
    assert sorted(results)==[False,True]
    with Session(monday_db) as db:
        assert len(db.get(PurchasingOwnerDecision,('FINANCING','ORDINARY')).value['classification_history'])==1
        assert db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action=='SKU_FINANCING_CLASSIFIED'))==1


def test_sales_calculator_never_queries_purchase_or_receiving_tables(monday_db):
    from sqlalchemy import event
    statements=[]
    def capture(connection,cursor,statement,parameters,context,executemany):statements.append(statement.lower())
    event.listen(monday_db,'before_cursor_execute',capture)
    try:
        with Session(monday_db) as db:
            result=sales.calculate(db,start_date=START,end_date=END)
            assert result['totals']['CREDIT_CARD_POOL']=='90.00'
    finally:event.remove(monday_db,'before_cursor_execute',capture)
    assert statements and not any('purchase_order' in sql or 'order_payments' in sql or 'square_sync_events' in sql for sql in statements)


def test_established_effective_sku_cost_and_card_association_work_without_receiving(report_db):
    from app.models import FundingSkuMapping
    from app.services.card_balance_service import adjust_balance
    with Session(report_db) as db:
        fact=db.scalar(select(ConsignmentSaleFact).where(ConsignmentSaleFact.square_variation_id=='ORDINARY'))
        fact.quantity_sold=20;fact.unit_cost_snapshot=None;fact.extended_cogs_snapshot=None
        db.add(FundingSkuMapping(account_id=10,normalized_sku='ORDINARY',sku_snapshot='ORDINARY',square_variation_id='ORDINARY',
            effective_start_date=START,unit_cost=8,status='ACTIVE',reason='Existing established cost and account association',created_by_principal_id=1))
        adjust_balance(db,account_id=20,balance=0,actor_id=1,idempotency_key='mapping-example');db.commit()
        assert rec(db)['rows'][2]['recommended']=='160.00'
        r=report(db);assert r.calculated_cogs==160 and r.units_sold==20
        assert 'FUNDING_SKU_MAPPING' in r.warning_summary['sales_classification']['rows'][0]['cost_source']


def test_conflicting_effective_costs_are_explicit_exceptions_without_losing_sale_units(monday_db):
    from app.models import FundingSkuMapping
    with Session(monday_db) as db:
        fact=db.scalar(select(ConsignmentSaleFact).where(ConsignmentSaleFact.square_variation_id=='ORDINARY'));fact.unit_cost_snapshot=None;fact.extended_cogs_snapshot=None
        for account,cost in [(10,8),(20,9)]:
            db.add(FundingSkuMapping(account_id=account,normalized_sku='ORDINARY',sku_snapshot='ORDINARY',square_variation_id='ORDINARY',
                effective_start_date=START,unit_cost=cost,status='ACTIVE',reason='Conflicting owner cost evidence',created_by_principal_id=1))
        db.commit();basis=sales.calculate(db,start_date=START,end_date=END)
        assert basis['detected_sale_units']=='60.000' and basis['issues'][0]['quantity']=='30.000'
        assert basis['issues'][0]['unit_cost'] is None and basis['totals']['BIG']=='50.00'



def test_calculator_normalizes_numeric_orm_values_without_requiring_a_reload(monday_db):
    with Session(monday_db) as db:
        fact=db.scalar(select(ConsignmentSaleFact).where(ConsignmentSaleFact.square_variation_id=='ORDINARY'))
        fact.quantity_sold=20;fact.unit_cost_snapshot=8;fact.extended_cogs_snapshot=160
        # Int-valued ORM assignments remain in the identity map even after a
        # SELECT/autoflush; no transaction or manual expire should be required.
        result=sales.calculate(db,start_date=START,end_date=END)
        assert result['totals']['CREDIT_CARD_POOL']=='160.00' and not result['issues']


def test_verified_loyalty_markers_are_discounts_not_merchandise(report_db):
    from app.services.purchasing_product_policy_service import REWARD_VARIATIONS
    with Session(report_db) as db:
        ordinary=db.scalar(select(ConsignmentSaleFact).where(ConsignmentSaleFact.square_variation_id=='ORDINARY'))
        # A real product discounted 100% still consumes inventory and has COGS.
        ordinary.net_sales_amount=0;ordinary.discount_amount=ordinary.gross_sales_amount
        for n in range(2):
            db.add(ConsignmentSaleFact(square_order_id=f'loyalty-{n}',square_line_item_uid='reward',
                square_variation_id=next(iter(REWARD_VARIATIONS)),square_location_id='L1',store_id=1,
                business_date=START,transacted_at=datetime(2026,10,12,20,tzinfo=timezone.utc),quantity_sold=1,
                gross_sales_amount=1,discount_amount=1,tax_amount=0,net_sales_amount=0,currency='USD',
                product_name_snapshot='BOGO 50% off loyalty',variation_name_snapshot='*Rewards',
                attribution_status='MISSING_COST',attribution_source='SQUARE',source_synchronized_at=NOW))
        db.commit()
        basis=sales.calculate(db,start_date=START,end_date=END)
        assert not basis['issues'] and basis['totals']['CREDIT_CARD_POOL']=='90.00'
        assert len(basis['excluded'])==2 and all(x['reason']=='LOYALTY_DISCOUNT_MARKER' for x in basis['excluded'])
        assert Decimal(basis['detected_sale_units'])==62 and Decimal(basis['merchandise_sale_units'])==60
        assert report(db).units_sold==30
        assert rec(db)['rows'][2]['recommended']=='30.00'


@pytest.mark.parametrize('cost',[None,Decimal('7.25')])
def test_entire_kratom_category_preserves_historical_sales_without_purchasing_tasks(report_db,cost):
    from app.services.purchasing_product_policy_service import KRATOM_CATEGORY_IDS
    with Session(report_db) as db:
        f=db.scalar(select(ConsignmentSaleFact).where(ConsignmentSaleFact.square_variation_id=='ORDINARY'))
        f.product_name_snapshot='New product without Kratom in its name';f.unit_cost_snapshot=cost;f.extended_cogs_snapshot=None
        db.add(OrderingCatalogIdentity(square_variation_id='ORDINARY',sku='KRATOM-NEW',last_seen_at=NOW,
            catalog_attributes={'category_ids':list(KRATOM_CATEGORY_IDS)}));db.delete(db.get(PurchasingProductAuthority,'ORDINARY'));db.commit()
        basis=sales.calculate(db,start_date=START,end_date=END)
        assert not basis['issues'] and Decimal(basis['detected_sale_units'])==60
        if cost is None:
            assert len(basis['historical_limitations'])==1 and not basis['historical_cogs_complete']
            assert basis['historical_limitations'][0]['unit_cost'] is None
        else:
            assert not basis['historical_limitations'] and basis['totals']['CREDIT_CARD_POOL']=='217.50'
        result=rec(db)
        assert result['rows'][0]['recommended']=='50.00' and result['rows'][1]['recommended']=='80.00'
        assert result['rows'][2]['recommended']==('0.00' if cost is None else '72.50')
        r=report(db);db.commit()
        assert r.units_sold==30 and r.calculated_cogs==(0 if cost is None else Decimal('217.50'))
        assert r.warning_summary['sales_classification']['historical_cogs_complete']==(cost is not None)
        if cost is None:
            line=db.scalar(select(FundingReportLine).where(FundingReportLine.report_id==r.id))
            assert line.extended_cogs is None and line.unit_cost_snapshot is None
            funding.finalize_report(db,report_id=r.id,actor_id=1);db.commit()
            assert r.finalized_snapshot['sales_classification']['historical_limitations']


def test_cost_lookup_keeps_sibling_battery_variations_distinct(monday_db):
    with Session(monday_db) as db:
        f=db.scalar(select(ConsignmentSaleFact).where(ConsignmentSaleFact.square_variation_id=='ORDINARY'))
        f.product_name_snapshot='Samsung 18650 Battery 2500mAh 20A';f.variation_name_snapshot='Samsung 35A 21700'
        f.unit_cost_snapshot=None;f.extended_cogs_snapshot=None;f.sku_snapshot=None
        db.add_all([OrderingCatalogIdentity(square_variation_id='ORDINARY',square_item_id='BATTERIES',sku='S491722',variation_name='Samsung 35A 21700',last_seen_at=NOW),
            OrderingCatalogIdentity(square_variation_id='SIBLING-18650',square_item_id='BATTERIES',sku='518553J',last_seen_at=NOW),
            VendorSkuConfig(vendor_id=4,sku='518553J',square_variation_id='SIBLING-18650',unit_cost=Decimal('3.05'),active=True,is_default_vendor=True),
            VendorSkuConfig(vendor_id=4,sku='S491722',square_variation_id='ORDINARY',unit_cost=None,active=True,is_default_vendor=True)])
        db.commit();count=db.scalar(select(func.count()).select_from(VendorSkuConfig))
        result=sales.calculate(db,start_date=START,end_date=END)
        assert len(result['issues'])==1 and result['issues'][0]['sku']=='S491722'
        assert 'Samsung 35A 21700' in result['issues'][0]['product'] and result['issues'][0]['unit_cost'] is None
        assert db.scalar(select(func.count()).select_from(VendorSkuConfig))==count


def test_kratom_category_has_no_replenishment_forecast_cost_or_association_queue(engine):
    from dataclasses import replace
    from test_purchasing_phase2 import Gateway
    from app.services import purchasing_session_service as sessions,purchasing_attention_service as attention
    from app.services.purchasing_product_policy_service import KRATOM_CATEGORY_IDS
    gateway=Gateway();gateway.category_names={next(iter(KRATOM_CATEGORY_IDS)):'Kratom'}
    gateway.catalog['VAR1']=replace(gateway.catalog['VAR1'],vendor_ids=(),category_ids=tuple(KRATOM_CATEGORY_IDS))
    with Session(engine) as db:
        db.scalar(select(VendorSkuConfig).where(VendorSkuConfig.vendor_id==1,VendorSkuConfig.square_variation_id=='VAR1')).unit_cost=None;db.commit()
        # The ordinary category retains its normal purchasing and cost rules.
        evidence=sessions.collect_evidence(db,sessions.policy(),gateway=gateway)
        product=evidence['products']['VAR1']
        assert product['lifecycle']=='NO_FUTURE_REORDER' and product['quantity']==0 and product['review']==[]
        assert not product['owner_financing_account_id']
        assert all('VAR1' not in call[1]['variation_ids'] for call in gateway.calls if call[0]=='evidence')
        workspace=attention.workspace(db,actor_id=1,gateway=gateway)
        assert not any(r['variation_id']=='VAR1' for r in workspace['rows'])


def test_catalog_refresh_includes_sold_only_variations_and_keeps_costs_local(monday_db):
    from types import SimpleNamespace
    from app.services import v2_ordering_catalog_service as catalog
    from app.services.v2_ordering_square_gateway import SquareProductMetadata,SquareOrderingReadMetrics
    from app.services.purchasing_product_policy_service import KRATOM_CATEGORY_IDS
    seen=[]
    product=SquareProductMetadata('ORDINARY','S491722','Battery parent','Samsung 35A 21700',NOW,False,
        item_id='BATTERY-PARENT',category_ids=tuple(KRATOM_CATEGORY_IDS))
    def fetch(ids):
        seen.extend(ids)
        return SimpleNamespace(products={'ORDINARY':product},metrics=SquareOrderingReadMetrics())
    with Session(monday_db) as db:
        before=db.scalar(select(func.count()).select_from(VendorSkuConfig))
        assert not db.get(OrderingCatalogIdentity,'ORDINARY')
        catalog.refresh_ordering_catalog_identity(db,actor_principal_id=1,ip=None,gateway=SimpleNamespace(fetch_catalog_identity=fetch),attempted_at=NOW)
        db.commit()
        assert 'ORDINARY' in seen
        identity=db.get(OrderingCatalogIdentity,'ORDINARY')
        assert identity.sku=='S491722' and identity.variation_name=='Samsung 35A 21700'
        assert identity.catalog_attributes['category_ids']==list(KRATOM_CATEGORY_IDS)
        assert db.scalar(select(func.count()).select_from(VendorSkuConfig))==before
        # Source refresh does not overwrite a captured accounting cost either.
        fact=db.scalar(select(ConsignmentSaleFact).where(ConsignmentSaleFact.square_variation_id=='ORDINARY'))
        assert fact.unit_cost_snapshot==3 and fact.extended_cogs_snapshot==90


def test_explicit_owner_cost_corrects_existing_mapping_without_copying_sibling_or_saved_cost(monday_db):
    from app.services.purchase_order_admin_service import upsert_vendor_sku_config
    with Session(monday_db) as db:
        fact=db.scalar(select(ConsignmentSaleFact).where(ConsignmentSaleFact.square_variation_id=='ORDINARY'))
        fact.unit_cost_snapshot=None;fact.extended_cogs_snapshot=None
        mapping=VendorSkuConfig(vendor_id=4,sku='OWNER-18650',square_variation_id='ORDINARY',unit_cost=Decimal('2.95'),active=True,is_default_vendor=True)
        db.add(mapping);db.commit();row_id=mapping.id
        before=db.scalar(select(func.count()).select_from(VendorSkuConfig));saved_po=db.get(PurchaseOrderLine,1).unit_cost
        updated=upsert_vendor_sku_config(db,vendor_id=4,sku='OWNER-18650',square_variation_id='ORDINARY',unit_cost=Decimal('3.05'),pack_size=1,min_order_qty=0)
        db.commit()
        assert updated.id==row_id and db.scalar(select(func.count()).select_from(VendorSkuConfig))==before
        assert sales.calculate(db,start_date=START,end_date=END)['totals']['CREDIT_CARD_POOL']=='91.50'
        assert fact.unit_cost_snapshot is None and db.get(PurchaseOrderLine,1).unit_cost==saved_po


def test_discontinued_category_does_not_hide_invalid_source_currency(monday_db):
    from app.services.purchasing_product_policy_service import KRATOM_CATEGORY_IDS
    with Session(monday_db) as db:
        fact=db.scalar(select(ConsignmentSaleFact).where(ConsignmentSaleFact.square_variation_id=='ORDINARY'))
        fact.unit_cost_snapshot=None;fact.extended_cogs_snapshot=None;fact.currency='CAD'
        db.add(OrderingCatalogIdentity(square_variation_id='ORDINARY',last_seen_at=NOW,catalog_attributes={'category_ids':list(KRATOM_CATEGORY_IDS)}));db.commit()
        basis=sales.calculate(db,start_date=START,end_date=END)
        assert not basis['historical_limitations'] and len(basis['issues'])==1
        assert 'currency' in basis['issues'][0]['message']
