"""Production-compatible recovery preserves forward-version commitment evidence."""
import copy
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from decimal import Decimal

import pytest
from sqlalchemy import select, func, text
from sqlalchemy.orm import Session
from test_purchasing_phase2 import engine, receiving_db, database_template, Gateway
from test_purchasing_ui import app_client
from app.models import (PurchasingSessionLine, PurchasingSessionGroup, PurchaseOrder,
    PurchaseOrderLine, PurchaseOrderStoreAllocation, VendorSkuConfig)
from app.services import purchasing_session_service as service, purchasing_draft_service as drafts


def seed(db):
    session=service.generate(db,actor_id=1,request_key='recovery-acceptance',gateway=Gateway())
    original=db.scalar(select(PurchasingSessionLine).where(PurchasingSessionLine.session_id==session.id))
    product=copy.deepcopy(original.accepted);rows=[]
    for i in range(6):
        sid=1+i%2;category=('Disposable','Freebase','Salt')[i//2];vid=f'RECOVERY-{i}'
        accepted={**copy.deepcopy(product),'variation_id':vid,'sku':vid,'item_name':category+str(i),
            'inventory_item_type':{'id':category,'name':category,'issue':None},'review':[],'unit_cost':'3'}
        row=original if i==0 else PurchasingSessionLine(session_id=session.id,variation_id=vid,
            canonical_vendor_id=sid,supplier_id=sid,quantity=10,allocations={},accepted=accepted,decisions={})
        if i:db.add(row)
        row.variation_id=vid;row.supplier_id=sid;row.canonical_vendor_id=sid;row.accepted=accepted
        row.quantity=10;row.allocations={'1':1,'2':2,'3':3,'4':4};row.unit_cost=Decimal(3);row.cost_source='CONFIGURED';row.decisions={};row.proposed=None
        db.add(VendorSkuConfig(vendor_id=sid,sku=vid,square_variation_id=vid,unit_cost=3,
            pack_size=1,min_order_qty=0,active=True,is_default_vendor=True))
        service._group(db,session,sid);rows.append(row)
    db.commit();return session,rows


def forward_commit(db,session,rows):
    """Persist the PR #13 wire/data contract; no recovery writer shortcuts."""
    ids=[]
    for sid in sorted({r.supplier_id for r in rows}):
        selected=[r for r in rows if r.supplier_id==sid]
        po=PurchaseOrder(vendor_id=sid,status='DRAFT',created_by_principal_id=1,
            creation_idempotency_key=f'forward-fixture-{session.id}-{sid}-{selected[0].id}')
        db.add(po);db.flush();ids.append(po.id)
        for row in selected:
            line=PurchaseOrderLine(purchase_order_id=po.id,variation_id=row.variation_id,sku=row.accepted['sku'],
                item_name=row.accepted['item_name'],variation_name=row.accepted.get('variation_name',''),unit_cost=row.unit_cost,ordered_qty=row.quantity,
                received_qty_total=0,in_transit_qty=0,removed=False)
            db.add(line);db.flush()
            for store,qty in row.allocations.items():db.add(PurchaseOrderStoreAllocation(
                purchase_order_line_id=line.id,store_id=int(store),expected_qty=qty,allocated_qty=qty,
                store_received_qty=0,variance_qty=0))
            row.decisions={**row.decisions,'purchase_commitment':{'purchase_order_id':po.id,'quantity':row.quantity,
                'allocations':dict(row.allocations),'category':row.accepted['inventory_item_type']['id'],
                'intended_account_id':service._group(db,session,sid).intended_account_id}}
        db.flush()
        if not service.uncommitted_lines(db,session,[r for r in service._lines(db,session) if r.supplier_id==sid and not r.removed and r.quantity>0]):
            group=service._group(db,session,sid);group.status='CONVERTED';group.purchase_order_id=po.id
    service._complete(db,session);db.commit();return ids


def financial_inventory(db):
    tables=('funding_reports','funding_report_lines','funding_payments','funding_payment_allocations',
        'funding_ledger_entries','funding_actual_activity','order_payments','consignment_sale_facts',
        'consignment_return_facts','square_sync_events','purchase_order_receipts','purchase_order_receipt_lines',
        'purchase_order_receipt_allocations','vendor_sku_configs')
    return {table:db.execute(text(f'SELECT row_to_json(t)::text FROM {table} t ORDER BY 1')).scalars().all() for table in tables}


@pytest.mark.parametrize('committed_indices',[(0,),(0,1),(0,2,4),(0,1,2,3,4,5)])
def test_partial_full_and_mixed_supplier_recovery_orders_only_uncommitted(engine,committed_indices):
    with Session(engine) as db:
        session,rows=seed(db);ids=forward_commit(db,session,[rows[i] for i in committed_indices]);before=financial_inventory(db)
        committed={r.id:copy.deepcopy(r.decisions) for r in rows if r.id in [rows[i].id for i in committed_indices]}
        for sid in (1,2):
            pending=[r for r in rows if r.supplier_id==sid and not service.committed_order(r,service._group(db,session,sid))]
            po=service.create_po(db,session_id=session.id,supplier_id=sid,actor_id=1);db.commit()
            count=db.scalar(select(func.count()).select_from(PurchaseOrder))
            assert service.create_po(db,session_id=session.id,supplier_id=sid,actor_id=1).id==po.id
            assert db.scalar(select(func.count()).select_from(PurchaseOrder))==count
            if pending:
                ordered=db.scalars(select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id==po.id)).all()
                assert {r.variation_id for r in pending}=={r.variation_id for r in ordered}
        lines=db.scalars(select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id!=1)).all()
        assert len(lines)==6 and len({line.variation_id for line in lines})==6 and sum(line.ordered_qty for line in lines)==60
        for row in rows:
            db.refresh(row);assert row.allocations=={'1':1,'2':2,'3':3,'4':4} and row.quantity==10
            if row.id in committed:assert row.decisions==committed[row.id]
            assert service.purchasing_order_context(db,service.committed_order(row)).session_id==session.id
        assert financial_inventory(db)==before and session.status=='COMPLETE'


@pytest.mark.parametrize('action',['edit','transfer','remove','undo','batch_edit','batch_remove'])
def test_committed_edits_rejected_without_changing_evidence(engine,action):
    with Session(engine) as db:
        session,rows=seed(db);forward_commit(db,session,rows[:2]);row=rows[0]
        original=(row.quantity,copy.deepcopy(row.allocations),row.supplier_id,copy.deepcopy(row.decisions),row.removed)
        with pytest.raises(ValueError):
            if action=='edit':service.update_line(db,session_id=session.id,line_id=row.id,actor_id=1,quantity=12,allocations={'1':3,'2':2,'3':3,'4':4})
            elif action=='transfer':service.update_line(db,session_id=session.id,line_id=row.id,actor_id=1,supplier_id=2)
            elif action in ('remove','undo'):service.remove_line(db,session_id=session.id,line_id=row.id,actor_id=1,undo=action=='undo')
            else:drafts.save(db,session_id=session.id,actor_id=1,version=session.version,request_key='blocked-'+action,
                operations=[{'kind':'line' if action=='batch_edit' else 'remove','line_id':row.id,'changes':{'quantity':12} if action=='batch_edit' else {}}])
        db.rollback();db.refresh(row);assert (row.quantity,row.allocations,row.supplier_id,row.decisions,row.removed)==original
        pending=rows[2];service.update_line(db,session_id=session.id,line_id=pending.id,actor_id=1,
            quantity=12,allocations={'1':3,'2':2,'3':3,'4':4});db.commit();assert pending.quantity==12


def test_refresh_discard_account_and_history_keep_committed_records(engine):
    from app.routers import management
    with Session(engine) as db:
        session,rows=seed(db);service.set_account(db,session_id=session.id,supplier_id=1,actor_id=1,account_id=10);db.commit()
        ids=forward_commit(db,session,rows[:2]);po_id=service.committed_order(rows[0]);snapshot=copy.deepcopy(rows[0].decisions)
        service.set_account(db,session_id=session.id,supplier_id=1,actor_id=1,account_id=None);db.commit()
        assert service.purchasing_order_context(db,po_id).intended_account_id==10
        service.refresh(db,session_id=session.id,actor_id=1,request_key='recovery-refresh',gateway=Gateway());db.commit()
        db.refresh(rows[0]);assert rows[0].decisions==snapshot and rows[0].proposed is None
        client,app=app_client(db);app.include_router(management.router)
        assert client.post(f'/management/ordering-tool/orders/{po_id}/delete',data={'csrf_token':'test'}).status_code==400
        service.discard_group(db,session_id=session.id,supplier_id=1,actor_id=1);db.commit()
        assert not rows[0].removed and rows[2].removed and rows[4].removed
        assert rows[0].decisions==snapshot


def test_concurrent_recovery_finalization_does_not_duplicate_partial_order(engine):
    with Session(engine) as db:
        session,rows=seed(db);forward_commit(db,session,rows[:2]);sid=session.id
    barrier=Barrier(2)
    def run(_):
        with Session(engine) as db:
            barrier.wait();po=service.create_po(db,session_id=sid,supplier_id=1,actor_id=1);db.commit();return po.id
    with ThreadPoolExecutor(max_workers=2) as pool:ids=list(pool.map(run,range(2)))
    assert ids[0]==ids[1]
    with Session(engine) as db:
        lines=db.scalars(select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id!=1)).all()
        assert len(lines)==len({line.variation_id for line in lines})==4
