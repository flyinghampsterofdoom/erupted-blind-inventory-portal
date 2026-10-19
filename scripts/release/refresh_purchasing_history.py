"""Bounded operational invocation of reviewed code. No PO/payment/Square writer.

Runs as a finite Render job, not inside the web handler. This file is not added
as an application feature or recurring job. Only hashes/counts/timings are logged.
"""
import json,time,signal,hashlib,resource,sys,urllib.request,urllib.parse,os
from scripts.release.history_refresh_result import require_current, failure_message, safe_diagnostic
from scripts.release.rfc3339 import timestamp
from datetime import datetime,timedelta,timezone
from decimal import Decimal
from collections import defaultdict
from sqlalchemy import select,text
from app.services.runtime_identity_service import capture_identity
EXPECTED=os.environ['ERUPTED_EXPECTED_REVISION']
EXPECTED_DIGEST=os.environ['ERUPTED_EXPECTED_SOURCE_SHA256']
identity=capture_identity();assert identity['status']=='VERIFIED' and identity['revision']==EXPECTED
assert identity['source_tree_sha256']==EXPECTED_DIGEST
from app.db import SessionLocal
from app.models import ConsignmentSaleFact,ConsignmentReturnFact,PurchasingInventoryEvent,PurchasingSessionLine,Store,PurchasingSaleCorrection
from app.services.purchasing_evidence_service import BUSINESS_TIMEZONE as TZ
from app.services import v2_square_data_service as sync
from app.services.purchasing_history_service import daily_sales,movement_evidence
from app.services.v2_ordering_square_gateway import _parse_datetime
start=time.monotonic();calls=defaultdict(int);sales=defaultdict(Decimal);events={};source_sales={};source_orders=0;claimed=False
allowed={'/v2/orders/search','/v2/inventory/changes/batch-retrieve'}
phase='history_refresh';original_open=urllib.request.OpenerDirector.open

def network(self,request,*args,**kwargs):
    global source_orders
    url=getattr(request,'full_url',str(request));path=urllib.parse.urlsplit(url).path
    if 'squareup.com' in urllib.parse.urlsplit(url).hostname:
        if path not in allowed:raise RuntimeError('Unapproved Square endpoint for this finite verification job')
        if sum(calls.values())>=500 or time.monotonic()-start>900:raise RuntimeError('Initial refresh operational budget exceeded')
        calls[path]+=1
    return original_open(self,request,*args,**kwargs)
urllib.request.OpenerDirector.open=network
# Observe successful source pages at the existing central HTTP boundary.
from app.services import square_ordering_data_service as square
original_post=square._square_post

def observed(path,payload,**kwargs):
    global source_orders
    response=original_post(path,payload,**kwargs)
    if phase=='history_refresh' and path=='/v2/orders/search' and payload.get('query',{}).get('sort',{}).get('sort_field')=='CLOSED_AT':
        for order in response.get('orders',[]):
            source_orders+=1;day=_parse_datetime(order['closed_at']).astimezone(TZ).date()
            for line in order.get('line_items',[]):
                if line.get('catalog_object_id'):
                    key=(order['id'],line['uid']);value=(order.get('version'),timestamp(order.get('updated_at') or order.get('created_at') or order['closed_at']),order['location_id'],line['catalog_object_id'],day,Decimal(line['quantity']))
                    previous=source_sales.get(key)
                    newer=previous is None or (value[0]>previous[0] if value[0] is not None and previous[0] is not None and value[0]!=previous[0] else value[1]>=previous[1])
                    if newer:source_sales[key]=value
    if phase=='history_refresh' and path=='/v2/inventory/changes/batch-retrieve':
        for change in response.get('changes',[]):
            kind=change['type'];item=change['adjustment' if kind=='ADJUSTMENT' else 'physical_count'];delta=Decimal(0)
            relevant=kind=='PHYSICAL_COUNT' or item.get('from_state') in ('IN_STOCK','SOLD') or item.get('to_state') in ('IN_STOCK','SOLD')
            if not relevant and (kind,item['id']) not in before_events:continue
            if kind=='ADJUSTMENT':
                if item.get('to_state')=='IN_STOCK':delta+=Decimal(item['quantity'])
                if item.get('from_state')=='IN_STOCK':delta-=Decimal(item['quantity'])
            events[(kind,item['id'])]=(item['location_id'],item['catalog_object_id'],_parse_datetime(item['occurred_at']).astimezone(TZ).date(),delta,kind=='PHYSICAL_COUNT')
    return response
square._square_post=observed
import app.services.v2_ordering_square_gateway as gateway_module
gateway_module._square_post=observed

def fact_hashes(db,model):
    result={}
    for row in db.execute(select(model)).scalars():
        value={column.name:getattr(row,column.name) for column in model.__table__.columns if column.name not in ('source_synchronized_at','source_order_version')}
        result[row.id]=hashlib.sha256(json.dumps(value,sort_keys=True,default=str).encode()).hexdigest()
    return result

def protected_draft(db):
    row=db.execute(text('select count(*) as lines,sum(quantity) as units from purchasing_session_lines where session_id=1')).one()
    assert tuple(row)==(896,3842)
    return {'lines':row.lines,'units':row.units}

def alarm(*args):raise RuntimeError('Finite job deadline exceeded')
signal.signal(signal.SIGALRM,alarm);signal.alarm(1000)
try:
    with SessionLocal() as db:
        db.execute(text('SET TRANSACTION READ ONLY'))
        before={m.__name__:fact_hashes(db,m) for m in (ConsignmentSaleFact,ConsignmentReturnFact)}
        protected_draft(db)
        before_events={(r.event_type,r.event_id):(r.location_id,r.variation_id,r.business_date,r.quantity_delta,r.physical_count) for r in db.scalars(select(PurchasingInventoryEvent))}
        locations=list(db.scalars(select(Store.square_location_id).where(Store.active.is_(True),Store.square_location_id.is_not(None))))
    result=sync.refresh_square_sales_data(actor_id=6,force=True)
    claimed=result.started
    require_current(result)
    # A resumed refresh reads only its unfinished tail. Independently exhaust the
    # entire required source range before comparing full-period SQL aggregates.
    # This is a read-only validation pass, never another import or checkpoint.
    source_sales.clear();events.clear();source_orders=0
    from app.services.v2_consignment_facts_service import SquareOrdersReader
    from app.services.purchasing_history_service import history_post
    end=datetime.combine(result.end_at.astimezone(TZ).date(),datetime.min.time(),TZ)
    begin=end-timedelta(days=84);cursor=begin
    while cursor<result.end_at:
        stop=min(cursor+timedelta(days=7),result.end_at)
        for page in SquareOrdersReader().search(location_ids=locations,start_at=cursor.astimezone(timezone.utc),end_at=stop.astimezone(timezone.utc),date_filter='CLOSED_AT'):
            if any(order.get('location_id') not in locations or not cursor<=_parse_datetime(order.get('closed_at'))<=stop for order in page):
                raise ValueError('Independent sales validation received a record outside its requested scope')
            # The observer independently records successful raw responses.
        cursor=stop
    for location in locations:
        cursor=begin
        while cursor<result.end_at:
            stop=min(cursor+timedelta(days=7),result.end_at);page_cursor=None;seen=set()
            while True:
                payload={'location_ids':[location],'types':['ADJUSTMENT','PHYSICAL_COUNT'],
                    'updated_after':(cursor.astimezone(timezone.utc)-timedelta(microseconds=1)).isoformat().replace('+00:00','Z'),
                    'updated_before':stop.astimezone(timezone.utc).isoformat().replace('+00:00','Z'),'limit':1000}
                if page_cursor:payload['cursor']=page_cursor
                response=history_post('/v2/inventory/changes/batch-retrieve',payload)
                if response.get('errors') or not isinstance(response.get('changes',[]),list):
                    raise ValueError('Independent inventory validation received an incomplete page')
                if any((change.get('adjustment') or change.get('physical_count') or {}).get('location_id')!=location for change in response.get('changes',[])):
                    raise ValueError('Independent inventory validation received another location')
                page_cursor=response.get('cursor')
                if not page_cursor:break
                if page_cursor in seen or len(seen)>=10000:raise ValueError('Independent inventory validation cursor failed to advance')
                seen.add(page_cursor)
            cursor=stop
    for version,updated_at,location,variation,day,quantity in source_sales.values():sales[(location,variation,day)]+=quantity
    elapsed=time.monotonic()-start
    with SessionLocal() as db:
        db.execute(text('SET TRANSACTION READ ONLY'))
        after={m.__name__:fact_hashes(db,m) for m in (ConsignmentSaleFact,ConsignmentReturnFact)}
        for name,hashes in before.items():assert all(after[name].get(key)==digest for key,digest in hashes.items()),'Historical financial business values changed'
        protected_draft(db)
        variations=sorted({key[1] for key in sales}|{v for v in db.scalars(select(ConsignmentSaleFact.square_variation_id).distinct()) if v}|{v for v in db.scalars(select(PurchasingSaleCorrection.variation_id).distinct()) if v})
        expected={key:value for key,value in sales.items() if begin.date()<=key[2]<end.date() and value}
        query_start=time.monotonic();actual=daily_sales(db,locations=locations,variations=variations,start_at=begin,end_at=end,as_of=datetime.now(timezone.utc));query_seconds=time.monotonic()-query_start
        assert actual==expected,'Independent sales aggregate mismatch'
        actual_events={(r.event_type,r.event_id):(r.location_id,r.variation_id,r.business_date,r.quantity_delta,r.physical_count) for r in db.scalars(select(PurchasingInventoryEvent))}
        assert all(actual_events.get(key)==value for key,value in events.items()),'Independent movement event mismatch'
        assert set(actual_events)<=set(before_events)|set(events),'Unexpected movement event appeared'
        assert all(actual_events.get(key)==value for key,value in before_events.items() if key not in events),'Prior movement evidence changed outside verified scope'
        query_start=time.monotonic();movement_evidence(db,locations=locations,variations=variations,start_at=begin,as_of=datetime.now(timezone.utc));movement_seconds=time.monotonic()-query_start
        scopes=[dict(r) for r in db.execute(text('select source,location_id,start_at,through_at,verified_at from purchasing_history_coverage order by source,location_id')).mappings()]
    print(json.dumps({'event':'ERUPTED_HISTORY_REFRESH_VERIFIED','revision':EXPECTED,'seconds':elapsed,'Square_requests':dict(calls),'source_orders_observed':source_orders,'sales_groups_exact':len(expected),'gross_units':str(sum(expected.values())),'inventory_events_exact':len(events),'sales_SQL_seconds':query_seconds,'movement_SQL_seconds':movement_seconds,'scope_count':len(scopes),'scopes':scopes,'historical_business_values_unchanged':True,'order1':{'lines':896,'units':3842},'peak_RSS_MiB':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/(1024*1024 if sys.platform=='darwin' else 1024),'generation_or_PO_or_payment_writes':0,'Square_mutations':0},default=str),flush=True)
    # Read-only execution of the expensive Generate evidence/forecast stage. No
    # draft INSERTs, sequence consumption, commits, or owner decision changes.
    phase='generation_core';allowed.update({'/v2/catalog/search-catalog-items','/v2/inventory/counts/batch-retrieve','/v2/inventory/batch-retrieve-counts','/v2/catalog/search'})
    from app.services import purchasing_session_service as purchasing
    t=time.monotonic();calls_before=dict(calls)
    with SessionLocal() as db:
        db.execute(text('SET TRANSACTION READ ONLY'))
        evidence=purchasing.collect_evidence(db,purchasing.policy(None))
        digest=hashlib.sha256(json.dumps(evidence,sort_keys=True).encode()).hexdigest()
        proposed=[p for p in evidence['products'].values() if purchasing._needs_session_line(p,evidence)]
        assert evidence['engine_version']=='purchasing-v3-p2-exact-20261018'
        assert len(evidence['stores'])==4 and purchasing.policy(None)['history_days']==84
        protected_draft(db)
        db.rollback()
    print(json.dumps({'event':'ERUPTED_GENERATION_READONLY_VERIFIED','revision':EXPECTED,'seconds':time.monotonic()-t,'product_store_pairs':len(evidence['products'])*4,'proposed_lines':len(proposed),'proposed_units':sum(p['quantity'] for p in proposed),'evidence_sha256':digest,'history_days':84,'Square_requests':{k:v-calls_before.get(k,0) for k,v in calls.items() if v-calls_before.get(k,0)},'peak_RSS_MiB':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/(1024*1024 if sys.platform=='darwin' else 1024),'database_writes':0,'saved_generate_endpoint_invoked':False},default=str),flush=True)
except Exception as exc:
    if claimed and phase=='history_refresh':
        from app.models import ConsignmentSalesSyncState
        with SessionLocal() as failed_db:
            state=failed_db.get(ConsignmentSalesSyncState,1)
            if state is not None:
                message=failure_message(state,exc)
                state.last_result='FAILED';state.last_error=message;failed_db.commit()
    print(json.dumps({'event':'ERUPTED_OPERATION_STOP','error_type':type(exc).__name__,'reason':safe_diagnostic(exc),'phase':phase,'Square_requests':dict(calls),'seconds':time.monotonic()-start}),flush=True)
    raise SystemExit(1)
finally:signal.alarm(0)
