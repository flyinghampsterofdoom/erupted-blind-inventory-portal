"""Category purchasing acceptance against disposable PostgreSQL; no external writes."""
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

CATEGORIES=('Disposable','Freebase','Salt')


def seed(db):
    session=service.generate(db,actor_id=1,request_key='category-acceptance',gateway=Gateway())
    original=db.scalar(select(PurchasingSessionLine).where(PurchasingSessionLine.session_id==session.id))
    product=copy.deepcopy(original.accepted)
    rows=[]
    for i in range(6):
        sid=1+i%2;category=CATEGORIES[i//2];vid=f'CATEGORY-{i}'
        accepted={**copy.deepcopy(product),'variation_id':vid,'sku':vid,'item_name':category+str(i),
            'inventory_item_type':{'id':category,'name':category,'issue':None},'review':[],'unit_cost':'3'}
        if i==0:row=original
        else:
            row=PurchasingSessionLine(session_id=session.id,variation_id=vid,canonical_vendor_id=sid,
                supplier_id=sid,quantity=10,allocations={},accepted=accepted,decisions={})
            db.add(row)
        row.variation_id=vid;row.supplier_id=sid;row.canonical_vendor_id=sid;row.accepted=accepted
        row.quantity=10;row.allocations={'1':1,'2':2,'3':3,'4':4};row.unit_cost=Decimal(3);row.cost_source='CONFIGURED'
        row.decisions={};row.proposed=None
        db.add(VendorSkuConfig(vendor_id=sid,sku=vid,square_variation_id=vid,unit_cost=3,pack_size=1,
            min_order_qty=0,active=True,is_default_vendor=True))
        service._group(db,session,sid);rows.append(row)
    db.commit();return session,rows


def create(db,session,category,**kw):
    result=service.create_category_orders(db,session_id=session.id,actor_id=1,category=category,
        expected_version=session.version,**kw);db.commit();return result


def protected(db):
    tables=('funding_reports','funding_report_lines','funding_payments','funding_payment_allocations',
        'funding_ledger_entries','funding_actual_activity','order_payments','consignment_sale_facts',
        'consignment_return_facts','square_sync_events','purchase_order_receipts','purchase_order_receipt_lines',
        'purchase_order_receipt_allocations','vendor_sku_configs')
    return {table:db.execute(text(f'SELECT row_to_json(t)::text FROM {table} t ORDER BY 1')).scalars().all() for table in tables}


def test_isolation_supplier_grouping_four_locations_independent_categories_and_financial_preservation(engine):
    with Session(engine) as db:
        session,rows=seed(db);before=protected(db)
        untouched={r.id:(r.quantity,copy.deepcopy(r.allocations),r.supplier_id,copy.deepcopy(r.decisions)) for r in rows[2:]}
        existing=db.execute(text('SELECT row_to_json(t)::text FROM purchase_orders t WHERE id=1')).scalar_one()
        for index,category in enumerate(CATEGORIES):
            result=create(db,session,category);assert len(result['purchase_order_ids'])==2
            for po_id in result['purchase_order_ids']:
                po=db.get(PurchaseOrder,po_id);assert po.status.value=='DRAFT'
                lines=db.scalars(select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id==po_id)).all()
                assert len(lines)==1 and lines[0].item_name.startswith(category) and lines[0].ordered_qty==10
                splits=db.scalars(select(PurchaseOrderStoreAllocation).where(PurchaseOrderStoreAllocation.purchase_order_line_id==lines[0].id)).all()
                assert {str(a.store_id):a.allocated_qty for a in splits}=={'1':1,'2':2,'3':3,'4':4}
                assert service.purchasing_order_context(db,po_id).session_id==session.id
            tracking=service.category_order_status(db,session)
            assert tracking[category]['state']=='fully ordered'
            assert session.status==('COMPLETE' if index==2 else 'ACTIVE')
            if index==0:
                for row in rows[2:]:
                    db.refresh(row);assert (row.quantity,row.allocations,row.supplier_id,row.decisions)==untouched[row.id]
                assert tracking['Freebase']['state']==tracking['Salt']['state']=='ready'
        assert protected(db)==before
        assert db.execute(text('SELECT row_to_json(t)::text FROM purchase_orders t WHERE id=1')).scalar_one()==existing


def test_partial_category_then_all_types_only_commit_remaining_quantities(engine):
    with Session(engine) as db:
        session,rows=seed(db)
        first=create(db,session,'Disposable',supplier_id=1)['purchase_order_ids']
        assert service.category_order_status(db,session)['Disposable']['state']=='partially ordered'
        result=create(db,session,'')
        assert len(result['purchase_order_ids'])==2
        ids=first+result['purchase_order_ids']
        lines=db.scalars(select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id.in_(ids))).all()
        assert len(lines)==6 and len({l.variation_id for l in lines})==6
        assert service.category_order_status(db,session)['']['state']=='fully ordered'


def test_repeat_stale_overlap_and_legacy_finalization_cannot_duplicate(engine):
    with Session(engine) as db:
        session,rows=seed(db);old_version=session.version
        create(db,session,'Disposable')
        for version in (old_version,session.version):
            with pytest.raises(ValueError):service.create_category_orders(db,session_id=session.id,actor_id=1,
                category='Disposable',expected_version=version)
            db.rollback()
        # Existing supplier finalization consumes only remaining products.
        po=service.create_po(db,session_id=session.id,supplier_id=1,actor_id=1);db.commit()
        assert service.create_po(db,session_id=session.id,supplier_id=1,actor_id=1).id==po.id
        committed=db.scalars(select(PurchaseOrderLine.variation_id).where(PurchaseOrderLine.purchase_order_id!=1)).all()
        assert len(committed)==len(set(committed))==4


def test_committed_line_cannot_be_edited_removed_or_reassigned_but_other_categories_can(engine):
    with Session(engine) as db:
        session,rows=seed(db);create(db,session,'Disposable')
        for action in ('edit','remove','batch'):
            with pytest.raises(ValueError):
                if action=='edit':service.update_line(db,session_id=session.id,line_id=rows[0].id,actor_id=1,quantity=20,supplier_id=2)
                elif action=='remove':service.remove_line(db,session_id=session.id,line_id=rows[0].id,actor_id=1)
                else:drafts.save(db,session_id=session.id,actor_id=1,version=session.version,request_key='remove-ordered',operations=[{'kind':'remove','line_id':rows[0].id}])
            db.rollback()
        service.update_line(db,session_id=session.id,line_id=rows[2].id,actor_id=1,quantity=12,allocations={'1':2,'2':3,'3':3,'4':4});db.commit()
        create(db,session,'Freebase');assert rows[2].quantity==12


def test_blocked_supplier_rolls_back_entire_category_and_ignores_filters(engine):
    with Session(engine) as db:
        session,rows=seed(db);rows[1].unit_cost=None;db.commit();before=db.scalar(select(func.count()).select_from(PurchaseOrder))
        client,_=app_client(db);base=f'/management/purchasing/sessions/{session.id}'
        response=client.post(base+'/category-orders',json={'version':session.version,'category':'Disposable'},headers={'X-CSRF-Token':'test'})
        assert response.status_code==400 and db.scalar(select(func.count()).select_from(PurchaseOrder))==before
        assert all(not r.decisions.get('purchase_commitment') for r in rows)
        # Product/search/store/page filters are never part of the mutation scope.
        rows[1].unit_cost=3;db.commit()
        result=client.post(base+'/category-orders?q=not-visible&page=99',json={'version':session.version,'category':'Disposable'},headers={'X-CSRF-Token':'test'})
        assert result.status_code==200 and len(result.json()['purchase_order_ids'])==2
        data=client.get(base+'/workspace?supplier=1').json()
        assert data['category_orders']['Disposable']['state']=='fully ordered'
        assert not next(r for r in data['rows'] if r['variation_id']==rows[0].variation_id)['editable']


def test_concurrent_same_category_one_commit(engine):
    with Session(engine) as db:
        session,rows=seed(db);sid=session.id;version=session.version
    barrier=Barrier(2)
    def run(_):
        with Session(engine) as db:
            barrier.wait()
            try:
                result=service.create_category_orders(db,session_id=sid,actor_id=1,category='Disposable',expected_version=version);db.commit();return result
            except ValueError:db.rollback();return None
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(run,range(2)))
    assert sum(r is not None for r in results)==1
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(PurchaseOrder).where(PurchaseOrder.id!=1))==2


def test_legacy_converted_group_is_fully_ordered_without_backfill(engine):
    with Session(engine) as db:
        session,rows=seed(db)
        group=db.scalar(select(PurchasingSessionGroup).where(PurchasingSessionGroup.session_id==session.id,PurchasingSessionGroup.supplier_id==1))
        group.status='CONVERTED';group.purchase_order_id=1;db.commit()
        assert service.category_order_status(db,session,supplier=1)['']['state']=='fully ordered'
        assert not any(r.decisions.get('purchase_commitment') for r in rows)
        create(db,session,'Disposable')
        assert not rows[0].decisions.get('purchase_commitment') and rows[1].decisions['purchase_commitment']


def test_category_account_snapshots_preserve_financing_decisions(engine):
    with Session(engine) as db:
        session,rows=seed(db)
        service.set_account(db,session_id=session.id,supplier_id=1,actor_id=1,account_id=10);db.commit()
        first=create(db,session,'Disposable',supplier_id=1)['purchase_order_ids'][0]
        service.set_account(db,session_id=session.id,supplier_id=1,actor_id=1,account_id=None);db.commit()
        second=create(db,session,'Freebase',supplier_id=1)['purchase_order_ids'][0]
        assert service.purchasing_order_context(db,first).intended_account_id==10
        assert service.purchasing_order_context(db,second).intended_account_id is None


def test_split_draft_retains_existing_order_history_deletion_protection(engine):
    from app.routers import management
    with Session(engine) as db:
        session,rows=seed(db);first=create(db,session,'Disposable')['purchase_order_ids']
        client,app=app_client(db);app.include_router(management.router)
        for po_id in first:
            response=client.post(f'/management/ordering-tool/orders/{po_id}/delete',data={'csrf_token':'test'})
            assert response.status_code==400 and 'purchasing-session history' in response.text
            assert db.get(PurchaseOrder,po_id) is not None
        service.discard_group(db,session_id=session.id,supplier_id=1,actor_id=1);db.commit()
        db.refresh(rows[0]);assert not rows[0].removed and rows[0].decisions['purchase_commitment']
        assert rows[2].removed and rows[4].removed


def test_refresh_cannot_unlock_committed_quantities(engine):
    with Session(engine) as db:
        session,rows=seed(db);create(db,session,'Disposable')
        before={r.id:(copy.deepcopy(r.decisions),copy.deepcopy(r.accepted),copy.deepcopy(r.allocations),r.quantity) for r in rows[:2]}
        service.refresh(db,session_id=session.id,actor_id=1,request_key='after-category',gateway=Gateway());db.commit()
        for row in rows[:2]:
            db.refresh(row);assert (row.decisions,row.accepted,row.allocations,row.quantity)==before[row.id]
            assert not service.table_workspace(db,session_id=session.id,actor_id=1,supplier=row.supplier_id)['rows'][0]['editable']


def test_category_route_requires_csrf_and_rejects_unknown_scope(engine):
    with Session(engine) as db:
        session,rows=seed(db);client,app=app_client(db);base=f'/management/purchasing/sessions/{session.id}/category-orders'
        assert client.post(base,json={'category':'Salt','version':session.version}).status_code==403
        for category in ('unknown','missing','unavailable'):
            response=client.post(base,json={'category':category,'version':session.version},headers={'X-CSRF-Token':'test'})
            assert response.status_code==400
        assert db.scalar(select(func.count()).select_from(PurchaseOrder))==1
