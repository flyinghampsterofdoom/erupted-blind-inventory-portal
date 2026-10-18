"""Direct store shipment submission reuses receipt evidence and durable Square commands."""
import copy
from datetime import datetime,timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4
import pytest
from sqlalchemy import select,func
from sqlalchemy.orm import Session
from test_purchasing_receiving_extension import database_template,receiving_db
from test_purchasing_ui import app_client
from app.config import settings
from app.models import (PurchaseOrder,PurchaseOrderLine,PurchaseOrderStoreAllocation,PurchaseOrderReceipt,
    PurchaseOrderReceiptAllocation,SquareSyncEvent,FundingActualActivity,FundingLedgerEntry,PurchaseOrderStatus)
from app.services import purchase_order_admin_service as receiving
from app.services.purchasing_inventory_service import load_incoming_supply


@pytest.fixture
def shipments(receiving_db,monkeypatch):
    monkeypatch.setattr(settings,'purchasing_v3_phase2_enabled',True)
    monkeypatch.setattr('app.services.v2_order_payments_service.initialize_new_order_if_configured',lambda *a,**kw:None)
    sent=[]
    def post(path,payload,**kw):
        assert path=='/v2/inventory/changes/batch-create' and kw=={'inventory_quantity_write':True}
        sent.append(copy.deepcopy(payload));return {'counts':[]}
    monkeypatch.setattr(receiving,'_square_post',post)
    with Session(receiving_db) as db:
        db.get(PurchaseOrderLine,1).ordered_qty=100
        for a in db.scalars(select(PurchaseOrderStoreAllocation)):a.expected_qty=a.allocated_qty=25
        db.add(PurchaseOrderLine(id=2,purchase_order_id=1,variation_id='VAR2',sku='SKU2',item_name='Citrus',variation_name='Lemon',ordered_qty=80))
        db.flush()
        for sid in range(1,5):db.add(PurchaseOrderStoreAllocation(purchase_order_line_id=2,store_id=sid,expected_qty=20,allocated_qty=20,store_received_qty=0))
        db.commit()
    return receiving_db,sent


def submit(db,cells,key):
    return receiving.submit_receiving_shipment(db,purchase_order_id=1,actor_principal_id=1,
        received_qty_by_line_store=cells,request_key=key)


def view(db):return receiving.receiving_workspace(db,purchase_order_id=1,actor_principal_id=1)


def test_three_shipments_full_zero_second_third_and_pdf_preservation(shipments):
    engine,sent=shipments
    with Session(engine) as db:
        po=db.get(PurchaseOrder,1);po.submitted_at=datetime.now(timezone.utc);pdf=receiving.ensure_current_purchase_order_pdf(db,purchase_order_id=1);before=pdf.read_bytes();path=po.pdf_path;db.commit()
        first={(1,1):25,(1,2):20,(1,3):25,(1,4):20,(2,1):0}
        result=submit(db,first,'shipment1');assert result['succeeded']==4
        assert view(db)['history'][0]['outstanding_after']==90  # Entire 180-unit PO, not just the first SKU's ten missing units.
        first_id=result['receipt_id'];snapshot=copy.deepcopy(db.get(PurchaseOrderReceipt,first_id).request_payload)
        assert view(db)['status']=='Partially Received' and view(db)['outstanding']==90
        assert db.get(PurchaseOrderLine,1).ordered_qty==100 and db.get(PurchaseOrderLine,1).received_qty_total==90
        assert submit(db,first,'shipment1')['skipped_already_synced']==4 and len(sent)==4
        submit(db,{(1,2):5},'shipment2');assert len(sent)==5
        assert view(db)['outstanding']==85
        assert view(db)['history'][0]['outstanding_after']==85
        submit(db,{(1,4):5,**{(2,sid):20 for sid in range(1,5)}},'shipment3')
        assert view(db)['status']=='Fully Received' and view(db)['outstanding']==0 and not view(db)['can_receive']
        assert [(r['received'],r['outstanding_after'],r['status']) for r in reversed(view(db)['history'])]==[(90,90,'Submitted'),(5,85,'Submitted'),(85,0,'Complete')]
        assert len(sent)==10 and db.get(PurchaseOrder,1).status==PurchaseOrderStatus.COMPLETED
        assert db.scalar(select(func.count()).select_from(PurchaseOrderReceipt))==3
        assert db.get(PurchaseOrderReceipt,first_id).request_payload==snapshot
        assert receiving.ensure_current_purchase_order_pdf(db,purchase_order_id=1)==pdf
        assert db.get(PurchaseOrder,1).pdf_path==path and Path(pdf).read_bytes()==before
        assert db.get(PurchaseOrderLine,1).ordered_qty==100
        assert {a.allocated_qty for a in db.scalars(select(PurchaseOrderStoreAllocation).where(PurchaseOrderStoreAllocation.purchase_order_line_id==1))}=={25}
        assert sum(x.quantity for x in load_incoming_supply(db))==0
        for model in (FundingActualActivity,FundingLedgerEntry):assert db.scalar(select(func.count()).select_from(model))==0


def test_zero_shipment_and_explicit_close_with_shortage(shipments):
    engine,sent=shipments
    with Session(engine) as db:
        submit(db,{(1,1):0,(2,1):0},'zero-shipment')
        assert len(view(db)['history'])==1 and view(db)['status']=='Awaiting Receipt' and not sent
        submit(db,{(1,1):25,(1,2):20,(1,3):25,(1,4):20},'partial')
        receiving.close_receiving_shortages(db,purchase_order_id=1,actor_principal_id=1,request_key='close');db.commit()
        assert view(db)['status']=='Closed with Shortage' and view(db)['closed']==90 and view(db)['outstanding']==0
        closures=[event for event in view(db)['history'] if event['kind']=='CLOSURE']
        assert closures[0]['outstanding_after']==0 and all(event['outstanding_after'] is not None for event in closures)
        count=db.scalar(select(func.count()).select_from(PurchaseOrderReceipt))
        receiving.close_receiving_shortages(db,purchase_order_id=1,actor_principal_id=1,request_key='close');db.commit()
        assert db.scalar(select(func.count()).select_from(PurchaseOrderReceipt))==count
        assert len(sent)==4 and db.get(PurchaseOrderLine,1).ordered_qty==100


def test_actual_store_split_can_differ_but_no_extra_incoming_or_over_receiving(shipments):
    engine,sent=shipments
    with Session(engine) as db:
        submit(db,{(1,1):90},'reallocated-actual')
        assert sum(x.quantity for x in load_incoming_supply(db) if x.purchase_order_id==1 and x.variation_id=='VAR1')==10
        assert sum(x.quantity for x in load_incoming_supply(db,store_ids=[2]) if x.variation_id=='VAR1')<=10
        for quantity in (-1,11):
            with pytest.raises(ValueError):submit(db,{(1,2):quantity},'invalid'+str(quantity))
            db.rollback()
        root=db.scalar(select(PurchaseOrderReceiptAllocation).where(PurchaseOrderReceiptAllocation.event_kind=='DELIVERY'))
        with pytest.raises(ValueError,match='remaining'):
            receiving.correct_purchase_order_receiving(db,purchase_order_id=1,original_allocation_id=root.id,
                corrected_quantity=101,actor_principal_id=1,request_key='bad-correction')
        db.rollback()
        assert db.get(PurchaseOrderLine,1).received_qty_total==90 and len(sent)==1
        assert db.scalar(select(func.count()).select_from(PurchaseOrderReceipt))==1


def test_two_concurrent_counts_cannot_over_receive(shipments):
    engine,_=shipments
    def worker(store):
        with Session(engine) as db:
            try:
                receiving.save_purchase_order_received_quantities(db,purchase_order_id=1,actor_principal_id=1,
                    received_qty_by_line_store={(1,store):80},request_key=uuid4().hex);db.commit();return True
            except ValueError:db.rollback();return False
    with ThreadPoolExecutor(2) as pool:
        a,b=pool.submit(worker,1),pool.submit(worker,2)
        assert sorted([a.result(15),b.result(15)])==[False,True]
    with Session(engine) as db:
        assert db.get(PurchaseOrderLine,1).received_qty_total==80
        assert db.scalar(select(func.count()).select_from(PurchaseOrderReceipt))==1
        assert db.scalar(select(func.count()).select_from(SquareSyncEvent))==0


def test_failed_and_uncertain_post_recovers_same_event_without_replaying_old_counts(shipments,monkeypatch):
    engine,sent=shipments
    attempts=[]
    def uncertain(path,payload,**kw):attempts.append(copy.deepcopy(payload));raise TimeoutError('Reply lost')
    with Session(engine) as db:
        # A legacy saved-but-not-pushed modern delivery is NOT automatically posted by a new submission.
        receiving.save_purchase_order_received_quantities(db,purchase_order_id=1,actor_principal_id=1,received_qty_by_line_store={(2,1):1},request_key='older-saved');db.commit()
        monkeypatch.setattr(receiving,'_square_post',uncertain)
        first=submit(db,{(1,1):90},'uncertain-new');assert first['uncertain']==1
        assert db.get(PurchaseOrderLine,1).received_qty_total==90 and len(attempts)==1
        def fail(path,payload,**kw):attempts.append(copy.deepcopy(payload));raise ValueError('Square rejected')
        monkeypatch.setattr(receiving,'_square_post',fail)
        assert submit(db,{(1,1):90},'uncertain-new')['failed']==1
        def ok(path,payload,**kw):attempts.append(copy.deepcopy(payload));return {}
        monkeypatch.setattr(receiving,'_square_post',ok)
        assert submit(db,{(1,1):90},'uncertain-new')['succeeded']==1
        assert attempts[0]==attempts[1]==attempts[2]
        assert db.scalar(select(func.count()).select_from(PurchaseOrderReceipt))==2
        assert db.scalar(select(func.count()).select_from(SquareSyncEvent))==1
        assert len(view(db)['history'])==2 and db.get(PurchaseOrderLine,2).received_qty_total==1
        older=db.scalar(select(PurchaseOrderReceipt).where(PurchaseOrderReceipt.request_key=='older-saved'))
        assert next(r for r in view(db)['history'] if r['id']==older.id)['outstanding_after'] is None
        with pytest.raises(ValueError,match='cannot be replayed'):
            receiving.receive_purchase_order(db,purchase_order_id=1,actor_principal_id=1,receipt_id=older.id)
        db.rollback()
        with pytest.raises(ValueError,match='different input'):submit(db,{(1,1):89},'uncertain-new')
        db.rollback()


def test_receiving_http_owner_csrf_validation_and_single_action(shipments):
    from app.auth import Principal as Actor,Role
    from app.routers import management
    engine,sent=shipments
    with Session(engine) as db:
        client,app=app_client(db);url='/management/purchasing/orders/1'
        response=client.get(url+'/receiving');assert response.status_code==200,response.text
        assert 'data-cancel-confirmation' in response.text
        assert 'Submit to Stores' in response.text and 'Warehouse' not in response.text and 'receiving-bootstrap' in response.text
        payload={'request_key':'http-first','cells':[[1,1,90],[2,1,0]]}
        assert client.post(url+'/shipments',json=payload).status_code==403
        headers={'X-CSRF-Token':'test'}
        response=client.post(url+'/shipments',json=payload,headers=headers);assert response.status_code==200,response.text
        assert client.post(url+'/shipments',json=payload,headers=headers).json()['skipped_already_synced']==1
        page=client.get(url+'/receiving');assert 'Receive Remaining Items' in page.text and 'Partially Received' in page.text
        assert 'Outstanding After' in page.text and 'data-history-summary=' in page.text and 'data-history-detail=' in page.text
        assert 'data-history-detail="'+str(response.json()['receipt_id'])+'" hidden' in page.text
        for cells in ([[1,2,-1]],[[1,2,11]],[[1,2,0.5]],[[1,2,True]],[[1,2,1],[1,2,1]],[[999,2,0]]):
            r=client.post(url+'/shipments',json={'request_key':uuid4().hex,'cells':cells},headers=headers);assert r.status_code==400,r.text
        app.dependency_overrides[management.ordering_access]=lambda:Actor(2,'other',Role.ADMIN,None,True)
        assert client.get(url+'/receiving').status_code==403
        assert client.post(url+'/shipments',json=payload,headers=headers).status_code==403
        assert len(sent)==1


def test_barcode_cannot_bypass_remaining_balance_or_reopen_closed_shortage(shipments):
    engine,_=shipments
    with Session(engine) as db:
        submit(db,{(1,1):99},'ninety-nine')
        receiving.scan_purchase_order_barcode(db,purchase_order_id=1,barcode='SKU1',actor_principal_id=1,request_key='last-unit');db.commit()
        assert db.get(PurchaseOrderLine,1).received_qty_total==100
        with pytest.raises(ValueError,match='remaining'):
            receiving.scan_purchase_order_barcode(db,purchase_order_id=1,barcode='SKU1',actor_principal_id=1,request_key='over-unit')
        db.rollback()
        assert db.get(PurchaseOrderLine,1).received_qty_total==100
        assert db.scalar(select(func.count()).select_from(PurchaseOrderReceipt))==2
        receiving.close_receiving_shortages(db,purchase_order_id=1,actor_principal_id=1,request_key='close-second-line');db.commit()
        with pytest.raises(ValueError,match='remaining'):
            submit(db,{(2,1):1},'cannot-receive-closed')
        db.rollback()
        assert db.get(PurchaseOrderLine,2).received_qty_total==0
