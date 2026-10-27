"""Explicit supplier-only recovery of saved, unresolved, uncommitted draft lines.

Preview reads persisted evidence, never refreshes demand or contacts Square. Apply
serializes with draft/PO mutations and checks the exact preview again. No PO,
financial, catalog, mapping, inventory or historical recommendation writes.
"""
import hashlib
import json
from sqlalchemy import select
from app.models import (PurchasingDraftBatch, PurchasingSessionLine, PurchaseOrderLine,
                        OrderingProductLifecycle, OrderingCatalogIdentity)
from app.services import purchasing_session_service as purchasing
from app.services.purchasing_supplier_service import SupplierContext
from app.services.purchasing_attention_service import snapshot_decisions


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


def _plan(db, session, *, lock=False):
    lines=purchasing._lines(db,session)
    groups={g.supplier_id:g for g in purchasing._groups(db,session)}
    ids=[line.variation_id for line in lines]
    suppliers=SupplierContext(db,ids,lock=lock)
    configured,historical=purchasing._cost_context(db,ids)
    lifecycle={r.square_variation_id:r.status for r in db.scalars(select(OrderingProductLifecycle).where(
        OrderingProductLifecycle.square_variation_id.in_(ids)))}
    deleted=set(db.scalars(select(OrderingCatalogIdentity.square_variation_id).where(
        OrderingCatalogIdentity.square_variation_id.in_(ids),OrderingCatalogIdentity.square_is_deleted.is_(True))))
    # Protect any product/store already represented in a linked PO, even if a
    # damaged legacy draft lost its per-product commitment marker. Never split it.
    po_ids={g.purchase_order_id for g in groups.values() if g.purchase_order_id}
    po_ids.update(purchasing.committed_order(line,groups.get(line.supplier_id)) for line in lines)
    po_ids.discard(None)
    committed=set(db.scalars(select(PurchaseOrderLine.variation_id).where(PurchaseOrderLine.purchase_order_id.in_(po_ids)))) if po_ids else set()
    rows=[]
    for line in lines:
        current=line.proposed or line.accepted
        sid,reason=suppliers.resolve(line.variation_id,current.get('square_vendor_ids',()))
        decisions=line.decisions or {}
        blocked=None
        if line.supplier_id is not None:blocked='Supplier already selected'
        elif decisions.get('supplier_override') or decisions.get('manual_added'):blocked='Manual supplier decision'
        elif line.removed:blocked='Product removed from draft'
        elif decisions.get('purchase_commitment') or line.variation_id in committed:blocked='Product or store quantity already committed'
        elif current.get('lifecycle')!='ACTIVE' or lifecycle.get(line.variation_id,'ACTIVE')!='ACTIVE' or line.variation_id in deleted:blocked='Product no longer eligible'
        elif not sid:blocked='Supplier relationship needs manual review'
        elif sid in groups and groups[sid].status!='OPEN':blocked='Supplier group already ordered or discarded'
        cost,source,_=purchasing._cost(db,line.variation_id,sid,configured=configured,historical=historical) if sid else (None,'UNKNOWN',1)
        row={'id':line.id,'variation_id':line.variation_id,'name':line.accepted.get('item_name',''),
             'variant':line.accepted.get('variation_name',''),'sku':line.accepted.get('sku',''),
             'supplier_id':sid,'supplier_name':suppliers.vendors[sid].name if sid else None,
             'resolution':reason,'quantity':line.quantity,'allocations':dict(line.allocations),
             'cost':str(line.unit_cost) if line.unit_cost is not None else None,'cost_source':line.cost_source,
             'configured_cost':str(cost) if cost is not None and source=='CONFIGURED' else None,
             'intended_account_id':decisions.get('intended_account_id'),
             'eligible':blocked is None,'blocked':blocked}
        # Full saved decisions and evidence are part of confirmation, including
        # changes from paths that predate optimistic draft versions.
        row['token']=digest({'row':row,'accepted':line.accepted,'proposed':line.proposed,
                             'decisions':decisions,'canonical_vendor_id':line.canonical_vendor_id,
                             'groups':[(g.supplier_id,g.status,g.purchase_order_id,g.intended_account_id) for g in groups.values()]})
        rows.append(row)
    return rows


@snapshot_decisions
def preview(db, *, session_id, actor_id):
    session=purchasing._session(db,session_id,actor_id,lock=False,active=True)
    rows=_plan(db,session)
    return {'session_id':session.id,'version':session.version,'revision':session.revision,
            'observed_at':purchasing.revision_metadata(db,session)[1],
            'rows':[r for r in rows if r['eligible']],
            'blocked':[r for r in rows if not r['eligible'] and r['blocked']!='Supplier already selected']}


@snapshot_decisions
def apply(db, *, session_id, actor_id, version, request_key, selections, confirmed=False, fill_missing_costs=False):
    if confirmed is not True:raise ValueError('Confirm the reviewed supplier recovery before saving')
    if type(fill_missing_costs) is not bool or type(version) is not int:raise ValueError('Invalid recovery confirmation')
    if not isinstance(selections,list) or not 1<=len(selections)<=1000:raise ValueError('Select between 1 and 1,000 products')
    if any(not isinstance(s,dict) or set(s)!={'id','token'} or type(s['id']) is not int or not isinstance(s['token'],str) for s in selections):
        raise ValueError('Select saved products from the recovery preview')
    if len({s['id'] for s in selections})!=len(selections):raise ValueError('Select each product once')
    request_key=purchasing._key(request_key)
    payload=digest({'operation':'SUPPLIER_RECOVERY','version':version,'selections':selections,'fill_missing_costs':fill_missing_costs})
    session=purchasing._session(db,session_id,actor_id)
    previous=db.scalar(select(PurchasingDraftBatch).where(PurchasingDraftBatch.session_id==session_id,PurchasingDraftBatch.request_key==request_key))
    if previous:
        if previous.payload_sha256!=payload:raise ValueError('Recovery request identity was already used for different decisions')
        return {**previous.result,'replayed':True}
    if session.status!='ACTIVE' or version!=session.version:raise ValueError('This draft changed; review supplier recovery again')
    plan={r['id']:r for r in _plan(db,session,lock=True)}
    if any(s['id'] not in plan or not plan[s['id']]['eligible'] or plan[s['id']]['token']!=s['token'] for s in selections):
        raise ValueError('Supplier evidence or saved decisions changed; review supplier recovery again')
    lines={line.id:line for line in purchasing._lines(db,session)}
    groups={g.supplier_id:g for g in purchasing._groups(db,session)}
    changes=[]
    for selection in selections:
        row=plan[selection['id']];line=lines[row['id']];sid=row['supplier_id']
        if sid not in groups:
            group=purchasing._group(db,session,sid);groups[sid]=group
            accounts={plan[s['id']]['intended_account_id'] for s in selections if plan[s['id']]['supplier_id']==sid}
            # Carry forward one unanimous saved decision, never a new financing default.
            if len(accounts)==1:group.intended_account_id=next(iter(accounts))
        before={'supplier_id':line.supplier_id,'canonical_vendor_id':line.canonical_vendor_id,
                'unit_cost':row['cost'],'cost_source':line.cost_source,'decisions':dict(line.decisions)}
        line.supplier_id=sid;line.canonical_vendor_id=sid
        if fill_missing_costs and line.unit_cost is None and row['configured_cost'] is not None:
            from decimal import Decimal
            line.unit_cost=Decimal(row['configured_cost']);line.cost_source='CONFIGURED'
        line.decisions={**line.decisions,'supplier_recovery':{'request_key':request_key,'actor_id':actor_id,
                       'resolution':row['resolution'],'revision':session.revision}}
        line.updated_at=purchasing.now()
        changes.append({'line_id':line.id,'before':before,'supplier_id':sid,'unit_cost':str(line.unit_cost) if line.unit_cost is not None else None,
                        'cost_source':line.cost_source,'quantity':line.quantity,'allocations':dict(line.allocations),
                        'intended_account_id':line.decisions.get('intended_account_id')})
    # Historical recommendations/review flags and proposals stay immutable. Owner
    # review and evidence freshness are still required by normal PO finalization.
    purchasing._complete(db,session)
    result={'saved':True,'version':session.version,'recovered':len(changes),'units':sum(c['quantity'] for c in changes),
            'request_key':request_key,'purchase_orders_created':0}
    db.add(PurchasingDraftBatch(session_id=session_id,request_key=request_key,payload_sha256=payload,result=result,actor_id=actor_id))
    purchasing._audit(db,actor_id,'SUPPLIERS_RECOVERED',purchasing_session_id=session_id,request_key=request_key,
                      fill_missing_costs=fill_missing_costs,changes=changes)
    db.flush();return result
