"""HWY 99 receipt and automatic approved split; no arrival-reporting workflow.

Physical receipt, approved allocation, remote inventory outcome and commercial
obligation are distinct facts. All quantities are individual units.
"""
from collections import defaultdict
from datetime import datetime,timezone
from decimal import Decimal,InvalidOperation
from uuid import UUID,uuid5
from sqlalchemy import text
from . import controls,submission,worksheets
from .identity import digest
from .isolation import require_isolated
from .receiving_square import API_VERSION,payload as square_payload,quarantine_payload

CAPABILITIES={'VIEW','ENTER','CONFIRM','DISCREPANCY'}


def units(value):
    if type(value) is not int or not 0<=value<=2147483647:raise controls.InvalidPolicy('Use nonnegative whole individual units; blanks are unanswered')
    return value


def is_owner(c,actor):
    try:submission.owner(c,actor);return True
    except PermissionError:return False


def capability(c,actor,kind,store=None):
    require_isolated(c)
    active=c.execute(text('SELECT active AND NOT EXISTS(SELECT 1 FROM public.employees e WHERE e.principal_id=p.id AND NOT e.active) FROM public.principals p WHERE id=:id'),{'id':actor}).scalar_one_or_none()
    if not active:raise PermissionError('Active authenticated receiving principal required')
    if is_owner(c,actor):return
    row=c.execute(text('''SELECT enabled,store_id FROM purchasing_v4.receiving_grant WHERE principal_id=:actor AND capability=:kind
       ORDER BY created_at DESC,command_id DESC LIMIT 1'''),dict(actor=actor,kind=kind)).mappings().one_or_none()
    if not row or not row['enabled'] or (row['store_id'] is not None and row['store_id']!=store):raise PermissionError('Explicit '+kind.lower()+' receiving capability required')


def grant(c,*,actor_id,command_id,principal_id,kind,enabled,reason,store_id=None):
    submission.owner(c,actor_id)
    if kind not in CAPABILITIES or type(enabled) is not bool or not isinstance(reason,str) or not reason.strip():raise controls.InvalidPolicy('Explicit receiving capability, decision and reason required')
    cid=controls.uuid(command_id);value=dict(principal_id=principal_id,capability=kind,store_id=store_id,enabled=enabled,owner_id=actor_id,reason=reason)
    prior=c.execute(text('SELECT principal_id,capability,store_id,enabled,owner_id,reason FROM purchasing_v4.receiving_grant WHERE command_id=:id'),{'id':cid}).mappings().one_or_none()
    if prior:
        if dict(prior)!=value:raise controls.Conflict('Capability command identity already used with different fields')
        return dict(command_id=cid,replayed=True)
    c.execute(text('INSERT INTO purchasing_v4.receiving_grant(command_id,principal_id,capability,store_id,enabled,owner_id,reason) VALUES (:id,:principal_id,:capability,:store_id,:enabled,:owner_id,:reason)'),dict(id=cid,**value))
    return dict(command_id=cid,replayed=False)


def fixed_stores(c):
    stores=[dict(r) for r in c.execute(text('SELECT id,name,square_location_id,active FROM public.stores WHERE active ORDER BY id')).mappings()]
    central=[s for s in stores if s['name']=='HWY 99']
    if len(central)!=1 or not central[0]['square_location_id']:raise controls.InvalidPolicy('Resolve the existing HWY 99 store identity; no receiving-location setting exists')
    locations=[s['square_location_id'] for s in stores if s['square_location_id']]
    if len(locations)!=len(set(locations)):raise controls.InvalidPolicy('Conflicting active store Square locations require reconciliation')
    return stores,central[0]


def capture_order(c,po_id):
    prior=c.execute(text('SELECT snapshot FROM purchasing_v4.receiving_order WHERE purchase_order_id=:id'),{'id':po_id}).scalar_one_or_none()
    if prior:return prior
    row=c.execute(text('''SELECT v.id::text,v.snapshot FROM purchasing_v4.authorized_vendor v JOIN purchasing_v4.po_ownership o
      ON o.vendor_authorization_id=v.id WHERE v.purchase_order_id=:id AND v.kind='PO' AND o.origin='V4' '''),{'id':po_id}).mappings().one_or_none()
    if not row:raise controls.InvalidPolicy('Legacy PO remains with its original receiving owner; automatic adoption is prohibited')
    stores,central=fixed_stores(c);by_id={s['id']:s for s in stores}
    if any(s['id'] not in by_id or not by_id[s['id']]['square_location_id'] for s in row['snapshot']['stores']):raise controls.InvalidPolicy('Every approved destination must have one verified active store location')
    lines=[dict(r) for r in c.execute(text('''SELECT l.id,l.variation_id,l.ordered_qty,l.unit_cost,l.received_qty_total,l.item_name,l.variation_name,l.sku,b.row_id::text
       FROM public.purchase_order_lines l JOIN purchasing_v4.financial_line_bridge b ON b.purchase_order_line_id=l.id
       WHERE l.purchase_order_id=:id ORDER BY l.id'''),{'id':po_id}).mappings()]
    if not lines or any(l['received_qty_total'] for l in lines) or c.execute(text('SELECT EXISTS(SELECT 1 FROM public.purchase_order_receipts WHERE purchase_order_id=:id)'),{'id':po_id}).scalar_one():raise controls.InvalidPolicy('Prior receiving evidence requires explicit reconciliation; it cannot be replayed')
    approved={l['row_id']:l for l in row['snapshot']['lines']}
    for l in lines:
        l.pop('unit_cost');source=approved[l['row_id']]
        l['pack_size']=source['pack_size'];l['ordered']={str(s['id']):u for s,u in zip(row['snapshot']['stores'],source['units'])}
        if sum(l['ordered'].values())!=l['ordered_qty']:raise controls.InvalidPolicy('Approved original allocation does not conserve ordered units')
    snapshot=dict(central=central,stores=[by_id[s['id']] for s in row['snapshot']['stores']],lines=lines,contract='HWY99_RECEIPT_APPROVED_SPLIT_V1',api_version=API_VERSION,physical_semantics='Physical receipt is verified at HWY 99; destination inventory posting is not a destination-arrival attestation')
    c.execute(text('INSERT INTO purchasing_v4.receiving_order(purchase_order_id,vendor_authorization_id,central_store_id,snapshot) VALUES (:id,:vendor,:store,CAST(:snapshot AS jsonb))'),dict(id=po_id,vendor=row['id'],store=central['id'],snapshot=worksheets.js(snapshot)))
    c.execute(text('INSERT INTO purchasing_v4.receiving_head(purchase_order_id) VALUES (:id)'),{'id':po_id})
    return snapshot


def events(c,po_id):
    return [dict(r) for r in c.execute(text('SELECT id::text,version,actor_id,kind,payload,result,created_at FROM purchasing_v4.receiving_event WHERE purchase_order_id=:id ORDER BY version'),{'id':po_id}).mappings()]


def projection(c,po_id,*,actor_id=None):
    snap=c.execute(text('SELECT snapshot FROM purchasing_v4.receiving_order WHERE purchase_order_id=:id'),{'id':po_id}).scalar_one_or_none()
    if not snap:raise controls.InvalidPolicy('This PO has no HWY 99 receiving snapshot')
    if actor_id is not None:capability(c,actor_id,'VIEW',snap['central']['id'])
    head=c.execute(text('SELECT version FROM purchasing_v4.receiving_head WHERE purchase_order_id=:id'),{'id':po_id}).scalar_one();history=events(c,po_id)
    records={str(l['id']):dict(l,received_good=0,received_damaged=0,closed={k:0 for k in l['ordered']},overage={k:0 for k in l['ordered']},allocated={k:0 for k in l['ordered']},reallocated={k:0 for k in l['ordered']},resolved_damage=0,unassigned=0,discrepancies=[],balances=[],posting=[]) for l in snap['lines']}
    for e in history:
        for value in e['payload'].get('lines',[]):
            line=records[str(value['line_id'])]
            if e['kind']=='RECEIPT':line['received_good']+=value['good'];line['received_damaged']+=value['damaged']
            if e['kind'] in ('RECEIPT','ALLOCATE'):
                for k,v in value.get('split',{}).items():line['allocated'][k]+=v
            if e['kind']=='CLOSE':
                for k,v in value['stores'].items():line['closed'][k]+=v
            if e['kind']=='OVERAGE':
                for k,v in value['stores'].items():line['overage'][k]+=v
            if e['kind']=='CORRECTION':
                line['received_good']+=value.get('good_delta',0)
                for k,v in value.get('split_delta',{}).items():line['allocated'][k]+=v
                for k,v in value.get('approved_split_delta',{}).items():line['reallocated'][k]+=v
            if e['kind']=='DISCREPANCY':line['discrepancies'].append(dict(event_id=e['id'],units=value.get('units'),observed_product=value.get('observed_product'),description=value['description'],returned_units=0,complete=False))
            if e['kind']=='RESOLVE':
                line['resolved_damage']+=value.get('acknowledged_damage',0)
                for issue in line['discrepancies']:
                    if issue['event_id']==value.get('discrepancy_event_id'):
                        issue['returned_units']+=value['returned_units'];issue['complete']=value['complete']

    balances=c.execute(text('''SELECT b.* FROM purchasing_v4.inventory_balance b JOIN public.purchase_order_lines l ON l.id=b.purchase_order_line_id
      WHERE l.purchase_order_id=:id ORDER BY b.purchase_order_line_id,b.destination_store_id NULLS FIRST,b.account'''),{'id':po_id}).mappings()
    for b in balances:
        line=records[str(b['purchase_order_line_id'])];line['balances'].append(dict(store_id=b['destination_store_id'],account=b['account'],units=b['units']))
        if b['account']=='CENTRAL_UNASSIGNED':line['unassigned']+=b['units']
    for r in c.execute(text('''SELECT x.id::text,x.payload_digest,x.payload,x.predecessor_id::text,m.purchase_order_line_id,m.to_account,m.destination_store_id,s.state,s.evidence
      FROM purchasing_v4.inventory_command x JOIN purchasing_v4.inventory_operation s ON s.command_id=x.id
      JOIN purchasing_v4.inventory_movement m ON m.id=x.movement_id JOIN public.purchase_order_lines l ON l.id=m.purchase_order_line_id
      WHERE l.purchase_order_id=:id ORDER BY x.created_at,x.id'''),{'id':po_id}).mappings():records[str(r['purchase_order_line_id'])]['posting'].append(dict(r))
    for l in records.values():
        l['remaining']={k:max(l['ordered'][k]+l['overage'][k]+l['reallocated'][k]-l['allocated'][k]-l['closed'][k],0) for k in l['ordered']}
        l['supplier_outstanding']=max(sum(l['ordered'].values())+sum(l['overage'].values())-l['received_good']-sum(l['closed'].values()),0)
        l['damage_unresolved']=max(l['received_damaged']-l['resolved_damage'],0)
        l['posting_complete']=all(p['state']=='VERIFIED' for p in l['posting'])
        l['status']='Delivery discrepancy requires owner resolution' if any(not x['complete'] for x in l['discrepancies']) else 'Damaged delivery requires owner resolution' if l['damage_unresolved'] else 'Needs inventory reconciliation' if not l['posting_complete'] else 'Owner split needed' if l['unassigned'] else 'Supplier balance open' if l['supplier_outstanding'] else 'Owner closed remaining supplier balance' if any(l['closed'].values()) else 'Receipt and inventory split posted'
    if actor_id is not None and not is_owner(c,actor_id):
        for event in history:
            if event['kind']=='OVERAGE':
                event['payload']['reason']='Owner approved supplier overage quantities; financial valuation is restricted to the owner'
                for value in event['payload']['lines']:
                    for field in ('approved_total','currency','valuation_reason'):value.pop(field,None)
    allowed=[]
    if actor_id is not None:
        for name in CAPABILITIES:
            try:capability(c,actor_id,name,snap['central']['id']);allowed.append(name)
            except PermissionError:pass
    return dict(purchase_order_id=po_id,actor_id=actor_id,capabilities=sorted(allowed),version=head,snapshot=snap,lines=list(records.values()),history=history[-100:],owner=is_owner(c,actor_id) if actor_id is not None else False)


def _movement(c,eid,line,dest,source,target,count,sequence,*,remote=None,predecessor=None,source_store_id=None):
    if not count:return predecessor
    mid=str(uuid5(UUID(eid),'movement:'+str(sequence)));c.execute(text('INSERT INTO purchasing_v4.inventory_movement(id,event_id,purchase_order_line_id,destination_store_id,source_store_id,from_account,to_account,units) VALUES (:id,:event,:line,:store,:source_store,:source,:target,:units)'),dict(id=mid,event=eid,line=line['id'],store=dest,source_store=source_store_id,source=source,target=target,units=count))
    if remote is None:return predecessor
    cid=str(uuid5(UUID(mid),'square-inventory:v1'));body=quarantine_payload(cid,variation_id=line['variation_id'],location_id=remote['location'],units=count,occurred_at=remote['occurred_at']) if remote.get('quarantine') else square_payload(cid,variation_id=line['variation_id'],location_id=remote['location'],units=count,from_state=remote['from'],to_state=remote['to'],occurred_at=remote['occurred_at'],to_location_id=remote.get('to_location'))
    c.execute(text('INSERT INTO purchasing_v4.inventory_command(id,event_id,movement_id,api_version,payload,payload_digest,predecessor_id) VALUES (:id,:event,:movement,:version,CAST(:payload AS jsonb),:digest,:prior)'),dict(id=cid,event=eid,movement=mid,version=API_VERSION,payload=worksheets.js(body),digest=digest(body),prior=predecessor))
    c.execute(text("INSERT INTO purchasing_v4.inventory_operation(command_id,state) VALUES (:id,'PENDING')"),{'id':cid})
    return cid


def _split(c,eid,line,split,stores,central,seq,now,predecessor):
    for key,count in split.items():
        if not count:continue
        sid=int(key);own=sid==central['id'];seq+=1
        _movement(c,eid,line,sid,'CENTRAL_UNASSIGNED','CENTRAL_AVAILABLE' if own else 'CENTRAL_RESERVED',count,seq)
        if not own:
            seq+=1;predecessor=_movement(c,eid,line,sid,'CENTRAL_RESERVED','DESTINATION_AVAILABLE',count,seq,remote=dict(location=central['square_location_id'],to_location=stores[sid]['square_location_id'],**{'from':'IN_STOCK','to':'IN_STOCK'},occurred_at=now),predecessor=predecessor)
    return seq,predecessor


def command(c,*,actor_id,command_id,purchase_order_id,expected_version,kind,lines,reason,confirmed):
    """One immutable owner/employee receipt command; same UUID recovers its result."""
    require_isolated(c);cid=controls.uuid(command_id)
    if type(purchase_order_id) is not int or type(expected_version) is not int:raise controls.InvalidPolicy('Exact PO and receiving version required')
    if not isinstance(reason,str) or not reason.strip() or confirmed is not True:raise controls.InvalidPolicy('Explicit verified receipt/decision and explanation required')
    if kind not in {'RECEIPT','ALLOCATE','CLOSE','OVERAGE','DISCREPANCY','CORRECTION','RESOLVE'}:raise controls.InvalidPolicy('Unsupported receiving decision')
    if not isinstance(lines,list) or not 0<len(lines)<=1000:raise controls.InvalidPolicy('Review one to 1,000 receiving lines per submission')
    capability(c,actor_id,'VIEW',fixed_stores(c)[1]['id'])
    doc=dict(purchase_order_id=purchase_order_id,expected_version=expected_version,kind=kind,lines=lines,reason=reason,confirmed=confirmed,actor_id=actor_id);hashed=digest(doc)
    # PO-specific lock; no network or global worksheet lock is held here.
    c.execute(text('SELECT pg_advisory_xact_lock(73520405)'))
    c.execute(text('SELECT pg_advisory_xact_lock(73520707,:po)'),{'po':purchase_order_id})
    old=c.execute(text('SELECT payload_digest,result,actor_id FROM purchasing_v4.receiving_event WHERE id=:id'),{'id':cid}).mappings().one_or_none()
    if old:
        if old['payload_digest']!=hashed or old['actor_id']!=actor_id:raise controls.Conflict('This receiving command already identifies a different immutable decision')
        return dict(old['result'],replayed=True)
    snap=c.execute(text('SELECT snapshot FROM purchasing_v4.receiving_order WHERE purchase_order_id=:id'),{'id':purchase_order_id}).scalar_one_or_none()
    if not snap:snap=capture_order(c,purchase_order_id)
    capability(c,actor_id,'CONFIRM' if kind=='RECEIPT' else 'DISCREPANCY' if kind=='DISCREPANCY' else 'VIEW',snap['central']['id'])
    if kind=='RECEIPT':capability(c,actor_id,'ENTER',snap['central']['id'])
    if kind in {'ALLOCATE','CLOSE','OVERAGE','CORRECTION','RESOLVE'}:submission.owner(c,actor_id)
    actual_stores,actual_central=fixed_stores(c)
    actual={s['id']:s for s in actual_stores}
    if actual_central['id']!=snap['central']['id'] or any(s['id'] not in actual or actual[s['id']]['square_location_id']!=s['square_location_id'] for s in snap['stores']):raise controls.Conflict('Store location mapping changed; reconcile the pinned receiving and inventory identities before a new receipt')
    current=projection(c,purchase_order_id)
    if current['version']!=expected_version:raise controls.Conflict('Receiving or distribution changed; review the current quantities')
    by_id={str(l['id']):l for l in current['lines']};stores={s['id']:s for s in snap['stores']};normalized=[];seen=set()
    for entry in lines:
        if not isinstance(entry,dict) or str(entry.get('line_id')) not in by_id or str(entry['line_id']) in seen:raise controls.InvalidPolicy('Each exact PO line may appear once')
        key=str(entry['line_id']);seen.add(key);line=by_id[key];value=dict(entry,line_id=line['id'])
        if kind=='RECEIPT':
            if set(entry)-{'line_id','good','damaged','split'}:raise controls.InvalidPolicy('Unexpected receipt fields')
            value['good']=units(entry.get('good'));value['damaged']=units(entry.get('damaged'))
            if not value['good']+value['damaged']:raise controls.InvalidPolicy('Record only quantities physically received')
            if value['good']+value['damaged']>line['supplier_outstanding']:raise controls.InvalidPolicy('Supplier overage needs prior explicit owner quantity and valuation approval')
            split=entry.get('split')
            if split is None:
                split=line['remaining'] if value['good']==sum(line['remaining'].values()) and not line['unassigned'] else {k:0 for k in line['ordered']}
            else:
                submission.owner(c,actor_id)
            if not isinstance(split,dict) or set(split)!=set(line['ordered']):raise controls.InvalidPolicy('Use the original four-store allocation identities')
            split={k:units(v) for k,v in split.items()}
            if any(v>line['remaining'][k] for k,v in split.items()) or sum(split.values())>value['good']:raise controls.InvalidPolicy('Distribution cannot exceed the physical receipt or approved store balance')
            value['split']=split;value['owner_split_override']=entry.get('split') is not None
        elif kind=='ALLOCATE':
            if set(entry)!={'line_id','split'} or not isinstance(entry['split'],dict) or set(entry['split'])!=set(line['ordered']):raise controls.InvalidPolicy('Approve a split using the existing store allocations')
            split={k:units(v) for k,v in entry['split'].items()}
            if not sum(split.values()) or sum(split.values())>line['unassigned'] or any(v>line['remaining'][k] for k,v in split.items()):raise controls.InvalidPolicy('Approved split exceeds held stock or store allocation')
            value['split']=split
        elif kind=='CLOSE':
            if not line['posting_complete'] or line['unassigned'] or line['damage_unresolved']:raise controls.InvalidPolicy('Resolve pending postings, unassigned units and damaged supplier discrepancies before closure')
            if set(entry)!={'line_id','stores'} or set(entry['stores'])!=set(line['ordered']):raise controls.InvalidPolicy('Explicit line/store closure required')
            value['stores']={k:units(v) for k,v in entry['stores'].items()}
            if not sum(value['stores'].values()) or any(v>line['remaining'][k] for k,v in value['stores'].items()):raise controls.InvalidPolicy('Closure exceeds remaining supplier obligation')
        elif kind=='OVERAGE':
            if set(entry)!={'line_id','stores','currency','approved_total','valuation_reason'} or entry['currency']!='USD' or not isinstance(entry['valuation_reason'],str) or not entry['valuation_reason'].strip():raise controls.InvalidPolicy('Owner must approve excess units and an explicit separate USD valuation')
            if set(entry['stores'])!=set(line['ordered']):raise controls.InvalidPolicy('Original store identities required')
            value['stores']={k:units(v) for k,v in entry['stores'].items()}
            if not sum(value['stores'].values()):raise controls.InvalidPolicy('Positive excess approval required')
            try:amount=Decimal(entry['approved_total'])
            except (ValueError,InvalidOperation,TypeError):raise controls.InvalidPolicy('Explicit exact overage valuation required') from None
            if not isinstance(entry['approved_total'],str) or not amount.is_finite() or amount<0 or amount>=Decimal('1000000000000') or amount!=amount.quantize(Decimal('.01')):raise controls.InvalidPolicy('Explicit nonnegative USD cents required; no inferred overage price')
            value['approved_total']=format(amount,'.2f')
        elif kind=='DISCREPANCY':
            if set(entry)-{'line_id','description','units','observed_product'} or not isinstance(entry.get('description'),str) or not entry['description'].strip():raise controls.InvalidPolicy('Explain the observed discrepancy; never substitute product identities')
            value['units']=None if entry.get('units') is None else units(entry['units'])
            value['observed_product']=entry.get('observed_product') or None
            if value['units'] is not None and (not value['units'] or not isinstance(value['observed_product'],str) or not value['observed_product'].strip()):raise controls.InvalidPolicy('Positive observed units require a product description; unresolved identity remains quarantined without Square posting')
            if value['observed_product'] is not None and not isinstance(value['observed_product'],str):raise controls.InvalidPolicy('Observed product description must be literal text')
        elif kind=='RESOLVE':
            if 'discrepancy_event_id' in entry:
                if set(entry)!={'line_id','discrepancy_event_id','returned_units','physical_evidence','complete'} or type(entry['complete']) is not bool or not isinstance(entry['physical_evidence'],str) or not entry['physical_evidence'].strip():raise controls.InvalidPolicy('Owner must confirm the actual discrepancy return and whether the physical exception is fully resolved')
                issue=next((x for x in line['discrepancies'] if x['event_id']==controls.uuid(entry['discrepancy_event_id'])),None)
                value['returned_units']=units(entry['returned_units'])
                if not issue or issue['complete'] or not value['returned_units']:raise controls.InvalidPolicy('Select an unresolved retained delivery exception and verified positive return quantity')
                if issue['units'] is not None:
                    remaining=issue['units']-issue['returned_units']
                    if value['returned_units']>remaining or entry['complete']!=(value['returned_units']==remaining):raise controls.InvalidPolicy('Return and completion must reconcile the exact observed exception quantity')
            else:
                if set(entry)!={'line_id','acknowledged_damage','physical_evidence'} or not isinstance(entry['physical_evidence'],str) or not entry['physical_evidence'].strip():raise controls.InvalidPolicy('Owner must retain evidence and explicitly resolve damaged supplier discrepancies')
                value['acknowledged_damage']=units(entry['acknowledged_damage'])
                if not line['posting_complete'] or not 0<value['acknowledged_damage']<=line['damage_unresolved']:raise controls.InvalidPolicy('Resolve exact verified damaged quantities; unresolved posting commands cannot be closed')
        elif kind=='CORRECTION':
            required={'line_id','from_account','store_id','units','disposition','physical_evidence'}
            if set(entry)!=(required|({'to_store_id'} if entry.get('disposition')=='REALLOCATE_POSTED' else set())) or entry['disposition'] not in ('RETURN_FOR_REPLACEMENT','RECEIPT_COUNT_CORRECTION','INTERNAL_LOSS','REALLOCATE_POSTED') or not isinstance(entry['physical_evidence'],str) or not entry['physical_evidence'].strip():raise controls.InvalidPolicy('Explicit owner verified physical receipt, return, loss or store-split correction required')
            if not line['posting_complete']:raise controls.InvalidPolicy('Reconcile outstanding inventory operations before correcting physical custody')
            value['units']=units(entry['units'])
            if not value['units']:raise controls.InvalidPolicy('Positive correction quantity required')
            allowed={'CENTRAL_UNASSIGNED','CENTRAL_AVAILABLE','CENTRAL_QUARANTINE','DESTINATION_AVAILABLE'}
            if entry['from_account'] not in allowed or (entry['store_id'] is not None and str(entry['store_id']) not in line['ordered']):raise controls.InvalidPolicy('Correct only verified existing custody at an original store')
            available=sum(b['units'] for b in line['balances'] if b['account']==entry['from_account'] and b['store_id']==entry['store_id'])
            if value['units']>available:raise controls.InvalidPolicy('Physical correction exceeds accounted custody')
            if entry['disposition']!='RETURN_FOR_REPLACEMENT' and entry['from_account']=='CENTRAL_QUARANTINE':raise controls.InvalidPolicy('Quarantined merchandise is already unavailable; record its disposition without a second waste adjustment')
            value['good_delta']=-value['units'] if entry['disposition'] in ('RETURN_FOR_REPLACEMENT','RECEIPT_COUNT_CORRECTION') and entry['from_account']!='CENTRAL_QUARANTINE' else 0
            value['split_delta']={str(entry['store_id']):-value['units']} if value['good_delta'] and entry['store_id'] is not None else {}

            if entry['disposition']=='REALLOCATE_POSTED':
                if entry['from_account'] not in ('CENTRAL_AVAILABLE','DESTINATION_AVAILABLE') or type(entry.get('to_store_id')) is not int or str(entry['to_store_id']) not in line['ordered'] or entry['to_store_id']==entry['store_id']:raise controls.InvalidPolicy('Review distinct original stores and verified posted stock for a misallocation correction')
                value['split_delta']={str(entry['store_id']):-value['units'],str(entry['to_store_id']):value['units']}
                value['approved_split_delta']=value['split_delta']
        normalized.append(value)
    # Store submitted bytes separately from normalized execution facts in one event.
    result=dict(command_id=cid,purchase_order_id=purchase_order_id,version=expected_version+1,kind=kind,replayed=False,inventory_mode='ISOLATED_ONLY',posting_complete=False)
    payload=dict(doc,lines=normalized)
    c.execute(text('INSERT INTO purchasing_v4.receiving_event(id,purchase_order_id,version,actor_id,kind,payload,payload_digest,result) VALUES (:id,:po,:version,:actor,:kind,CAST(:payload AS jsonb),:digest,CAST(:result AS jsonb))'),dict(id=cid,po=purchase_order_id,version=expected_version+1,actor=actor_id,kind=kind,payload=worksheets.js(payload),digest=hashed,result=worksheets.js(result)))
    now=datetime.now(timezone.utc).isoformat();seq=0;central=snap['central']
    for value in normalized:
        line=by_id[str(value['line_id'])]
        prior=c.execute(text('''SELECT x.id::text FROM purchasing_v4.inventory_command x JOIN purchasing_v4.inventory_movement m ON m.id=x.movement_id
          JOIN purchasing_v4.receiving_event e ON e.id=x.event_id WHERE m.purchase_order_line_id=:line AND NOT EXISTS(SELECT 1 FROM purchasing_v4.inventory_command successor WHERE successor.predecessor_id=x.id) ORDER BY e.version DESC,x.created_at DESC,x.id DESC LIMIT 1'''),{'line':line['id']}).scalar_one_or_none()
        if kind=='RECEIPT':
            for count,account,to_state in [(value['good'],'CENTRAL_UNASSIGNED','IN_STOCK'),(value['damaged'],'CENTRAL_QUARANTINE','IN_STOCK')]:
                seq+=1;prior=_movement(c,cid,line,None,'SUPPLIER_EXTERNAL',account,count,seq,remote=dict(location=central['square_location_id'],**{'from':'NONE','to':to_state},occurred_at=now,quarantine=account=='CENTRAL_QUARANTINE'),predecessor=prior)
        if kind=='CORRECTION':
            source=value['from_account'];disposition=value['disposition'];dest=value['store_id'];target='RETURNED_EXTERNAL' if disposition=='RETURN_FOR_REPLACEMENT' else 'CORRECTED_EXTERNAL' if disposition=='RECEIPT_COUNT_CORRECTION' else 'DISCREPANCY'
            if disposition=='REALLOCATE_POSTED':
                seq+=1;prior=_movement(c,cid,line,value['to_store_id'],source,'CENTRAL_AVAILABLE' if value['to_store_id']==central['id'] else 'DESTINATION_AVAILABLE',value['units'],seq,source_store_id=value['store_id'],remote=dict(location=stores[value['store_id']]['square_location_id'],to_location=stores[value['to_store_id']]['square_location_id'],**{'from':'IN_STOCK','to':'IN_STOCK'},occurred_at=now),predecessor=prior)
                continue
            location=stores[dest]['square_location_id'] if source=='DESTINATION_AVAILABLE' or (source=='DISCREPANCY' and dest is not None and dest!=central['id']) else central['square_location_id']
            from_state='WASTE' if source in ('CENTRAL_QUARANTINE','DISCREPANCY') else 'IN_STOCK';to_state='NONE' if target in ('RETURNED_EXTERNAL','CORRECTED_EXTERNAL') else 'WASTE'
            if source=='CENTRAL_QUARANTINE' and disposition=='RETURN_FOR_REPLACEMENT':
                seq+=1;_movement(c,cid,line,dest,source,target,value['units'],seq)
                continue
            seq+=1;prior=_movement(c,cid,line,dest,source,target,value['units'],seq,remote=dict(location=location,**{'from':from_state,'to':to_state},occurred_at=now),predecessor=prior)
        if kind in ('RECEIPT','ALLOCATE'):seq,prior=_split(c,cid,line,value['split'],stores,central,seq,now,prior)
    c.execute(text('UPDATE purchasing_v4.receiving_head SET version=:version,event_id=:event,updated_at=now() WHERE purchase_order_id=:po'),dict(version=expected_version+1,event=cid,po=purchase_order_id))
    return result


def save_draft(c,*,actor_id,purchase_order_id,expected_draft_version,base_version,payload):
    snap=c.execute(text('SELECT snapshot FROM purchasing_v4.receiving_order WHERE purchase_order_id=:id'),{'id':purchase_order_id}).scalar_one_or_none()
    if not snap:raise controls.InvalidPolicy('Open the existing V4 purchase order first')
    capability(c,actor_id,'ENTER',snap['central']['id'])
    if type(expected_draft_version) is not int or type(base_version) is not int or base_version<0 or not isinstance(payload,dict):raise controls.InvalidPolicy('Exact receiving draft and source versions required')
    fields=payload.get('fields');touched=payload.get('split_touched')
    if set(payload)!={'fields','split_touched'} or not isinstance(fields,list) or len(fields)>40000 or any(not isinstance(v,list) or len(v)!=2 or any(not isinstance(x,str) or len(x)>4096 for x in v) for v in fields) or not isinstance(touched,list) or any(type(v) is not int for v in touched):raise controls.InvalidPolicy('Bounded literal receiving draft fields required')
    c.execute(text('SELECT pg_advisory_xact_lock(73520707,:po)'),{'po':purchase_order_id})
    current=c.execute(text('SELECT version FROM purchasing_v4.receiving_head WHERE purchase_order_id=:id'),{'id':purchase_order_id}).scalar_one()
    if current!=base_version:raise controls.Conflict('A new receipt changed this PO; review the draft against current balances')
    old=c.execute(text('SELECT version FROM purchasing_v4.receiving_draft WHERE purchase_order_id=:po AND actor_id=:actor FOR UPDATE'),dict(po=purchase_order_id,actor=actor_id)).scalar_one_or_none()
    if (old or 0)!=expected_draft_version:raise controls.Conflict('This receiving draft changed in another session')
    c.execute(text('''INSERT INTO purchasing_v4.receiving_draft(purchase_order_id,actor_id,version,base_version,payload)
      VALUES (:po,:actor,:version,:base,CAST(:payload AS jsonb)) ON CONFLICT(purchase_order_id,actor_id)
      DO UPDATE SET version=EXCLUDED.version,base_version=EXCLUDED.base_version,payload=EXCLUDED.payload,updated_at=now()'''),dict(po=purchase_order_id,actor=actor_id,version=expected_draft_version+1,base=base_version,payload=worksheets.js(payload)))
    return dict(version=expected_draft_version+1,base_version=base_version,receipt_submitted=False,inventory_commands_created=0)
