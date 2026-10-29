"""Shared pure Phase1/Phase2 source projections; no second evidence authority."""
from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo
from .contracts import QuantityEvidence, EvidenceState, Availability, classify_interval
from .identity import valid_reference

BUSINESS_ZONE=ZoneInfo('America/Los_Angeles')

# Reuse the collector's durable per-pair proof, not its unrelated 24-hour UI
# freshness preference. The caller still supplies the approved evidence age.
INVENTORY_SQL='''SELECT i.*,r.result,r.started_at,r.completed_at,r.error_code,
 r.expected_pair_count,r.covered_pair_count,r.missing_pair_count,proof.certificates
 FROM public.ordering_current_inventory i JOIN public.ordering_inventory_refresh_runs r ON r.id=i.refresh_run_id
 LEFT JOIN LATERAL (SELECT jsonb_agg(to_jsonb(p) ORDER BY p.id) AS certificates
   FROM public.purchasing_evidence_runs p WHERE p.source='INVENTORY'
   AND p.started_at=r.started_at AND p.completed_at=r.completed_at) proof ON true'''


def verified_partial_pair(row,variation_id,store_id):
    certificates=row.get('certificates')
    if row['result']!='PARTIAL' or row.get('error_code')!='PARTIAL_COVERAGE' or not isinstance(certificates,list) or len(certificates)!=1:return False
    proof=certificates[0]
    if not isinstance(proof,dict) or type(proof.get('id')) is not int:return False
    scope=proof.get('scope') or {};evidence=proof.get('evidence') or {}
    if not isinstance(scope,dict) or not isinstance(evidence,dict):return False
    if proof.get('source')!='INVENTORY' or proof.get('status')!='PARTIAL' or evidence.get('error_code')!='PARTIAL_COVERAGE':return False
    try:
        if datetime.fromisoformat(proof['started_at'])!=row['started_at'] or datetime.fromisoformat(proof['completed_at'])!=row['completed_at']:return False
        if not 0<row['covered_pair_count']<row['expected_pair_count'] or row['covered_pair_count']+row['missing_pair_count']!=row['expected_pair_count']:return False
        if variation_id not in scope['variation_ids'] or str(store_id) not in {str(s) for s in scope['store_ids'] if type(s) is int}:return False
        return any(isinstance(pair,list) and len(pair)==2 and pair[0]==variation_id and type(pair[1]) is int and str(pair[1])==str(store_id) for pair in evidence['observed_pairs'])
    except (KeyError,TypeError,ValueError):return False

INCOMING_SQL='''SELECT l.id, p.id AS purchase_order_id, l.variation_id, l.sku,
 l.ordered_qty,l.received_qty_total,a.store_id,a.allocated_qty,a.store_received_qty,
 COALESCE((SELECT sum(c.closed_quantity) FROM public.purchase_order_receipt_allocations c
           WHERE c.purchase_order_line_id=l.id AND c.store_id=a.store_id),0) AS closed
 FROM public.purchase_order_lines l JOIN public.purchase_orders p ON p.id=l.purchase_order_id
 LEFT JOIN public.purchase_order_store_allocations a ON a.purchase_order_line_id=l.id
 WHERE p.status IN ('IN_TRANSIT','RECEIVED_SPLIT_PENDING') AND NOT l.removed
 ORDER BY l.id,a.store_id'''


def coverage_status(row,failed,*,start,end,as_of,max_age):
    if not isinstance(max_age,timedelta) or max_age<timedelta():raise ValueError('Explicit nonnegative source age required')
    if failed:return EvidenceState.UNAVAILABLE,'SOURCE_REFRESH_FAILED',None
    if row is None:return EvidenceState.UNAVAILABLE,'NO_COVERAGE',None
    verified=row['verified_at']
    if verified<row['through_at']:return EvidenceState.UNKNOWN,'INVALID_COVERAGE_CERTIFICATE',verified
    if verified>as_of:return EvidenceState.UNKNOWN,'COVERAGE_AFTER_CUTOFF',verified
    if as_of-verified>max_age:return EvidenceState.UNAVAILABLE,'STALE_COVERAGE',verified
    if row['start_at']>start or row['through_at']<end:return EvidenceState.UNKNOWN,'INCOMPLETE_COVERAGE',verified
    return EvidenceState.KNOWN,'',verified


def inventory_observation(row,location,*,variation_id,store_id,as_of,max_age):
    if not isinstance(max_age,timedelta) or max_age<timedelta():raise ValueError('Explicit nonnegative source age required')
    source='legacy.square_inventory_observation'
    if row is None:return QuantityEvidence(EvidenceState.UNAVAILABLE,None,source,'NO_OBSERVATION')
    pair_verified=verified_partial_pair(row,variation_id,store_id)
    reason=''
    if not location or row['square_location_id']!=location:reason='UNRESOLVED_STORE_LOCATION'
    elif row['refreshed_at']>as_of or row['completed_at']>as_of:reason='OBSERVATION_AFTER_CUTOFF'
    elif not row['started_at']<=row['refreshed_at']<=row['completed_at']:reason='INVALID_REFRESH_MEMBERSHIP'
    elif row['source_calculated_at'] is not None and row['source_calculated_at']>row['refreshed_at']:
        reason='SOURCE_CALCULATION_AFTER_OBSERVATION'
    elif (row['result']!='COMPLETE' and not pair_verified) or row['freshness_state']!='FRESH':reason='INCOMPLETE_OR_STALE_SOURCE'
    elif as_of-row['refreshed_at']>max_age:reason='STALE_OBSERVATION'
    try:value=Decimal(str(row['counted_quantity']))
    except Exception:value=None
    if value is None or not value.is_finite():reason='INVALID_INVENTORY_QUANTITY'
    return QuantityEvidence(EvidenceState.UNKNOWN if reason else EvidenceState.KNOWN,
        None if reason else value,source,reason,row['refreshed_at'],
        (f'ordering_current_inventory:{variation_id}/{store_id}',f'ordering_inventory_refresh_runs:{row["refresh_run_id"]}')+
        ((f'purchasing_evidence_runs:{row["certificates"][0]["id"]}',) if pair_verified else ()))


def incoming_quantity(rows,variation_id):
    """Return all-store projection; store choices never proportionally inferred."""
    source='legacy.po_allocations';lines={}
    for row in rows:
        if not valid_reference(row['variation_id']):
            if (type(row['ordered_qty']) is int and type(row['received_qty_total']) is int
                    and 0<=row['ordered_qty']<=row['received_qty_total']):continue
            return None,'UNRESOLVED_OPEN_PO_IDENTITY',()
        if row['variation_id']==variation_id:lines.setdefault(row['id'],[]).append(row)
    references=tuple(f'purchase_order_lines:{line_id}' for line_id in sorted(lines))
    by_store={}
    for allocations in lines.values():
        first=allocations[0]
        values=[first['ordered_qty'],first['received_qty_total']]
        values += [a[c] for a in allocations for c in ('allocated_qty','store_received_qty','closed')]
        if any(type(v) is not int or v<0 for v in values) or any(a['store_id'] is None for a in allocations):
            return None,'INCOMPLETE_INCOMING_BALANCES',references
        if len({a['store_id'] for a in allocations})!=len(allocations):return None,'DUPLICATE_STORE_ALLOCATION',references
        if (sum(a['allocated_qty'] for a in allocations)!=first['ordered_qty'] or
                sum(a['store_received_qty'] for a in allocations)!=first['received_qty_total']):
            return None,'CONFLICTING_INCOMING_ALLOCATION',references
        if sum(a['closed'] for a in allocations)>max(first['ordered_qty']-first['received_qty_total'],0):
            return None,'INVALID_CLOSURE_BALANCE',references
        remaining=[max(a['allocated_qty']-a['store_received_qty']-a['closed'],0) for a in allocations]
        total=max(first['ordered_qty']-first['received_qty_total']-sum(a['closed'] for a in allocations),0)
        if sum(remaining)!=total:return None,'CONFLICTING_INCOMING_ALLOCATION',references
        for a,value in zip(allocations,remaining):by_store[a['store_id']]=by_store.get(a['store_id'],0)+value
    return by_store,'',references


def availability_projection(*,location,inventory,anchor_row,coverage,events,boundary,
                            sales,start_day,end_day,as_of):
    start=datetime.combine(start_day,time(),BUSINESS_ZONE);end=datetime.combine(end_day,time(),BUSINESS_ZONE)
    anchor=inventory.observed_at;state,reason,_=coverage
    if end>datetime.combine(as_of.astimezone(BUSINESS_ZONE).date(),time(),BUSINESS_ZONE):
        raise ValueError('Only completed availability days are supported')
    if not location:state,reason=EvidenceState.UNKNOWN,'UNRESOLVED_STORE_LOCATION'
    if inventory.state!=EvidenceState.KNOWN or anchor is None or anchor<end:
        if state!=EvidenceState.UNAVAILABLE:state,reason=EvidenceState.UNKNOWN,'NO_QUALIFIED_INVENTORY_ANCHOR'
    if anchor_row and (anchor_row['source_calculated_at'] is None or anchor_row['source_calculated_at']>anchor):
        state,reason=EvidenceState.UNKNOWN,'UNQUALIFIED_COUNT_TIMESTAMP'
    if anchor and any(e['synchronized_at']>as_of or e['calculated_before_at']>anchor
                      or e['occurred_at']>anchor for e in events):
        state,reason=EvidenceState.UNKNOWN,'UNQUALIFIED_MOVEMENT_MEMBERSHIP'
    if anchor and anchor_row and any(e['occurred_at']>=anchor_row['started_at'] for e in events):
        state,reason=EvidenceState.UNKNOWN,'MOVEMENT_DURING_COUNT_READ'
    if boundary:state,reason=EvidenceState.UNKNOWN,'UNCERTAIN_BOUNDARY_MOVEMENT'
    output=[]
    for n in range((end_day-start_day).days):
        day=start_day+timedelta(days=n);status=Availability.UNKNOWN;used=();day_reason=reason
        if state==EvidenceState.KNOWN:
            begin=datetime.combine(day,time(),BUSINESS_ZONE)
            finish=datetime.combine(day+timedelta(days=1),time(),BUSINESS_ZONE)
            later=[e for e in events if begin<=e['occurred_at']<=anchor]
            if any(e['physical_count'] for e in later):
                output.append((day,Availability.UNKNOWN,EvidenceState.UNKNOWN,'PHYSICAL_COUNT_BETWEEN_DAY_AND_ANCHOR',tuple(e['event_id'] for e in later)))
                continue
            opening=inventory.value-sum((e['quantity_delta'] for e in later),Decimal())
            within=[e for e in later if e['occurred_at']<finish]
            if len({e['occurred_at'] for e in within})==len(within):
                status=classify_interval(QuantityEvidence(EvidenceState.KNOWN,opening,'trajectory'),
                    tuple(e['quantity_delta'] for e in within),complete=True)
            used=tuple(e['event_id'] for e in later)
            if status==Availability.STOCKOUT and sales[day].state==EvidenceState.KNOWN and sales[day].value>0:
                status=Availability.UNKNOWN;day_reason='CONTRADICTORY_SALES_DURING_STOCKOUT'
        output.append((day,status,EvidenceState.KNOWN if status!=Availability.UNKNOWN else
                       state if state!=EvidenceState.KNOWN else EvidenceState.UNKNOWN,
                       day_reason or ('MIXED_OR_AMBIGUOUS_INTERVAL' if status==Availability.UNKNOWN else ''),used))
    return tuple(output)
