"""Prospective account configuration, immutable history, balance-only card split."""
import copy
from uuid import uuid4
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import pytest
from sqlalchemy import select,func,text
from sqlalchemy.orm import Session
from test_purchasing_monday_payments import monday_db,engine,receiving_db,database_template,rec,save,actuals
from test_purchasing_phase2 import Gateway
from test_purchasing_ui import app_client
from app.models import (FundingAccount,PaymentMethod,VendorPaymentSetting,VendorPaymentClassification,
    FundingPayment,FundingActualActivity,PurchaseOrder,PurchasingSessionGroup,PurchasingSessionLine,
    PurchasingOwnerDecision,OrderPayment,Principal,AuditLog,ConsignmentSaleFact,PurchaseOrderLine)
from app.services import v2_order_payments_service as finance,purchasing_payments_service as monday,purchasing_session_service as purchasing
from app.services.purchasing_attention_service import Conflict
from app.services.card_balance_service import adjust_balance,actual_card_balance


def payload(db,operation,**values):
    return dict(operation=operation,token=finance.payment_configuration(db)['token'],request_key=uuid4().hex,**values)
def change(db,operation,**values):
    p=payload(db,operation,**values);finance.configure_payment_accounts(db,actor_id=1,payload=p);db.commit();return p


def test_roles_follow_actual_account_balances_and_saved_history_is_frozen(monday_db):
    with Session(monday_db) as db:
        original=rec(db); batch=save(db,original,key='historical');db.commit()
        frozen=copy.deepcopy(batch.recommendations)
        change(db,'account',account_id=10,name='Chase Visa',last_four='1234')
        change(db,'roles',card1=20,card2=10)
        fresh=rec(db)
        assert [r['account_id'] for r in fresh['rows'][2:]]==[20,10]
        assert [r['name'] for r in fresh['rows'][2:]]==['Card Two','Chase Visa']
        assert [r['recommended'] for r in fresh['rows'][2:]]==['60.00','30.00']
        db.refresh(batch);assert batch.recommendations==frozen
        assert save(db,original,key='historical').id==batch.id
        assert db.scalar(select(func.count()).select_from(FundingPayment))==4
        assert actual_card_balance(db,account_id=10)==970


def test_vendor_moves_change_only_new_defaults_not_monday_or_financing_history(monday_db):
    with Session(monday_db) as db:
        before=rec(db)
        classifications=list(db.scalars(select(VendorPaymentClassification.id)))
        change(db,'vendor',vendor_id=4,account_id=10)
        assert finance.prospective_vendor_account(db,4).id==10
        change(db,'vendor',vendor_id=4,account_id=20)
        assert finance.prospective_vendor_account(db,4).id==20
        assert rec(db)==before
        assert list(db.scalars(select(VendorPaymentClassification.id)))==classifications
        assert db.scalar(select(func.count()).select_from(FundingActualActivity))==0
        assert db.scalar(select(func.count()).select_from(FundingPayment))==0


def test_new_group_uses_default_preserves_draft_and_explicit_sku_beats_move(monday_db):
    with Session(monday_db) as db:
        change(db,'vendor',vendor_id=1,account_id=10)
        first=purchasing.generate(db,actor_id=1,request_key='before-move',gateway=Gateway());db.commit()
        group=db.scalar(select(PurchasingSessionGroup).where(PurchasingSessionGroup.session_id==first.id))
        assert group.intended_account_id==10
        change(db,'vendor',vendor_id=1,account_id=20)
        purchasing.refresh(db,session_id=first.id,actor_id=1,request_key='refresh-after-move',gateway=Gateway());db.commit()
        db.refresh(group);assert group.intended_account_id==10
        second=purchasing.generate(db,actor_id=1,request_key='after-move',gateway=Gateway());db.commit()
        assert db.scalar(select(PurchasingSessionGroup).where(PurchasingSessionGroup.session_id==second.id)).intended_account_id==20
        db.add(PurchasingOwnerDecision(kind='FINANCING',subject_key='VAR1',value={'account_id':10,'vendor_ids':['V1']},version=1,actor_id=1,
            updated_at=datetime.now(timezone.utc)-timedelta(days=1)));db.commit()
        third=purchasing.generate(db,actor_id=1,request_key='explicit-sku',gateway=Gateway());db.commit()
        line=db.scalar(select(PurchasingSessionLine).where(PurchasingSessionLine.session_id==third.id,PurchasingSessionLine.variation_id=='VAR1'))
        assert line.decisions['intended_account_id']==10


def test_create_masked_account_is_atomic_idempotent_and_unavailable_until_balance(monday_db):
    with Session(monday_db) as db:
        for table in ('payment_methods','funding_accounts'):
            db.execute(text(f"SELECT setval(pg_get_serial_sequence('{table}','id'),100,true)"))
        p=change(db,'create',name='Second Owner Card',last_four='4321')
        a=db.scalar(select(FundingAccount).where(FundingAccount.display_name=='Second Owner Card'))
        assert db.get(PaymentMethod,a.payment_method_id).last_four=='4321'
        finance.configure_payment_accounts(db,actor_id=1,payload=p);db.commit()
        assert db.scalar(select(func.count()).select_from(FundingAccount).where(FundingAccount.display_name=='Second Owner Card'))==1
        change(db,'roles',card1=10,card2=a.id)
        assert rec(db)['rows'][3]['recommended'] is None
        assert actual_card_balance(db,account_id=a.id) is None
        assert db.scalar(select(func.count()).select_from(FundingPayment))==0
        with pytest.raises(ValueError):change(db,'create',name='Bad Card',last_four='1234567890123456')
        db.rollback()
        assert db.scalar(select(func.count()).select_from(PaymentMethod).where(PaymentMethod.display_name=='Bad Card'))==0


@pytest.mark.parametrize('one,two,pool,expected',[
    ('1','1','0.01',('0.01','0.00')),('1','2','90',('30.00','60.00')),
    ('-50','200','90',('0.00','90.00')),('100','0','90',('90.00','0.00')),
    ('0','0','90',(None,None)),('0','0','0',(None,None))])
def test_card_split_uses_positive_actual_balances_and_exact_pennies(monday_db,one,two,pool,expected):
    with Session(monday_db) as db:
        adjust_balance(db,account_id=10,balance=Decimal(one),actor_id=1,idempotency_key='new-one')
        adjust_balance(db,account_id=20,balance=Decimal(two),actor_id=1,idempotency_key='new-two')
        fact=db.scalar(select(ConsignmentSaleFact).where(ConsignmentSaleFact.square_variation_id=='ORDINARY'))
        fact.quantity_sold=1;fact.unit_cost_snapshot=Decimal(pool);fact.extended_cogs_snapshot=Decimal(pool);db.commit()
        r=rec(db);assert tuple(row['recommended'] for row in r['rows'][2:])==expected
        if expected[0] is not None:assert sum(Decimal(v) for v in expected)==Decimal(r['gross_card_pool'])
        else:assert all(row['unavailable'] for row in r['rows'][2:])


def test_concurrent_configuration_cannot_lose_edits(monday_db):
    with Session(monday_db) as db:
        p1=payload(db,'roles',card1=20,card2=10);p2=payload(db,'account',account_id=10,name='Other Name',last_four='')
    def write(p):
        with Session(monday_db) as db:
            try:finance.configure_payment_accounts(db,actor_id=1,payload=p);db.commit();return 'saved'
            except Conflict:db.rollback();return 'conflict'
    with ThreadPoolExecutor(2) as pool:assert sorted(pool.map(write,[p1,p2]))==['conflict','saved']


def test_admin_config_ui_csrf_and_stale_review(monday_db):
    from app.routers import v2_order_payments as routes
    from app.auth import Principal as Actor,Role
    with Session(monday_db) as db:
        client,app=app_client(db);app.include_router(routes.router);client.headers['x-forwarded-for']='127.0.0.1'
        app.dependency_overrides[routes.feature_access]=lambda:Actor(1,'owner',Role.ADMIN,None,True)
        app.dependency_overrides[routes.owner_access]=lambda:Actor(1,'owner',Role.ADMIN,None,True)
        page=client.get('/v2/payment-methods');assert page.status_code==200,page.text
        assert 'Save card roles' in page.text and 'Vendor purchasing defaults' in page.text
        p=payload(db,'roles',card1=20,card2=10)
        assert client.post('/v2/payment-methods/configuration',data=p).status_code==403
        p['csrf_token']='test'
        response=client.post('/v2/payment-methods/configuration',data=p);assert response.status_code==303,response.text
        stale=payload(db,'account',account_id=10,name='Stale',last_four='')
        change(db,'account',account_id=20,name='Fresh',last_four='')
        assert client.post('/v2/payment-methods/configuration',data={**stale,'csrf_token':'test'}).status_code==409
        assert db.get(FundingAccount,10).display_name!='Stale'
        app.dependency_overrides[routes.owner_access]=lambda:Actor(1,'manager',Role.MANAGER,None,True)
        assert client.post('/v2/payment-methods/configuration',data=p).status_code==403
        # Service boundary also checks the persisted principal, not a caller claim.
        db.get(Principal,1).role='MANAGER';db.commit()
        with pytest.raises(PermissionError):finance.configure_payment_accounts(db,actor_id=1,payload=payload(db,'roles',card1=10,card2=20))


def test_new_default_affects_future_purchase_but_existing_lot_is_untouched(monday_db):
    with Session(monday_db) as db:
        # Existing stock is financed by card 1; a future vendor default moves to card 2.
        db.add(OrderPayment(purchase_order_id=1,vendor_id=1,payment_method_id=10,
            payment_category_snapshot='CREDIT_CARD',financial_treatment='INVOICE',status='UNPAID'))
        old_line=db.scalar(select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id==1));old_line.received_qty_total=5
        db.commit()
        before=db.execute(text('SELECT row_to_json(p) FROM order_payments p')).scalars().all()
        change(db,'vendor',vendor_id=1,account_id=20)
        session=purchasing.generate(db,actor_id=1,request_key='existing-lot',gateway=Gateway());db.commit()
        line=db.scalar(select(PurchasingSessionLine).where(PurchasingSessionLine.session_id==session.id,PurchasingSessionLine.variation_id==old_line.variation_id))
        assert line.decisions['intended_account_id']==20
        assert db.execute(text('SELECT row_to_json(p) FROM order_payments p')).scalars().all()==before


from test_purchasing_consignment_inventory_value import inventory,add_lot


def test_vendor_defaults_cannot_reclassify_existing_consignment_inventory(inventory):
    engine,gateway=inventory
    with Session(engine) as db:
        add_lot(db,poid=25)
        db.commit()
        observed_at=monday.now()
        before=monday.consignment_inventory_values(db,gateway=gateway,as_of=observed_at)
        historic=db.execute(text('SELECT row_to_json(p) FROM order_payments p ORDER BY id')).scalars().all()
        change(db,'vendor',vendor_id=1,account_id=20)
        change(db,'vendor',vendor_id=3,account_id=10)
        assert monday.consignment_inventory_values(db,gateway=gateway,as_of=observed_at)==before
        assert db.execute(text('SELECT row_to_json(p) FROM order_payments p ORDER BY id')).scalars().all()==historic


def test_role_change_invalidates_old_monday_save_token(monday_db):
    with Session(monday_db) as db:
        old=rec(db)
        change(db,'roles',card1=20,card2=10)
        with pytest.raises(monday.Conflict):save(db,old,key='stale-accounts')
        db.rollback()
        assert db.scalar(select(func.count()).select_from(FundingPayment))==0


def test_missing_and_duplicate_roles_are_explicit(monday_db):
    with Session(monday_db) as db:
        with pytest.raises(ValueError,match='distinct'):change(db,'roles',card1=10,card2=10)
        db.rollback()
        change(db,'roles',card1=10,card2='')
        assert finance.payment_configuration(db)['roles']['card2'] is None
        row=rec(db)['rows'][3]
        assert row['account_id'] is None and 'Needs Attention' in row['name'] and row['recommended'] is None
