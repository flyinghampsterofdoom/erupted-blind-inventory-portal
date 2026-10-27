"""Purchasing defaults are independent of sales financing; PostgreSQL recovery is explicit."""
import copy
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select, func, text
from sqlalchemy.orm import Session
from test_purchasing_phase2 import engine, receiving_db, database_template, Gateway, generate, line
from test_purchasing_ui import app_client
from app.models import (Vendor, VendorSkuConfig, PurchasingSessionGroup, PurchasingSessionLine,
                        PurchaseOrder, PurchaseOrderLine, PurchaseOrderStoreAllocation, AuditLog,
                        OrderingProductLifecycle, OrderingCatalogIdentity)
from app.services import purchasing_session_service as purchasing
from app.services import purchasing_supplier_recovery_service as recovery
from app.services.purchasing_supplier_service import SupplierContext
from test_purchasing_category_orders import protected


@pytest.mark.parametrize('candidates,defaults,expected,reason',[
    (('V1',),set(),1,'SINGLE_CATALOG_SUPPLIER'),
    (('V1','V2'),{1},1,'EXPLICIT_DEFAULT'),
    (('V1','V2'),set(),None,'NEEDS_SUPPLIER'),
    (('V1','V2'),{1,2},None,'CONFLICTING_DEFAULTS'),
    (('V1',),{2},None,'CONFLICTING_DEFAULTS'),
    (('V1','INACTIVE','DELETED'),set(),1,'SINGLE_CATALOG_SUPPLIER'),
    (('INACTIVE','DELETED'),set(),None,'NEEDS_SUPPLIER'),
    ((),{1},None,'CONFLICTING_DEFAULTS'),
    (('V2','V1','V1'),{1},1,'EXPLICIT_DEFAULT'),
])
def test_precedence(candidates,defaults,expected,reason):
    context=object.__new__(SupplierContext)
    context.catalog={'V1':{1},'V2':{2}};context.defaults={'VAR1':defaults}
    assert context.resolve('VAR1',candidates)==(expected,reason)


def multi_gateway():
    gateway=Gateway()
    gateway.catalog={key:replace(product,vendor_ids=('V1','V2')) for key,product in gateway.catalog.items()}
    return gateway


def unresolved(db):
    session=generate(db,multi_gateway());row=line(db,session)
    row.supplier_id=None;row.canonical_vendor_id=None;row.unit_cost=None;row.cost_source='UNKNOWN'
    row.quantity=13;row.allocations={'1':1,'2':2,'3':3,'4':7}
    row.accepted={**row.accepted,'canonical_vendor_id':None,'unit_cost':None,'cost_source':'UNKNOWN',
                  'review':['Canonical Square vendor needs review','Purchase cost needs review']}
    row.decisions={**row.decisions,'quantity_override':True,'allocation_override':True,'intended_account_id':10}
    for group in purchasing._groups(db,session):db.delete(group)
    db.commit();return session,row


def selection(db,session,row):
    plan=recovery.preview(db,session_id=session.id,actor_id=1)
    found=next(r for r in plan['rows'] if r['id']==row.id)
    return {'id':row.id,'token':found['token']}


def recover(db,session,row,*,key='recover',fill=False):
    return recovery.apply(db,session_id=session.id,actor_id=1,version=session.version,request_key=key,
        selections=[selection(db,session,row)],confirmed=True,fill_missing_costs=fill)


def saved(row):
    return copy.deepcopy((row.quantity,row.allocations,row.accepted,row.proposed,row.decisions,row.unit_cost,row.cost_source))


def test_multivendor_default_cost_and_financing_independence(engine):
    with Session(engine) as db:
        session=generate(db,multi_gateway());row=line(db,session)
        assert row.supplier_id==row.canonical_vendor_id==1 and row.unit_cost==3
        assert row.accepted['supplier_resolution']=='EXPLICIT_DEFAULT'
        assert [g.supplier_id for g in purchasing._groups(db,session)]==[1]
        # Conflicting explicit defaults block even a single catalog candidate.
        db.scalar(select(VendorSkuConfig).where(VendorSkuConfig.vendor_id==2)).is_default_vendor=True
        evidence=purchasing.collect_evidence(db,purchasing.policy(),gateway=Gateway())
        assert evidence['products']['VAR1']['canonical_vendor_id'] is None


@pytest.mark.parametrize('state',('no_default','inactive_mapping','inactive_vendor','missing_cost'))
def test_database_mapping_validity(engine,state):
    with Session(engine) as db:
        mapping=db.scalar(select(VendorSkuConfig).where(VendorSkuConfig.vendor_id==1,VendorSkuConfig.square_variation_id=='VAR1'))
        if state=='no_default':mapping.is_default_vendor=False
        if state=='inactive_mapping':mapping.active=False
        if state=='inactive_vendor':db.get(Vendor,1).active=False
        if state=='missing_cost':mapping.unit_cost=None
        db.flush()
        evidence=purchasing.collect_evidence(db,purchasing.policy(),gateway=multi_gateway())['products']['VAR1']
        assert evidence['canonical_vendor_id']==(1 if state=='missing_cost' else 2 if state=='inactive_vendor' else None)
        if state=='missing_cost':assert evidence['unit_cost'] is None and 'Purchase cost needs review' in evidence['review']


def test_preview_readonly_confirmation_and_recovery_idempotency(engine):
    with Session(engine) as db:
        session,row=unresolved(db);before=saved(row);finance=protected(db)
        po_count=db.scalar(select(func.count()).select_from(PurchaseOrder))
        choice=selection(db,session,row);version=session.version
        assert not db.new and not db.dirty and not db.deleted
        kwargs=dict(session_id=session.id,actor_id=1,version=version,request_key='same',selections=[choice])
        with pytest.raises(ValueError,match='Confirm'):recovery.apply(db,**kwargs)
        result=recovery.apply(db,**kwargs,confirmed=True);db.commit()
        assert result['recovered']==1 and result['units']==13 and row.supplier_id==row.canonical_vendor_id==1
        after=saved(row)
        assert after[:4]==before[:4] and after[5:]==before[5:]
        assert {k:v for k,v in row.decisions.items() if k!='supplier_recovery'}==before[4]
        assert purchasing._groups(db,session)[0].intended_account_id==10
        assert protected(db)==finance and db.scalar(select(func.count()).select_from(PurchaseOrder))==po_count
        assert recovery.apply(db,**kwargs,confirmed=True)['replayed']
        assert not recovery.preview(db,session_id=session.id,actor_id=1)['rows']
        assert db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action=='PURCHASING_SUPPLIERS_RECOVERED'))==1
        with pytest.raises(ValueError,match='different decisions'):
            recovery.apply(db,**kwargs,confirmed=True,fill_missing_costs=True)


@pytest.mark.parametrize('existing_cost,fill',[(None,True),(Decimal('2.7500'),True),(Decimal('2.7500'),False)])
def test_recovery_preserves_costs_or_explicitly_fills_missing(engine,existing_cost,fill):
    with Session(engine) as db:
        session,row=unresolved(db);row.unit_cost=existing_cost
        if existing_cost is not None:row.cost_source='OWNER_CONFIRMED'
        db.commit();before=saved(row)
        recover(db,session,row,fill=fill);db.commit()
        assert row.unit_cost==(3 if existing_cost is None and fill else existing_cost)
        assert saved(row)[:4]==before[:4]
        assert row.cost_source==('CONFIGURED' if existing_cost is None and fill else 'OWNER_CONFIRMED')
        # Recovery is never an acknowledgement of old demand or uncertainty.
        review=purchasing.po_review(db,session_id=session.id,supplier_id=1,actor_id=1)
        assert not review['ready']


@pytest.mark.parametrize('protection',('manual_override','manual_added','selected_supplier','committed','store_commitment','removed','inactive','deleted','closed_group'))
def test_recovery_protects_manual_decisions_commitments_and_ineligible_products(engine,protection):
    with Session(engine) as db:
        session,row=unresolved(db)
        if protection in ('manual_override','manual_added'):
            row.decisions={**row.decisions,('supplier_override' if protection=='manual_override' else 'manual_added'):True}
        if protection=='selected_supplier':row.supplier_id=2
        if protection=='committed':row.decisions={**row.decisions,'purchase_commitment':{'purchase_order_id':1,'quantity':1,'allocations':{'1':1}}}
        if protection=='store_commitment':
            # Link existing PO/store quantities despite a missing product marker.
            group=purchasing._group(db,session,2);group.status='CONVERTED';group.purchase_order_id=1
        if protection=='removed':row.removed=True
        if protection=='inactive':db.add(OrderingProductLifecycle(square_variation_id='VAR1',status='NO_FUTURE_REORDER',no_future_reorder_at=purchasing.now(),no_future_reorder_by_principal_id=1))
        if protection=='deleted':db.add(OrderingCatalogIdentity(square_variation_id='VAR1',square_is_deleted=True,last_seen_at=purchasing.now(),catalog_attributes={}))
        if protection=='closed_group':purchasing._group(db,session,1).status='DISCARDED'
        db.commit();before=saved(row);assert not recovery.preview(db,session_id=session.id,actor_id=1)['rows'];assert saved(row)==before


@pytest.mark.parametrize('change',('quantity','mapping','cost','supplier','allocation','proposal','financing'))
def test_preview_cannot_be_applied_after_evidence_or_decisions_change(engine,change):
    with Session(engine) as db:
        session,row=unresolved(db);choice=selection(db,session,row)
        if change=='quantity':row.quantity+=1
        if change=='mapping':db.scalar(select(VendorSkuConfig).where(VendorSkuConfig.vendor_id==2)).is_default_vendor=True
        if change=='cost':db.scalar(select(VendorSkuConfig).where(VendorSkuConfig.vendor_id==1,VendorSkuConfig.square_variation_id=='VAR1')).unit_cost=9
        if change=='supplier':row.supplier_id=2
        if change=='allocation':row.allocations={'1':2,'2':1,'3':3,'4':7}
        if change=='proposal':row.proposed={**row.accepted,'quantity':999}
        if change=='financing':row.decisions={**row.decisions,'intended_account_id':None}
        db.commit()
        with pytest.raises(ValueError,match='changed'):
            recovery.apply(db,session_id=session.id,actor_id=1,version=session.version,request_key='stale',selections=[choice],confirmed=True)
        db.rollback();assert row.supplier_id==(2 if change=='supplier' else None)


def test_refresh_and_recovery_keep_saved_demand_and_manual_supplier(engine):
    with Session(engine) as db:
        session,row=unresolved(db);gateway=multi_gateway();gateway.units=Decimal(2)
        purchasing.refresh(db,session_id=session.id,actor_id=1,request_key='refresh',gateway=gateway);db.commit()
        assert row.supplier_id is None and row.quantity==13 and row.proposed['quantity']!=13
        before=saved(row);recover(db,session,row,fill=True);db.commit()
        assert row.quantity==13 and row.proposed==before[3] and row.allocations==before[1]
        purchasing.refresh(db,session_id=session.id,actor_id=1,request_key='refresh',gateway=gateway)
        assert row.quantity==13 and row.supplier_id==1
        purchasing.update_line(db,session_id=session.id,line_id=row.id,actor_id=1,supplier_id=2);db.commit()
        purchasing.refresh(db,session_id=session.id,actor_id=1,request_key='refresh-again',gateway=gateway);db.commit()
        assert row.supplier_id==2 and row.quantity==13 and row.decisions['supplier_override']
        assert not recovery.preview(db,session_id=session.id,actor_id=1)['rows']


def test_recovery_routes_require_explicit_confirmation_and_preserve_saved_state(engine):
    with Session(engine) as db:
        session,row=unresolved(db);client,_=app_client(db);base=f'/management/purchasing/sessions/{session.id}'
        view=client.get(base+'/supplier-recovery');assert view.status_code==200
        assert 'not newly calculated demand' in view.text and 'Confirm selected supplier recovery' in view.text
        choice=selection(db,session,row)
        fields={'version':str(session.version),'request_key':'route','csrf_token':'test','selection':str(row.id)+':'+choice['token']}
        assert client.post(base+'/supplier-recovery',data=fields).status_code==400
        assert row.supplier_id is None
        fields.update(confirmed='yes',fill_missing_costs='yes')
        assert client.post(base+'/supplier-recovery',data=fields,follow_redirects=False).status_code==303
        assert row.supplier_id==1 and row.unit_cost==3


def test_recovery_rechecks_concurrent_changes_and_replays_once(engine):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    with Session(engine) as db:
        session,row=unresolved(db);sid=session.id;version=session.version;choice=selection(db,session,row)
    barrier=Barrier(2)
    def work():
        with Session(engine) as db:
            barrier.wait()
            result=recovery.apply(db,session_id=sid,actor_id=1,version=version,request_key='concurrent',
                                  selections=[choice],confirmed=True,fill_missing_costs=True)
            db.commit();return result
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:work(),range(2)))
    assert sum(bool(r.get('replayed')) for r in results)==1
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action=='PURCHASING_SUPPLIERS_RECOVERED'))==1
        assert db.scalar(select(func.count()).select_from(PurchaseOrder))==1
        assert db.scalar(select(PurchasingSessionLine.quantity).where(PurchasingSessionLine.session_id==sid))==13


def test_recovery_atomic_validation_owner_csrf_and_stale_version(engine):
    with Session(engine) as db:
        session,row=unresolved(db);choice=selection(db,session,row)
        with pytest.raises(PermissionError):recovery.preview(db,session_id=session.id,actor_id=2)
        for selections,version in (([choice,{'id':999,'token':'missing'}],session.version),([choice,choice],session.version),([choice],session.version-1)):
            with pytest.raises(ValueError):recovery.apply(db,session_id=session.id,actor_id=1,version=version,request_key='invalid',selections=selections,confirmed=True)
            db.rollback();assert row.supplier_id is None and not purchasing._groups(db,session)
        client,_=app_client(db)
        fields={'version':str(session.version),'request_key':'csrf','selection':str(row.id)+':'+choice['token'],'confirmed':'yes'}
        client.cookies.clear()
        assert client.post(f'/management/purchasing/sessions/{session.id}/supplier-recovery',data=fields).status_code==403
        assert row.supplier_id is None
