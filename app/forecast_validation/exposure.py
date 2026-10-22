"""Audit's forward-only exposure ledger, anchored only to observed absolute counts."""
from datetime import datetime,timedelta
from fractions import Fraction as F
from collections import defaultdict
from .contracts import TZ,stamp,number,EvidenceError
from app.services.v2_ordering_square_gateway import SquareOrderingReadGateway

class EvidenceGateway(SquareOrderingReadGateway):
    def fetch_verified_inventory_events(self,locations,variations,start,end):
        records=[]
        for offset in range(0,len(variations),100):
            cursor=None;seen=set()
            while True:
                payload={'location_ids':locations,'catalog_object_ids':variations[offset:offset+100],'types':['ADJUSTMENT','PHYSICAL_COUNT'],'states':['IN_STOCK','SOLD'],'updated_after':start.isoformat(),'updated_before':end.isoformat(),'limit':1000}
                if cursor:payload['cursor']=cursor
                result=self._read_post('/v2/inventory/changes/batch-retrieve',payload)
                if result.get('errors') or not isinstance(result.get('changes',[]),list):raise EvidenceError('Verified exposure source is incomplete')
                records.extend(result.get('changes',[]));cursor=result.get('cursor')
                if not cursor:break
                if cursor in seen:raise EvidenceError('Repeated exposure pagination cursor')
                seen.add(cursor)
        return records

def normalize(changes,cutoff):
    result=defaultdict(list);ids=set()
    for change in changes:
        kind=change.get('type');raw=change.get('physical_count' if kind=='PHYSICAL_COUNT' else 'adjustment',{})
        if kind not in ('PHYSICAL_COUNT','ADJUSTMENT'):raise EvidenceError('Invalid inventory source event')
        if kind=='PHYSICAL_COUNT' and raw.get('state')!='IN_STOCK':continue
        if kind=='ADJUSTMENT' and 'IN_STOCK' not in (raw.get('from_state'),raw.get('to_state')):continue
        q=F(number(raw.get('quantity'),nonnegative=True));occurred=stamp(raw['occurred_at']);known=max(stamp(raw['created_at']),stamp(raw.get('updated_at') or raw['created_at']))
        identity=(kind,raw.get('id'))
        if not identity[1] or identity in ids:raise EvidenceError('Duplicate or missing exposure source identity')
        ids.add(identity)
        if occurred>cutoff or known>cutoff:raise EvidenceError('Exposure event exceeds source cutoff')
        key=(raw['location_id'],raw['catalog_object_id'])
        # Retain reproducible event fields without employee identifiers or free-text notes.
        retained={k:v for k,v in raw.items() if k in {'id','location_id','catalog_object_id','quantity','occurred_at','created_at','updated_at','calculated_at','state','from_state','to_state','version'}}
        minimal={'type':kind,('physical_count' if kind=='PHYSICAL_COUNT' else 'adjustment'):retained}
        result[key].append({'id':raw['id'],'kind':kind,'time':occurred,'known':known,'quantity':q,'delta':q*((raw.get('to_state')=='IN_STOCK')-(raw.get('from_state')=='IN_STOCK')),'raw':minimal})
    for values in result.values():values.sort(key=lambda e:(e['time'],e['kind']=='ADJUSTMENT',e['id']))
    return result

def states(events,days):
    balance=None;index=0;out=[]
    for day in days:
        begin=datetime.combine(day,datetime.min.time(),TZ);finish=begin+timedelta(days=1)
        while index<len(events) and events[index]['time']<begin:
            e=events[index];balance=e['quantity'] if e['kind']=='PHYSICAL_COUNT' else balance+e['delta'] if balance is not None else None;index+=1
        unknown=balance is None;minimum=maximum=balance;physical=False
        while index<len(events) and events[index]['time']<finish:
            e=events[index];balance=e['quantity'] if e['kind']=='PHYSICAL_COUNT' else balance+e['delta'] if balance is not None else None
            physical|=e['kind']=='PHYSICAL_COUNT'
            if balance is None:unknown=True
            else:
                minimum=min(minimum,balance) if minimum is not None else balance;maximum=max(maximum,balance) if maximum is not None else balance
            index+=1
        out.append('UNKNOWN' if unknown or physical or minimum is not None and minimum<0 else 'IN_STOCK' if minimum is not None and minimum>0 else 'STOCKOUT' if maximum is not None and maximum<=0 else 'PARTIAL')
    return out

def closing_balance(events,cutoff):
    balance=None
    for e in events:
        if e['time']>cutoff:break
        balance=e['quantity'] if e['kind']=='PHYSICAL_COUNT' else balance+e['delta'] if balance is not None else None
    return balance
