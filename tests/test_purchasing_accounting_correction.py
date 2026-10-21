"""PO-specific financing authority; disposable PostgreSQL, real existing services."""
import copy
import json
import os
from datetime import datetime, timezone, date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select,func,text
from sqlalchemy.orm import Session
from test_purchasing_phase2 import engine,receiving_db,database_template,Gateway
from test_purchasing_predeployment import REAL_INITIALIZER,isolate_pdf
from app.models import (Vendor,PaymentMethod,VendorPaymentClassification,FundingAccount,
    PurchaseOrder,PurchaseOrderLine,OrderPayment,OrderPaymentEvent,ConsignmentReplenishment,
    FundingActualActivity,FundingLedgerEntry,PurchasingSessionLine,PurchasingSessionGroup,
    FundingReport,FundingPayment,AuditLog)
from app.services import v2_order_payments_service as payments
from app.services import purchase_order_admin_service as receiving
from app.services import purchasing_session_service as purchasing


@pytest.fixture
def financing(engine,monkeypatch,tmp_path):
    monkeypatch.setattr(payments,'initialize_new_order_if_configured',REAL_INITIALIZER)
    monkeypatch.setattr(receiving,'_square_post',lambda *a,**kw:{})
    isolate_pdf(monkeypatch,tmp_path)
    with Session(engine) as db:
        db.add_all([Vendor(id=3,name='Streamline',square_vendor_id='STREAM',active=True),
            Vendor(id=4,name='Other Card Supplier',square_vendor_id='OTHER',active=True)])
        db.add_all([PaymentMethod(id=i,display_name=name,category=category,is_active=True,
            created_by_principal_id=1,updated_by_principal_id=1) for i,name,category in (
                (11,'Big consignment','CONSIGNMENT'),(12,'Streamline consignment','CONSIGNMENT'),
                (13,'Explicit bank','WIRE'),(20,'Card Two','CREDIT_CARD'))]);db.flush()
        for vendor,method,category in ((1,11,'CONSIGNMENT'),(2,10,'CREDIT_CARD'),(3,12,'CONSIGNMENT'),(4,20,'CREDIT_CARD')):
            db.add(VendorPaymentClassification(vendor_id=vendor,payment_method_id=method,payment_category=category,
                payment_method_label_snapshot=category,is_consignment=category=='CONSIGNMENT',
                effective_date=payments.portal_today(),is_current=True,created_by_principal_id=1))
        db.add_all([FundingAccount(id=i,account_type='CONSIGNMENT',vendor_id=vendor,display_name=name,
            created_by_principal_id=1,updated_by_principal_id=1) for i,vendor,name in ((11,1,'Big'),(12,3,'Streamline'))])
        db.add(FundingAccount(id=20,account_type='CREDIT_CARD',payment_method_id=20,display_name='Card Two',
            created_by_principal_id=1,updated_by_principal_id=1))
        po=db.get(PurchaseOrder,1);po.vendor_id=1;po.status='IN_TRANSIT';po.ordered_at=datetime.now(timezone.utc)
        db.get(PurchaseOrderLine,1).unit_cost=Decimal(3)
        db.commit()
    yield engine


def receive(db,poid,lineid,qty,key):
    return receiving.submit_receiving_shipment(db,purchase_order_id=poid,actor_principal_id=1,
        received_qty_by_line_store={(lineid,1):qty},request_key=key)


@pytest.mark.parametrize('vendor,method,changed',[(1,11,False),(1,11,True),(3,12,True)])
def test_existing_consignment_lot_preserves_assignment_and_syncs_receipts(financing,vendor,method,changed):
    with Session(financing) as db:
        po=db.get(PurchaseOrder,1);po.vendor_id=vendor;db.commit()
        receive(db,1,1,8,'original-receipt')
        payment=db.scalar(select(OrderPayment).where(OrderPayment.purchase_order_id==1))
        assert (payment.vendor_id,payment.payment_method_id)==(vendor,method)
        event_ids=list(db.scalars(select(OrderPaymentEvent.id)))
        if changed:
            # Simulate supplier metadata changes at the integration boundary.
            # This is not a financial reassignment operation.
            for supplier in (2,4,1,2):
                po.vendor_id=supplier;db.commit()
                assert REAL_INITIALIZER(db,order=po,actor_id=1).id==payment.id;db.commit()
                assert (payment.vendor_id,payment.payment_method_id)==(vendor,method)
                assert list(db.scalars(select(OrderPaymentEvent.id)))==event_ids
        receive(db,1,1,5,'later-receipt')
        replenishment=db.scalar(select(ConsignmentReplenishment).where(ConsignmentReplenishment.purchase_order_id==1))
        assert replenishment.vendor_id==vendor and replenishment.received_cost_value==Decimal(39)
        assert db.scalar(select(func.count()).select_from(OrderPayment))==1
        assert db.scalar(select(func.count()).select_from(FundingActualActivity))==0
        assert db.scalar(select(func.count()).select_from(FundingLedgerEntry))==0


def create_new_purchase(db,account,supplier=2):
    gateway=Gateway();gateway.units=Decimal(10)
    session=purchasing.generate(db,actor_id=1,request_key=f'new-{account}',gateway=gateway)
    line=db.scalar(select(PurchasingSessionLine).where(PurchasingSessionLine.session_id==session.id))
    purchasing.update_line(db,session_id=session.id,line_id=line.id,actor_id=1,quantity=100,supplier_id=supplier,acknowledge=True)
    if account is not None:purchasing.set_account(db,session_id=session.id,supplier_id=supplier,actor_id=1,account_id=account)
    po=purchasing.create_po(db,session_id=session.id,supplier_id=supplier,actor_id=1);db.commit()
    receiving.submit_purchase_order(db,purchase_order_id=po.id,actor_principal_id=1);db.commit()
    pol=db.scalar(select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id==po.id))
    return po,pol


@pytest.mark.parametrize('account,vendor,method,category',[(11,1,11,'CONSIGNMENT'),(12,3,12,'CONSIGNMENT'),
    (10,2,10,'CREDIT_CARD'),(20,2,20,'CREDIT_CARD'),(None,2,10,'CREDIT_CARD')])
def test_new_purchase_uses_its_financing_not_an_old_consigned_sku(financing,account,vendor,method,category):
    with Session(financing) as db:
        receive(db,1,1,8,'historical-big')
        # New purchases must not mutate a prior lot's financial evidence.
        before={table:db.execute(text(f"SELECT coalesce(json_agg(row_to_json(t))::text,'[]') FROM {table} t")).scalar_one()
            for table in ('order_payments','order_payment_events','consignment_replenishments','consignment_replenishment_receipts','consignment_ledger_entries')}
        po,line=create_new_purchase(db,account)
        if category=='CONSIGNMENT':assert db.scalar(select(OrderPayment).where(OrderPayment.purchase_order_id==po.id)) is None
        receive(db,po.id,line.id,90,'new-lot-receipt')
        payment=db.scalar(select(OrderPayment).where(OrderPayment.purchase_order_id==po.id))
        assert (payment.vendor_id,payment.payment_method_id,payment.payment_category_snapshot)==(vendor,method,category)
        assert po.vendor_id==2 and line.ordered_qty==100
        from app.services.v2_funding_reports_service import funding_account_purchase_lines
        expected_account=account or 10
        for candidate in (10,11,12,20):
            assigned=funding_account_purchase_lines(db,account=db.get(FundingAccount,candidate))
            assert (line.id in {item['line'].id for item in assigned})==(candidate==expected_account)
        group=db.scalar(select(PurchasingSessionGroup).where(PurchasingSessionGroup.purchase_order_id==po.id))
        assert group.intended_account_id==account
        # Compare old rows by their saved primary IDs, allowing append-only new evidence.
        for table,old_json in before.items():
            for old in json.loads(old_json):
                actual=json.loads(db.execute(text(f'SELECT row_to_json(t)::text FROM {table} t WHERE id=:id'),{'id':old['id']}).scalar_one())
                assert actual==old,(table,old['id'])
        if account:
            audit=db.scalar(select(AuditLog).where(AuditLog.action=='ORDER_FINANCING_INITIALIZED'))
            assert audit and audit.meta['after']['intended_account_id']==account
        assert db.scalar(select(func.count()).select_from(FundingActualActivity))==0
        assert db.scalar(select(func.count()).select_from(FundingLedgerEntry))==0


def test_explicit_bank_assignment_is_not_replaced_by_supplier_or_card_defaults(financing):
    with Session(financing) as db:
        po=db.get(PurchaseOrder,1);po.vendor_id=2;db.commit()
        payment=payments.save_order_financial_assignment(db,order_id=1,financial_vendor_id=2,
            payment_method_id=13,due_date=None,actor_id=1);db.commit()
        assert payment.payment_category_snapshot=='WIRE'
        before=list(db.scalars(select(OrderPaymentEvent.id)))
        for supplier in (1,3,4,2):
            po.vendor_id=supplier;db.commit()
            REAL_INITIALIZER(db,order=po,actor_id=1);db.commit()
            assert (payment.vendor_id,payment.payment_method_id,payment.payment_category_snapshot)==(2,13,'WIRE')
        assert list(db.scalars(select(OrderPaymentEvent.id)))==before
        assert db.scalar(select(func.count()).select_from(FundingActualActivity))==0


def test_missing_consignment_evidence_is_reported_not_reclassified(financing):
    with Session(financing) as db:
        po=db.get(PurchaseOrder,1);po.vendor_id=2;db.commit()
        # An inconsistent legacy assignment must not turn into a card purchase.
        db.add(OrderPayment(purchase_order_id=1,vendor_id=1,payment_method_id=11,payment_category_snapshot='CONSIGNMENT',
            status='CONSIGNMENT_ORDERED',financial_treatment='REPLENISHMENT',order_amount=240,order_cost_complete=True));db.commit()
        with pytest.raises(ValueError,match='owner review'):REAL_INITIALIZER(db,order=po,actor_id=1)
        db.rollback()
        assert db.scalar(select(OrderPayment)).vendor_id==1


@pytest.mark.parametrize('account,invalid',[(11,'inactive'),(11,'classification'),(20,'method')])
def test_invalid_intended_financing_fails_closed_before_order_placement(financing,account,invalid):
    with Session(financing) as db:
        if invalid=='inactive':db.get(FundingAccount,account).is_active=False
        elif invalid=='classification':db.scalar(select(VendorPaymentClassification).where(VendorPaymentClassification.vendor_id==1)).is_consignment=False
        else:db.get(PaymentMethod,20).is_active=False
        db.commit()
        with pytest.raises(ValueError):create_new_purchase(db,account)
        db.rollback()
        assert db.scalar(select(func.count()).select_from(OrderPayment))==0


def test_later_owner_assignment_outranks_original_intended_account(financing):
    with Session(financing) as db:
        po,_=create_new_purchase(db,10)
        payment=db.scalar(select(OrderPayment).where(OrderPayment.purchase_order_id==po.id))
        assert payment.payment_method_id==10
        payments.save_order_financial_assignment(db,order_id=po.id,financial_vendor_id=2,
            payment_method_id=20,due_date=None,actor_id=1);db.commit()
        events=db.scalar(select(func.count()).select_from(OrderPaymentEvent))
        assert events>=2
        REAL_INITIALIZER(db,order=po,actor_id=1);db.commit();db.refresh(payment)
        assert payment.payment_method_id==20
        assert db.scalar(select(func.count()).select_from(OrderPaymentEvent))==events
        assert db.scalar(select(func.count()).select_from(FundingActualActivity))==0


def test_four_accounts_preserve_sunday_saturday_reports_and_edited_monday_payments(financing,monkeypatch,tmp_path):
    """Existing per-account payment backend, not an invented combined Save Payments UI."""
    from app.services import v2_funding_reports_service as reports
    from app.services.card_balance_service import adjust_balance,actual_card_balance
    monday=date(2026,10,19)
    with Session(financing) as db:
        for account in (10,20):create_new_purchase(db,account)
        monkeypatch.setattr(payments,'portal_today',lambda *a,**kw:monday)
        for account,balance in ((10,1000),(20,2000)):
            adjust_balance(db,account_id=account,balance=balance,actor_id=1,idempotency_key=f'checkpoint-{account}')
        db.commit()
        saved=[]
        for account,vendor,owed,actual in ((11,1,100,10),(12,3,200,20),(10,2,300,25),(20,2,400,35)):
            account_row=db.get(FundingAccount,account)
            report=FundingReport(account_id=account,vendor_id=vendor,report_number=f'weekly-{account}',
                account_name_snapshot=account_row.display_name,account_type_snapshot=account_row.account_type,
                sales_start_date=date(2026,10,11),sales_end_date=date(2026,10,17),status='FINALIZED',
                calculated_cogs=owed,finalized_snapshot={'synthetic':True},created_by_principal_id=1)
            db.add(report);db.commit()
            assert report.sales_start_date.weekday()==6 and report.sales_end_date.weekday()==5 and monday.weekday()==0
            payment=reports.record_compact_payment(db,account_id=account,report_id=report.id,
                payment_date=monday,amount=Decimal(actual),paid_in_full=False,actor_id=1,idempotency_key=f'payment-{account}')
            db.commit()
            assert (payment.account_id,payment.amount,payment.payment_date)==(account,Decimal(actual),monday)
            assert reports.report_position(db,report_id=report.id)['remaining_amount']==owed-actual
            saved.append({'account_id':account,'report_amount':owed,'actual_payment':actual,'remaining':owed-actual})
        assert actual_card_balance(db,account_id=10)==975 and actual_card_balance(db,account_id=20)==1965
        assert db.scalar(select(func.count()).select_from(FundingPayment))==4
        assert db.scalar(select(func.count()).select_from(FundingActualActivity))==2  # Repayments only.
        evidence=Path(os.getenv('ACCOUNTING_EVIDENCE_DIR',str(tmp_path)));evidence.mkdir(exist_ok=True,parents=True)
        (evidence/'four-account-payment-verification.json').write_text(json.dumps(saved,indent=2))


def test_supplier_moves_cannot_drop_or_replace_an_explicit_draft_financing_decision(financing):
    with Session(financing) as db:
        session=purchasing.generate(db,actor_id=1,request_key='move-financing',gateway=Gateway())
        line=db.scalar(select(PurchasingSessionLine).where(PurchasingSessionLine.session_id==session.id))
        purchasing.set_account(db,session_id=session.id,supplier_id=1,actor_id=1,account_id=11);db.commit()
        for supplier in (2,1,2):
            purchasing.update_line(db,session_id=session.id,line_id=line.id,actor_id=1,quantity=100,supplier_id=supplier,acknowledge=True);db.commit()
            assert line.decisions['intended_account_id']==11
        purchasing.refresh(db,session_id=session.id,actor_id=1,request_key='move-refresh',gateway=Gateway());db.commit()
        assert line.decisions['intended_account_id']==11
        purchasing.update_line(db,session_id=session.id,line_id=line.id,actor_id=1,acknowledge=True);db.commit()
        with pytest.raises(ValueError,match='intended financing account'):
            purchasing.create_po(db,session_id=session.id,supplier_id=2,actor_id=1)
        db.rollback()
        # A deliberate group-account choice is the existing audited override.
        purchasing.set_account(db,session_id=session.id,supplier_id=2,actor_id=1,account_id=20);db.commit()
        assert line.decisions['intended_account_id']==20
        po=purchasing.create_po(db,session_id=session.id,supplier_id=2,actor_id=1);db.commit()
        receiving.submit_purchase_order(db,purchase_order_id=po.id,actor_principal_id=1);db.commit()
        payment=db.scalar(select(OrderPayment).where(OrderPayment.purchase_order_id==po.id))
        assert po.vendor_id==2 and payment.payment_method_id==20
        assert db.scalar(select(func.count()).select_from(FundingActualActivity))==0
