"""Owner purchasing sessions: evidence, decisions and idempotent draft conversion.

This service never calls a Square inventory writer or initializes accounting.
Quantity calculations belong exclusively to purchase_order_math_service.
"""
from __future__ import annotations
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from statistics import median
from uuid import uuid4

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (PurchasingSession, PurchasingSessionRevision, PurchasingSessionLine, PurchasingSessionGroup,
    Vendor, VendorSkuConfig, Store, FundingAccount, PurchaseOrder, PurchaseOrderLine, PurchaseOrderStoreAllocation,
    PurchaseOrderReceiptAllocation, PurchaseOrderReceiptLine, PurchaseOrderReceipt, OrderingProductLifecycle,
    PurchaseOrderStatus, PurchaseOrderConfidenceState, ParLevelSource)
from app.services.purchase_order_admin_service import authorize_purchasing_owner
from app.services.purchasing_inventory_service import purchasing_store_locations, load_incoming_supply
from app.services.purchasing_evidence_service import BUSINESS_TIMEZONE, quantity as parse_quantity
from app.services.purchase_order_math_service import DemandEvidence, forecast_demand, purchase_multiple, ENGINE_VERSION, qualified_availability
from app.services.v2_ordering_square_gateway import SquareOrderingReadGateway
from app.services.audit_service import log_audit
from app.services.manual_ordering_service import purchase_cost


def now(): return datetime.now(timezone.utc)


def authorize(db, actor_id):
    if not settings.purchasing_v3_phase2_enabled:
        raise PermissionError('Purchasing Phase 2 is not enabled')
    authorize_purchasing_owner(db,actor_id)


def policy(values=None):
    values=values or {}
    result={'reorder_weeks':int(values.get('reorder_weeks',5)), 'target_weeks':int(values.get('target_weeks',10)),
        'history_days':int(values.get('history_days',84))}
    if not 1<=result['reorder_weeks']<result['target_weeks']<=52:
        raise ValueError('Choose 1 <= reorder weeks < target weeks <= 52')
    if not 28<=result['history_days']<=180:
        raise ValueError('History must be between 28 and 180 days')
    return result


def _key(key):
    if not key or len(str(key))>128:raise ValueError('A stable request identity is required')
    return str(key)


def _audit(db, actor, action, **data):
    log_audit(db,actor_principal_id=actor,action='PURCHASING_'+action,session_id=None,ip=None,metadata=data)


def _session(db, session_id, actor_id, *, lock=True, active=False):
    authorize(db,actor_id)
    stmt=select(PurchasingSession).where(PurchasingSession.id==session_id, PurchasingSession.owner_id==actor_id)
    if lock:stmt=stmt.with_for_update()
    row=db.scalar(stmt.execution_options(populate_existing=True))
    if not row:raise LookupError('Purchasing session not found')
    if active and row.status!='ACTIVE':raise ValueError('This session is complete; generate fresh recommendations')
    return row


def _cost_context(db, variations):
    configured={}
    for row in db.scalars(select(VendorSkuConfig).where(VendorSkuConfig.active.is_(True),
        VendorSkuConfig.square_variation_id.in_(variations)).order_by(VendorSkuConfig.id)).all():
        configured.setdefault((row.square_variation_id,row.vendor_id),[]).append(row)
    history={}
    query=select(PurchaseOrder.vendor_id,PurchaseOrderLine).join(PurchaseOrderLine,
        PurchaseOrderLine.purchase_order_id==PurchaseOrder.id).where(PurchaseOrderLine.variation_id.in_(variations),
        PurchaseOrderLine.unit_cost.is_not(None),PurchaseOrder.status.in_((PurchaseOrderStatus.IN_TRANSIT,
            PurchaseOrderStatus.RECEIVED_SPLIT_PENDING,PurchaseOrderStatus.SENT_TO_STORES,PurchaseOrderStatus.COMPLETED))).distinct(
        PurchaseOrder.vendor_id,PurchaseOrderLine.variation_id).order_by(PurchaseOrder.vendor_id,
        PurchaseOrderLine.variation_id,PurchaseOrder.created_at.desc(),PurchaseOrderLine.id.desc())
    for vendor,row in db.execute(query):history[(row.variation_id,vendor)]=row
    return configured,history


def _cost(db, variation_id, supplier_id, *, configured=None, historical=None):
    from app.services.purchasing_attention_service import cost_choice
    costs=(configured.get((variation_id,supplier_id),[]) if configured is not None else db.scalars(select(VendorSkuConfig).where(VendorSkuConfig.square_variation_id==variation_id,
        VendorSkuConfig.vendor_id==supplier_id, VendorSkuConfig.active.is_(True)).order_by(VendorSkuConfig.id)).all()) if supplier_id else []
    if supplier_id:
        current_cost, problem=cost_choice(db,variation_id,supplier_id,{'source':'configured','rows':sorted([(m.id,str(m.unit_cost) if m.unit_cost is not None else None) for m in costs])})
        if current_cost is not None:
            multiples={max(int(m.pack_size or 1),1) for m in costs} or {1}
            if len(multiples)>1:return None,'AMBIGUOUS',1
            return current_cost,'CONFIGURED',next(iter(multiples))
        if problem:return None,'AMBIGUOUS',1
    try:
        values={(purchase_cost(row.unit_cost),max(int(row.pack_size or 1),1)) for row in costs if row.unit_cost is not None}
    except ValueError:
        return None,'INVALID',1
    if len(values)==1:
        chosen=next(row for row in costs if row.unit_cost is not None)
        return chosen.unit_cost, 'CONFIGURED', max(int(chosen.pack_size or 1),1)
    if len(values)>1:return None,'AMBIGUOUS',1
    # A historical cost is a visible provisional value requiring owner review.
    prior=(historical.get((variation_id,supplier_id)) if historical is not None else db.scalar(select(PurchaseOrderLine).join(PurchaseOrder,PurchaseOrder.id==PurchaseOrderLine.purchase_order_id).where(
        PurchaseOrderLine.variation_id==variation_id, PurchaseOrder.vendor_id==supplier_id,
        PurchaseOrderLine.unit_cost.is_not(None), PurchaseOrder.status.in_((PurchaseOrderStatus.IN_TRANSIT,
            PurchaseOrderStatus.RECEIVED_SPLIT_PENDING,PurchaseOrderStatus.SENT_TO_STORES,PurchaseOrderStatus.COMPLETED))).order_by(PurchaseOrder.created_at.desc(),PurchaseOrderLine.id.desc()).limit(1))) if supplier_id else None
    multiple=max(int(costs[0].pack_size or 1),1) if costs else 1
    if prior:
        try:return purchase_cost(prior.unit_cost),'HISTORICAL_REVIEW',multiple
        except ValueError:return None,'INVALID',multiple
    return None,'UNKNOWN',multiple


def _history_context(db):
    rows=db.execute(select(PurchaseOrder,PurchaseOrderReceiptAllocation,PurchaseOrderReceipt).join(
        PurchaseOrderLine,PurchaseOrderLine.purchase_order_id==PurchaseOrder.id).join(
        PurchaseOrderReceiptAllocation,PurchaseOrderReceiptAllocation.purchase_order_line_id==PurchaseOrderLine.id).join(
        PurchaseOrderReceiptLine,PurchaseOrderReceiptLine.id==PurchaseOrderReceiptAllocation.receipt_line_id).join(
        PurchaseOrderReceipt,PurchaseOrderReceipt.id==PurchaseOrderReceiptLine.receipt_id).where(
        PurchaseOrderReceiptAllocation.event_kind=='DELIVERY')).all()
    corrections={}
    for original,delta in db.execute(select(PurchaseOrderReceiptAllocation.original_allocation_id,
        func.sum(PurchaseOrderReceiptAllocation.quantity_delta)).where(
        PurchaseOrderReceiptAllocation.event_kind=='CORRECTION').group_by(PurchaseOrderReceiptAllocation.original_allocation_id)):
        corrections[original]=delta
    rows=[(po,event,receipt) for po,event,receipt in rows if event.quantity_delta+corrections.get(event.id,0)>0]
    last_receipts={}
    for po,event,receipt in rows:
        stamp=receipt.received_at or event.created_at
        last_receipts[(event.square_variation_id,event.store_id)]=max(stamp,last_receipts.get((event.square_variation_id,event.store_id),stamp))
    # Require at least three separate POs, not three store splits of one PO.
    qualifying={}
    for po,event,receipt in rows:
        if po.ordered_at:
            days=((receipt.received_at or event.created_at)-po.ordered_at).total_seconds()/86400
            if 0<=days<=180:
                items=qualifying.setdefault(po.vendor_id,{})
                items[po.id]=min(days,items.get(po.id,days))
    return {vendor:int(round(median(items.values()))) for vendor,items in qualifying.items() if len(items)>=3},last_receipts


from app.services.purchasing_attention_service import snapshot_decisions


@snapshot_decisions
def collect_evidence(db, settings_, *, gateway=None, as_of=None):
    """Read fresh catalog, four-store counts and bounded sales; no writes to Square."""
    from app.services.purchasing_history_service import DatabaseHistoryGateway
    as_of=as_of or now();gateway=gateway or DatabaseHistoryGateway(db)
    locations,issues=purchasing_store_locations(db)
    stores={str(s.id):s.name for s in db.scalars(select(Store).where(Store.id.in_(locations))).all()}
    catalog=gateway.fetch_active_catalog()
    remote=gateway.fetch(location_by_store=locations,variation_ids=sorted(catalog),as_of=as_of,
        history_days=settings_['history_days'],catalog=catalog,include_today=True)
    from app.services.purchasing_attention_service import facts,financing_choice
    _, financing_lots, _, _, _ = facts(db,gateway=type('CatalogSnapshot',(),{'fetch_active_catalog':lambda self:catalog})())
    vendor_candidates={}
    for vendor in db.scalars(select(Vendor).where(Vendor.active.is_(True))).all():
        vendor_candidates.setdefault(vendor.square_vendor_id,[]).append(vendor)
    vendors={key:values[0] for key,values in vendor_candidates.items() if key and len(values)==1}
    lifecycle={r.square_variation_id:r.status for r in db.scalars(select(OrderingProductLifecycle).where(
        OrderingProductLifecycle.square_variation_id.in_(catalog))).all()}
    incoming={};unknown=set()
    for row in load_incoming_supply(db,store_ids=list(locations)):
        key=(row.store_id,row.variation_id)
        if row.quantity is None:unknown.add(key)
        else:incoming[key]=incoming.get(key,0)+row.quantity
    configured,historical=_cost_context(db,list(catalog))
    leads,receipts=_history_context(db)
    output={};end=as_of.astimezone(BUSINESS_TIMEZONE).date()-timedelta(days=1)
    for vid,product in catalog.items():
        ids=tuple(product.vendor_ids)
        canonical=vendors.get(ids[0]) if len(ids)==1 else None
        owner_account,owner_problem=financing_choice(db,vid,ids,{r['role'] for r in financing_lots.get(vid,[])},prospective=True)
        owner_supplier=next((v for v in vendors.values() if owner_account and v.id==owner_account.vendor_id),None)
        if owner_supplier and not owner_problem:canonical=owner_supplier
        store_evidence={};forecasts={};review=[]
        for sid,location in locations.items():
            source=remote.by_store_variation.get((sid,vid))
            sources={x.source:x for x in source.required_sources} if source else {}
            sales_source=sources.get('sales');inventory_source=sources.get('inventory');stockout_source=sources.get('stockout_history')
            sales_complete=bool(sales_source and sales_source.available and sales_source.complete)
            sales_by_day={row.day:row.quantity for row in source.daily_sales} if source else {}
            days=[end-timedelta(days=n) for n in range(settings_['history_days']-1,-1,-1)]
            daily=tuple(sales_by_day.get(day,Decimal(0)) for day in days)
            hand=source.current_on_hand if source and source.inventory_valid else None
            stockout_reliable=bool(stockout_source and stockout_source.available and stockout_source.complete
                and getattr(source,'current_day_inventory_delta',None) is not None)
            availability=qualified_availability(days,sales_by_day,
                {row.day:row.quantity for row in source.daily_inventory_deltas} if source else {},hand,
                getattr(source,'current_day_inventory_delta',None),reliable=stockout_reliable)
            age=max(0,(end-product.created_at.astimezone(BUSINESS_TIMEZONE).date()).days+1) if product.created_at else None
            lead=leads.get(canonical.id) if canonical else None
            forecast=forecast_demand(DemandEvidence(daily,hand, incoming.get((sid,vid),0) if (sid,vid) not in unknown else None,
                sales_complete=sales_complete, availability=tuple(availability),
                lifecycle=lifecycle.get(vid,'ACTIVE'),product_age_days=age,lead_time_days=lead,
                reorder_weeks=settings_['reorder_weeks'],target_weeks=settings_['target_weeks']))
            forecasts[str(sid)]=_json_forecast(forecast)
            if forecast.quantity is None:review.append(f"{stores[str(sid)]}: {', '.join(forecast.reasons)}")
            receipt=receipts.get((vid,sid))
            store_evidence[str(sid)]={'location_id':location,'current_inventory':str(hand) if hand is not None else None,
                'incoming':incoming.get((sid,vid),0) if (sid,vid) not in unknown else None,
                'sales_complete':sales_complete,'stockout_evidence_reliable':stockout_reliable,
                'sales':{day.isoformat():str(value) for day,value in sales_by_day.items() if value},
                'availability':list(availability),'last_known_receipt_at':receipt.isoformat() if receipt else None,
                'inventory_age_days':None, 'sources':[{'source':x.source,'available':x.available,'complete':x.complete,
                    'observed_at':x.observed_at.isoformat() if x.observed_at else None,'detail':x.detail} for x in sources.values()]}
        if not canonical:review.append('Canonical Square vendor needs review')
        review.extend(issues)
        cost,cost_source,multiple=_cost(db,vid,canonical.id if canonical else None,configured=configured,historical=historical)
        if cost_source!='CONFIGURED':review.append('Purchase cost needs review')
        output[vid]={'variation_id':vid,'sku':product.sku,'item_name':product.item_name,'variation_name':product.variation_name,
            'canonical_vendor_id':canonical.id if canonical else None,'square_vendor_ids':list(ids),
            'owner_financing_account_id':owner_account.id if owner_account and not owner_problem else None,
            'lifecycle':lifecycle.get(vid,'ACTIVE'),'product_created_at':product.created_at.isoformat() if product.created_at else None,
            'supplier_lead_time_days':leads.get(canonical.id) if canonical else None,
            'unit_cost':str(cost) if cost is not None else None,'cost_source':cost_source,'multiple':multiple,
            'stores':store_evidence,'forecasts':forecasts,'review':review,
            'quantity':sum(f['quantity'] or 0 for f in forecasts.values()),'revision':None}
    return {'observed_at':as_of.isoformat(),'source':'LIVE_SQUARE','engine_version':ENGINE_VERSION,
        'stores':stores,'location_by_store':{str(k):v for k,v in locations.items()},'mapping_issues':list(issues),
        'sales_start':(end-timedelta(days=settings_['history_days']-1)).isoformat(),'sales_end':end.isoformat(),
        'products':output}


def _json_forecast(forecast):
    return {key:str(value) if isinstance(value,Decimal) else list(value) if isinstance(value,tuple) else value for key,value in asdict(forecast).items()}


def _allocation(total, forecast, stores):
    from app.services.purchase_order_math_service import allocate_purchase
    weights={int(sid):int(row.get('raw_need') or 0) for sid,row in forecast.get('forecasts',{}).items()}
    return {str(k):v for k,v in allocate_purchase(total,weights or {int(sid):0 for sid in stores}).items()}


def _groups(db, session):
    return db.scalars(select(PurchasingSessionGroup).where(PurchasingSessionGroup.session_id==session.id)
        .execution_options(populate_existing=True)).all()


def _group(db, session, supplier, *, reopen=False, cache=None):
    if cache is not None and supplier in cache:return cache[supplier]
    vendor=db.get(Vendor,supplier)
    if not vendor or not vendor.active:raise ValueError('Select an active purchasing supplier')
    group=db.scalar(select(PurchasingSessionGroup).where(PurchasingSessionGroup.session_id==session.id,
        PurchasingSessionGroup.supplier_id==supplier).execution_options(populate_existing=True))
    if not group:
        group=PurchasingSessionGroup(session_id=session.id,supplier_id=supplier,status='OPEN');db.add(group);db.flush()
    elif reopen and group.status=='DISCARDED':group.status='OPEN'
    if cache is not None:cache[supplier]=group
    return group


def _lines(db, session):
    return db.scalars(select(PurchasingSessionLine).where(PurchasingSessionLine.session_id==session.id)
        .execution_options(populate_existing=True)).all()


def _complete(db, session):
    db.flush();groups=_groups(db,session);lines=_lines(db,session)
    # Empty proposals remain available for catalog additions; actual groups
    # complete only by explicit conversion/discard, never by hiding a product.
    session.status='COMPLETE' if groups and all(g.status!='OPEN' for g in groups) and not any(
        not line.removed and line.supplier_id is None for line in lines) else 'ACTIVE'
    session.updated_at=now();session.version+=1;db.flush()


def _insert_line(db, session, product, stores, *, manual=False, selected_supplier=None, context=None):
    canonical=product['canonical_vendor_id'];supplier=selected_supplier or canonical;quantity=purchase_multiple(product['quantity'],product['multiple'])
    group=_group(db,session,supplier,cache=context['groups'] if context is not None else None) if supplier else None
    if group and not product.get('owner_financing_account_id'):
        from app.services.v2_order_payments_service import prospective_vendor_account
        from app.models import PurchasingProductAuthority, PurchasingOwnerDecision
        # New purchase defaults never change existing lot financing. Explicit
        # SKU decisions (including unresolved ones) and cash exceptions win.
        authority=context['authorities'].get(product['variation_id']) if context is not None else db.get(PurchasingProductAuthority,product['variation_id'])
        account=None
        has_owner_decision=product['variation_id'] in context['owner_financing'] if context is not None else db.get(PurchasingOwnerDecision,('FINANCING',product['variation_id']))
        if not (authority and authority.cash_payment_method_id is not None) and not has_owner_decision:
            if context is not None:
                if supplier not in context['defaults']:context['defaults'][supplier]=prospective_vendor_account(db,supplier)
                account=context['defaults'][supplier]
            else:account=prospective_vendor_account(db,supplier)
        if account: product={**product,'owner_financing_account_id':account.id}
    if group and product.get('owner_financing_account_id') and group.intended_account_id is None:group.intended_account_id=product['owner_financing_account_id']
    line=PurchasingSessionLine(session_id=session.id,variation_id=product['variation_id'],canonical_vendor_id=canonical,
        supplier_id=supplier,accepted=product,quantity=quantity,allocations=_allocation(quantity,product,stores),
        unit_cost=Decimal(product['unit_cost']) if product['unit_cost'] is not None else None,cost_source=product['cost_source'],
        decisions={'manual_added':manual,'quantity_override':False,'supplier_override':False,'allocation_override':False,**({'intended_account_id':product['owner_financing_account_id']} if product.get('owner_financing_account_id') else {})},
        removed=bool(group and group.status=='DISCARDED'))
    db.add(line);db.flush();return line


def _needs_session_line(product, evidence):
    # A missing cost or vendor for a product with confirmed zero need is not
    # an actionable purchasing uncertainty. It remains searchable in the catalog.
    return product['lifecycle']=='ACTIVE' and (product['quantity']>0 or
        any(row['quantity'] is None for row in product['forecasts'].values()) or
        not product['forecasts'] or bool(evidence.get('mapping_issues')))


def generate(db, *, actor_id, request_key, settings_=None, gateway=None):
    authorize(db,actor_id);request_key=_key(request_key);settings_=policy(settings_)
    existing=db.scalar(select(PurchasingSession).where(PurchasingSession.request_key==request_key))
    if existing:
        if existing.owner_id!=actor_id or existing.settings!=settings_:raise ValueError('Request identity conflicts with another generation')
        return existing
    evidence=collect_evidence(db,settings_,gateway=gateway)
    session=PurchasingSession(owner_id=actor_id,request_key=request_key,settings=settings_,revision=1,status='ACTIVE')
    from sqlalchemy.exc import IntegrityError
    try:
        with db.begin_nested():
            db.add(session);db.flush()
    except IntegrityError:
        existing=db.scalar(select(PurchasingSession).where(PurchasingSession.request_key==request_key))
        if not existing or existing.owner_id!=actor_id or existing.settings!=settings_:raise
        return existing
    from app.models import PurchasingProductAuthority, PurchasingOwnerDecision
    context={'groups':{},'defaults':{},'authorities':{a.square_variation_id:a for a in db.scalars(select(PurchasingProductAuthority))},
        'owner_financing':set(db.scalars(select(PurchasingOwnerDecision.subject_key).where(PurchasingOwnerDecision.kind=='FINANCING')))}
    for product in evidence['products'].values():
        if _needs_session_line(product,evidence):
            product={**product,'revision':1};_insert_line(db,session,product,evidence['stores'],context=context)
    db.add(PurchasingSessionRevision(session_id=session.id,number=1,request_key=request_key,
        settings=settings_,evidence=evidence,created_by=actor_id));db.flush()
    _audit(db,actor_id,'GENERATED',purchasing_session_id=session.id,settings=settings_,mapping_issues=evidence['mapping_issues'])
    _complete(db,session);return session


def refresh(db, *, session_id, actor_id, request_key, gateway=None):
    session=_session(db,session_id,actor_id,lock=False,active=True);request_key=_key(request_key)
    prior=db.scalar(select(PurchasingSessionRevision).where(PurchasingSessionRevision.request_key==request_key))
    if prior:
        if prior.session_id!=session.id:raise ValueError('Refresh identity belongs to another session')
        return session
    evidence=collect_evidence(db,session.settings,gateway=gateway)
    session=_session(db,session_id,actor_id,active=True);number=session.revision+1
    # Recheck after locking: duplicate refresh requests cannot create revisions.
    prior=db.scalar(select(PurchasingSessionRevision).where(PurchasingSessionRevision.request_key==request_key))
    if prior:
        if prior.session_id!=session.id:raise ValueError('Refresh identity belongs to another session')
        return session
    by_id={line.variation_id:line for line in _lines(db,session)}
    groups={g.supplier_id:g for g in _groups(db,session)}
    for vid,product in evidence['products'].items():
        product={**product,'revision':number}
        line=by_id.get(vid)
        if line and groups.get(line.supplier_id) and groups[line.supplier_id].status=='CONVERTED':continue
        if line:
            line.proposed=product  # Owner decisions are deliberately untouched.
        elif _needs_session_line(product,evidence):
            if not(groups.get(product['canonical_vendor_id']) and groups[product['canonical_vendor_id']].status=='CONVERTED'):
                _insert_line(db,session,product,evidence['stores'])
    for vid,line in by_id.items():
        if vid not in evidence['products'] and not(groups.get(line.supplier_id) and groups[line.supplier_id].status=='CONVERTED'):
            line.proposed={**line.accepted,'revision':number,'lifecycle':'UNAVAILABLE','review':['Product no longer verified in the active catalog'],'quantity':0}
    db.add(PurchasingSessionRevision(session_id=session.id,number=number,request_key=request_key,
        settings=session.settings,evidence=evidence,created_by=actor_id));session.revision=number;db.flush()
    _audit(db,actor_id,'REFRESHED',purchasing_session_id=session.id,revision=number)
    _complete(db,session);return session


def _editable_line(db, session, line_id):
    line=db.scalar(select(PurchasingSessionLine).where(PurchasingSessionLine.id==line_id,
        PurchasingSessionLine.session_id==session.id).execution_options(populate_existing=True))
    if not line:raise LookupError('Session product not found')
    group=db.scalar(select(PurchasingSessionGroup).where(PurchasingSessionGroup.session_id==session.id,
        PurchasingSessionGroup.supplier_id==line.supplier_id).execution_options(populate_existing=True)) if line.supplier_id else None
    if group and group.status=='CONVERTED':raise ValueError('This vendor group already has a PO; edit it in the PO editor')
    return line


def update_line(db, *, session_id, line_id, actor_id, quantity=None, supplier_id=None, allocations=None,
        accept_proposal=False, acknowledge=False, cost=None, expected_version=None):
    session=_session(db,session_id,actor_id,active=True);line=_editable_line(db,session,line_id)
    if expected_version is not None and expected_version!=session.version:raise ValueError('This session changed; reload before saving')
    before={'quantity':line.quantity,'supplier_id':line.supplier_id,'allocations':line.allocations,
        'unit_cost':str(line.unit_cost) if line.unit_cost is not None else None,'cost_source':line.cost_source}
    revision=db.scalar(select(PurchasingSessionRevision).where(PurchasingSessionRevision.session_id==session.id,
        PurchasingSessionRevision.number==session.revision))
    product=line.proposed if accept_proposal and line.proposed else line.accepted
    current=line.proposed or line.accepted
    if current.get('lifecycle')!='ACTIVE':raise ValueError('This product is not eligible for purchasing')
    supplier_id=int(supplier_id or line.supplier_id or 0)
    group=_group(db,session,supplier_id,reopen=True)
    if group.status!='OPEN':raise ValueError('The selected supplier group already has a PO')
    current_cost,cost_source,multiple=_cost(db,line.variation_id,supplier_id)
    if cost is not None and str(cost).strip():
        if current_cost is not None and cost_source=='CONFIGURED' and purchase_cost(cost)!=current_cost:
            raise ValueError('Configured cost cannot be replaced through this session')
        current_cost=purchase_cost(cost);cost_source='OWNER_CONFIRMED' if cost_source!='CONFIGURED' else cost_source
    if cost is None and current_cost is None and line.cost_source=='OWNER_CONFIRMED' and line.supplier_id==supplier_id:
        current_cost=line.unit_cost;cost_source='OWNER_CONFIRMED'
    decisions=dict(line.decisions or {})
    if before['supplier_id'] != supplier_id:
        # Retain the owner's financing decision when a product leaves its group.
        # A destination group's different account must be explicitly reconfirmed.
        previous_group=_group(db,session,before['supplier_id']) if before['supplier_id'] else None
        prior_account=previous_group.intended_account_id if previous_group else None
        if 'intended_account_id' not in decisions and prior_account is not None:
            decisions['intended_account_id']=prior_account
    if accept_proposal:
        line.accepted=current;line.proposed=None;line.canonical_vendor_id=current.get('canonical_vendor_id')
        quantity=purchase_multiple(current['quantity'],multiple)
        allocations=_allocation(quantity,current,revision.evidence['stores'])
        decisions.update(quantity_override=False,allocation_override=False)
    elif quantity is None:
        # Supplier substitution preserves forecast; apply the new supplier's
        # multiple only to automatic quantities, never to an owner override.
        quantity=line.quantity if decisions.get('quantity_override') else purchase_multiple(product['quantity'],multiple)
    else:
        if type(quantity) is not int or not 0<=quantity<=1000000:raise ValueError('Quantity must be a whole number between 0 and 1000000')
        decisions['quantity_override']=True
    if allocations is not None:
        allocations={str(k):int(v) for k,v in allocations.items()}
        if set(allocations)-set(revision.evidence['stores']) or any(v<0 for v in allocations.values()) or sum(allocations.values())!=quantity:
            raise ValueError('Store allocations must sum to the purchasing quantity')
        decisions['allocation_override']=not accept_proposal
    elif quantity!=line.quantity:
        if decisions.get('allocation_override'):
            raise ValueError('Redistribute the manually edited allocations to match the new total')
        allocations=_allocation(quantity,product,revision.evidence['stores'])
    else:allocations=dict(line.allocations)
    line.supplier_id=supplier_id;line.quantity=quantity;line.allocations=allocations
    line.unit_cost=current_cost;line.cost_source=cost_source
    decisions['supplier_override']=supplier_id!=line.canonical_vendor_id
    if acknowledge:decisions['review_revision']=session.revision
    line.decisions=decisions;line.updated_at=now();db.flush()
    # Supplier switches remove an empty old group without leaving a hidden
    # open group that would prevent completion. Removed products still retain undo.
    if before['supplier_id'] and before['supplier_id']!=supplier_id:
        old=_group(db,session,before['supplier_id'])
        if old.status=='OPEN' and not db.scalar(select(PurchasingSessionLine.id).where(
            PurchasingSessionLine.session_id==session.id,PurchasingSessionLine.supplier_id==old.supplier_id).limit(1)):
            old.status='DISCARDED'
    _audit(db,actor_id,'LINE_UPDATED',purchasing_session_id=session.id,line_id=line.id,before=before,
        after={'quantity':quantity,'supplier_id':supplier_id,'allocations':allocations,
            'intended_account_id':decisions.get('intended_account_id'),
            'unit_cost':str(current_cost) if current_cost is not None else None,'cost_source':cost_source},accepted_refresh=accept_proposal)
    _complete(db,session);return line


def remove_line(db, *, session_id, line_id, actor_id, undo=False):
    session=_session(db,session_id,actor_id,active=True);line=_editable_line(db,session,line_id)
    line.removed=not undo
    if undo and line.supplier_id:_group(db,session,line.supplier_id,reopen=True)
    _audit(db,actor_id,'LINE_RESTORED' if undo else 'LINE_REMOVED',purchasing_session_id=session.id,line_id=line.id)
    db.flush();_complete(db,session);return line


def add_product(db, *, session_id, actor_id, variation_id, quantity, supplier_id, cost=None):
    session=_session(db,session_id,actor_id,active=True)
    revision=db.scalar(select(PurchasingSessionRevision).where(PurchasingSessionRevision.session_id==session.id,
        PurchasingSessionRevision.number==session.revision))
    product=revision.evidence['products'].get(variation_id)
    if not product or product['lifecycle']!='ACTIVE':raise ValueError('Select a product from the complete active catalog')
    if type(quantity) is not int or not 1<=quantity<=1000000:raise ValueError('Enter a positive whole quantity')
    existing=db.scalar(select(PurchasingSessionLine).where(PurchasingSessionLine.session_id==session.id,
        PurchasingSessionLine.variation_id==variation_id))
    if existing:
        if not existing.removed:raise ValueError('This product is already in this session')
        line=_editable_line(db,session,existing.id);line.removed=False;db.flush()
    else:
        line=_insert_line(db,session,{**product,'revision':session.revision},revision.evidence['stores'],manual=True,selected_supplier=supplier_id)
    return update_line(db,session_id=session.id,line_id=line.id,actor_id=actor_id,quantity=quantity,
        supplier_id=supplier_id,acknowledge=True,cost=cost)


def discard_group(db, *, session_id, supplier_id, actor_id):
    session=_session(db,session_id,actor_id,active=True);group=_group(db,session,supplier_id)
    if group.status=='CONVERTED':raise ValueError('A converted group cannot be discarded')
    group.status='DISCARDED'
    for line in _lines(db,session):
        if line.supplier_id==supplier_id:
            line.removed=True;line.decisions={**line.decisions,'group_discarded':True}
    _audit(db,actor_id,'GROUP_DISCARDED',purchasing_session_id=session.id,supplier_id=supplier_id)
    db.flush();_complete(db,session);return group


def set_account(db, *, session_id, supplier_id, actor_id, account_id):
    session=_session(db,session_id,actor_id);group=_group(db,session,supplier_id)
    if group.status=='DISCARDED':raise ValueError('The vendor group was discarded')
    if group.purchase_order_id and db.get(PurchaseOrder,group.purchase_order_id).status!=PurchaseOrderStatus.DRAFT:
        raise ValueError('Intended account is frozen after PO submission')
    account=db.get(FundingAccount,account_id) if account_id else None
    if account_id and (not account or not account.is_active):raise ValueError('Select an active intended account')
    prior_account_id=group.intended_account_id
    selected=[line for line in _lines(db,session) if line.supplier_id==supplier_id and not line.removed]
    prior_line_accounts={str(line.id):(line.decisions or {}).get('intended_account_id') for line in selected}
    group.intended_account_id=account_id or None
    for line in selected:
        line.decisions={**(line.decisions or {}),'intended_account_id':account_id or None}
    _audit(db,actor_id,'INTENDED_ACCOUNT_SELECTED',purchasing_session_id=session.id,supplier_id=supplier_id,
        account_id=account_id,prior_account_id=prior_account_id,prior_line_accounts=prior_line_accounts)
    db.flush();_complete(db,session);return group


def create_po(db, *, session_id, supplier_id, actor_id):
    session=_session(db,session_id,actor_id);group=_group(db,session,supplier_id)
    if group.purchase_order_id:return db.get(PurchaseOrder,group.purchase_order_id)
    if session.status!='ACTIVE' or group.status!='OPEN':raise ValueError('This vendor group is no longer open')
    lines=[line for line in _lines(db,session) if line.supplier_id==supplier_id and not line.removed and line.quantity>0]
    if not lines:raise ValueError('This group has no purchasing quantities')
    revision=db.scalar(select(PurchasingSessionRevision).where(PurchasingSessionRevision.session_id==session.id,
        PurchasingSessionRevision.number==session.revision))
    observed=datetime.fromisoformat(revision.evidence['observed_at'])
    if (now()-observed).total_seconds()>86400:
        raise ValueError('Refresh the source evidence before creating a PO')
    for line in lines:
        retained_account=(line.decisions or {}).get('intended_account_id')
        if retained_account is not None and retained_account!=group.intended_account_id:
            raise ValueError('A moved product retains a different intended financing account. Confirm this supplier group\'s intended account before creating its PO.')
        current=line.proposed or line.accepted
        if current.get('lifecycle')!='ACTIVE':raise ValueError('Refresh or remove ineligible products before creating a PO')
        needs_review=bool(current.get('review')) or line.cost_source!='CONFIGURED'
        if needs_review and line.decisions.get('review_revision')!=session.revision:
            raise ValueError('Review uncertain evidence and confirm the selected quantities before creating a PO')
        configured,source,multiple=_cost(db,line.variation_id,supplier_id)
        if line.unit_cost is None:raise ValueError('Confirm a purchase cost for every product')
        if source=='CONFIGURED' and configured!=line.unit_cost:
            raise ValueError('Configured cost changed; Update the product before creating a PO')
        if sum(line.allocations.values())!=line.quantity:raise ValueError('Store allocations must sum to the order quantity')
        if set(line.allocations)-set(revision.evidence['stores']):raise ValueError('Store allocation identity is not verified')
    po=PurchaseOrder(vendor_id=supplier_id,status=PurchaseOrderStatus.DRAFT,created_by_principal_id=actor_id,
        reorder_weeks=session.settings['reorder_weeks'],stock_up_weeks=session.settings['target_weeks'],
        history_lookback_days=session.settings['history_days'],creation_idempotency_key=f'purchase-session-{session.id}-vendor-{supplier_id}',
        notes=f'Purchasing session {session.id}; engine {ENGINE_VERSION}. Estimates and intended account only.')
    db.add(po);db.flush()
    for line in lines:
        product=line.accepted
        pol=PurchaseOrderLine(purchase_order_id=po.id,variation_id=line.variation_id,sku=product.get('sku') or None,
            item_name=product['item_name'],variation_name=product.get('variation_name',''),unit_cost=line.unit_cost,
            suggested_qty=product['quantity'],ordered_qty=line.quantity,received_qty_total=0,in_transit_qty=0,
            confidence_score=Decimal('.8'),confidence_state=PurchaseOrderConfidenceState.NORMAL,
            par_source=ParLevelSource.DYNAMIC,removed=False)
        db.add(pol);db.flush()
        for sid,qty in line.allocations.items():
            db.add(PurchaseOrderStoreAllocation(purchase_order_line_id=pol.id,store_id=int(sid),
                expected_qty=qty,allocated_qty=qty,store_received_qty=0,variance_qty=0))
    group.purchase_order_id=po.id;group.status='CONVERTED';db.flush()
    _audit(db,actor_id,'PO_CREATED',purchasing_session_id=session.id,supplier_id=supplier_id,purchase_order_id=po.id,
        intended_account_id=group.intended_account_id)
    _complete(db,session);return po


def catalog_search(db, *, session_id, actor_id, query):
    session=_session(db,session_id,actor_id,lock=False)
    revision=db.scalar(select(PurchasingSessionRevision).where(PurchasingSessionRevision.session_id==session.id,
        PurchasingSessionRevision.number==session.revision))
    tokens=str(query).casefold().split()
    if not tokens:return []
    products=[p for p in revision.evidence['products'].values() if p['lifecycle']=='ACTIVE' and all(
        token in f"{p['item_name']} {p['variation_name']} {p['sku']}".casefold() for token in tokens)]
    return sorted(products,key=lambda p:(p['item_name'],p['variation_name'],p['variation_id']))[:50]


def view_session(db, *, session_id, actor_id):
    session=_session(db,session_id,actor_id,lock=False)
    lines=_lines(db,session);groups=_groups(db,session)
    vendors={vendor.id:vendor for vendor in db.scalars(select(Vendor).where(Vendor.active.is_(True))).all()}
    accounts=db.scalars(select(FundingAccount).where(FundingAccount.is_active.is_(True)).order_by(FundingAccount.display_name)).all()
    revision=db.scalar(select(PurchasingSessionRevision).where(PurchasingSessionRevision.session_id==session.id,
        PurchasingSessionRevision.number==session.revision))
    group_by_supplier={group.supplier_id:group for group in groups}
    configured,historical=_cost_context(db,[line.variation_id for line in lines])
    ready=[];review=[];removed=[]
    for line in lines:
        current=line.proposed or line.accepted
        group=group_by_supplier.get(line.supplier_id)
        reviewed=line.decisions.get('review_revision')==session.revision
        uncertain=bool(current.get('review')) and not reviewed or line.unit_cost is None or not line.supplier_id
        material=bool(line.proposed and (abs(current.get('quantity',0)-line.accepted.get('quantity',0))>=max(2,line.accepted.get('quantity',0)*.1)
            or current.get('review')!=line.accepted.get('review') or current.get('unit_cost')!=line.accepted.get('unit_cost')
            or current.get('canonical_vendor_id')!=line.canonical_vendor_id))
        item={'line':line,'product':line.accepted,'current':current,'material_change':material,
            'uncertain':uncertain,'group':group,'converted':bool(group and group.status=='CONVERTED'),
            'supplier_name':vendors[line.supplier_id].name if line.supplier_id in vendors else 'Choose supplier',
            'current_configured_cost':_cost(db,line.variation_id,line.supplier_id,configured=configured,historical=historical)[0] if line.supplier_id else None}
        if line.removed:removed.append(item)
        elif uncertain:review.append(item)
        else:ready.append(item)
    group_views=[]
    for group in groups:
        items=[item for item in ready if item['line'].supplier_id==group.supplier_id and item['line'].quantity>0]
        unresolved=[item for item in review if item['line'].supplier_id==group.supplier_id]
        group_views.append({'group':group,'name':vendors[group.supplier_id].name if group.supplier_id in vendors else f'Supplier {group.supplier_id}',
            'items':items,'review_count':len(unresolved),'product_count':len(items),
            'total':sum((item['line'].unit_cost*item['line'].quantity for item in items),Decimal(0)) if all(item['line'].unit_cost is not None for item in items) else None,
            'small_standalone':sum(item['line'].quantity for item in items)==1})
    return {'session':session,'groups':group_views,'review':review,'removed':removed,
        'vendors':sorted(vendors.values(),key=lambda vendor:vendor.name.casefold()),'accounts':accounts,
        'stores':revision.evidence['stores'],'evidence':revision.evidence}


def visible_inventory(db, *, session_id, actor_id, line_ids, gateway=None):
    """Read current Square counts for one visible page, without changing draft evidence."""
    session=_session(db,session_id,actor_id,lock=False)
    if not line_ids or len(line_ids)>100 or any(type(i) is not int or i<=0 for i in line_ids):
        raise ValueError('Choose 1 to 100 saved product rows')
    lines=db.scalars(select(PurchasingSessionLine).where(PurchasingSessionLine.session_id==session.id,
        PurchasingSessionLine.id.in_(line_ids))).all()
    if len(lines)!=len(set(line_ids)):raise LookupError('Saved product row not found in this session')
    stores=db.scalars(select(Store).where(Store.active.is_(True)).order_by(Store.id)).all()
    locations=[s.square_location_id for s in stores if s.square_location_id]
    mapped={str(s.id):s.square_location_id for s in stores if s.square_location_id and locations.count(s.square_location_id)==1}
    started=now();counts={};problem=None
    try:
        if mapped:
            counts=(gateway or SquareOrderingReadGateway()).fetch_current_inventory_counts(
                location_ids=list(mapped.values()),variation_ids=sorted({line.variation_id for line in lines})).counts
    except (ValueError,RuntimeError,OSError):
        problem='Square inventory is unavailable. Saved recommendations and your allocations are unchanged.'
    read_at=now();rows={}
    for line in lines:
        values={}
        for store in stores:
            location=mapped.get(str(store.id));count=counts.get((location,line.variation_id))
            hand=parse_quantity(count.quantity) if count else None
            if count and count.calculated_at and count.calculated_at>started:
                hand=None
            reason=problem or ('Store Square mapping needs review' if not location else
                'Square did not provide a valid current count' if hand is None else None)
            values[str(store.id)]={'quantity':str(hand) if hand is not None else None,
                'calculated_at':count.calculated_at.isoformat() if count and count.calculated_at else None,
                'read_at':read_at.isoformat(),'problem':reason}
        rows[str(line.id)]=values
    return {'read_at':read_at.isoformat(),'rows':rows,'problem':problem}


def table_workspace(db, *, session_id, actor_id, supplier=None, query='', store=None,
                    sort='need_desc', page=1, page_size=50, state='active'):
    """Bounded presentation of saved decisions. Does not calculate recommendations."""
    from sqlalchemy import or_, and_, case, func
    session=_session(db,session_id,actor_id,lock=False)
    if page_size not in (20,50,100) or page<1:
        raise ValueError('Choose a valid page and page size')
    if state not in ('active','review','all','removed'):raise ValueError('Invalid product view')
    L=PurchasingSessionLine
    active_stores=db.scalars(select(Store).where(Store.active.is_(True)).order_by(Store.id)).all()
    stores=[{'id':str(row.id),'name':row.name} for row in active_stores]
    snapshot=db.execute(select(PurchasingSessionRevision.evidence['stores'],
        PurchasingSessionRevision.evidence['observed_at']).where(PurchasingSessionRevision.session_id==session.id,
        PurchasingSessionRevision.number==session.revision)).one()
    allocation_stores=[{'id':str(key),'name':name} for key,name in snapshot[0].items()]
    valid_store_ids={row['id'] for row in stores}
    if store and str(store) not in valid_store_ids:raise ValueError('Choose an active store')
    vendors=db.scalars(select(Vendor).where(Vendor.active.is_(True)).order_by(Vendor.name)).all()
    vendor_names={row.id:row.name for row in vendors}
    groups=_groups(db,session)
    totals={sid:(count,units,cost,missing) for sid,count,units,cost,missing in db.execute(select(
        L.supplier_id,func.count(L.id),func.sum(L.quantity),func.sum(L.unit_cost*L.quantity),
        func.sum(case((L.unit_cost.is_(None),1),else_=0))).where(
            L.session_id==session.id,L.removed.is_(False)).group_by(L.supplier_id))}
    pending_review=or_(L.unit_cost.is_(None),L.supplier_id.is_(None),and_(
        func.json_array_length(func.coalesce(L.proposed['review'],L.accepted['review']))>0,
        or_(L.decisions['review_revision'].as_integer().is_(None),L.decisions['review_revision'].as_integer()!=session.revision)))
    review_count=db.scalar(select(func.count(L.id)).where(L.session_id==session.id,L.removed.is_(False),pending_review)) or 0
    filters=[L.session_id==session.id,L.removed.is_(state=='removed')]
    if state=='active':filters.extend((L.quantity>0,~pending_review))
    if state=='review':filters.append(pending_review)
    for token in str(query).strip()[:200].casefold().split():
        filters.append(or_(*(func.lower(L.accepted[key].as_string()).contains(token,autoescape=True)
            for key in ('item_name','variation_name','sku'))))
    if store:
        need=L.accepted['forecasts'][str(store)]['raw_need'].as_integer()
        filters.append(or_(need>0,need.is_(None)))
    matches=dict(db.execute(select(L.supplier_id,func.count(L.id)).where(*filters).group_by(L.supplier_id)).all())
    group_views=[]
    for group in groups:
        count,units,cost,missing=totals.get(group.supplier_id,(0,0,Decimal(0),0))
        group_views.append({'id':group.supplier_id,'name':vendor_names.get(group.supplier_id,f'Supplier {group.supplier_id}'),
            'status':group.status,'po_id':group.purchase_order_id,'account_id':group.intended_account_id,
            'product_count':count,'units':int(units or 0),'cost':str(cost or 0) if not missing else None,
            'matched_count':matches.get(group.supplier_id,0)})
    if None in totals or None in matches:
        count,units,cost,missing=totals.get(None,(0,0,None,0))
        group_views.append({'id':0,'name':'Needs supplier','status':'OPEN','po_id':None,'account_id':None,
            'product_count':count,'units':int(units or 0),'cost':None,'matched_count':matches.get(None,0)})
    group_views.sort(key=lambda g:g['name'].casefold())
    orders={'need_desc':L.accepted['quantity'].as_integer().desc(),
        'quantity_desc':L.quantity.desc(),'quantity_asc':L.quantity.asc(),
        'cost_desc':L.unit_cost.desc().nullslast(),'cost_asc':L.unit_cost.asc().nullslast(),
        'total_cost_desc':(L.quantity*L.unit_cost).desc().nullslast(),
        'product':func.lower(L.accepted['item_name'].as_string())}
    if sort not in orders:raise ValueError('Choose a valid sort order')
    rows=[];total=0
    if supplier is not None:
        supplier=int(supplier)
        filters.append(L.supplier_id==supplier if supplier else L.supplier_id.is_(None))
        total=db.scalar(select(func.count(L.id)).where(*filters)) or 0
        pages=max(1,(total+page_size-1)//page_size);page=min(page,pages)
        lines=db.scalars(select(L).where(*filters).order_by(orders[sort],L.id).offset((page-1)*page_size).limit(page_size)).all()
        configured,historical=_cost_context(db,[line.variation_id for line in lines])
        by_supplier={g.supplier_id:g for g in groups}
        for line in lines:
            original=line.accepted;current=line.proposed or original
            group=by_supplier.get(line.supplier_id)
            reviewed=line.decisions.get('review_revision')==session.revision
            rows.append({'id':line.id,'variation_id':line.variation_id,'name':original['item_name'],
                'variant':original.get('variation_name',''),'sku':original.get('sku') or '',
                'supplier_id':line.supplier_id or 0,'canonical_vendor_id':line.canonical_vendor_id,
                'quantity':line.quantity,'need':original['quantity'],'forecasts':original.get('forecasts',{}),
                'observations':{sid:{key:values.get(key) for key in ('current_inventory','incoming','sales_complete','stockout_evidence_reliable','last_known_receipt_at','sources')} for sid,values in original.get('stores',{}).items()},'allocations':line.allocations,
                'supplier_lead_time_days':original.get('supplier_lead_time_days'),
                'cost':str(line.unit_cost) if line.unit_cost is not None else None,'cost_source':line.cost_source,
                'configured_cost':str(_cost(db,line.variation_id,line.supplier_id,configured=configured,historical=historical)[0]) if line.supplier_id and _cost(db,line.variation_id,line.supplier_id,configured=configured,historical=historical)[0] is not None else None,
                'decisions':line.decisions,'review':current.get('review',[]),
                'needs_review':bool(current.get('review')) and not reviewed or line.unit_cost is None or not line.supplier_id,
                'proposal':{'quantity':current['quantity'],'forecasts':current.get('forecasts',{})} if line.proposed and (current['quantity']!=original['quantity'] or current.get('forecasts')!=original.get('forecasts') or current.get('review')!=original.get('review') or current.get('unit_cost')!=original.get('unit_cost') or current.get('canonical_vendor_id')!=original.get('canonical_vendor_id')) else None,
                'latest_observations':{sid:{key:values.get(key) for key in ('current_inventory','incoming')} for sid,values in current.get('stores',{}).items()},
                'removed':line.removed,'editable':session.status=='ACTIVE' and not(group and group.status=='CONVERTED')})
    accounts=db.scalars(select(FundingAccount).where(FundingAccount.is_active.is_(True)).order_by(FundingAccount.display_name)).all()
    return {'id':session.id,'owner_id':actor_id,'created_at':session.created_at.isoformat(),'version':session.version,
        'revision':session.revision,'status':session.status,'settings':session.settings,'observed_at':snapshot[1],
        'stores':stores,'allocation_stores':allocation_stores,'vendors':[{'id':v.id,'name':v.name} for v in vendors],
        'accounts':[{'id':a.id,'name':a.display_name} for a in accounts],'groups':group_views,
        'rows':rows,'total':total,'page':page,'page_size':page_size,'pages':max(1,(total+page_size-1)//page_size),
        'matched_count':sum(matches.values()),'review_count':review_count,'removed_count':db.scalar(select(func.count(L.id)).where(L.session_id==session.id,L.removed.is_(True))) or 0}
