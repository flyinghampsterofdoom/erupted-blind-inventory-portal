"""Audit's forward-only exposure ledger, anchored only to observed absolute counts."""
from datetime import datetime,timedelta,timezone
from fractions import Fraction as F
from collections import defaultdict
from .contracts import TZ,stamp,number,EvidenceError
from app.services.v2_ordering_square_gateway import SquareOrderingReadGateway

class EvidenceGateway(SquareOrderingReadGateway):
    def _read_post(self,path,payload):
        began=datetime.now(timezone.utc)
        response=super()._read_post(path,payload)
        if not hasattr(self,'page_evidence'):
            self.page_evidence=[];self.catalog_versions={};self.catalog_signatures={};self.count_signatures={}
        from .contracts import digest
        field='items' if 'catalog' in path else 'counts' if '/counts/' in path else 'changes'
        if response.get('errors') or not isinstance(response.get(field,[]),list):raise EvidenceError('Incomplete Square source page')
        if field=='items':
            for item in response.get('items',[]):
                for v in (item.get('item_data') or {}).get('variations',[]):
                    vid=v.get('id');signature=digest({'item_id':item.get('id'),'variation':v,'item_data':item.get('item_data')})
                    if vid in self.catalog_signatures and self.catalog_signatures[vid]!=signature:raise EvidenceError('Catalog changed during pagination')
                    self.catalog_signatures[vid]=signature;self.catalog_versions[vid]={'item_version':item.get('version'),'variation_version':v.get('version'),'updated_at':v.get('updated_at') or item.get('updated_at')}
        elif field=='counts':
            for v in response.get('counts',[]):
                key=(v.get('location_id'),v.get('catalog_object_id'),v.get('state'));signature=digest(v)
                if key in self.count_signatures and self.count_signatures[key]!=signature:raise EvidenceError('Inventory changed across duplicate source records')
                self.count_signatures[key]=signature
        self.page_evidence.append({'path':path,'query':{k:v for k,v in payload.items() if k!='cursor'},'request_cursor_sha256':digest(payload['cursor']) if payload.get('cursor') else None,'started_at':began.isoformat(),'completed_at':datetime.now(timezone.utc).isoformat(),'records':len(response.get(field,[])),'response_sha256':digest(response),'next_cursor_sha256':digest(response['cursor']) if response.get('cursor') else None})
        return response

    def fetch_verified_inventory_events(self,locations,variations,start,end):
        records=[];self.history_batches=[]
        for offset in range(0,len(variations),100):
            cursor=None;seen=set();pages=0;count=0
            while True:
                payload={'location_ids':locations,'catalog_object_ids':variations[offset:offset+100],'types':['ADJUSTMENT','PHYSICAL_COUNT'],'states':['IN_STOCK','SOLD'],'updated_after':start.isoformat(),'updated_before':end.isoformat(),'limit':100}
                if cursor:payload['cursor']=cursor
                result=self._read_post('/v2/inventory/changes/batch-retrieve',payload)
                if result.get('errors') or not isinstance(result.get('changes',[]),list):raise EvidenceError('Verified exposure source is incomplete')
                pages+=1;count+=len(result.get('changes',[]));records.extend(result.get('changes',[]));cursor=result.get('cursor')
                if pages>400 or len(records)>100000:raise EvidenceError('Inventory source size bound exceeded')
                if not cursor:break
                if cursor in seen:raise EvidenceError('Repeated exposure pagination cursor')
                seen.add(cursor)
            self.history_batches.append({'offset':offset,'variations':len(variations[offset:offset+100]),'pages':pages,'records':count,'pagination_exhausted':True})
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
