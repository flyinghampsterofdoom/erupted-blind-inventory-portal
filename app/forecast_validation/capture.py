"""Read-only source adapter. Runs only in an explicit isolated worker, never a page load."""
from collections import defaultdict
from datetime import datetime,timedelta,timezone,date
from decimal import Decimal
from fractions import Fraction as F
from sqlalchemy import select,text
from app.models import (Store,Vendor,VendorSkuConfig,ConsignmentSaleFact,PurchasingSaleCorrection,
 PurchasingInventoryEvent,PurchasingHistoryCoverage,ConsignmentSalesSyncState,
 PurchaseOrder,PurchaseOrderLine,PurchaseOrderStoreAllocation,PurchaseOrderReceiptAllocation,
 PurchaseOrderReceipt,PurchaseOrderReceiptLine,OrderingProductLifecycle,PurchasingOwnerDecision)
from .contracts import TZ,SCHEMA,stamp,number,EvidenceError,digest


def _record(source,ident,occurred,known,data,version=None):
    return {'source':source,'id':str(ident),'version':version,'occurred_at':stamp(occurred).isoformat(),'known_at':stamp(known).isoformat(),'data':data}

def _states(days,events,hand,origin,reliable):
    unknown=['UNKNOWN']*84
    if hand is None or not reliable:return unknown,unknown
    daily=defaultdict(F);physical=[]
    for ev in events:
        d=ev.business_date;daily[d]+=F(ev.quantity_delta)
        if ev.physical_count:physical.append(d)
    today=origin.astimezone(TZ).date();closing=F(hand)-daily[today];app=[]
    for day in reversed(days):
        opening=closing-daily[day];app.append('STOCKOUT' if opening<=0 and closing<=0 else 'IN_STOCK' if opening>0 and closing>0 else 'UNKNOWN');closing=opening
    app=list(reversed(app)) if not physical else unknown
    return app,unknown  # Verified exposure requires independent absolute-count anchors.

def capture(db,gateway,*,max_variations=1500,budget_seconds=180):
    import time
    start_clock=time.monotonic();began=datetime.now(timezone.utc)
    # No production write session can be passed accidentally.
    if db.bind.dialect.name=='postgresql':
        if db.execute(text('SHOW transaction_read_only')).scalar()!='on':raise EvidenceError('Source database connection must be read-only')
    elif db.execute(text('PRAGMA query_only')).scalar()!=1:raise EvidenceError('Source database connection must be read-only')
    current_day=began.astimezone(TZ).date();first=current_day-timedelta(days=84);days=[first+timedelta(days=i) for i in range(84)]
    aliases={'andresen':'Andresen','hwy99':'HWY 99','highway99':'HWY 99','sr503':'SR 503','longview':'Longview'};stores={}
    for s in db.scalars(select(Store).where(Store.active.is_(True))):
        key=''.join(c for c in s.name.lower() if c.isalnum())
        if key in aliases:
            if aliases[key] in {v['name'] for v in stores.values()} or not s.square_location_id:raise EvidenceError('Ambiguous store identity')
            stores[str(s.id)]={'name':aliases[key],'location_id':s.square_location_id}
    if len(stores)!=4:raise EvidenceError('Four store identities are required')
    catalog=gateway.fetch_active_catalog()
    if len(catalog)>max_variations:raise EvidenceError('Scope exceeds bounded variation budget; no partial publication')
    vids=sorted(catalog);locations={int(k):v['location_id'] for k,v in stores.items()}
    inventory=gateway.fetch_current_inventory_counts(location_ids=list(locations.values()),variation_ids=vids)
    observed=datetime.now(timezone.utc)
    from .exposure import normalize,states,closing_balance
    raw_exposure=gateway.fetch_verified_inventory_events(list(locations.values()),vids,datetime.combine(first,datetime.min.time(),TZ),observed) if hasattr(gateway,'fetch_verified_inventory_events') else None
    observed=datetime.now(timezone.utc)
    exposure=normalize(raw_exposure,observed) if raw_exposure is not None else {}
    vendor_candidates=defaultdict(list)
    for v in db.scalars(select(Vendor).where(Vendor.active.is_(True))):vendor_candidates[v.square_vendor_id].append(v)
    vendors={k:v[0] for k,v in vendor_candidates.items() if k and len(v)==1}
    lifecycle={r.square_variation_id:r for r in db.scalars(select(OrderingProductLifecycle).where(OrderingProductLifecycle.square_variation_id.in_(vids)))}
    receipts=[];lead_samples=defaultdict(dict);correction_totals=defaultdict(int)
    events=db.scalars(select(PurchaseOrderReceiptAllocation).where(PurchaseOrderReceiptAllocation.square_variation_id.in_(vids))).all()
    for e in events:
        if e.original_allocation_id:correction_totals[e.original_allocation_id]+=e.quantity_delta
    receipt_context=db.execute(select(PurchaseOrder,PurchaseOrderReceiptAllocation,PurchaseOrderReceipt).join(PurchaseOrderLine,PurchaseOrderLine.purchase_order_id==PurchaseOrder.id).join(PurchaseOrderReceiptAllocation,PurchaseOrderReceiptAllocation.purchase_order_line_id==PurchaseOrderLine.id).join(PurchaseOrderReceiptLine,PurchaseOrderReceiptLine.id==PurchaseOrderReceiptAllocation.receipt_line_id).join(PurchaseOrderReceipt,PurchaseOrderReceipt.id==PurchaseOrderReceiptLine.receipt_id).where(PurchaseOrderReceiptAllocation.square_variation_id.in_(vids))).all()
    for po,e,r in receipt_context:
        when=r.received_at or e.created_at
        if any(stamp(at)>observed for at in (when,e.created_at,po.ordered_at) if at is not None):
            raise EvidenceError('Receipt evidence is from the future; no forecast published')
        receipts.append(_record('actual_receipt',e.id,when,e.created_at,{'purchase_order_id':po.id,'vendor_id':po.vendor_id,'ordered_at':stamp(po.ordered_at).isoformat() if po.ordered_at else None,'received_at':stamp(when).isoformat(),'receipt_timestamp_source':'receipt.received_at' if r.received_at else 'allocation.created_at','variation_id':e.square_variation_id,'store_id':str(e.store_id),'kind':e.event_kind,'quantity_delta':e.quantity_delta,'closed':e.closed_quantity,'original_id':e.original_allocation_id}))
        if po.ordered_at and e.event_kind=='DELIVERY' and e.quantity_delta+correction_totals[e.id]>0:
            receipt_lead_days=(when-po.ordered_at).total_seconds()/86400
            if 0<=receipt_lead_days<=180:lead_samples[po.vendor_id][po.id]=min(receipt_lead_days,lead_samples[po.vendor_id].get(po.id,receipt_lead_days))
    from statistics import median
    leads={k:int(round(median(v.values()))) for k,v in lead_samples.items() if len(v)>=3}
    decisions=db.scalars(select(PurchasingOwnerDecision).where(PurchasingOwnerDecision.kind.in_(('COST','FINANCING')))).all()
    costs=defaultdict(list)
    for c in db.scalars(select(VendorSkuConfig).where(VendorSkuConfig.active.is_(True),VendorSkuConfig.square_variation_id.in_(vids))):costs[(c.square_variation_id,c.vendor_id)].append(c)
    facts=db.scalars(select(ConsignmentSaleFact).where(ConsignmentSaleFact.square_variation_id.in_(vids),ConsignmentSaleFact.business_date.between(first,current_day-timedelta(days=1)))).all()
    corrections=db.scalars(select(PurchasingSaleCorrection).where(PurchasingSaleCorrection.kind=='LINE',PurchasingSaleCorrection.variation_id.in_(vids),PurchasingSaleCorrection.business_date.between(first,current_day-timedelta(days=1)))).all()
    replaced=set(db.execute(select(PurchasingSaleCorrection.square_order_id,PurchasingSaleCorrection.square_line_item_uid).where(PurchasingSaleCorrection.kind=='LINE')).all());sales=defaultdict(lambda:defaultdict(F));records=defaultdict(list)
    for f in facts:
        if (f.square_order_id,f.square_line_item_uid) in replaced:continue
        key=(str(f.store_id),f.square_variation_id);sales[key][f.business_date]+=F(f.quantity_sold)
        records[key].append(_record('square_order_line',f.square_order_id+':'+f.square_line_item_uid,f.transacted_at,f.source_synchronized_at,{'quantity':str(f.quantity_sold),'business_date':str(f.business_date),'location_id':f.square_location_id},f.source_order_version))
    for c in corrections:
        sid=next((s for s,v in stores.items() if v['location_id']==c.location_id),None);key=(sid,c.variation_id);sales[key][c.business_date]+=F(c.quantity)
        records[key].append(_record('square_line_correction',c.square_order_id+':'+c.square_line_item_uid,datetime.combine(c.business_date,datetime.min.time(),TZ),c.synchronized_at,{'quantity':str(c.quantity),'reason':c.reason},c.order_version))
    movements=defaultdict(list)
    for ev in db.scalars(select(PurchasingInventoryEvent).where(PurchasingInventoryEvent.variation_id.in_(vids),PurchasingInventoryEvent.business_date.between(first,current_day),PurchasingInventoryEvent.calculated_before_at<=observed,PurchasingInventoryEvent.occurred_at<=observed)):
        sid=next((s for s,v in stores.items() if v['location_id']==ev.location_id),None);movements[(sid,ev.variation_id)].append(ev)
    scopes={(c.source,c.location_id):c for c in db.scalars(select(PurchasingHistoryCoverage))}
    sync=db.get(ConsignmentSalesSyncState,1)
    # Capture actual supplier commitments independently of forecast formulas.
    incoming=defaultdict(F);unknown=set();commitments=defaultdict(list)
    for po,line in db.execute(select(PurchaseOrder,PurchaseOrderLine).join(PurchaseOrderLine,PurchaseOrderLine.purchase_order_id==PurchaseOrder.id).where(PurchaseOrder.status.in_(('IN_TRANSIT','RECEIVED_SPLIT_PENDING')),PurchaseOrderLine.removed.is_(False))):
        if line.variation_id not in catalog:
            if not line.variation_id or str(line.variation_id).startswith('SKU::'):
                for vid,p in catalog.items():
                    if any(v.id==po.vendor_id and key in p.vendor_ids for key,v in vendors.items()):unknown.update((sid,vid) for sid in stores)
            continue
        allocations=db.scalars(select(PurchaseOrderStoreAllocation).where(PurchaseOrderStoreAllocation.purchase_order_line_id==line.id)).all()
        closed=dict(db.execute(select(PurchaseOrderReceiptAllocation.store_id, __import__('sqlalchemy').func.sum(PurchaseOrderReceiptAllocation.closed_quantity)).where(PurchaseOrderReceiptAllocation.purchase_order_line_id==line.id).group_by(PurchaseOrderReceiptAllocation.store_id)).all())
        total_closed=sum(closed.values());remaining=max(0,line.ordered_qty-(line.received_qty_total or 0)-total_closed)
        planned={str(a.store_id):max(0,a.allocated_qty-(a.store_received_qty or 0)-int(closed.get(a.store_id,0))) for a in allocations}
        if sum(planned.values())>remaining:
            total=sum(planned.values());exact={s:F(remaining*q,total) for s,q in planned.items()};planned={s:int(q) for s,q in exact.items()}
            for s in sorted(planned,key=lambda s:(-(exact[s]-planned[s]),int(s)))[:remaining-sum(planned.values())]:planned[s]+=1
        for sid in stores:
            key=(sid,line.variation_id)
            if not allocations:unknown.add(key)
            incoming[key]+=planned.get(sid,0)
            commitments[key].append(_record('purchase_commitment',str(po.id)+':'+str(line.id)+':'+sid,po.ordered_at or po.submitted_at or po.created_at,po.updated_at,{'purchase_order_id':po.id,'status':po.status.value,'ordered':line.ordered_qty,'received':line.received_qty_total,'allocated':planned.get(sid),'closed':int(closed.get(int(sid),0)),'remaining_cap':remaining,'source_allocations':[{'store_id':str(a.store_id),'allocated':a.allocated_qty,'received':a.store_received_qty,'closed':int(closed.get(a.store_id,0))} for a in allocations]}))
    rows=[]
    for vid,p in sorted(catalog.items()):
        relevant_decisions=[d for d in decisions if d.subject_key==vid or d.subject_key.startswith(vid+':')]
        vendor=vendors.get(p.vendor_ids[0]) if len(p.vendor_ids)==1 else None
        if any(d.kind=='FINANCING' for d in relevant_decisions):vendor=None
        configs=costs.get((vid,vendor.id),[]) if vendor else []
        pricing={(str(c.unit_cost),c.pack_size) for c in configs if c.unit_cost is not None}
        unit_cost,pack=next(iter(pricing)) if len(pricing)==1 else (None,1)
        if relevant_decisions:unit_cost=None  # Fail closed until owner decision basis is reconciled; never override it.
        for sid,store in stores.items():
            if time.monotonic()-start_clock>budget_seconds:raise TimeoutError('Source capture budget exceeded; no partial run')
            key=(sid,vid);loc=store['location_id'];count=inventory.counts.get((loc,vid));hand=count.quantity if count else None
            sales_scope=scopes.get(('SALES',loc));movement_scope=scopes.get(('MOVEMENTS',loc))
            def covered(c):return bool(c and stamp(c.start_at)<=datetime.combine(first,datetime.min.time(),TZ) and stamp(c.through_at)>=datetime.combine(current_day,datetime.min.time(),TZ) and timedelta(0)<=observed-stamp(c.verified_at)<=timedelta(hours=24))
            sales_complete=covered(sales_scope) and not (sync and sync.last_result=='FAILED')
            reliable=covered(movement_scope) and count is not None and count.calculated_at is not None and stamp(count.calculated_at)<=stamp(movement_scope.through_at) and all(not (stamp(e.calculated_after_at)<datetime.combine(first,datetime.min.time(),TZ)<stamp(e.calculated_before_at)) for e in movements[key])
            app,strict=_states(days,movements[key],hand,observed,reliable)
            if raw_exposure is not None:
                anchored=exposure.get((loc,vid),[])
                strict=states(anchored,days) if hand is not None and closing_balance(anchored,observed)==F(hand) else ['UNKNOWN']*84
            # Sales on a zero-boundary day make it unknown in the current model.
            app=[v if v!='STOCKOUT' or sales[key][d]==0 else 'UNKNOWN' for d,v in zip(days,app)]
            provenance=list(records[key])+list(commitments[key])
            for decision in relevant_decisions:provenance.append(_record('owner_source_decision',decision.kind+':'+decision.subject_key,decision.updated_at,decision.updated_at,decision.value,decision.version))
            provenance.append(_record('square_catalog_metadata',vid,p.created_at or observed,observed,{'item_id':p.item_id,'sku':p.sku,'name':p.item_name,'variation':p.variation_name,'vendor_ids':list(p.vendor_ids),'category_ids':list(p.category_ids),'updated_at':p.updated_at.isoformat() if p.updated_at else None}))
            for e in exposure.get((loc,vid),[]):provenance.append(_record('square_absolute_inventory_event',e['id'],e['time'],e['known'],e['raw'],e['raw'].get('version')))
            for ev in movements[key]:provenance.append(_record('inventory_event',ev.event_id,ev.occurred_at,ev.synchronized_at,{'type':ev.event_type,'delta':str(ev.quantity_delta),'physical_count':ev.physical_count,'calculated_before_at':stamp(ev.calculated_before_at).isoformat(),'calculated_after_at':stamp(ev.calculated_after_at).isoformat(),'digest':ev.source_digest}))
            if count:provenance.append(_record('square_inventory_count',loc+':'+vid,count.calculated_at or observed,observed,{'quantity':str(hand),'state':'IN_STOCK'}))
            for c in configs:provenance.append(_record('local_vendor_cost',c.id,c.created_at,c.updated_at,{'cost':str(c.unit_cost) if c.unit_cost is not None else None,'pack':c.pack_size,'vendor_id':c.vendor_id}))
            if vid in lifecycle:
                state=lifecycle[vid];provenance.append(_record('local_lifecycle',vid,state.updated_at,state.updated_at,{'status':state.status},state.row_version))
            for label,coverage in [('sales',sales_scope),('movements',movement_scope)]:
                if coverage:provenance.append(_record('coverage',label+':'+loc,coverage.start_at,coverage.verified_at,{'through_at':stamp(coverage.through_at).isoformat()}))
            age=max(0,(current_day-p.created_at.astimezone(TZ).date()).days) if p.created_at else None
            rows.append({'store_id':sid,'variation_id':vid,'product_id':getattr(p,'item_id',''),'sku':p.sku,'name':p.item_name,'variant':p.variation_name,'vendor_id':vendor.id if vendor else None,'category':'Unclassified' if not getattr(p,'category_ids',()) else ','.join(p.category_ids),'history_start':str(first),'sales':[str(Decimal(sales[key][d].numerator)/Decimal(sales[key][d].denominator)) for d in days],'sales_complete':bool(sales_complete),'availability':app,'verified_availability':strict,'on_hand':str(hand) if hand is not None else None,'incoming':None if key in unknown else str(incoming[key]),'unit_cost':unit_cost,'pack':int(pack),'pack_known':len(pricing)==1,'inventory_observed_at':observed.isoformat(),'inventory_calculated_at':stamp(count.calculated_at).isoformat() if count and count.calculated_at else None,'product_created_at':p.created_at.isoformat() if p.created_at else None,'product_age_days':age,'lifecycle':lifecycle[vid].status if vid in lifecycle else 'ACTIVE','lead_time_days':leads.get(vendor.id) if vendor else None,'source_records':provenance,'limitations':['Historical selling hours not verified','Verified exposure requires a historical absolute count and forward-ledger reconciliation to the current count; current counts never backfill it','Catalog category IDs retained; merchandising substitutions unknown']+(['Movement history incomplete'] if not reliable else [])+(['Owner decision source requires reconciliation; cost is unknown'] if relevant_decisions else [])})
    origin=datetime.now(timezone.utc)
    if origin.astimezone(TZ).date()!=current_day:raise EvidenceError('Capture crossed Pacific midnight; retry')
    return {'schema':SCHEMA,'origin':origin.isoformat(),'cutoff':origin.isoformat(),'evaluation_start':str(current_day+timedelta(days=1)),'kind':'PROSPECTIVE','scope':digest({'stores':stores,'variations':vids}),'stores':stores,'rows':rows,'source':'READ_ONLY_BUSINESS_DB_AND_SQUARE','source_budget':{'seconds':budget_seconds,'max_variations':max_variations},'observational_events':receipts+[c for values in commitments.values() for c in values]}

def capture_outcome(db,gateway,run,horizon,revision):
    """Read mature outcomes; inactive/deleted/missing source pairs remain UNKNOWN.

    The bounded collector retains 84 days. Capture within 28 days of 56-day maturity;
    older unsupported outcomes fail closed instead of inventing history.
    """
    from .contracts import validate
    snapshot=run['payload']['snapshot'];start=date.fromisoformat(snapshot['evaluation_start']);end=start+timedelta(days=horizon)
    now=datetime.now(timezone.utc)
    if horizon not in (28,56) or now<datetime.combine(end,datetime.min.time(),TZ):raise EvidenceError('Outcome not mature')
    fresh=capture(db,gateway);validate(fresh,now=datetime.now(timezone.utc))
    history_start=date.fromisoformat(fresh['rows'][0]['history_start'])
    if start<history_start:raise EvidenceError('Outcome outside bounded retained collection window; provide an independently certified evidence file')
    if fresh['stores']!=snapshot['stores']:raise EvidenceError('Store identities changed; manual evidence reconciliation required')
    available={(r['store_id'],r['variation_id']):r for r in fresh['rows']};rows=[]
    for expected in snapshot['rows']:
        key=(expected['store_id'],expected['variation_id']);r=available.get(key)
        item={'store_id':key[0],'variation_id':key[1],'complete':bool(r and r['sales_complete']),'days':[],'source_records':r['source_records'] if r else []}
        if r:
            offset=(start-history_start).days
            item['days']=[{'date':str(start+timedelta(days=i)),'units':r['sales'][offset+i],'availability':r['verified_availability'][offset+i]} for i in range(horizon)]
        rows.append(item)
    return {'horizon':horizon,'revision':revision,'observed_at':fresh['origin'],'cutoff':fresh['cutoff'],'rows':rows,'observational_events':fresh['observational_events'],'collection_limitations':['84-day bounded source history','Deleted catalog variations abstain; source revisions are captured at outcome collection time']}
