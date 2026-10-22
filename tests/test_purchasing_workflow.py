"""Atomic save, transfer, review and finalization acceptance on disposable PostgreSQL."""
import copy
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from time import perf_counter
from uuid import uuid4
import pytest
from sqlalchemy import select,func,event,text
from sqlalchemy.orm import Session
from test_ordering_navigation import engine,receiving_db,database_template,seed
from test_purchasing_ui import app_client
from test_purchasing_phase2 import Gateway
from app.models import (PurchasingSessionLine,PurchasingSessionGroup,PurchaseOrder,PurchaseOrderLine,VendorSkuConfig,PurchasingDraftBatch)
from app.services import purchasing_session_service as service,purchasing_draft_service as drafts


def lineop(row,quantity=17,supplier=None):
    fields={'quantity':quantity,'allocations':{'1':quantity,'2':0,'3':0,'4':0}}
    if supplier:fields['supplier_id']=supplier
    return {'kind':'line','line_id':row.id,'changes':fields}


def save(client,sid,version,ops,key=None):
    return client.patch(f'/management/purchasing/sessions/{sid}/draft/batch',json={'version':version,'operations':ops,'request_key':key or uuid4().hex},headers={'X-CSRF-Token':'test'})


def test_30_edits_one_atomic_save_and_vendor_transfers_preserve_selections(engine):
    with Session(engine) as db:
        session=seed(db);client,app=app_client(db);sid=session.id;version=session.version
        rows=db.scalars(select(PurchasingSessionLine).where(PurchasingSessionLine.session_id==sid).order_by(PurchasingSessionLine.id).limit(30)).all()
        po_before=db.scalar(select(func.count()).select_from(PurchaseOrder))
        accepted={r.id:copy.deepcopy(r.accepted) for r in rows}
        ops=[lineop(row,i+1,2 if i<3 else None) for i,row in enumerate(rows)]
        # Baseline state remains unchanged while the working payload is built.
        assert rows[0].quantity!=ops[0]['changes']['quantity']
        sql=[]
        def record(conn,cursor,statement,params,ctx,many):sql.append(statement)
        event.listen(engine,'before_cursor_execute',record);started=perf_counter()
        result=save(client,sid,version,ops,key='thirty');elapsed=(perf_counter()-started)*1000
        event.remove(engine,'before_cursor_execute',record)
        assert result.status_code==200,result.text
        assert result.json()['version']==version+1 and result.json()['changes_saved']==30
        assert db.scalar(select(func.count()).select_from(PurchasingDraftBatch))==1
        assert db.scalar(select(func.count()).select_from(PurchaseOrder))==po_before
        for i,row in enumerate(rows):
            db.refresh(row);assert row.quantity==i+1 and sum(row.allocations.values())==i+1
            assert row.supplier_id==(2 if i<3 else 1) and row.accepted==accepted[row.id]
        assert rows[1].unit_cost==3 and rows[1].cost_source=='TRANSFERRED_UNCONFIRMED'
        confirmed=save(client,sid,result.json()['version'],[{'kind':'line','line_id':rows[1].id,'changes':{'acknowledge':True}}],key='confirm-transfer')
        assert confirmed.status_code==200,confirmed.text
        db.refresh(rows[1]);assert rows[1].unit_cost==3 and rows[1].quantity==2
        before=client.get(f'/management/purchasing/sessions/{sid}/workspace').json()
        assert next(g for g in before['groups'] if g['id']==2)['units']==6
        # Lost acknowledgement: retrying EXACT identity is a no-op even after commit.
        repeat=save(client,sid,version,ops,key='thirty');assert repeat.status_code==200 and repeat.json()['replayed']
        assert db.scalar(select(func.count()).select_from(PurchasingDraftBatch))==2
        changed=copy.deepcopy(ops);changed[0]['changes']['quantity']=99
        assert save(client,sid,version,changed,key='thirty').status_code==409
        print(f'30 edits / 3 transfers: one HTTP save, {len(sql)} SQL statements, {elapsed:.1f} ms, one version increment, no PO creation.')


def test_batch_failure_rolls_back_all_30_decisions_and_can_retry(engine,monkeypatch):
    with Session(engine) as db:
        session=seed(db);client,app=app_client(db)
        rows=db.scalars(select(PurchasingSessionLine).where(PurchasingSessionLine.session_id==session.id).limit(30)).all()
        before={r.id:(r.quantity,r.supplier_id,copy.deepcopy(r.allocations)) for r in rows};version=session.version
        ops=[lineop(row,13) for row in rows];ops[-1]['changes']['allocations']['1']=12
        bad=save(client,session.id,version,ops,key='recover');assert bad.status_code==400
        for row in rows:db.refresh(row);assert (row.quantity,row.supplier_id,row.allocations)==before[row.id]
        assert db.scalar(select(func.count()).select_from(PurchasingDraftBatch))==0
        ops[-1]['changes']['allocations']['1']=13
        original=service.update_line
        def interrupted(*args,**kw):
            if kw['line_id']==rows[15].id:raise RuntimeError('synthetic outage')
            return original(*args,**kw)
        monkeypatch.setattr(service,'update_line',interrupted)
        failed=save(client,session.id,version,ops,key='recover');assert failed.status_code==503
        for row in rows:db.refresh(row);assert (row.quantity,row.supplier_id,row.allocations)==before[row.id]
        monkeypatch.setattr(service,'update_line',original)
        assert save(client,session.id,version,ops,key='recover').status_code==200


def test_concurrent_batch_saves_have_one_winner_and_no_partial_overwrite(engine):
    with Session(engine) as db:
        session=seed(db);sid=session.id;version=session.version
        row=db.scalar(select(PurchasingSessionLine).where(PurchasingSessionLine.session_id==sid));rid=row.id
    barrier=Barrier(2)
    def run(qty):
        with Session(engine) as db:
            barrier.wait()
            try:
                result=drafts.save(db,session_id=sid,actor_id=1,version=version,request_key=str(qty),operations=[{'kind':'line','line_id':rid,'changes':{'quantity':qty,'allocations':{'1':qty,'2':0,'3':0,'4':0}}}]);db.commit();return result
            except drafts.Conflict:db.rollback();return None
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(run,[11,22]))
    assert sum(r is not None for r in results)==1
    with Session(engine) as db:
        assert db.get(PurchasingSessionLine,rid).quantity in (11,22)
        assert db.scalar(select(func.count()).select_from(PurchasingDraftBatch))==1


def test_confirmation_matches_quantity_cost_and_evidence_and_native_errors_are_html(engine,monkeypatch):
    gateway=Gateway();gateway.missing={(1,'VAR1')}
    with Session(engine) as db:
        session=service.generate(db,actor_id=1,request_key='review',gateway=gateway)
        row=db.scalar(select(PurchasingSessionLine).where(PurchasingSessionLine.session_id==session.id))
        service.update_line(db,session_id=session.id,line_id=row.id,actor_id=1,quantity=17,allocations={'1':17,'2':0,'3':0,'4':0});db.commit()
        client,app=app_client(db);url=f'/management/purchasing/sessions/{session.id}'
        review=client.get(url+'/groups/1/review').json()
        assert not review['ready'] and review['items'][0]['id']==row.id and review['items'][0]['can_confirm']
        native=client.post(url+'/groups/1/po',data={'csrf_token':'test','version':session.version})
        assert native.status_code==400 and 'text/html' in native.headers['content-type']
        assert 'Everyday Product' in native.text and 'Review uncertain' in native.text and 'Save Draft' in native.text
        result=save(client,session.id,session.version,[{'kind':'line','line_id':row.id,'changes':{'acknowledge':True}}]);assert result.status_code==200
        assert client.get(url+'/groups/1/review').json()['ready']
        unchanged=client.post(url+f'/lines/{row.id}',data={'csrf_token':'test','quantity':17,'supplier_id':1,'version':result.json()['version']})
        assert unchanged.status_code==303 and client.get(url+'/groups/1/review').json()['ready'],'Unchanged legacy form saves must retain confirmation'
        service.refresh(db,session_id=session.id,actor_id=1,request_key='same-evidence',gateway=gateway);db.commit()
        assert client.get(url+'/groups/1/review').json()['ready'],'Identical evidence must retain acknowledgement'
        db.refresh(session)
        result=save(client,session.id,session.version,[lineop(row,18)]);assert result.status_code==200
        assert not client.get(url+'/groups/1/review').json()['ready'],'A changed quantity requires a fresh acknowledgement'
        result=save(client,session.id,result.json()['version'],[{'kind':'line','line_id':row.id,'changes':{'acknowledge':True}}]);assert result.status_code==200
        gateway.hand=Decimal(9)
        service.refresh(db,session_id=session.id,actor_id=1,request_key='changed-evidence',gateway=gateway);db.commit()
        assert not client.get(url+'/groups/1/review').json()['ready']


def test_save_unfinished_draft_never_finalizes_and_provisional_costs_are_visible(engine):
    with Session(engine) as db:
        gateway=Gateway();session=service.generate(db,actor_id=1,request_key='unfinished',gateway=gateway)
        row=db.scalar(select(PurchasingSessionLine).where(PurchasingSessionLine.session_id==session.id))
        # A nonconfigured positive cost needs confirmation even if no forecast warning exists.
        cfg=db.scalar(select(VendorSkuConfig).where(VendorSkuConfig.vendor_id==1,VendorSkuConfig.square_variation_id=='VAR1'));cfg.unit_cost=None
        row.cost_source='OWNER_CONFIRMED';row.unit_cost=Decimal(3);row.accepted={**row.accepted,'review':[]};db.commit()
        client,app=app_client(db);base=f'/management/purchasing/sessions/{session.id}'
        review=client.get(base+'/workspace?state=review&supplier=1').json();assert review['review_count']==1 and review['rows'][0]['id']==row.id
        count=db.scalar(select(func.count()).select_from(PurchaseOrder));result=save(client,session.id,session.version,[lineop(row,7)])
        assert result.status_code==200 and db.scalar(select(func.count()).select_from(PurchaseOrder))==count
        review=client.get(base+'/groups/1/review').json();assert not review['ready']
        result=save(client,session.id,result.json()['version'],[{'kind':'line','line_id':row.id,'changes':{'acknowledge':True}}]);assert result.status_code==200
        created=client.post(base+'/groups/1/po',data={'csrf_token':'test','version':result.json()['version']},headers={'Accept':'application/json'})
        assert created.status_code==200 and created.json()['purchase_order_id']
        po=db.get(PurchaseOrder,created.json()['purchase_order_id']);assert po.pdf_path is None and po.status.value=='DRAFT'
        assert db.scalar(select(PurchaseOrderLine.ordered_qty).where(PurchaseOrderLine.purchase_order_id==po.id))==7


def test_batch_rejects_cross_session_and_csrf_without_writes(engine):
    with Session(engine) as db:
        session=seed(db);client,app=app_client(db)
        assert save(client,session.id,session.version,[{'kind':'line','line_id':999999,'changes':{'quantity':2}}]).status_code==404
        assert client.patch(f'/management/purchasing/sessions/{session.id}/draft/batch',json={}).status_code==403
        assert db.scalar(select(func.count()).select_from(PurchasingDraftBatch))==0


def test_batch_cost_against_previous_per_row_persistence(engine,tmp_path):
    import importlib.util
    from pathlib import Path
    import subprocess
    baseline_path=tmp_path/'purchasing_before_batch.py'
    try:baseline_path.write_bytes(subprocess.check_output(['git','show','771a289d0df3ba58530a2e9408eaeb5e39f155ba:app/services/purchasing_session_service.py'],cwd=Path(__file__).resolve().parents[1],stderr=subprocess.DEVNULL))
    except subprocess.CalledProcessError:pytest.skip('Comparative benchmark requires the pre-batch release in local Git history')
    spec=importlib.util.spec_from_file_location('purchasing_before_batch',baseline_path);baseline=importlib.util.module_from_spec(spec);spec.loader.exec_module(baseline)
    with Session(engine) as db:
        session=seed(db);sid=session.id
        rows=db.scalars(select(PurchasingSessionLine).where(PurchasingSessionLine.session_id==sid).limit(30)).all()
        ids=[row.id for row in rows]
        statements=[]
        def record(conn,cursor,statement,params,ctx,many):statements.append(statement)
        event.listen(engine,'before_cursor_execute',record)
        start=perf_counter()
        for rid in ids:
            baseline.update_line(db,session_id=sid,line_id=rid,actor_id=1,quantity=17,allocations={'1':17,'2':0,'3':0,'4':0});db.commit()
        before_ms=(perf_counter()-start)*1000;before_queries=len(statements);statements.clear()
        db.refresh(session);version=session.version
        start=perf_counter();drafts.save(db,session_id=sid,actor_id=1,version=version,request_key='benchmark30',operations=[{'kind':'line','line_id':rid,'changes':{'quantity':18,'allocations':{'1':18,'2':0,'3':0,'4':0}}} for rid in ids]);db.commit()
        after_ms=(perf_counter()-start)*1000;after_queries=len(statements)
        event.remove(engine,'before_cursor_execute',record)
        assert after_queries<before_queries/3
        print(f'Persistence benchmark / 30 products: before {before_ms:.1f} ms, {before_queries} SQL, 30 commits; after {after_ms:.1f} ms, {after_queries} SQL, 1 commit. Service/database only; excludes WAN and browser paint.')


def test_review_confirmation_pins_the_cost_shown_to_the_owner(engine):
    with Session(engine) as db:
        gateway=Gateway();gateway.missing={(1,'VAR1')}
        session=service.generate(db,actor_id=1,request_key='cost-pinned',gateway=gateway)
        row=db.scalar(select(PurchasingSessionLine).where(PurchasingSessionLine.session_id==session.id))
        service.update_line(db,session_id=session.id,line_id=row.id,actor_id=1,quantity=17,allocations={'1':17,'2':0,'3':0,'4':0});db.commit()
        client,app=app_client(db)
        config=db.scalar(select(VendorSkuConfig).where(VendorSkuConfig.vendor_id==1,VendorSkuConfig.square_variation_id=='VAR1'))
        config.unit_cost=Decimal(9);db.commit();version=session.version
        result=save(client,session.id,version,[{'kind':'line','line_id':row.id,'changes':{'acknowledge':True,'cost':'3'}}])
        assert result.status_code==400 and 'Configured cost' in result.json()['detail']
        db.refresh(row);assert row.unit_cost==3 and not service._reviewed(row,session)


def test_saved_draft_then_explicit_po_submission_uses_the_existing_pdf_renderer(engine,monkeypatch):
    from app.routers import management
    from app.models import FundingActualActivity,FundingLedgerEntry
    calls=[]
    monkeypatch.setattr('app.services.v2_order_payments_service.initialize_new_order_if_configured',lambda *args,**kwargs:calls.append(kwargs['order'].id))
    with Session(engine) as db:
        session=service.generate(db,actor_id=1,request_key='full-flow-pdf',gateway=Gateway());db.commit()
        row=db.scalar(select(PurchasingSessionLine).where(PurchasingSessionLine.session_id==session.id))
        client,app=app_client(db);app.include_router(management.router)
        from fastapi.testclient import TestClient
        client=TestClient(app,follow_redirects=False,client=('127.0.0.1',50000));client.cookies.set('csrf_token','test')
        saved=save(client,session.id,session.version,[lineop(row,17)]);assert saved.status_code==200 and not calls
        created=client.post(f'/management/purchasing/sessions/{session.id}/groups/1/po',data={'csrf_token':'test','version':saved.json()['version']},headers={'Accept':'application/json'})
        assert created.status_code==200 and not calls
        po_id=created.json()['purchase_order_id'];assert db.get(PurchaseOrder,po_id).pdf_path is None
        started=perf_counter()
        submitted=client.post(f'/management/ordering-tool/orders/{po_id}/submit',data={'csrf_token':'test'})
        assert submitted.status_code==303,submitted.text
        generated_ms=(perf_counter()-started)*1000
        assert calls==[po_id]
        first=client.get(f'/management/ordering-tool/orders/{po_id}/pdf');assert first.status_code==200 and first.content.startswith(b'%PDF-')
        second=client.get(f'/management/ordering-tool/orders/{po_id}/pdf');assert second.content==first.content
        assert db.scalar(select(func.count()).select_from(FundingActualActivity))==0
        assert db.scalar(select(func.count()).select_from(FundingLedgerEntry))==0
        print(f'Existing PDF submit/generate path: {generated_ms:.1f} ms; repeated exports byte-identical. Financial initializer mocked for isolated flow.')


def test_completed_draft_account_selection_still_obeys_existing_submission_freeze(engine):
    with Session(engine) as db:
        session=service.generate(db,actor_id=1,request_key='account-freeze',gateway=Gateway())
        po=service.create_po(db,session_id=session.id,supplier_id=1,actor_id=1);db.commit()
        assert session.status=='COMPLETE'
        client,app=app_client(db)
        saved=save(client,session.id,session.version,[{'kind':'account','supplier_id':1,'changes':{'account_id':'10'}}])
        assert saved.status_code==200,saved.text
        group=db.scalar(select(PurchasingSessionGroup).where(PurchasingSessionGroup.session_id==session.id));db.refresh(group)
        assert group.intended_account_id==10
        po.status='IN_TRANSIT';db.commit();db.refresh(session)
        frozen=save(client,session.id,session.version,[{'kind':'account','supplier_id':1,'changes':{'account_id':''}}])
        assert frozen.status_code==400 and 'frozen' in frozen.json()['detail']
        db.refresh(group);assert group.intended_account_id==10


def test_cached_supplier_groups_do_not_bypass_active_supplier_or_owner_checks(engine):
    from app.models import Vendor
    from app.routers import management
    from app.auth import Principal as Actor,Role
    with Session(engine) as db:
        session=service.generate(db,actor_id=1,request_key='active-owner',gateway=Gateway());db.commit()
        row=db.scalar(select(PurchasingSessionLine).where(PurchasingSessionLine.session_id==session.id));quantity=row.quantity
        db.get(Vendor,1).active=False;db.commit()
        client,app=app_client(db)
        inactive=save(client,session.id,session.version,[lineop(row,19)]);assert inactive.status_code==400
        db.refresh(row);assert row.quantity==quantity
        app.dependency_overrides[management.ordering_access]=lambda:Actor(2,'other-admin',Role.ADMIN,None,True)
        assert save(client,session.id,session.version,[lineop(row,19)]).status_code==403


def test_changed_cost_evidence_on_refresh_invalidates_confirmation(engine):
    gateway=Gateway();gateway.missing={(1,'VAR1')}
    with Session(engine) as db:
        session=service.generate(db,actor_id=1,request_key='price-evidence',gateway=gateway)
        row=db.scalar(select(PurchasingSessionLine).where(PurchasingSessionLine.session_id==session.id))
        service.update_line(db,session_id=session.id,line_id=row.id,actor_id=1,quantity=17,acknowledge=True);db.commit()
        assert service._reviewed(row,session)
        config=db.scalar(select(VendorSkuConfig).where(VendorSkuConfig.vendor_id==1,VendorSkuConfig.square_variation_id=='VAR1'))
        config.unit_cost=Decimal(9);db.commit()
        service.refresh(db,session_id=session.id,actor_id=1,request_key='changed-price-evidence',gateway=gateway);db.commit()
        assert not service._reviewed(row,session)
        assert row.unit_cost==3,'Refresh must not replace the saved chosen cost'
