"""Shared gross-sales and stockout evidence. No inventory or financial writer.

Interactive readers never download history. The existing Square Data refresh
populates/validates these scopes outside page GETs. Missing coverage fails closed.
Financial fact attribution is deliberately independent of source-unit coverage.
"""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json

from sqlalchemy import select, func, exists, and_, or_
from app.models import (ConsignmentSaleFact, PurchasingSaleCorrection,
    PurchasingHistoryCoverage, PurchasingInventoryEvent, PurchasingOrderSourceState)
from app.services.purchasing_evidence_service import BUSINESS_TIMEZONE, utc, quantity

ZERO = Decimal(0)
FRESH_FOR = timedelta(hours=24)
OVERLAP = timedelta(hours=72)


class HistoryUnavailable(ValueError):
    pass


def certify(db, *, source, locations, start_at, through_at, verified_at):
    """Called only after exhausting and validating the requested source interval."""
    start_at, through_at, verified_at = map(utc, (start_at, through_at, verified_at))
    if through_at <= start_at or verified_at < through_at:
        raise ValueError('Invalid completed history scope')
    for location in sorted(set(locations)):
        row = db.get(PurchasingHistoryCoverage, (source, location))
        if row is None:
            db.add(PurchasingHistoryCoverage(source=source, location_id=location,
                start_at=start_at, through_at=through_at, verified_at=verified_at))
        elif start_at <= utc(row.through_at) and through_at >= utc(row.start_at):
            row.start_at = min(start_at, utc(row.start_at))
            row.through_at = max(through_at, utc(row.through_at))
            row.verified_at = verified_at
        # A disjoint interval must never certify the intervening gap.
    db.flush()


def covered(db, *, source, locations, start_at, through_at, as_of):
    from app.models import ConsignmentSalesSyncState
    state = db.get(ConsignmentSalesSyncState,1)
    if state is not None and state.last_result == 'FAILED':
        raise HistoryUnavailable('Square history refresh failed; verify source coverage before purchasing.')
    rows = db.scalars(select(PurchasingHistoryCoverage).where(
        PurchasingHistoryCoverage.source == source,
        PurchasingHistoryCoverage.location_id.in_(locations))).all()
    by_location = {r.location_id:r for r in rows}
    for location in locations:
        row = by_location.get(location)
        if (row is None or utc(row.start_at) > utc(start_at) or utc(row.through_at) < utc(through_at)
                or not timedelta(0) <= utc(as_of)-utc(row.verified_at) <= FRESH_FOR):
            raise HistoryUnavailable(f'{source} history incomplete or stale for {location}; run Square Data refresh before generating recommendations.')
    return rows


def daily_sales(db, *, locations, variations, start_at, end_at, as_of):
    covered(db, source='SALES', locations=locations, start_at=start_at, through_at=end_at, as_of=as_of)
    start, end = start_at.astimezone(BUSINESS_TIMEZONE).date(), (end_at-timedelta(microseconds=1)).astimezone(BUSINESS_TIMEZONE).date()
    # A correction replaces only a changed source line; historical money/costs stay untouched.
    correction_exists = exists(select(PurchasingSaleCorrection.square_order_id).where(
        PurchasingSaleCorrection.kind == 'LINE',
        PurchasingSaleCorrection.square_order_id == ConsignmentSaleFact.square_order_id,
        PurchasingSaleCorrection.square_line_item_uid == ConsignmentSaleFact.square_line_item_uid))
    fields = (ConsignmentSaleFact.square_location_id, ConsignmentSaleFact.square_variation_id, ConsignmentSaleFact.business_date)
    base = select(*fields, func.sum(ConsignmentSaleFact.quantity_sold)).where(
        ConsignmentSaleFact.square_location_id.in_(locations),
        ConsignmentSaleFact.square_variation_id.in_(variations),
        ConsignmentSaleFact.business_date.between(start,end), ~correction_exists).group_by(*fields)
    result = {(loc,vid,day):qty for loc,vid,day,qty in db.execute(base) if qty}
    C = PurchasingSaleCorrection
    fields = (C.location_id, C.variation_id, C.business_date)
    for loc,vid,day,qty in db.execute(select(*fields,func.sum(C.quantity)).where(
            C.kind=='LINE', C.location_id.in_(locations), C.variation_id.in_(variations), C.business_date.between(start,end)).group_by(*fields)):
        if qty: result[(loc,vid,day)] = result.get((loc,vid,day),ZERO)+qty
    return result


def movement_evidence(db, *, locations, variations, start_at, as_of):
    midnight = datetime.combine(as_of.astimezone(BUSINESS_TIMEZONE).date(), datetime.min.time(), BUSINESS_TIMEZONE)
    scopes = covered(db, source='MOVEMENTS', locations=locations, start_at=start_at,
        through_at=midnight, as_of=as_of)
    E=PurchasingInventoryEvent
    fields=(E.location_id,E.variation_id,E.business_date)
    # Square filters calculated_at, not occurred_at. When that timestamp is not
    # returned, preserve the observed query bounds; never invent an exact time.
    known=and_(
        or_(E.calculated_after_at>start_at,and_(E.calculated_after_at==start_at,E.calculated_before_at>start_at)),
        or_(E.calculated_before_at<as_of,and_(E.calculated_before_at==as_of,E.calculated_after_at<E.calculated_before_at)))
    query=select(*fields,func.sum(E.quantity_delta),func.bool_or(E.physical_count)).where(
        E.location_id.in_(locations),E.variation_id.in_(variations),known,
        E.occurred_at<=as_of).group_by(*fields)
    deltas={};uncertain=set()
    for loc,vid,day,qty,physical in db.execute(query):
        if qty:deltas[(loc,vid,day)]=qty
        if physical:uncertain.add((loc,vid))
    uncertain.update(db.execute(select(E.location_id,E.variation_id).where(
        E.location_id.in_(locations),E.variation_id.in_(variations),
        E.calculated_before_at>start_at,E.calculated_after_at<start_at,
        or_(E.quantity_delta!=0,E.physical_count.is_(True))).distinct()).all())
    return deltas,uncertain,{r.location_id:utc(r.through_at) for r in scopes}


def import_movement_page(db, changes, *, synchronized_at, scope_start_at=None):
    """Idempotent minimal event upsert, including corrections to existing IDs."""
    from app.services.v2_ordering_square_gateway import _parse_datetime
    normalized=[]
    for change in changes:
        kind=change.get('type')
        if kind not in ('ADJUSTMENT','PHYSICAL_COUNT'):raise ValueError('Unsupported inventory history event')
        event=change.get('adjustment' if kind=='ADJUSTMENT' else 'physical_count') or {}
        identity=str(event.get('id') or '').strip();loc=str(event.get('location_id') or '').strip();vid=str(event.get('catalog_object_id') or '').strip()
        occurred=_parse_datetime(event.get('occurred_at'));updated=_parse_datetime(event.get('updated_at') or event.get('created_at'))
        qty=quantity(event.get('quantity'))
        if not identity or not loc or not vid or occurred is None or updated is None or qty is None or qty<0:
            raise ValueError('Inventory event identity, timestamp or quantity incomplete')
        delta=ZERO
        if kind=='ADJUSTMENT':
            if event.get('to_state')=='IN_STOCK':delta+=qty
            if event.get('from_state')=='IN_STOCK':delta-=qty
        # Irrelevant physical counts do not affect the IN_STOCK/SOLD history contract.
        physical=kind=='PHYSICAL_COUNT'
        calculated=_parse_datetime(event.get('calculated_at'))
        lower=calculated or max(updated,utc(scope_start_at)) if scope_start_at is not None else calculated or updated
        upper=calculated or synchronized_at
        if lower>upper:raise ValueError('Inventory calculation time outside completed source scope')
        normalized.append(dict(event_type=kind,event_id=identity,location_id=loc,variation_id=vid,
            occurred_at=occurred,business_date=occurred.astimezone(BUSINESS_TIMEZONE).date(),quantity_delta=delta,
            physical_count=physical,source_updated_at=updated,calculated_after_at=lower,calculated_before_at=upper,
            source_digest=hashlib.sha256(json.dumps(change,sort_keys=True).encode()).hexdigest(),synchronized_at=synchronized_at,
            _relevant=physical or event.get('from_state') in ('IN_STOCK','SOLD') or event.get('to_state') in ('IN_STOCK','SOLD')))
    # Batch the identity lookup. Pages are bounded by the provider's requested limit.
    ids=[x['event_id'] for x in normalized]
    existing={(r.event_type,r.event_id):r for r in db.scalars(select(PurchasingInventoryEvent).where(PurchasingInventoryEvent.event_id.in_(ids))).all()} if ids else {}
    for values in normalized:
        key=(values['event_type'],values['event_id']);old=existing.get(key)
        relevant=values.pop('_relevant')
        if not relevant and old is None:continue
        if old is None:
            old=PurchasingInventoryEvent(**values);db.add(old);existing[key]=old
        elif utc(values['synchronized_at']) >= utc(old.synchronized_at) and utc(values['source_updated_at']) >= utc(old.source_updated_at):
            for name,value in values.items():setattr(old,name,value)
    db.flush()


def purchasing_source_orders(db, orders, *, synchronized_at, actor_id=None):
    """Record changed gross-unit source evidence without rewriting financial facts.

    Return only unchanged/new completed financial lines to the established importer.
    Refunds retain that importer's existing matching/integrity protections.
    """
    from app.services.v2_ordering_square_gateway import _parse_datetime
    if any(not isinstance(order,dict) for order in orders):
        raise ValueError('Square orders page contains a non-object order; history not certified')
    ids=[str(o.get('id') or '') for o in orders]
    originals=defaultdict(dict)
    for row in db.scalars(select(ConsignmentSaleFact).where(ConsignmentSaleFact.square_order_id.in_(ids))).all():
        originals[row.square_order_id][row.square_line_item_uid]=row
    corrections={(r.kind,r.square_order_id,r.square_line_item_uid):r for r in db.scalars(select(PurchasingSaleCorrection).where(PurchasingSaleCorrection.square_order_id.in_(ids))).all()}
    from app.models import ConsignmentReturnFact, AuditLog
    returns={(r.square_return_order_id,r.square_return_uid,r.square_return_line_uid):r for r in db.scalars(
        select(ConsignmentReturnFact).where(ConsignmentReturnFact.square_return_order_id.in_(ids))).all()}
    states={r.square_order_id:r for r in db.scalars(select(PurchasingOrderSourceState).where(PurchasingOrderSourceState.square_order_id.in_(ids)).with_for_update()).all()}
    def audit(oid,uid,version,reason,digest):
        db.add(AuditLog(actor_principal_id=actor_id,action='PURCHASING_SOURCE_CORRECTION',
            meta={'square_order_id':oid,'line_uid':uid,'source_version':version,'reason':reason,'source_digest':digest}))
    financial=[];changed=0
    for order in orders:
        oid=str(order.get('id') or '').strip();loc=str(order.get('location_id') or '').strip();version=order.get('version')
        stamp=_parse_datetime(order.get('closed_at') or order.get('updated_at') or order.get('created_at'))
        from scripts.release.rfc3339 import timestamp
        label=f"Square order {oid or '<missing id>'}"
        if not isinstance(order.get('id'),str) or not isinstance(order.get('location_id'),str) or not oid or not loc or stamp is None:
            raise ValueError(f'{label}: missing/invalid id, location_id or transaction timestamp; history not certified')
        if version is not None and (type(version) is not int or not 0 <= version <= 2147483647):
            raise ValueError(f'{label}: version must be a nonnegative 32-bit integer when supplied; history not certified')
        source_time=order.get('updated_at') or order.get('created_at') or order.get('closed_at')
        try:
            clock=timestamp(source_time)
            for name in ('created_at','updated_at','closed_at'):
                if order.get(name) is not None:timestamp(order[name])
        except (ValueError,TypeError,OverflowError) as exc:
            raise ValueError(f'{label}: missing/invalid RFC3339 source timestamp; history not certified') from exc
        if version is None and not order.get('updated_at'):
            raise ValueError(f'{label}: unversioned order requires updated_at for safe update ordering')
        if not isinstance(order.get('state'),str) or order['state'] not in ('OPEN','DRAFT','COMPLETED','CANCELED'):
            raise ValueError(f'{label}: missing/invalid state; history not certified')
        digest=hashlib.sha256(json.dumps(order,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        previous=states.get(oid)
        if previous is not None:
            if previous.location_id!=loc:
                raise ValueError(f'{label}: location_id conflicts with retained source identity')
            previous_clock=timestamp(previous.source_updated_at)
            if version is not None and previous.order_version is not None:
                # Square's explicit revision counter takes precedence when both
                # observations supply it; timestamps order unversioned updates.
                if version<previous.order_version or (version==previous.order_version and clock<previous_clock):
                    continue
            elif clock<previous_clock:
                continue
            same_version=version==previous.order_version
            if clock==previous_clock and same_version:
                if digest!=previous.source_digest:
                    raise ValueError(f'{label}: conflicting payloads at the same source timestamp/version; review captured responses')
                continue
        # Bootstrap from immutable existing facts without inventing a version.
        known_versions=[old.source_order_version for old in originals[oid].values() if old.source_order_version is not None]
        if version is not None and known_versions and max(known_versions)>version:
            continue
        if any(version is not None and r.order_version is not None and r.order_version>version
               for key,r in corrections.items() if key[1]==oid):
            continue
        if previous is None:
            previous=PurchasingOrderSourceState(square_order_id=oid)
            db.add(previous);states[oid]=previous
        previous.location_id=loc;previous.order_version=version
        previous.source_updated_at=source_time;previous.source_digest=digest;previous.synchronized_at=synchronized_at
        completed=order.get('state')=='COMPLETED';lines=order.get('line_items') or [];seen=set();retained=[]
        if not isinstance(lines,list):raise ValueError(f'{label}: line_items must be an array; history not certified')
        for line in lines:
            if not isinstance(line,dict):raise ValueError(f'{label}: line_items contains a non-object line; history not certified')
            uid=str(line.get('uid') or '').strip();qty=quantity(line.get('quantity'));vid=str(line.get('catalog_object_id') or '').strip() or None
            if not isinstance(line.get('uid'),str) or not uid or qty is None or qty<0:raise ValueError(f"{label}: line {uid or '<missing uid>'} has missing/invalid uid or quantity; history not certified")
            if uid in seen:raise ValueError(f'{label}: duplicate line uid {uid}; history not certified')
            seen.add(uid);old=originals[oid].get(uid);current=corrections.get(('LINE',oid,uid));qty=qty if completed else ZERO
            source_digest=hashlib.sha256(json.dumps(line,sort_keys=True).encode()).hexdigest()
            money_changed=old is not None and any(Decimal(str((line.get(source) or {}).get('amount',0)))/100 != getattr(old,target) for source,target in [('gross_sales_money','gross_sales_amount'),('total_discount_money','discount_amount'),('total_tax_money','tax_amount'),('total_money','net_sales_amount')])
            extra_precision=qty != qty.quantize(Decimal('.001'))
            differs=extra_precision or money_changed or old is not None and (old.quantity_sold!=qty or old.square_variation_id!=vid or old.square_location_id!=loc or old.business_date!=stamp.astimezone(BUSINESS_TIMEZONE).date())
            if differs or current is not None:
                if current is None:
                    current=PurchasingSaleCorrection(kind='LINE',square_order_id=oid,square_line_item_uid=uid);db.add(current);corrections[('LINE',oid,uid)]=current
                reason='SOURCE_RESTORED' if not differs else 'SOURCE_MONEY_CHANGED' if money_changed else 'SOURCE_LINE_CHANGED'
                if current.order_version!=version or current.source_digest!=source_digest or current.reason!=reason:audit(oid,uid,version,reason,source_digest)
                current.reason=reason;current.source_digest=source_digest
                current.location_id=loc;current.variation_id=vid;current.business_date=stamp.astimezone(BUSINESS_TIMEZONE).date();current.quantity=qty;current.order_version=version;current.synchronized_at=synchronized_at
                changed+=1
            if completed and (old is None or not differs):retained.append(line)
        # Removed lines and a completed order later cancelled become explicit zero source units.
        for uid,old in originals[oid].items():
            if uid in seen:continue
            current=corrections.get(('LINE',oid,uid))
            if current is None:
                current=PurchasingSaleCorrection(kind='LINE',square_order_id=oid,square_line_item_uid=uid);db.add(current);corrections[('LINE',oid,uid)]=current
            digest=hashlib.sha256((oid+uid+str(version)+'REMOVED').encode()).hexdigest()
            if current.order_version!=version or current.source_digest!=digest:audit(oid,uid,version,'SOURCE_LINE_REMOVED',digest)
            current.reason='SOURCE_LINE_REMOVED';current.source_digest=digest
            current.location_id=old.square_location_id;current.variation_id=old.square_variation_id;current.business_date=old.business_date;current.quantity=ZERO;current.order_version=version;current.synchronized_at=synchronized_at
            changed+=1
        # Returns never subtract gross demand. Changed historical return money or
        # quantities require financial review, not a rewrite of finalized facts.
        incoming_returns={}
        for r in order.get('returns') or []:
            for line in r.get('return_line_items') or []:
                incoming_returns[(oid,str(r.get('uid')),str(line.get('uid')))]=(quantity(line.get('quantity')),Decimal(str((line.get('total_money') or {}).get('amount',0)))/100)
        for refund in order.get('refunds') or []:
            if not order.get('returns'):
                incoming_returns[(oid,str(refund.get('id')),'UNITEMIZED_REFUND')]=(None,Decimal(str((refund.get('amount_money') or {}).get('amount',0)))/100)
        for key,old_return in returns.items():
            if key[0]!=oid:continue
            incoming=incoming_returns.get(key)
            uid=json.dumps(key[1:],separators=(',',':'))
            restored=incoming==(old_return.quantity_returned,old_return.refund_amount)
            if restored and ('RETURN',oid,uid) not in corrections:continue
            digest=hashlib.sha256(json.dumps(incoming,default=str).encode()).hexdigest()
            current=corrections.get(('RETURN',oid,uid))
            if current is None:
                current=PurchasingSaleCorrection(kind='RETURN',square_order_id=oid,square_line_item_uid=uid);db.add(current);corrections[('RETURN',oid,uid)]=current
            if current.order_version!=version or current.source_digest!=digest:audit(oid,uid,version,'RETURN_SOURCE_CHANGED',digest)
            current.location_id=old_return.square_location_id;current.variation_id=old_return.square_variation_id;current.business_date=old_return.business_date;current.quantity=ZERO;current.order_version=version;current.reason='SOURCE_RESTORED' if restored else 'RETURN_SOURCE_CHANGED';current.source_digest=digest;current.synchronized_at=synchronized_at
            changed+=1
        if completed:financial.append({**order,'line_items':retained})
    db.flush()
    return financial,changed


from app.services.v2_ordering_square_gateway import SquareOrderingReadGateway


class DatabaseHistoryGateway(SquareOrderingReadGateway):
    """Live Square catalog/counts plus verified reusable historical source facts."""
    def __init__(self, db, post=None):
        super().__init__(post)
        self.db=db
        self._history_through={}

    def _daily_sales(self, location_ids, variation_ids, *, as_of, history_days=56):
        end=datetime.combine(as_of.astimezone(BUSINESS_TIMEZONE).date(), datetime.min.time(), BUSINESS_TIMEZONE)
        return daily_sales(self.db,locations=location_ids,variations=variation_ids,
            start_at=end-timedelta(days=history_days),end_at=end,as_of=as_of)

    def _inventory_deltas(self, location_ids, variation_ids, *, as_of, history_days=56, include_today=False, parallel_history=False):
        end=datetime.combine(as_of.astimezone(BUSINESS_TIMEZONE).date(), datetime.min.time(), BUSINESS_TIMEZONE)
        values,uncertain,through=movement_evidence(self.db,locations=location_ids,variations=variation_ids,
            start_at=end-timedelta(days=history_days),as_of=as_of)
        self._uncertain_history_pairs=uncertain;self._history_through=through
        return values

    def fetch(self, **kwargs):
        from dataclasses import replace
        result=super().fetch(**kwargs)
        # Current counts can be newer than the background movement checkpoint.
        # Never backcast availability using an unobserved current-day tail.
        locations=kwargs['location_by_store'];today=kwargs['as_of'].astimezone(BUSINESS_TIMEZONE).date()
        for key,row in result.by_store_variation.items():
            if row.current_on_hand is None:
                continue
            through=self._history_through.get(locations[key[0]])
            if through is None or row.source_calculated_at is None or utc(row.source_calculated_at)>through or through.astimezone(BUSINESS_TIMEZONE).date()<today:
                sources=tuple(replace(s,complete=False,detail='Movement refresh required: current Square count is newer than verified history.') if s.source=='stockout_history' else s for s in row.required_sources)
                result.by_store_variation[key]=replace(row,current_day_inventory_delta=None,required_sources=sources)
        return result


def history_post(path, payload):
    """Bounded read retries at the existing Square policy/HTTP boundary."""
    from app.services.square_ordering_data_service import _square_post
    from urllib.error import HTTPError
    import time
    for attempt in range(3):
        try:return _square_post(path,payload,timeout_seconds=30)
        except RuntimeError as exc:
            cause=exc.__cause__
            retryable=(isinstance(cause,HTTPError) and cause.code in (429,502,503,504))
            if not retryable or attempt==2:raise
            try:delay=min(5,max(0,float(cause.headers.get('Retry-After',2**attempt))))
            except (TypeError,ValueError):delay=2**attempt
            time.sleep(delay)


def synchronize_movements(db, *, locations, start_at, end_at, post=None):
    """Existing Square Data job's incremental stockout-evidence stage.

    Per-store checkpoints permit bounded repeatable source intervals. A failed
    stage/transaction cannot advance coverage. No inventory counts are written.
    """
    from time import monotonic
    post=post or history_post
    for location in sorted(set(locations)):
        scope=db.get(PurchasingHistoryCoverage,('MOVEMENTS',location))
        begin=max(start_at,utc(scope.through_at)-OVERLAP) if scope and utc(scope.start_at)<=start_at else start_at
        cursor=None;seen=set();pages=0;deadline=monotonic()+300
        while True:
            if monotonic()>deadline:raise RuntimeError('Inventory history interval exceeded its five-minute budget')
            payload={'location_ids':[location],'types':['ADJUSTMENT','PHYSICAL_COUNT'],
                'updated_after':(utc(begin)-timedelta(microseconds=1)).isoformat().replace('+00:00','Z'),
                'updated_before':utc(end_at).isoformat().replace('+00:00','Z'),'limit':1000}
            if cursor:payload['cursor']=cursor
            response=post('/v2/inventory/changes/batch-retrieve',payload);pages+=1
            if response.get('errors') or not isinstance(response.get('changes',[]),list):raise ValueError('Incomplete inventory history page')
            changes=response.get('changes') or []
            for c in changes:
                item=c.get('adjustment') or c.get('physical_count') or {}
                if item.get('location_id')!=location:raise ValueError('Inventory history outside requested location')
            import_movement_page(db,changes,synchronized_at=end_at,scope_start_at=utc(begin)-timedelta(microseconds=1))
            cursor=response.get('cursor')
            if cursor in seen:raise ValueError('Repeated inventory history cursor')
            if cursor:seen.add(cursor)
            if not cursor:break
            if pages>=10000:raise ValueError('Inventory history interval exceeds bounded job budget')
        certify(db,source='MOVEMENTS',locations=[location],start_at=begin,through_at=end_at,verified_at=datetime.now(timezone.utc))
