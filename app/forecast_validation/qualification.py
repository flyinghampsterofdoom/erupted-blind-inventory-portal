"""Finite Owner-approved capacity. Preparation is never execution authority.

Control receipts use the existing append-only fv_attempts table; source
reservations and their control receipts commit together under the FV mutex.
No schema provisioning, source request, deployment or scheduler occurs here.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from sqlalchemy import select, func
from .contracts import EvidenceError, canonical, digest, stamp
from .storage import attempts
from .operations import Period, period_events, _event

GRANT_ID = 'owner-stage3-controlled-20261023'
GRANT = {'schema':'fv-qualification-capacity-v1','grant_id':GRANT_ID,
         'historical_reservations':311,'additional_reservations':304,
         'cumulative_ceiling':615,'captures':2,'initial_per_capture':132,
         'retries_per_capture':20,'execution_authorized':False,'recurring_authorized':False}


def receipt(c, operation, status, value, *, key, now=None):
    """Append or verify an identical receipt. Never truncate an audit record."""
    body=canonical(value)
    if len(body.encode())>1000:raise EvidenceError('Control receipt exceeds audit bound')
    ident=digest({'operation':operation,'key':key})
    old=c.execute(select(attempts).where(attempts.c.id==ident)).mappings().first()
    if old:
        if (old['operation'],old['status'],old['detail'])!=(operation,status,body):
            raise EvidenceError('Immutable control receipt conflict')
    else:
        c.execute(attempts.insert().values(id=ident,operation=operation,status=status,
                  detail=body,recorded_at=stamp(now or datetime.now(timezone.utc)).isoformat()))
    return ident


def records(c, operation, *, maximum=1000):
    rows=c.execute(select(attempts).where(attempts.c.operation==operation)
                   .order_by(attempts.c.recorded_at,attempts.c.id).limit(maximum+1)).mappings().all()
    if len(rows)>maximum:raise EvidenceError('Control history requires bounded archival review')
    return [{**dict(r),'value':json.loads(r['detail'])} for r in rows]


def require_execution(permit, *, approved_sha256, deployment_sha, now=None):
    """The digest must come from a separate trusted Owner approval channel.

    A CLI cannot approve its own permit. The prepared package pins no permit.
    This is configuration custody, not a claim of cryptographic Owner identity.
    """
    if not approved_sha256 or digest(permit)!=approved_sha256:
        raise EvidenceError('Separate explicit execution authorization is required')
    current=stamp(now or datetime.now(timezone.utc))
    if (permit.get('schema')!='fv-manual-execution-v1' or permit.get('grant_id')!=GRANT_ID
            or permit.get('action')!='EXECUTE_ONE_CAPTURE' or permit.get('recurring') is not False
            or type(permit.get('slot')) is not int or permit['slot'] not in (0,1)
            or permit.get('deployment_sha')!=deployment_sha or len(deployment_sha)!=40
            or not permit.get('approval_id') or not permit.get('period_start')
            or not permit.get('scope_policy') or len(permit['scope_policy'])>120):
        raise EvidenceError('Execution authorization scope mismatch')
    from datetime import date,timedelta
    from .contracts import TZ
    start=date.fromisoformat(permit['period_start'])
    if start.weekday()!=0:raise EvidenceError('Execution period must start Monday')
    due=datetime.combine(start+timedelta(days=1),datetime.min.time(),TZ)+timedelta(hours=6)
    if not stamp(permit['not_before'])<=current<stamp(permit['not_after'])<=due:
        raise EvidenceError('Execution window expired, premature or after Tuesday deadline')
    if (stamp(permit['not_after'])-current).total_seconds()<900:
        raise EvidenceError('A full bounded execution window is required before Tuesday deadline')
    if current.astimezone(TZ).date()<start:raise EvidenceError('Future period execution is prohibited')
    return permit


class Capacity:
    def __init__(self, store, grant=None):
        self.store=store;self.grant=dict(grant or GRANT)
        if self.grant!=GRANT:raise EvidenceError('Approved capacity contract differs')

    def prepare(self, period):
        period._require_lock()
        with self.store.engine.begin() as c:
            old=records(c,'qualification-capacity')
            if not old:
                count=c.execute(select(func.count()).select_from(period_events)
                    .where(period_events.c.status=='SOURCE_REQUEST')).scalar_one()
                if count!=311:raise EvidenceError('Historical source baseline must be reconciled without reset')
            receipt(c,'qualification-capacity','CAPACITY_PREPARED',self.grant,key=GRANT_ID)

    def bind(self, period, slot):
        period._require_lock()
        if type(slot) is not int or slot not in (0,1):raise EvidenceError('Only two capture slots approved')
        with self.store.engine.begin() as c:
            bindings=records(c,'qualification-binding')
            if any(r['value']['period_id']==period.id and r['value']['slot']!=slot for r in bindings):
                raise EvidenceError('A period cannot consume two capture slots')
            receipt(c,'qualification-binding','CAPTURE_BOUND',
                    {'grant_id':GRANT_ID,'slot':slot,'period_id':period.id},key=str(slot))

    def reserve(self, period, slot, path, query_sha, *, retry=False):
        period._require_lock()
        if type(retry) is not bool:raise EvidenceError('Explicit retry identity required')
        if period.source():raise EvidenceError('Frozen source cannot be recollected')
        with self.store.engine.begin() as c:
            bound=records(c,'qualification-binding')
            if not any(r['value']=={'grant_id':GRANT_ID,'slot':slot,'period_id':period.id} for r in bound):
                raise EvidenceError('Capture slot has not been bound')
            used=records(c,'qualification-reservation',maximum=304)
            global_count=c.execute(select(func.count()).select_from(period_events)
                .where(period_events.c.status=='SOURCE_REQUEST')).scalar_one()
            if global_count!=311+len(used):raise EvidenceError('Unattributed reservations; no counters may be reset')
            if global_count>=615 or len(used)>=304:raise EvidenceError('Cumulative qualification budget exhausted')
            selected=[r for r in used if r['value']['slot']==slot]
            count=sum(r['value']['retry']==retry for r in selected)
            if count >= (20 if retry else 132):raise EvidenceError('Capture retry or initial request budget exhausted')
            # All source calls and the matching grant ledger entry are atomic.
            value={'grant_id':GRANT_ID,'slot':slot,'period_id':period.id,'retry':retry,
                   'ordinal':len(used)+1,'query_sha256':query_sha}
            receipt(c,'qualification-reservation','SOURCE_RESERVED',value,key=str(len(used)+1))
            _event(c,period.id,period.attempt_id,'SOURCE_REQUEST',
                   {'path':path,'query_sha256':query_sha,'retry':retry,'grant_id':GRANT_ID,
                    'capture_slot':slot,'attempt':len(selected)+1},now=period.now)
            return global_count+1

    def remaining(self, slot):
        with self.store.engine.connect() as c:rows=records(c,'qualification-reservation',maximum=304)
        selected=[r for r in rows if r['value']['slot']==slot]
        return {'initial':132-sum(not r['value']['retry'] for r in selected),
                'retry':20-sum(r['value']['retry'] for r in selected)}


class QualifiedPeriod(Period):
    def __init__(self,*args,capacity,slot,**kwargs):
        super().__init__(*args,**kwargs);self.capacity=capacity;self.slot=slot
    def claim(self):
        existing=self.source()
        if existing and not 0<=(stamp(self.now or datetime.now(timezone.utc))-stamp(existing['origin'])).total_seconds()<=900:
            raise EvidenceError('Frozen qualification lifecycle expired; original source remains immutable')
        result=super().claim();self.capacity.prepare(self);self.capacity.bind(self,self.slot);return result
    def reserve_request(self,path,query_sha,*,retry=False):
        return self.capacity.reserve(self,self.slot,path,query_sha,retry=retry)
