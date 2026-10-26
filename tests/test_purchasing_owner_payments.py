"""Owner payments are independent of advisory issuer-balance allocations."""
from decimal import Decimal
from uuid import uuid4
import copy
import pytest
from sqlalchemy import select,func,delete
from sqlalchemy.orm import Session
from test_purchasing_sales_classification import report_db,report,START,END
from test_purchasing_monday_payments import monday_db,engine,receiving_db,database_template,rec,save,NOW,MONDAY
from test_purchasing_ui import app_client
from app.models import (FundingBalanceCheckpoint,FundingActualActivity,FundingPayment,FundingReport,
    FundingReportFactLink,FundingAccount,PaymentMethod,FundingLedgerEntry,PurchasingPaymentBatch,AuditLog)
from app.services import purchasing_payments_service as monday,v2_funding_reports_service as funding,v2_order_payments_service as finance
from app.services.card_balance_service import actual_card_balance,adjust_balance


def unknown_balances(db):
    db.execute(delete(FundingBalanceCheckpoint));db.commit()
    assert all(actual_card_balance(db,account_id=a) is None for a in (10,20))
    card=db.get(FundingAccount,20);card.display_name='Credit Card 2';card.last_four=None;card.issuer=None
    method=db.get(PaymentMethod,card.payment_method_id);method.display_name='Credit Card 2';method.last_four=None;method.institution_or_company_name=None;db.commit()


@pytest.mark.parametrize('amounts',[
 {'big':'0','streamline':'0','card1':'150','card2':'25'},
 {'big':'1','streamline':'2','card1':'0','card2':'0'},
 {'big':'50','streamline':'80','card1':'5','card2':'1000'},
])
def test_unknown_balances_generic_card_save_exact_owner_amounts_and_retry(monday_db,amounts):
    with Session(monday_db) as db:
        unknown_balances(db);r=rec(db)
        assert r['gross_card_pool']=='90.00'
        assert all(row['status']=='advisory_unavailable' and row['payment_allowed'] and row['recommended'] is None for row in r['rows'][2:])
        assert r['rows'][3]['name']=='Credit Card 2'
        batch=save(db,r,key='unknown-owner',values=amounts);db.commit()
        assert {e['role']:Decimal(e['actual']) for e in monday.batch_view(db,batch)['entries']}=={k:Decimal(v) for k,v in amounts.items()}
        assert save(db,r,key='unknown-owner',values=amounts).id==batch.id
        assert db.scalar(select(func.count()).select_from(FundingPayment))==sum(Decimal(v)>0 for v in amounts.values())
        assert db.scalar(select(func.count()).select_from(FundingReport).where(FundingReport.account_type_snapshot=='CREDIT_CARD'))==0
        assert all(actual_card_balance(db,account_id=a) is None for a in (10,20))
        assert db.scalar(select(func.count()).select_from(FundingBalanceCheckpoint))==0
        assert db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action=='MONDAY_PAYMENTS_SAVED'))==1


def test_optional_balances_enable_advice_but_owner_can_pay_above_below_or_zero(monday_db):
    with Session(monday_db) as db:
        r=rec(db);assert [row['recommended'] for row in r['rows'][2:]]==['30.00','60.00']
        b=save(db,r,values={'big':'0','streamline':'0','card1':'300','card2':'0'});db.commit()
        entries=monday.batch_view(db,b)['entries']
        assert entries[2]['difference_from_recommendation']=='270.00' and entries[3]['difference_from_recommendation']=='-60.00'
        assert actual_card_balance(db,account_id=10)==700
        assert db.scalar(select(func.count()).select_from(FundingReport).where(FundingReport.account_type_snapshot=='CREDIT_CARD'))==0


def test_correction_of_unknown_balance_payments_reverses_and_preserves_history(monday_db):
    with Session(monday_db) as db:
        unknown_balances(db);r=rec(db);b=save(db,r,values={'big':'0','streamline':'0','card1':'100','card2':'250'});db.commit()
        original=copy.deepcopy(monday.batch_view(db,b));corrected=save(db,rec(db),corrects=b.id,values={'big':'0','streamline':'0','card1':'50','card2':'0'});db.commit()
        assert monday.batch_view(db,b)==original and corrected.supersedes_id==b.id
        assert db.scalar(select(func.sum(FundingActualActivity.signed_amount)))==-50
        assert all(actual_card_balance(db,account_id=a) is None for a in (10,20))
        # A later absolute balance covers all preceding known activity exactly once.
        adjust_balance(db,account_id=10,balance=800,actor_id=1,idempotency_key='later-issuer');db.commit()
        assert actual_card_balance(db,account_id=10)==800


def test_pool_report_finalizes_without_balances_and_creates_no_individual_card_debt(report_db):
    with Session(report_db) as db:
        unknown_balances(db);r=report(db,account=20);db.commit()
        data=r.warning_summary['sales_classification']
        assert r.units_sold==30 and r.calculated_cogs==90
        assert data['report_mode']=='COMBINED_POOL' and data['all_vendors'] and data['allocation_error'] is None
        assert data['recommended_allocation'] is None and set(data['issuer_balances'].values())=={None}
        assert funding.report_position(db,report_id=r.id)['remaining_amount']==0
        funding.finalize_report(db,report_id=r.id,actor_id=1);db.commit()
        assert r.status=='FINALIZED'
        assert all(actual_card_balance(db,account_id=a) is None for a in (10,20))
        # Informational COGS must not suppress independent Monday advice/payment entry.
        assert rec(db)['rows'][2]['status']=='advisory_unavailable'
        b=save(db,rec(db),values={'big':'0','streamline':'0','card1':'100','card2':'0'});db.commit()
        assert b.id
        duplicate=report(db,account=10);db.commit()
        with pytest.raises(ValueError,match='already appear'):funding.finalize_report(db,report_id=duplicate.id,actor_id=1)


def test_pool_cogs_report_is_not_changed_or_invalidated_by_optional_balance_advice(report_db):
    with Session(report_db) as db:
        unknown_balances(db);r=report(db);db.commit();token=r.warning_summary['sales_classification']['token']
        adjust_balance(db,account_id=10,balance=1000,actor_id=1,idempotency_key='optional-1')
        adjust_balance(db,account_id=20,balance=2000,actor_id=1,idempotency_key='optional-2');db.commit()
        from app.services.purchasing_sales_reports_service import snapshot
        current=snapshot(db,account=db.get(FundingAccount,10),start_date=START,end_date=END,store_ids=[])
        assert current['token']==token and current['calculated_cogs']=='90.00'
        assert current['recommended_allocation']=={'card1':'30.00','card2':'60.00'}
        funding.finalize_report(db,report_id=r.id,actor_id=1);db.commit()
        assert r.calculated_cogs==90


def test_http_manual_payment_with_unknown_balances_retains_owner_and_csrf_checks(monday_db):
    from app.auth import Principal as Actor,Role
    from app.routers import management
    with Session(monday_db) as db:
        unknown_balances(db);client,app=app_client(db);url='/management/purchasing/payments'
        assert client.get(url+'?monday='+MONDAY).status_code==200
        r=client.get(url+'/workspace?monday='+MONDAY).json()['recommendation']
        p={'monday':MONDAY,'amounts':{'big':'0','streamline':'0','card1':'90','card2':'0'},'token':r['token'],'request_key':uuid4().hex}
        assert client.post(url,json=p).status_code==403
        app.dependency_overrides[management.ordering_access]=lambda:Actor(2,'other',Role.ADMIN,None,True)
        assert client.post(url,json=p,headers={'X-CSRF-Token':'test'}).status_code==403
        app.dependency_overrides[management.ordering_access]=lambda:Actor(1,'owner',Role.ADMIN,None,True)
        assert client.post(url,json=p,headers={'X-CSRF-Token':'test'}).status_code==200
        assert client.post(url,json=p,headers={'X-CSRF-Token':'test'}).status_code==200
        assert db.scalar(select(func.count()).select_from(FundingPayment))==1


def test_standalone_card_activity_accepts_owner_payment_without_vendor_report_or_balance(report_db):
    with Session(report_db) as db:
        unknown_balances(db)
        payment=funding.record_compact_payment(db,account_id=20,payment_date=NOW.date(),amount=Decimal('1000'),
            paid_in_full=False,actor_id=1,idempotency_key='owner-standalone')
        db.commit()
        retry=funding.record_compact_payment(db,account_id=20,payment_date=NOW.date(),amount=Decimal('1000'),
            paid_in_full=False,actor_id=1,idempotency_key='owner-standalone')
        assert retry.id==payment.id and payment.vendor_id is None
        assert actual_card_balance(db,account_id=20) is None
        assert db.scalar(select(func.count()).select_from(FundingPayment))==1
        assert db.scalar(select(func.count()).select_from(FundingReport))==0


def test_generic_account_activity_page_does_not_require_vendor_or_optional_metadata(report_db):
    from app.auth import Principal as Actor,Role
    from app.routers import v2_funding_reports
    with Session(report_db) as db:
        unknown_balances(db);client,app=app_client(db);app.include_router(v2_funding_reports.router)
        app.dependency_overrides[v2_funding_reports.feature_access]=lambda:Actor(1,'owner',Role.ADMIN,None,True)
        app.dependency_overrides[v2_funding_reports.owner_access]=lambda:Actor(1,'owner',Role.ADMIN,None,True)
        page=client.get('/v2/funding-accounts/20')
        assert page.status_code==200,page.text
        assert 'Credit Card 2' in page.text and 'UNKNOWN' in page.text
        assert 'name="payment_vendor_id" required' not in page.text
        assert 'action="/v2/funding-accounts/20/payments"' in page.text


def test_pool_report_cannot_be_turned_into_a_card_obligation_by_payment_allocation(report_db):
    with Session(report_db) as db:
        unknown_balances(db);r=report(db);db.commit();funding.finalize_report(db,report_id=r.id,actor_id=1);db.commit()
        with pytest.raises(ValueError,match='informational'):
            funding.record_payment(db,account_id=10,entry_type='PAYMENT',amount=Decimal('10'),payment_date=NOW.date(),
                payment_source='Owner',confirmation_number='',reason='Owner payment',internal_note='',allocations={r.id:Decimal('10')},actor_id=1,idempotency_key='pool-cannot-pay')
        assert db.scalar(select(func.count()).select_from(FundingPayment))==0


def test_unknown_issuer_authority_stays_unknown_if_writers_are_later_paused(monday_db,monkeypatch):
    from app.config import settings
    from app.services.card_balance_service import uses_actual_authority
    with Session(monday_db) as db:
        unknown_balances(db);save(db,rec(db),values={'big':'0','streamline':'0','card1':'100','card2':'0'});db.commit()
        monkeypatch.setattr(settings,'purchasing_v3_phase1_enabled',False)
        assert uses_actual_authority(db,account_id=10)
        assert actual_card_balance(db,account_id=10) is None
        assert funding.tracked_balance(db,account_id=10) is None


def test_optional_issuer_advice_change_never_invalidates_or_reallocates_owner_payment(monday_db):
    with Session(monday_db) as db:
        unknown_balances(db);seen=rec(db)
        adjust_balance(db,account_id=10,balance=1000,actor_id=1,idempotency_key='optional-after-draft-1')
        adjust_balance(db,account_id=20,balance=2000,actor_id=1,idempotency_key='optional-after-draft-2');db.commit()
        assert rec(db)['token']==seen['token']
        actual={'big':'0','streamline':'0','card1':'500','card2':'0'}
        b=save(db,seen,values=actual);db.commit()
        assert [e['actual'] for e in monday.batch_view(db,b)['entries']]==['0.00','0.00','500.00','0.00']


def test_concurrent_financial_source_claim_still_requires_stale_payment_review(report_db):
    with Session(report_db) as db:
        seen=rec(db)
        claimed=report(db,account=11);db.commit();funding.finalize_report(db,report_id=claimed.id,actor_id=1);db.commit()
        assert rec(db)['token']!=seen['token']
        with pytest.raises(monday.Conflict,match='changed'):
            save(db,seen,values={'big':'50','streamline':'80','card1':'30','card2':'60'})
        assert db.scalar(select(func.count()).select_from(PurchasingPaymentBatch))==0
