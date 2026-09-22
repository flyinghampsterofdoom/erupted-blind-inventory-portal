"""Blind projections, acknowledged drafts and independent physical observations."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from uuid import uuid4

from sqlalchemy import select, delete
from sqlalchemy.orm import Session

from app.auth import assert_store_scope
from app.models import (AuditLog, CountSession, Entry, SnapshotLine, SessionStatus, Store,
    CountObservation, CountCorrection, CountCorrectionAttempt, CountReview,
    StoreRecountItem, StoreRecountState)
from app.services.audit_service import log_audit
from app.services.count_square_sync_service import _SquareClient

EXPLANATIONS = ('Mis-ring / wrong variant', 'Wrong product handed out', 'Damage',
                'Receiving discrepancy', 'Transfer discrepancy', 'Store use', 'Unable to determine', 'Other')


class DraftConflict(ValueError):
    pass


def now():
    return datetime.now(timezone.utc)


def quantity(raw):
    if raw is None or str(raw).strip() == '':
        return None
    try:
        value = Decimal(str(raw))
    except InvalidOperation as exc:
        raise ValueError('Enter a nonnegative quantity with at most three decimals.') from exc
    if not value.is_finite() or value < 0 or value >= Decimal('10000000000') or value != value.quantize(Decimal('.001')):
        raise ValueError('Enter a nonnegative quantity below 10 billion with at most three decimals.')
    return value.quantize(Decimal('.001'))


def locked_count(db, principal, session_id):
    row = db.scalar(select(CountSession).where(CountSession.id == session_id).with_for_update().execution_options(populate_existing=True))
    if row is None:
        raise ValueError('Count not found.')
    assert_store_scope(principal, row.store_id)
    return row


def employee_rows(db, session_id):
    # Deliberate allowlist: never query expected inventory or previous observations.
    rows = db.execute(select(SnapshotLine.variation_id, SnapshotLine.item_name,
        SnapshotLine.variation_name, SnapshotLine.section_type, Entry.front_qty, Entry.back_qty)
        .outerjoin(Entry, (Entry.session_id == SnapshotLine.session_id) & (Entry.variation_id == SnapshotLine.variation_id))
        .where(SnapshotLine.session_id == session_id)
        .order_by(SnapshotLine.section_type, SnapshotLine.item_name, SnapshotLine.variation_name)).all()
    return [dict(variation_id=r.variation_id, item_name=r.item_name, variation_name=r.variation_name,
        section_type=r.section_type.value, front_qty=str(r.front_qty) if r.front_qty is not None else None,
        back_qty=str(r.back_qty) if r.back_qty is not None else None) for r in rows]


def save_draft(db, *, principal, session_id, revision, changes, operation_id):
    count = locked_count(db, principal, session_id)
    if count.status != SessionStatus.DRAFT or count.observation_closed:
        raise ValueError('This observation is closed. Start a new count round.')
    if not isinstance(revision, int) or isinstance(revision, bool):
        raise ValueError('A draft revision is required.')
    if not isinstance(changes, dict) or len(changes) > 10000:
        raise ValueError('Invalid count changes.')
    if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 64:
        raise ValueError('A save operation ID is required.')
    digest = hashlib.sha256(json.dumps(changes, sort_keys=True).encode()).hexdigest()
    previous = db.scalar(select(AuditLog).where(AuditLog.session_id == session_id,
        AuditLog.actor_principal_id == principal.id, AuditLog.action == 'BLIND_COUNT_DRAFT_ACK',
        AuditLog.meta['operation_id'].as_string() == operation_id))
    if previous is not None:
        if previous.meta['digest'] != digest or previous.meta['response']['revision'] != count.draft_revision:
            raise DraftConflict('Saved operation conflicts with newer work. Reload and reconcile.')
        return previous.meta['response']
    if revision != count.draft_revision:
        raise DraftConflict('This count changed in another window. Reload and reconcile before saving.')
    valid = set(db.scalars(select(SnapshotLine.variation_id).where(SnapshotLine.session_id == session_id)))
    errors, changed = {}, {}
    for variation_id, fields in changes.items():
        if variation_id not in valid or not isinstance(fields, dict):
            raise ValueError('Product is not part of this count.')
        entry = db.get(Entry, (session_id, variation_id))
        before = {key: str(getattr(entry, key)) if entry is not None and getattr(entry, key) is not None else None for key in ('front_qty','back_qty','counted_qty')}
        accepted = {}
        for key, raw in fields.items():
            if key not in ('front_qty','back_qty'):
                raise ValueError('Unsupported count field.')
            try:
                accepted[key] = quantity(raw)
            except ValueError as exc:
                errors[f'{variation_id}:{key}'] = str(exc)
        if not accepted:
            continue
        if entry is None:
            entry = Entry(session_id=session_id, variation_id=variation_id, counted_qty=None, updated_by_principal_id=principal.id)
            db.add(entry)
        for key, value in accepted.items():
            setattr(entry, key, value)
        parts_changed = any(before[key] != (str(getattr(entry,key)) if getattr(entry,key) is not None else None) for key in ('front_qty','back_qty'))
        # An untouched legacy draft retains its saved total without inventing a split.
        if parts_changed or entry.front_qty is not None or entry.back_qty is not None:
            entry.counted_qty = entry.front_qty + entry.back_qty if entry.front_qty is not None and entry.back_qty is not None else None
        if entry.counted_qty is not None and entry.counted_qty >= Decimal('100000000000'):
            raise ValueError('Total quantity is too large.')
        after = {key: str(getattr(entry, key)) if getattr(entry, key) is not None else None for key in ('front_qty','back_qty','counted_qty')}
        if before != after:
            entry.updated_by_principal_id = principal.id
            entry.updated_at = now()
            changed[variation_id] = {'before': before, 'after': after}
    if changed:
        count.draft_revision += 1
        count.updated_at = now()
        log_audit(db, actor_principal_id=principal.id, action='BLIND_COUNT_DRAFT_CHANGED',
            session_id=count.id, ip=None, metadata={'operation_id':operation_id, 'revision':count.draft_revision,'changes':changed})
    response = {'revision':count.draft_revision, 'errors':errors, 'operation_id':operation_id}
    log_audit(db, actor_principal_id=principal.id, action='BLIND_COUNT_DRAFT_ACK', session_id=count.id, ip=None,
        metadata={'operation_id':operation_id,'digest':digest,'response':response})
    db.flush()
    return response


def submit_round(db, *, principal, session_id, revision, provider):
    count = locked_count(db, principal, session_id)
    # An HTTP replay never refetches inventory or advances the qualifying history.
    if count.observation_closed:
        pending_ids = list(db.scalars(select(CountCorrection.id)
            .join(CountObservation,CountObservation.id==CountCorrection.trigger_observation_id)
            .where(CountObservation.session_id==count.id,CountCorrection.status!='SUCCESS')))
        return count, pending_ids
    if count.status != SessionStatus.DRAFT or count.draft_revision != revision:
        raise DraftConflict('Save the latest count before submitting.')
    # Serialize all round evaluation and corrections for this store.
    store = db.scalar(select(Store).where(Store.id == count.store_id).with_for_update())
    lines = db.scalars(select(SnapshotLine).where(SnapshotLine.session_id == count.id)).all()
    entries = {r.variation_id:r for r in db.scalars(select(Entry).where(Entry.session_id == count.id))}
    if not lines or any(r.variation_id not in entries or entries[r.variation_id].front_qty is None or entries[r.variation_id].back_qty is None for r in lines):
        raise ValueError('Enter both Front Stock and Back Stock for every product. Enter 0 explicitly where appropriate.')
    fetched_at = now()
    provider_error = None
    try:
        inventory = provider.fetch_current_on_hand(store_id=count.store_id, variation_ids=[r.variation_id for r in lines])
    except (RuntimeError, ValueError, OSError) as exc:
        inventory = {}
        provider_error = type(exc).__name__
    observations, correction_ids = [], []
    for line in lines:
        entry = entries[line.variation_id]
        expected = inventory.get(line.variation_id)
        if expected is not None:
            try:
                expected = Decimal(str(expected))
                if not expected.is_finite() or abs(expected) >= Decimal('100000000000'): expected = None
            except InvalidOperation:
                expected = None
        total = entry.front_qty + entry.back_qty
        variance = total - expected if expected is not None else None
        line.expected_on_hand = expected
        line.previous_recount_variance = None
        observation = CountObservation(session_id=count.id, store_id=count.store_id,
            variation_id=line.variation_id, sku=line.sku, item_name=line.item_name,
            variation_name=line.variation_name, front_qty=entry.front_qty, back_qty=entry.back_qty,
            total_qty=total, expected_qty=expected, variance=variance,
            expected_provenance={'provider':type(provider).__name__, 'location_id':store.square_location_id,
                'fetched_at':fetched_at.isoformat(), 'state':'KNOWN' if expected is not None else 'UNKNOWN', 'error':provider_error},
            counted_by_principal_id=entry.updated_by_principal_id, submitted_by_principal_id=principal.id,
            observed_at=entry.updated_at, submitted_at=fetched_at)
        db.add(observation); db.flush(); observations.append(observation)
        queue = db.get(StoreRecountItem,(count.store_id,line.variation_id))
        if variance == 0:
            if queue is not None: db.delete(queue)
            continue
        if queue is None:
            queue = StoreRecountItem(store_id=count.store_id, variation_id=line.variation_id,
                sku=line.sku,item_name=line.item_name,variation_name=line.variation_name,
                last_variance=variance,last_counted_qty=total,
                consecutive_match_count=0,total_count_attempts=0)
            db.add(queue)
        # UNKNOWN is retained as an observation and breaks the streak, never later rebased.
        queue.last_counted_qty = total
        queue.total_count_attempts += 1
        queue.consecutive_match_count = (queue.consecutive_match_count + 1 if variance is not None and queue.last_variance == variance else (1 if variance is not None else 0))
        queue.last_variance = variance
        history = db.scalars(select(CountObservation).where(
            CountObservation.store_id == count.store_id, CountObservation.variation_id == line.variation_id)
            .order_by(CountObservation.id.desc()).limit(3)).all()
        pending = db.scalar(select(CountCorrection).where(CountCorrection.store_id == count.store_id,
            CountCorrection.variation_id == line.variation_id, CountCorrection.status != 'SUCCESS'))
        prior_success = db.scalar(select(CountCorrection.trigger_observation_id).where(
            CountCorrection.store_id == count.store_id,CountCorrection.variation_id == line.variation_id,
            CountCorrection.status == 'SUCCESS').order_by(CountCorrection.id.desc()).limit(1))
        qualifies = (variance is not None and variance != 0 and len(history)==3
            and all(h.variance==variance for h in history)
            and len({h.session_id for h in history})==3
            and all(h.id > (prior_success or 0) for h in history))
        if qualifies and pending is None:
            operation_id = f'blind-count-{uuid4().hex}'
            correction = CountCorrection(trigger_observation_id=observation.id,
                observation_ids=[h.id for h in reversed(history)],store_id=count.store_id,
                variation_id=line.variation_id,operation_id=operation_id,status='PENDING',attempts=0,
                request_payload={'idempotency_key':operation_id,'changes':[{'type':'PHYSICAL_COUNT','physical_count':{
                    'catalog_object_id':line.variation_id,'location_id':store.square_location_id,
                    'state':'IN_STOCK','quantity':format(total,'f'),'occurred_at':fetched_at.isoformat()}}],
                    'ignore_unchanged_counts':False})
            db.add(correction); db.flush(); correction_ids.append(correction.id)
            log_audit(db,actor_principal_id=principal.id,action='BLIND_COUNT_CORRECTION_PREPARED',session_id=count.id,ip=None,
                metadata={'correction_id':correction.id,'operation_id':operation_id,'observation_ids':correction.observation_ids})
    count.observation_closed = True
    count.status = SessionStatus.SUBMITTED
    count.submitted_at = fetched_at
    count.submitted_by_principal_id = principal.id
    count.submit_inventory_fetched_at = fetched_at
    count.updated_at = fetched_at
    state = db.get(StoreRecountState,count.store_id)
    if state is None:
        state=StoreRecountState(store_id=count.store_id); db.add(state)
    db.flush()
    state.is_active = db.scalar(select(StoreRecountItem.variation_id).where(StoreRecountItem.store_id==count.store_id).limit(1)) is not None
    log_audit(db,actor_principal_id=principal.id,action='BLIND_COUNT_OBSERVATIONS_SUBMITTED',session_id=count.id,ip=None,
        metadata={'observation_ids':[o.id for o in observations], 'unknown_expected_count':sum(o.expected_qty is None for o in observations)})
    db.flush()
    return count, correction_ids


def execute_correction(db, *, correction_id, actor_id, client=None):
    """Caller MUST commit preparation before entry. Retry preserves exact key/payload/time."""
    correction = db.scalar(select(CountCorrection).where(CountCorrection.id==correction_id).with_for_update().execution_options(populate_existing=True))
    if correction is None: raise ValueError('Correction not found.')
    if correction.status == 'SUCCESS': return correction
    db.scalar(select(Store).where(Store.id==correction.store_id).with_for_update())
    attempt = CountCorrectionAttempt(correction_id=correction.id,actor_principal_id=actor_id,status='PENDING')
    db.add(attempt)
    correction.attempts += 1
    try:
        if client is None:
            from app.config import settings
            trigger = db.get(CountObservation, correction.trigger_observation_id)
            if settings.snapshot_provider.strip().lower() != 'square' or trigger.expected_provenance.get('provider') != 'SquareSnapshotProvider':
                raise RuntimeError('Live inventory correction requires Square-sourced count evidence.')
        response = (client or _SquareClient()).post('/v2/inventory/changes/batch-create',correction.request_payload,inventory_quantity_write=True)
        correction.status='SUCCESS'; correction.response_payload=response; correction.error_text=None; correction.succeeded_at=now()
        attempt.status='SUCCESS'; attempt.response_payload=response
        if db.scalar(select(CountReview.id).where(CountReview.correction_id==correction.id)) is None:
            db.add(CountReview(correction_id=correction.id,status='OPEN',related_correction_ids=[]))
        # Do not erase a newer physical discrepancy that arrived while a retry was pending.
        latest=db.scalar(select(CountObservation.id).where(CountObservation.store_id==correction.store_id,
            CountObservation.variation_id==correction.variation_id).order_by(CountObservation.id.desc()).limit(1))
        if latest == correction.trigger_observation_id:
            db.execute(delete(StoreRecountItem).where(StoreRecountItem.store_id==correction.store_id,StoreRecountItem.variation_id==correction.variation_id))
        trigger=db.get(CountObservation,correction.trigger_observation_id)
        snapshot=db.get(SnapshotLine,(trigger.session_id,trigger.variation_id))
        snapshot.recount_closed_out=True
        count=db.get(CountSession,trigger.session_id); count.stable_variance=True
        state=db.get(StoreRecountState,correction.store_id)
        if state is not None:
            state.is_active=db.scalar(select(StoreRecountItem.variation_id).where(StoreRecountItem.store_id==correction.store_id).limit(1)) is not None
    except (RuntimeError, ValueError, OSError) as exc:
        correction.status='FAILED'; correction.error_text=str(exc)
        attempt.status='FAILED'; attempt.error_text=str(exc)
    log_audit(db,actor_principal_id=actor_id,action=f'BLIND_COUNT_CORRECTION_{correction.status}',session_id=None,ip=None,
        metadata={'correction_id':correction.id,'operation_id':correction.operation_id,'attempt':correction.attempts})
    db.commit()
    return correction


def review_correction(db, *, correction_id, principal, explanation, notes, related_ids):
    review=db.scalar(select(CountReview).where(CountReview.correction_id==correction_id).with_for_update())
    if review is None: raise ValueError('Only confirmed corrections can be reviewed.')
    if review.status != 'OPEN': raise ValueError('This review is already completed.')
    correction=db.get(CountCorrection,correction_id)
    if explanation not in EXPLANATIONS or not notes.strip() or len(notes)>5000:
        raise ValueError('Select an explanation and enter a review note (up to 5,000 characters).')
    related=sorted(set(related_ids))
    if len(related)>20: raise ValueError('Select at most 20 related corrections.')
    for other_id in related:
        other=db.get(CountCorrection,other_id)
        if other is None or other.id==correction.id or other.store_id!=correction.store_id or other.status!='SUCCESS':
            raise ValueError('Related corrections must be confirmed corrections at the same store.')
    review.status='REVIEWED'; review.explanation=explanation; review.notes=notes.strip()
    review.related_correction_ids=related; review.reviewed_by_principal_id=principal.id; review.reviewed_at=now()
    log_audit(db,actor_principal_id=principal.id,action='BLIND_COUNT_LEAD_REVIEWED',session_id=None,ip=None,
        metadata={'correction_id':correction.id,'before':{'status':'OPEN'},'after':{'status':'REVIEWED','explanation':explanation,'notes':review.notes,'related_correction_ids':related}})
    db.flush()


def review_queue(db):
    rows = db.execute(select(CountCorrection, CountObservation, CountReview, Store.name)
        .join(CountObservation,CountObservation.id==CountCorrection.trigger_observation_id)
        .join(Store,Store.id==CountCorrection.store_id)
        .outerjoin(CountReview,CountReview.correction_id==CountCorrection.id)
        .order_by(CountCorrection.id.desc())).all()
    return [dict(correction=c,observation=o,review=r,store_name=name) for c,o,r,name in rows]


def correction_detail(db, correction_id):
    from app.models import Principal as PrincipalModel
    correction=db.get(CountCorrection,correction_id)
    if correction is None: raise ValueError('Correction not found.')
    review=db.scalar(select(CountReview).where(CountReview.correction_id==correction_id))
    observations=db.scalars(select(CountObservation).where(CountObservation.id.in_(correction.observation_ids)).order_by(CountObservation.id)).all()
    actors=set(o.counted_by_principal_id for o in observations) | set(o.submitted_by_principal_id for o in observations)
    if review and review.reviewed_by_principal_id: actors.add(review.reviewed_by_principal_id)
    names={row.id:row.username for row in db.scalars(select(PrincipalModel).where(PrincipalModel.id.in_(actors)))}
    trigger=db.get(CountObservation,correction.trigger_observation_id)
    related=db.execute(select(CountCorrection,CountObservation)
        .join(CountObservation,CountObservation.id==CountCorrection.trigger_observation_id)
        .where(CountCorrection.store_id==correction.store_id,CountCorrection.id!=correction.id,
            CountObservation.item_name==trigger.item_name, CountCorrection.status=='SUCCESS')
        .order_by(CountCorrection.id.desc()).limit(30)).all()
    attempts=db.scalars(select(CountCorrectionAttempt).where(CountCorrectionAttempt.correction_id==correction.id).order_by(CountCorrectionAttempt.id)).all()
    return dict(correction=correction,review=review,observations=observations,actors=names,
        store=db.get(Store,correction.store_id), related=related, attempts=attempts,
        explanations=EXPLANATIONS)
