"""Destructive operations are restricted to a uniquely created local disposable DB."""
import hashlib
import json
import os
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
import pytest
from alembic import command
from sqlalchemy import create_engine, text, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from app.schema_contract import _alembic_config
from app.models import (Principal, PaymentMethod, FundingAccount, FundingLedgerEntry, Vendor, Store,
    PurchaseOrder, PurchaseOrderLine, PurchaseOrderStoreAllocation, SquareSyncEvent, AuditLog,
    FundingReport, FundingPayment, FundingPaymentAllocation, PurchaseOrderReceipt, PurchaseOrderReceiptLine)

TABLES=('principals','vendors','stores','payment_methods','funding_accounts','funding_ledger_entries',
        'purchase_orders','purchase_order_lines','purchase_order_store_allocations','square_sync_events',
        'audit_log','funding_reports','funding_payments','funding_payment_allocations',
        'purchase_order_receipts','purchase_order_receipt_lines')


def fingerprint(engine):
    data={}
    with engine.connect() as con:
        for table in TABLES:
            rows=con.execute(text(f"SELECT (to_jsonb(t) - 'event_kind' - 'request_key' - 'request_payload')::text FROM {table} t ORDER BY id" if table == 'purchase_order_receipts' else f'SELECT row_to_json(t)::text FROM {table} t ORDER BY id')).scalars().all()
            data[table]=hashlib.sha256(json.dumps(rows).encode()).hexdigest()
    return data


@pytest.fixture(autouse=True)
def historical_schema_sessions():
    # This test intentionally writes schema 37 before upgrading it. The current
    # application's ORM invalidation hooks require the current startup contract;
    # they must not run while constructing historical migration input.
    from app.services import v2_scheduling_freshness_service as freshness
    callbacks = [('before_flush',freshness._collect),
                 ('after_flush_postexec',freshness._revalidate),
                 ('after_soft_rollback',freshness._rollback)]
    installed=[(name,fn) for name,fn in callbacks if event.contains(Session,name,fn)]
    for name,fn in installed:event.remove(Session,name,fn)
    try:yield
    finally:
        for name,fn in installed:event.listen(Session,name,fn)


def test_additive_migration_preserves_history_and_refuses_evidence_loss():
    url=os.getenv('P1_TEST_DATABASE_URL')
    if not url: pytest.skip('Requires explicit disposable local PostgreSQL')
    parsed=make_url(url)
    assert parsed.database.startswith('erupted_purchasing_p1_') and parsed.host in {None,'localhost','127.0.0.1'}
    name='erupted_purchasing_p1_'+uuid.uuid4().hex
    admin=create_engine(parsed.set(database='postgres'), isolation_level='AUTOCOMMIT')
    with admin.connect() as con: con.execute(text(f'CREATE DATABASE "{name}"'))
    target=parsed.set(database=name).render_as_string(hide_password=False)
    engine=create_engine(target)
    try:
        config=_alembic_config(target)
        command.upgrade(config,'20261016_0037')
        with Session(engine) as db:
            db.add(Principal(id=6001,username='preservation',password_hash='synthetic',role='ADMIN',active=True));db.flush()
            db.add_all([Vendor(id=6001,square_vendor_id='V1',name='Vendor',active=True),Store(id=6001,name='Synthetic',square_location_id='L1',active=True),
                PaymentMethod(id=6001,display_name='Synthetic',category='CREDIT_CARD',is_active=True,created_by_principal_id=6001,updated_by_principal_id=6001)]);db.flush()
            db.add(FundingAccount(id=6001,account_type='CREDIT_CARD',payment_method_id=6001,display_name='Card',created_by_principal_id=6001,updated_by_principal_id=6001))
            db.add(PurchaseOrder(id=6001,vendor_id=6001,status='IN_TRANSIT',created_by_principal_id=6001));db.flush()
            db.add(PurchaseOrderLine(id=6001,purchase_order_id=6001,variation_id='VAR1',sku='SKU1',item_name='Product',variation_name='Blue',unit_cost=Decimal('3'),ordered_qty=20,received_qty_total=8));db.flush()
            db.add(PurchaseOrderStoreAllocation(id=6001,purchase_order_line_id=6001,store_id=6001,allocated_qty=20,store_received_qty=8))
            db.add(SquareSyncEvent(id=6001,purchase_order_id=6001,purchase_order_line_id=6001,store_id=6001,sync_type='PURCHASE_ORDER_RECEIVE_ADD_STOCK',idempotency_key='preserved',status='SUCCESS',request_payload={'received_qty_singles':'8'},response_payload={'counts':[]}))
            db.add(FundingLedgerEntry(id=6001,account_id=6001,entry_type='INVENTORY_PURCHASE',direction='INCREASE',amount=60,effective_date=date(2026,10,1),reason='Preserve original assignment',created_by_principal_id=6001))
            db.add(FundingReport(id=6001,account_id=6001,vendor_id=6001,report_number='LEGACY',account_name_snapshot='Card',account_type_snapshot='CREDIT_CARD',sales_start_date=date(2026,10,1),sales_end_date=date(2026,10,7),status='FINALIZED',finalized_snapshot={'legacy':True},created_by_principal_id=6001))
            db.add(FundingPayment(id=6001,account_id=6001,vendor_id=6001,entry_type='PAYMENT',amount=5,payment_date=date(2026,10,8),reason='Historical payment',created_by_principal_id=6001));db.flush()
            db.add(FundingPaymentAllocation(id=6001,payment_id=6001,report_id=6001,amount=5,created_by_principal_id=6001))
            db.add(AuditLog(id=6001,actor_principal_id=6001,action='PRESERVE_ME',meta={'history':True}))
            db.execute(text("INSERT INTO purchase_order_receipts (id,purchase_order_id,received_by_principal_id,status,is_partial) VALUES (6001,6001,6001,'SUBMITTED',true)"));db.flush()
            db.add(PurchaseOrderReceiptLine(id=6001,receipt_id=6001,purchase_order_line_id=6001,expected_qty=20,received_qty=8,difference_qty=-12));db.commit()
        before=fingerprint(engine)
        command.upgrade(config,'20261017_0032')
        assert fingerprint(engine)==before
        command.upgrade(config,'head')
        assert fingerprint(engine)==before
        # Empty additive schema can downgrade/re-upgrade, without changing old rows.
        command.downgrade(config,'20261016_0037'); command.upgrade(config,'head')
        assert fingerprint(engine)==before
        with engine.begin() as con:
            con.execute(text("INSERT INTO purchasing_evidence_runs(source,status,started_at,completed_at,scope,evidence) VALUES ('SALES','COMPLETE',now(),now(),'{}','{}')"))
        with pytest.raises(RuntimeError,match='Refusing to discard'):
            command.downgrade(config,'20261016_0037')
        assert fingerprint(engine)==before
        from alembic.autogenerate import compare_metadata
        from alembic.migration import MigrationContext
        from app.models import Base
        with engine.connect() as con:
            names={'purchasing_evidence_runs','purchasing_product_authority','funding_balance_checkpoints','funding_actual_activity','purchase_order_receipts','purchase_order_receipt_allocations'}
            def include_object(obj,name,type_,reflected,compare_to):
                table=obj if type_=='table' else getattr(obj,'table',None)
                return table is not None and table.name in names
            differences=compare_metadata(MigrationContext.configure(con, opts={'include_object':include_object}),Base.metadata)
            assert not differences, differences
    finally:
        engine.dispose()
        with admin.connect() as con: con.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()
