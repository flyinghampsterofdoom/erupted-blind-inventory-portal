"""One shared, unattended source collector. Explicit isolated DB and injected reads only.

No scheduler is installed here. DAILY and MANUAL use the same finite command;
HTTP is outside the projection transaction. Failed reads never certify coverage.
"""
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session
from app.services.evidence_collector_target import require_isolated, require_isolated_url
from app.services.square_request_policy import enforce_square_request_policy

ZONE=ZoneInfo('America/Los_Angeles')
LOCK=730202610280018
SCHEDULE={'timezone':'UTC','hour':12,'minute':0,'local_time':'04:00 PST / 05:00 PDT','enabled':False}
RECOVERY_DAYS=126
OVERLAP=timedelta(hours=72)


def instant(value):
    from dateutil.parser import isoparse
    from scripts.release.rfc3339 import timestamp
    if isinstance(value,str):timestamp(value)
    result=isoparse(value) if isinstance(value,str) else value
    if not isinstance(result,datetime) or result.utcoffset() is None:raise ValueError('Aware source timestamp required')
    return result.astimezone(timezone.utc)


def number(value):
    if value is None or isinstance(value,bool):raise ValueError('Explicit finite quantity required')
    result=Decimal(str(value))
    if not result.is_finite():raise ValueError('Finite quantity required')
    return result


def digest(value):return sha256(json.dumps(value,sort_keys=True,separators=(',',':'),default=str).encode()).hexdigest()


def daily_key(now):
    """Local calendar key prevents DST from running a daily command twice."""
    local=instant(now).astimezone(ZONE)
    return 'daily:'+local.date().isoformat() if local.time()>=time(4) else None


@dataclass(frozen=True)
class Scope:
    variations:tuple[str,...]
    stores:tuple[tuple[int,str],...]
    def __post_init__(self):
        if not self.variations or len(set(self.variations))!=len(self.variations) or any(not v for v in self.variations):raise ValueError('Unique real variation scope required')
        if len(self.stores)!=4 or len({s for s,l in self.stores})!=4 or len({l for s,l in self.stores})!=4 or any(not l for s,l in self.stores):raise ValueError('Four unique real store/location mappings required')


def load_scope(connection,guard=require_isolated):
    """All retained catalog variations, including unassigned supplier exceptions."""
    guard(connection)
    variations=tuple(connection.execute(text('SELECT square_variation_id FROM public.ordering_catalog_identity ORDER BY square_variation_id')).scalars())
    stores=tuple((r.id,r.square_location_id) for r in connection.execute(text('SELECT id,square_location_id FROM public.stores WHERE active ORDER BY id')))
    return Scope(variations,stores)


def normalize_changes(changes,*,scope,begin,anchor,observed):
    """Real ID per location leg; physical resets retain their absolute raw count."""
    result={};locations={l for s,l in scope.stores}
    for change in changes:
        kind=change.get('type');event=change.get('adjustment' if kind=='ADJUSTMENT' else 'physical_count') or {}
        if kind not in ('ADJUSTMENT','PHYSICAL_COUNT'):raise ValueError('Unsupported movement type')
        eid=event.get('id');vid=event.get('catalog_object_id')
        if not eid or not vid:raise ValueError('Missing source movement identity')
        qty=number(event.get('quantity'))
        if qty<0:raise ValueError('Negative movement magnitude')
        occurred=instant(event.get('occurred_at'));updated=instant(event.get('updated_at') or event.get('created_at'))
        calculated=instant(event['calculated_at']) if event.get('calculated_at') else None
        lower=calculated or max(updated,begin-timedelta(microseconds=1));upper=calculated or anchor
        if lower>upper or upper>anchor or updated>observed:raise ValueError('Movement outside completed source scope')
        deltas=defaultdict(Decimal)
        if kind=='ADJUSTMENT':
            fr=event.get('from_location_id') or event.get('location_id');to=event.get('to_location_id') or event.get('location_id')
            if not fr or not to:raise ValueError('Both movement locations required')
            if event.get('location_id') and (fr!=event['location_id'] or to!=event['location_id']):raise ValueError('Conflicting legacy/modern locations')
            if event.get('from_state')=='IN_STOCK':deltas[fr]-=qty
            if event.get('to_state')=='IN_STOCK':deltas[to]+=qty
            if not deltas:
                deltas[fr]=Decimal(0);deltas[to]=Decimal(0)
        else:
            if not event.get('location_id') or event.get('state')!='IN_STOCK':raise ValueError('Unsupported physical-count state/location')
            deltas[event['location_id']]=Decimal(0)
        if not locations.intersection(deltas):raise ValueError('Movement outside requested locations')
        for loc,delta in deltas.items():
            if loc not in locations:continue  # The other leg may be outside Erupted's four stores.
            row=dict(event_type=kind,event_id=eid,location_id=loc,variation_id=vid,occurred_at=occurred,
                business_date=occurred.astimezone(ZONE).date(),quantity_delta=delta,physical_count=kind=='PHYSICAL_COUNT',
                source_updated_at=updated,calculated_after_at=lower,calculated_before_at=upper,
                source_digest=digest(change),synchronized_at=observed)
            key=(kind,eid,loc)
            if key in result and result[key]['source_digest']!=row['source_digest']:raise ValueError('Conflicting duplicate movement revision')
            result[key]=row
    return list(result.values())


class Collector:
    def __init__(self,engine,post,*,clock=lambda:datetime.now(timezone.utc),target=None):
        self.target=target
        if target is None:require_isolated_url(engine.url)
        elif engine.url!=target.url:raise PermissionError('Collector target identity mismatch')
        self.guard=target.guard if target else require_isolated
        self.namespace=target.namespace if target else 'purchasing_v4'
        self.engine=engine;self.post=post;self.clock=clock
        self.pages=[]

    def sql(self,statement):
        return text(statement.replace('purchasing_v4.',self.namespace+'.'))

    def pages_for(self,path,payload,field):
        cursor=None;seen=set();rows=[]
        for ordinal in range(10000):
            request=dict(payload)
            if cursor:request['cursor']=cursor
            enforce_square_request_policy('POST',path,request)
            started=instant(self.clock());response=self.post(path,request);completed=instant(self.clock())
            if not isinstance(response,dict) or response.get('errors') or not isinstance(response.get(field,[]),list):raise ValueError('Incomplete source page')
            if completed<started:raise ValueError('Source read clock moved backwards')
            self.pages.append(dict(path=path,request=request,response=response,started_at=started.isoformat(),completed_at=completed.isoformat()))
            rows.extend(response.get(field,[]));cursor=response.get('cursor')
            if not cursor:return rows
            if not isinstance(cursor,str) or cursor in seen:raise ValueError('Repeated/invalid source cursor')
            seen.add(cursor)
        raise ValueError('Source pagination budget exceeded')

    def counts(self,scope):
        started=instant(self.clock());rows=[]
        for offset in range(0,len(scope.variations),500):
            rows+=self.pages_for('/v2/inventory/counts/batch-retrieve',dict(catalog_object_ids=list(scope.variations[offset:offset+500]),
                location_ids=[l for s,l in scope.stores],states=['IN_STOCK']), 'counts')
        return dict(started_at=started.isoformat(),completed_at=instant(self.clock()).isoformat(),rows=rows)

    def collect(self,scope,*,begin,reason,command_key):
        """Singleton across manual/daily workers; retries replay the last certified interval."""
        if self.target is None:require_isolated_url(self.engine.url)
        self.pages=[];started=instant(self.clock());attempt=str(uuid4())
        with self.engine.connect() as lock:
            self.guard(lock)
            if not lock.execute(self.sql('SELECT pg_try_advisory_lock(:key)'),{'key':LOCK}).scalar_one():raise RuntimeError('Collector already running')
            lock.commit()
            try:
                with self.engine.connect() as c:
                    if c.execute(self.sql("SELECT 1 FROM purchasing_v4.collection_attempt WHERE command_key=:key AND status='COMPLETE'"),{'key':command_key}).first():return {'status':'ALREADY_COLLECTED','command_key':command_key}
                try:
                    before=self.counts(scope);anchor=instant(before['completed_at']);end=datetime.combine(anchor.astimezone(ZONE).date(),time(),ZONE)
                    begin=instant(begin)
                    if begin>=end:raise ValueError('At least one completed day required')
                    locations=[l for s,l in scope.stores]
                    orders=self.pages_for('/v2/orders/search',dict(location_ids=locations,return_entries=False,limit=1000,
                        query={'filter':{'date_time_filter':{'closed_at':{'start_at':begin.isoformat(),'end_at':end.isoformat()}},'state_filter':{'states':['COMPLETED']}},'sort':{'sort_field':'CLOSED_AT','sort_order':'ASC'}}),'orders')
                    updates=self.pages_for('/v2/orders/search',dict(location_ids=locations,return_entries=False,limit=1000,
                        query={'filter':{'date_time_filter':{'updated_at':{'start_at':begin.isoformat(),'end_at':anchor.isoformat()}}},'sort':{'sort_field':'UPDATED_AT','sort_order':'ASC'}}),'orders')
                    for order in orders:
                        if order.get('state')!='COMPLETED' or not begin<=instant(order.get('closed_at'))<=anchor:raise ValueError('Completed order outside source interval')
                    for order in updates:
                        if not begin<=instant(order.get('updated_at'))<=anchor:raise ValueError('Order update outside source interval')
                    changes=self.pages_for('/v2/inventory/changes/batch-retrieve',dict(location_ids=locations,types=['ADJUSTMENT','PHYSICAL_COUNT'],
                        updated_after=(begin-timedelta(microseconds=1)).isoformat(),updated_before=anchor.isoformat(),limit=1000),'changes')
                    observed=instant(self.clock());after=self.counts(scope)
                    bundle=dict(before=before,after=after,begin=begin.isoformat(),sales_end=end.isoformat(),movement_end=anchor.isoformat(),
                        observed=observed.isoformat(),orders=orders,updates=updates,changes=changes,pages=self.pages)
                    return self.project(scope,bundle,reason=reason,command_key=command_key,attempt=attempt,started=started)
                except Exception as exc:
                    # Safe error type only; HTTP exception strings can include sensitive bodies.
                    self.record_failure(attempt,command_key,reason,started,type(exc).__name__)
                    raise
            finally:
                lock.execute(self.sql('SELECT pg_advisory_unlock(:key)'),{'key':LOCK});lock.commit()

    def record_failure(self,attempt,key,reason,started,error):
        with self.engine.begin() as c:
            self.guard(c)
            c.execute(self.sql('''INSERT INTO purchasing_v4.collection_attempt VALUES
                (CAST(:id AS uuid),:key,:reason,:start,:end,'FAILED',CAST(:manifest AS jsonb),:error)'''),
                dict(id=attempt,key=key,reason=reason,start=started,end=instant(self.clock()),manifest=json.dumps({'captured_pages':len(self.pages)}),error=error))
            self.retain_pages(c,attempt,self.pages)

    def retain_pages(self,c,attempt,pages):
        if pages:c.execute(self.sql('''INSERT INTO purchasing_v4.collection_page VALUES (CAST(:attempt AS uuid),:ordinal,:digest,CAST(:payload AS jsonb))'''),
            [dict(attempt=attempt,ordinal=n,digest=digest(page),payload=json.dumps(page,default=str)) for n,page in enumerate(pages)])

    def project(self,scope,bundle,*,reason,command_key,attempt=None,started=None):
        """Atomic local publication of fully collected source scope; offline replay uses this same path."""
        from app.models import OrderingInventoryRefreshRun,OrderingCurrentInventory
        from app.services.v2_ordering_inventory_repository import InventoryObservation,persist_inventory_refresh
        from app.services.purchasing_evidence_service import record_run
        from app.services.purchasing_history_service import purchasing_source_orders,certify
        attempt=attempt or str(uuid4());before=bundle['before'];after=bundle['after']
        start=instant(before['started_at']);anchor=instant(before['completed_at']);finish=instant(after['completed_at'])
        begin=instant(bundle['begin']);sales_end=instant(bundle['sales_end']);movement_end=instant(bundle['movement_end']);observed=instant(bundle['observed'])
        if not begin<sales_end<=anchor<=observed<=finish or movement_end!=anchor:raise ValueError('Invalid completed collection interval')
        rows=normalize_changes(bundle['changes'],scope=scope,begin=begin,anchor=anchor,observed=observed)
        def count_index(items):
            indexed={}
            for row in items:
                key=(row.get('catalog_object_id'),row.get('location_id'))
                if key in indexed or key[0] not in scope.variations or key[1] not in {l for s,l in scope.stores} or row.get('state')!='IN_STOCK':raise ValueError('Conflicting/out-of-scope count')
                number(row.get('quantity'))
                if row.get('calculated_at'):instant(row['calculated_at'])
                indexed[key]=row
            return indexed
        prior=count_index(before['rows']);final=count_index(after['rows']);checkpoints=[];observations=[]
        for vid in scope.variations:
            for sid,loc in scope.stores:
                a=prior.get((vid,loc));b=final.get((vid,loc));calculated=instant(a['calculated_at']) if a and a.get('calculated_at') else None
                valid=bool(a and b and a['quantity']==b['quantity'] and a.get('calculated_at')==b.get('calculated_at') and calculated and calculated<=start)
                why='' if valid else 'MISSING_EXPLICIT_COUNT' if not a or not b else 'UNQUALIFIED_OR_CHANGED_COUNT'
                checkpoints.append(dict(attempt=attempt,vid=vid,sid=sid,day=anchor.astimezone(ZONE).date(),observed=anchor,state='KNOWN' if valid else 'UNKNOWN',
                    quantity=number(a['quantity']) if valid else None,calculated=calculated,reason=why))
                if valid:observations.append(InventoryObservation(vid,sid,loc,number(a['quantity']),calculated))
        expected=len(checkpoints);count=len(observations);status='COMPLETE' if count==expected else 'PARTIAL' if count else 'FAILED'
        if count==0:raise ValueError('No qualified inventory observations; source scope not published')
        # Order versions determine replay ordering; all source payloads are retained.
        orders=bundle['orders']+bundle.get('updates',[])
        for order in orders:
            if order.get('location_id') not in {l for s,l in scope.stores}:raise ValueError('Order outside requested scope')
        orders=sorted(orders,key=lambda o:(o.get('id',''),o.get('version') if o.get('version') is not None else -1,o.get('updated_at') or o.get('closed_at') or ''))
        with self.engine.begin() as c:
            self.guard(c)
            # Replays and concurrent publication use the same lock and command key.
            c.execute(self.sql('SELECT pg_advisory_xact_lock(:key)'),{'key':LOCK+1})
            c.execute(self.sql('SELECT pg_advisory_xact_lock(730202607250009)'))
            previous=c.execute(self.sql("SELECT id,status,manifest FROM purchasing_v4.collection_attempt WHERE command_key=:key AND status!='FAILED' ORDER BY completed_at DESC,started_at DESC LIMIT 1"),{'key':command_key}).first()
            if previous and (previous.status=='COMPLETE' or previous.manifest.get('bundle_digest')==digest(bundle)):
                return {'status':'ALREADY_COLLECTED','command_key':command_key}
            newer={(r[0],r[1]) for r in c.execute(self.sql('SELECT square_variation_id,store_id FROM public.ordering_current_inventory WHERE refreshed_at>:anchor'),{'anchor':anchor})}
            if newer:
                observations=[o for o in observations if (o.square_variation_id,o.store_id) not in newer]
                for p in checkpoints:
                    if (p['vid'],p['sid']) in newer:p.update(state='UNKNOWN',quantity=None,reason='SUPERSEDED_BY_NEWER_OBSERVATION')
                count=len(observations);status='COMPLETE' if count==expected else 'PARTIAL' if count else 'FAILED'
                if not count:raise ValueError('Every count was superseded by a newer observation; retry source collection')
            legacy_coverage=[{k:(v.isoformat() if isinstance(v,datetime) else v) for k,v in r.items()} for r in c.execute(self.sql('SELECT * FROM public.purchasing_history_coverage ORDER BY source,location_id FOR UPDATE')).mappings()]
            c.execute(self.sql('''INSERT INTO purchasing_v4.collection_attempt VALUES
              (CAST(:id AS uuid),:key,:reason,:start,:end,:status,CAST(:manifest AS jsonb),NULL)'''),
              dict(id=attempt,key=command_key,reason=reason,start=started or start,end=finish,status=status,
                  manifest=json.dumps(dict(begin=bundle['begin'],sales_end=bundle['sales_end'],movement_end=bundle['movement_end'],scope=dict(variations=scope.variations,stores=scope.stores),qualified_counts=count,expected_counts=expected,bundle_digest=digest(bundle),legacy_coverage_before=legacy_coverage))))
            self.retain_pages(c,attempt,bundle.get('pages') or [bundle])
            c.execute(self.sql('''INSERT INTO purchasing_v4.inventory_checkpoint VALUES
              (CAST(:attempt AS uuid),:vid,:sid,:day,:observed,:state,:quantity,:calculated,:reason)'''),checkpoints)
            db=Session(bind=c)
            try:
                for offset in range(0,len(orders),250):purchasing_source_orders(db,orders[offset:offset+250],synchronized_at=observed,units_only=True)
                # Replacing an event replaces all of its location legs; never manufacture event IDs.
                identities=sorted({r['event_id'] for r in rows})
                retained={}
                if identities:
                    for r in c.execute(self.sql("SELECT event_type,event_id,source_updated_at,source_digest,synchronized_at FROM purchasing_v4.movement_leg WHERE event_id=ANY(:ids)"),{'ids':identities}).mappings():
                        retained[(r['event_type'],r['event_id'])]=r
                accepted=[]
                for row in rows:
                    old=retained.get((row['event_type'],row['event_id']))
                    if old and (old['source_updated_at']>row['source_updated_at'] or old['synchronized_at']>observed):continue
                    if old and old['source_updated_at']==row['source_updated_at'] and old['source_digest']!=row['source_digest']:
                        raise ValueError('Conflicting movement payload at same source revision')
                    accepted.append(row)
                for kind in ('ADJUSTMENT','PHYSICAL_COUNT'):
                    ids=sorted({r['event_id'] for r in accepted if r['event_type']==kind})
                    if ids:c.execute(self.sql('DELETE FROM purchasing_v4.movement_leg WHERE event_type=:kind AND event_id=ANY(:ids)'),dict(kind=kind,ids=ids))
                if accepted:
                    columns=tuple(accepted[0]);sql='INSERT INTO purchasing_v4.movement_leg ('+','.join(columns)+') VALUES ('+','.join(':'+col for col in columns)+')'
                    c.execute(self.sql(sql),accepted)
                run=OrderingInventoryRefreshRun(correlation_id=attempt,result=status,expected_variation_count=len(scope.variations),active_store_count=4,
                    expected_pair_count=expected,covered_pair_count=count,missing_pair_count=expected-count,square_request_count=len(bundle.get('pages',[])),
                    refreshed_by_principal_id=None,started_at=start,completed_at=anchor,error_code='PARTIAL_COVERAGE' if status=='PARTIAL' else None)
                persist_inventory_refresh(db,run=run,observations=tuple(observations),refreshed_at=anchor)
                missing=[p for p in checkpoints if p['state']=='UNKNOWN']
                if missing:c.execute(self.sql("UPDATE public.ordering_current_inventory SET freshness_state='STALE' WHERE square_variation_id=:vid AND store_id=:sid AND refreshed_at<=:observed"),missing)
                record_run(db,source='INVENTORY',status=status,started_at=start,completed_at=anchor,
                    scope={'variation_ids':list(scope.variations),'store_ids':[s for s,l in scope.stores]},
                    evidence={'observed_pairs':[[o.square_variation_id,o.store_id] for o in observations],'error_code':run.error_code,'collector_attempt':attempt})
                for source,through in [('SALES',sales_end),('MOVEMENTS',movement_end)]:
                    certify(db,source=source,locations=[l for s,l in scope.stores],start_at=begin,through_at=through,verified_at=finish)
                db.flush()
            finally:db.close()
        return dict(status=status,attempt_id=attempt,command_key=command_key,qualified_counts=count,unknown_counts=expected-count,movement_legs=len(rows),orders_observed=len(orders))


def run_once(engine,post,*,reason,now=None,command_key=None,clock=lambda:datetime.now(timezone.utc),target=None):
    """Finite runner for a future external daily job or explicit manual refresh.

    Initial recovery exhausts 126 completed local days. Subsequent runs overlap
    the oldest certified source by 72 hours and search UPDATED_AT as well as
    CLOSED_AT, so late-arriving/edited old orders are not lost between snapshots.
    """
    now=instant(now or clock())
    if reason not in ('DAILY','MANUAL','RECOVERY'):raise ValueError('Explicit collection reason required')
    key=command_key or (daily_key(now) if reason=='DAILY' else reason.lower()+':'+str(uuid4()))
    if key is None:return {'status':'NOT_DUE','schedule':SCHEDULE}
    end=datetime.combine(now.astimezone(ZONE).date(),time(),ZONE)
    history_start=end-timedelta(days=RECOVERY_DAYS)
    with engine.connect() as c:
        scope=load_scope(c,guard=target.guard if target else require_isolated)
        coverage=c.execute(text('SELECT * FROM public.purchasing_history_coverage WHERE source IN (\'SALES\',\'MOVEMENTS\')')).mappings().all()
        rows={(r['source'],r['location_id']):r for r in coverage}
        needed=[rows.get((source,loc)) for source in ('SALES','MOVEMENTS') for sid,loc in scope.stores]
        begin=history_start
        namespace=target.namespace if target else 'purchasing_v4'
        collected=c.scalar(text(f"SELECT EXISTS(SELECT 1 FROM {namespace}.collection_attempt WHERE status IN ('COMPLETE','PARTIAL'))"))
        if reason!='RECOVERY' and collected and all(r and r['start_at']<=history_start for r in needed):
            begin=max(history_start,min(r['through_at'] for r in needed)-OVERLAP)
    return Collector(engine,post,clock=clock,target=target).collect(scope,begin=begin,reason=reason,command_key=key)
