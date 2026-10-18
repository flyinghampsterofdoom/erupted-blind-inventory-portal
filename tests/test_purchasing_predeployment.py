"""Final owner acceptance: real routes/PG/PDF plus independent mocked Square ledger."""
import copy
import hashlib
import json
import os
from collections import Counter
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select, func
from sqlalchemy.orm import Session
from test_purchasing_phase2 import engine, receiving_db, database_template, Gateway
from test_purchasing_ui import app_client, patch, BulkGateway
from app.models import (PurchaseOrder, PurchaseOrderLine, PurchaseOrderStoreAllocation,
    PurchasingSessionLine, PurchasingSessionRevision, PurchasingSessionGroup, VendorSkuConfig,
    SquareSyncEvent, SquareSyncStatus, PurchaseOrderReceipt, PurchaseOrderReceiptAllocation,
    FundingActualActivity, FundingLedgerEntry)
from app.services import purchasing_session_service as service
from app.services import purchase_order_admin_service as admin
from app.services import v2_order_payments_service as payments
from app.services.square_request_policy import enforce_square_request_policy

# Capture before the preparation fixture installs its fail-closed writer sentinel.
REAL_INITIALIZER = payments.initialize_new_order_if_configured


def artifact_dir(tmp_path):
    result=Path(os.getenv('PREDEPLOY_EVIDENCE_DIR',str(tmp_path)))
    result.mkdir(parents=True,exist_ok=True)
    return result


def isolate_pdf(monkeypatch,tmp_path):
    fake=tmp_path/'app'/'services'/'purchase_order_admin_service.py'
    monkeypatch.setattr(admin,'Path',lambda value:fake if value==admin.__file__ else Path(value))


class SquareLedger:
    """Square-side deduplication independent of Erupted's command status."""
    def __init__(self,engine):
        self.engine=engine;self.seen={};self.ledger=Counter();self.attempts=[];self.failures={}
    def post(self,path,payload,**kw):
        enforce_square_request_policy('POST',path,payload,**kw)
        key=payload['idempotency_key'];self.attempts.append(copy.deepcopy(payload))
        with Session(self.engine) as observer:
            op=observer.scalar(select(SquareSyncEvent).where(SquareSyncEvent.idempotency_key==key))
            assert op and op.request_payload==payload and op.attempt_count>=1
        a=payload['changes'][0]['adjustment'];pair=(a['location_id'],a['catalog_object_id'])
        failure=self.failures.pop(pair,None)
        if failure=='reject':raise ValueError('Controlled rejection before Square accepted inventory')
        if key in self.seen:assert self.seen[key]==payload
        else:
            self.seen[key]=copy.deepcopy(payload)
            self.ledger[pair]+=int(a['quantity'])*(1 if a['to_state']=='IN_STOCK' else -1)
        if failure=='timeout':raise TimeoutError('Square accepted request; response lost')
        return {'counts':[]}


def test_actual_http_pipeline_three_shipments_and_independent_square_reconciliation(engine,monkeypatch,tmp_path):
    from app.routers import management
    isolate_pdf(monkeypatch,tmp_path)
    gateway=Gateway();monkeypatch.setattr(service,'SquareOrderingReadGateway',lambda:gateway)
    monkeypatch.setattr(payments,'initialize_new_order_if_configured',REAL_INITIALIZER)
    square=SquareLedger(engine);monkeypatch.setattr(admin,'_square_post',square.post)
    saved=artifact_dir(tmp_path)
    with Session(engine) as db:
        client,app=app_client(db);app.include_router(management.router)
        client.headers['x-forwarded-for']='127.0.0.1'
        def post(url,**data):
            r=client.post(url,data={'csrf_token':'test',**data});assert r.status_code==303,r.text
            return r
        start=post('/management/purchasing/generate',request_key='final-e2e',reorder_weeks=5,target_weeks=10,history_days=84)
        url=start.headers['location'];sid=int(url.rsplit('/',1)[1])
        row=db.scalar(select(PurchasingSessionLine).where(PurchasingSessionLine.session_id==sid))
        version=client.get(url+'/workspace').json()['version']
        r=patch(client,sid,version,{'quantity':100,'supplier_id':2,'allocations':{'1':25,'2':25,'3':25,'4':25}},line_id=row.id)
        assert r.status_code==200,r.text
        post(url+'/add',variation_id='VAR2',quantity=80,supplier_id=2,cost='2.50')
        post(url+'/groups/2/account',account_id=10)
        post(url+'/refresh',request_key='final-refresh')
        db.refresh(row);assert row.quantity==100 and row.supplier_id==2 and row.allocations=={'1':25,'2':25,'3':25,'4':25}
        for selected in db.scalars(select(PurchasingSessionLine).where(PurchasingSessionLine.session_id==sid)):
            service.update_line(db,session_id=sid,line_id=selected.id,actor_id=1,acknowledge=True)
        db.commit()
        po_url=post(url+'/groups/2/po').headers['location'];po_id=int(po_url.rsplit('/',1)[1])
        # Repeating the conversion can only return the existing PO, never create another.
        prior_count=db.scalar(select(func.count()).select_from(PurchaseOrder))
        assert service.create_po(db,session_id=sid,supplier_id=2,actor_id=1).id==po_id
        assert db.scalar(select(func.count()).select_from(PurchaseOrder))==prior_count
        lines=db.scalars(select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id==po_id)).all()
        byvar={x.variation_id:x for x in lines};one,two=byvar['VAR1'].id,byvar['VAR2'].id
        assert {x.variation_id:x.ordered_qty for x in lines}=={'VAR1':100,'VAR2':80}
        for line in lines:
            assert sum(db.scalars(select(PurchaseOrderStoreAllocation.allocated_qty).where(PurchaseOrderStoreAllocation.purchase_order_line_id==line.id)))==line.ordered_qty
        assert db.get(PurchaseOrder,po_id).vendor_id==2
        assert db.scalar(select(PurchasingSessionGroup).where(PurchasingSessionGroup.purchase_order_id==po_id)).intended_account_id==10
        assert db.scalar(select(func.count()).select_from(PurchasingSessionRevision))>=2
        assert not square.attempts
        post(po_url+'/submit')
        pdf=client.get(po_url+'/pdf');assert pdf.status_code==200
        (saved/'pipeline-vendor-po.pdf').write_bytes(pdf.content);original_hash=hashlib.sha256(pdf.content).hexdigest()
        route=f'/management/purchasing/orders/{po_id}'
        assert client.get(route+'/receiving').status_code==200 and not square.attempts
        headers={'X-CSRF-Token':'test'}
        def shipment(key,cells):
            r=client.post(route+'/shipments',json={'request_key':key,'cells':cells},headers=headers)
            assert r.status_code==200,r.text
            return r.json()
        square.failures={('L2','VAR1'):'timeout',('L3','VAR1'):'reject'}
        cells=[[one,1,25],[one,2,20],[one,3,25],[one,4,20],[two,1,0]]
        first=shipment('shipment-1',cells)
        assert first['uncertain']==1 and first['failed']==1 and first['succeeded']==2
        # Recovery reuses durable keys; accepted timeout is not applied twice at Square.
        retry=shipment('shipment-1',cells);assert retry['succeeded']==2 and retry['skipped_already_synced']==2
        applied=copy.deepcopy(square.ledger);attempts=len(square.attempts)
        duplicate=shipment('shipment-1',cells);assert duplicate['skipped_already_synced']==4
        assert square.ledger==applied and len(square.attempts)==attempts
        assert db.get(PurchaseOrderLine,one).received_qty_total==90
        workspace=admin.receiving_workspace(db,purchase_order_id=po_id,actor_principal_id=1)
        assert workspace['outstanding']==90  # VAR1=10 plus unreceived VAR2=80.
        shipment('shipment-2',[[one,2,5]])
        shipment('shipment-3',[[one,4,5],*[[two,s,20] for s in range(1,5)]])
        expected=Counter({('L1','VAR1'):25,('L2','VAR1'):25,('L3','VAR1'):25,('L4','VAR1'):25,
            **{(f'L{s}','VAR2'):20 for s in range(1,5)}})
        # Historical PO 1 and zero cells never create commands in this pipeline.
        assert square.ledger==expected and sum(expected.values())==180
        commands=db.scalars(select(SquareSyncEvent)).all();assert len(commands)==10
        assert all(op.status==SquareSyncStatus.SUCCESS and op.purchase_order_id==po_id for op in commands)
        evidence=Counter()
        for event in db.scalars(select(PurchaseOrderReceiptAllocation).where(PurchaseOrderReceiptAllocation.square_sync_event_id.is_not(None))):
            op=db.get(SquareSyncEvent,event.square_sync_event_id)
            assert op.status==SquareSyncStatus.SUCCESS
            evidence[(f'L{event.store_id}',event.square_variation_id)]+=event.quantity_delta
        assert evidence==square.ledger
        workspace=admin.receiving_workspace(db,purchase_order_id=po_id,actor_principal_id=1)
        assert workspace['status']=='Fully Received' and not workspace['can_receive']
        history=[(x['received'],x['outstanding_after']) for x in reversed(workspace['history'])]
        assert history==[(90,90),(5,85),(85,0)]
        assert db.get(PurchaseOrderLine,one).received_qty_total==100 and db.get(PurchaseOrderLine,two).received_qty_total==80
        assert db.scalar(select(func.count()).select_from(PurchaseOrderReceipt))==3
        assert client.post(route+'/shipments',json={'request_key':'over','cells':[[one,1,1]]},headers=headers).status_code==400
        assert hashlib.sha256(client.get(po_url+'/pdf').content).hexdigest()==original_hash
        for model in (FundingActualActivity,FundingLedgerEntry):assert db.scalar(select(func.count()).select_from(model))==0
        manifest={'po_id':po_id,'vendor':'Alternative Supplier','lines':[{'sku':x.sku,'name':x.item_name,'quantity':x.ordered_qty,'unit_cost':str(x.unit_cost)} for x in lines],
            'pdf_sha256':original_hash,'history':history,'commands':len(commands),'http_attempts':len(square.attempts),'unique_keys':len(square.seen),
            'reconciliation':[{'store':k[0],'sku':k[1],'square':v,'successful_receipt_delta':evidence[k]} for k,v in sorted(expected.items())],
            'actual_financial_entries':0}
        (saved/'inventory-reconciliation.json').write_text(json.dumps(manifest,indent=2))


def test_500_sku_multi_page_vendor_pdf_against_persisted_lines(engine,monkeypatch,tmp_path):
    isolate_pdf(monkeypatch,tmp_path);gateway=BulkGateway();saved=artifact_dir(tmp_path)
    with Session(engine) as db:
        for i,(vid,product) in enumerate(gateway.catalog.items()):
            if vid=='VAR1':continue
            vendor=1+i%2
            db.add(VendorSkuConfig(vendor_id=vendor,sku=vid,square_variation_id=vid,unit_cost=Decimal('3.1250'),
                pack_size=1,min_order_qty=0,active=True,is_default_vendor=True))
        db.flush();session=service.generate(db,actor_id=1,request_key='large-pdf',gateway=gateway);db.commit()
        assert db.scalar(select(func.count()).select_from(PurchasingSessionLine).where(PurchasingSessionLine.session_id==session.id))==500
        po=service.create_po(db,session_id=session.id,supplier_id=2,actor_id=1);db.commit()
        lines=db.scalars(select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id==po.id).order_by(PurchaseOrderLine.item_name)).all()
        assert len(lines)==250
        client,app=app_client(db)
        from app.routers import management
        app.include_router(management.router)
        response=client.get(f'/management/ordering-tool/orders/{po.id}/pdf');assert response.status_code==200,response.text
        (saved/'large-250-line-vendor-po.pdf').write_bytes(response.content)
        (saved/'large-pdf-records.json').write_text(json.dumps({'po_id':po.id,'vendor':'Alternative Supplier','rows':[
            {'sku':x.sku,'name':x.item_name,'quantity':x.ordered_qty,'unit_cost':str(x.unit_cost),'line_total':str((x.unit_cost*x.ordered_qty).quantize(Decimal('.01')))} for x in lines],
            'total':str(sum((x.unit_cost*x.ordered_qty).quantize(Decimal('.01')) for x in lines))},indent=2))


def test_explicit_big_financing_survives_supplier_substitution(engine,monkeypatch,tmp_path):
    """Acceptance requirement: supplier substitution must not change canonical COGS."""
    from app.config import settings
    from app.models import PaymentMethod, VendorPaymentClassification, OrderPayment, ConsignmentReplenishment, PurchasingProductAuthority, FundingAccount
    from app.services.purchasing_classification_service import classify_product
    from datetime import datetime,timezone
    isolate_pdf(monkeypatch,tmp_path)
    monkeypatch.setattr(settings,'purchasing_big_square_vendor_id','V1')
    monkeypatch.setattr(settings,'purchasing_streamline_square_vendor_id','STREAMLINE')
    monkeypatch.setattr(payments,'initialize_new_order_if_configured',REAL_INITIALIZER)
    monkeypatch.setattr(admin,'_square_post',lambda *a,**kw:{})
    with Session(engine) as db:
        db.add(PaymentMethod(id=11,display_name='Big Consignment',category='CONSIGNMENT',is_active=True,
            created_by_principal_id=1,updated_by_principal_id=1));db.flush()
        for vendor,method,category in ((1,11,'CONSIGNMENT'),(2,10,'CREDIT_CARD')):
            db.add(VendorPaymentClassification(vendor_id=vendor,payment_method_id=method,payment_category=category,
                payment_method_label_snapshot=category,is_consignment=category=='CONSIGNMENT',effective_date=payments.portal_today(),
                is_current=True,created_by_principal_id=1))
        db.add(PurchasingProductAuthority(square_variation_id='VAR1',square_vendor_ids=['V1'],observed_at=datetime.now(timezone.utc),source_complete=True))
        db.flush()
        db.add(FundingAccount(id=11,account_type='CONSIGNMENT',vendor_id=1,display_name='Big Consignment',created_by_principal_id=1,updated_by_principal_id=1))
        db.flush()
        session=service.generate(db,actor_id=1,request_key='financial-acceptance',gateway=Gateway())
        row=db.scalar(select(PurchasingSessionLine).where(PurchasingSessionLine.session_id==session.id))
        service.update_line(db,session_id=session.id,line_id=row.id,actor_id=1,quantity=100,supplier_id=2,acknowledge=True)
        service.set_account(db,session_id=session.id,supplier_id=2,actor_id=1,account_id=11)
        po=service.create_po(db,session_id=session.id,supplier_id=2,actor_id=1);db.commit()
        assert classify_product(db,variation_id='VAR1',as_of=datetime.now(timezone.utc)).category=='BIG'
        admin.submit_purchase_order(db,purchase_order_id=po.id,actor_principal_id=1);db.commit()
        line=db.scalar(select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id==po.id))
        admin.submit_receiving_shipment(db,purchase_order_id=po.id,actor_principal_id=1,
            received_qty_by_line_store={(line.id,1):90},request_key='financial-receipt');db.commit()
        payment=db.scalar(select(OrderPayment).where(OrderPayment.purchase_order_id==po.id))
        observed={'canonical_cogs':'BIG','canonical_vendor_id':row.canonical_vendor_id,'po_supplier_id':po.vendor_id,
            'legacy_financial_vendor_id':payment.vendor_id if payment else None,
            'legacy_payment_category':payment.payment_category_snapshot if payment else None,
            'consignment_replenishments':db.scalar(select(func.count()).select_from(ConsignmentReplenishment)),
            'actual_card_activity':db.scalar(select(func.count()).select_from(FundingActualActivity))}
        (artifact_dir(tmp_path)/'financial-attribution-corrected.json').write_text(json.dumps(observed,indent=2))
        assert payment is not None and payment.vendor_id==1 and payment.payment_category_snapshot=='CONSIGNMENT',observed


def test_zero_need_store_remains_zero_in_actual_workspace_route(engine):
    from dataclasses import replace
    # Override one observation while retaining the real forecasting engine.
    gateway=BulkGateway()
    original=gateway.fetch
    def evidence(**kw):
        result=original(**kw)
        changed=dict(result.by_store_variation)
        changed[(4,'VAR1')]=replace(changed[(4,'VAR1')],current_on_hand=Decimal(10000))
        return replace(result,by_store_variation=changed)
    gateway.fetch=evidence
    with Session(engine) as db:
        # Restrict this acceptance case to the already configured product.
        gateway.catalog={'VAR1':gateway.catalog['VAR1']}
        session=service.generate(db,actor_id=1,request_key='zero-store',gateway=gateway);db.commit()
        client,_=app_client(db)
        row=client.get(f'/management/purchasing/sessions/{session.id}/workspace?supplier=1').json()['rows'][0]
        assert {key:value['raw_need'] for key,value in row['forecasts'].items()}=={'1':59,'2':128,'3':197,'4':0}
        assert row['allocations']['4']==0 and row['quantity']==384
