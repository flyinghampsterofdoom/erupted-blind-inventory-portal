"""Isolated forward availability proof from retained absolute counts; never a production writer.

Require complete source coverage separately, exact current-anchor reconciliation,
known inventory, and immutable raw count hashes. Inferred count adjustments are
excluded by explicit source linkage so the same count is never applied twice.
"""
from datetime import datetime,time,timedelta
from decimal import Decimal

def forward_days(events,*,start_day,end_day,anchor,zone):
 events=[e for e in events if e['time']<=anchor]
 events.sort(key=lambda e:(e['time'],e['id']))
 balance=None;index=0;output={}
 def apply_group(group,b):
  physical=[e for e in group if e['physical']]
  if physical:
   if len(group)!=1:return None,True
   return physical[0]['absolute'],False
  return (b+sum((e['delta'] for e in group),Decimal()) if b is not None else None),False
 # A source physical count before the window can anchor later evidence, never earlier.
 while index<len(events) and events[index]['time']<datetime.combine(start_day,time(),zone):
  at=events[index]['time'];group=[]
  while index<len(events) and events[index]['time']==at:group.append(events[index]);index+=1
  balance,_=apply_group(group,balance)
 for n in range((end_day-start_day).days):
  day=start_day+timedelta(days=n);finish=datetime.combine(day+timedelta(days=1),time(),zone);opening=balance;minimum=balance;maximum=balance;uncertain=balance is None;physical=False;ties=False;refs=[]
  while index<len(events) and events[index]['time']<finish:
   at=events[index]['time'];group=[]
   while index<len(events) and events[index]['time']==at:group.append(events[index]);index+=1
   balance,ambiguous=apply_group(group,balance);physical|=any(e['physical'] for e in group);ties|=len(group)>1;refs.extend(e['id'] for e in group)
   if balance is None:uncertain=True
   else:minimum=balance if minimum is None else min(minimum,balance);maximum=balance if maximum is None else max(maximum,balance)
  state=-1
  if not uncertain and not physical and not ties and minimum is not None and minimum>=0:
   if minimum>0:state=1
   elif maximum==0:state=0
  output[day]=dict(state=state,physical_day=physical,ambiguous_order=ties,opening=opening,closing=balance,minimum=minimum,references=refs)
 # Reconcile to the actual same-vintage current inventory anchor, including partial current day.
 while index<len(events):
  at=events[index]['time'];group=[]
  while index<len(events) and events[index]['time']==at:group.append(events[index]);index+=1
  balance,_=apply_group(group,balance)
 return output,balance


def read_absolute_counts(fetch, movement_source, *, as_of):
    """Reuse immutable collector payloads; legacy-only databases stay readable."""
    if movement_source not in ('purchasing_evidence.shared_inventory_events',
                               'purchasing_v4.shared_inventory_events'):
        return {}
    schema=movement_source.split('.')[0]
    rows=fetch(f'''SELECT x.value AS change FROM {schema}.collection_page p
      JOIN {schema}.collection_attempt a ON a.id=p.attempt_id
      CROSS JOIN LATERAL jsonb_array_elements(coalesce(p.payload#>'{{response,changes}}','[]'::jsonb)) x(value)
      WHERE a.completed_at<=:cutoff AND x.value->>'type'='PHYSICAL_COUNT' ''',cutoff=as_of)
    from app.services.daily_square_collector import digest
    return {digest(r['change']):r['change']['physical_count'] for r in rows}


def reconcile_projection(base, *, absolute_counts, events, inventory, anchor_row,
                         coverage, sales, start_day, end_day, as_of, zone):
    """Fill only UNKNOWN intervals after a genuine count and exact reconciliation.

    Known state conflicts fail closed. No guessed opening balance, future count,
    reset-day exposure or missing inventory is converted to an observation.
    """
    from .contracts import Availability,EvidenceState
    from .identity import digest
    if not absolute_counts or inventory.state!=EvidenceState.KNOWN or coverage[0]!=EvidenceState.KNOWN:
        return base
    anchor=inventory.observed_at
    if anchor is None or anchor>as_of or not anchor_row:return base
    if any(e['occurred_at']>anchor or e['synchronized_at']>as_of or
           e['source_updated_at']>as_of or e['calculated_before_at']>anchor or
           e['occurred_at']>=anchor_row['started_at'] for e in events):return base
    begin=datetime.combine(start_day,time(),zone)
    scope=[e for e in events if e['occurred_at']>=begin]
    inferred=set();physical={}
    for e in scope:
        if not e['physical_count']:continue
        count=absolute_counts.get(e['source_digest'])
        if not count or count.get('id')!=e['event_id'] or count.get('state')!='IN_STOCK':return base
        if (count.get('catalog_object_id'),count.get('location_id'))!=(e['variation_id'],e['location_id']):return base
        from app.services.daily_square_collector import instant
        try:
            if not isinstance(count['quantity'],str):return base
            if instant(count['occurred_at'])!=e['occurred_at']:return base
            if instant(count.get('updated_at') or count['created_at'])!=e['source_updated_at']:return base
            quantity=Decimal(count['quantity'])
        except (KeyError,TypeError,ValueError,ArithmeticError):return base
        if not quantity.is_finite() or quantity<0:return base
        physical[e['event_id']]=quantity
        if count.get('adjustment_id'):inferred.add(count['adjustment_id'])
    timeline=[dict(time=e['occurred_at'],id=e['event_id'],physical=e['physical_count'],
                   absolute=physical.get(e['event_id']),delta=e['quantity_delta'])
              for e in scope if e['physical_count'] or e['event_id'] not in inferred]
    days,closing=forward_days(timeline,start_day=start_day,end_day=end_day,anchor=anchor,zone=zone)
    if closing is None or closing!=inventory.value:return base
    watermark='absolute-forward:'+digest([(e['event_type'],e['event_id'],e['source_digest']) for e in scope])
    output=[];conflict=False
    for day,old,state,reason,refs in base:
        inferred_state=days[day]['state']
        quantity=sales[day]
        if quantity.state!=EvidenceState.KNOWN:inferred_state=-1
        if inferred_state==0 and quantity.value>0:inferred_state=-1
        new=Availability.AVAILABLE if inferred_state==1 else Availability.STOCKOUT if inferred_state==0 else Availability.UNKNOWN
        if old!=Availability.UNKNOWN and new!=Availability.UNKNOWN and old!=new:conflict=True
        if old==Availability.UNKNOWN and new!=Availability.UNKNOWN:
            output.append((day,new,EvidenceState.KNOWN,'',tuple(inventory.references)+(watermark,)))
        else:output.append((day,old,state,reason,refs))
    if conflict:
        return tuple((d,Availability.UNKNOWN,EvidenceState.UNKNOWN,'CONFLICTING_ABSOLUTE_PROOF',(watermark,))
                     for d,_,_,_,_ in base)
    return tuple(output)
