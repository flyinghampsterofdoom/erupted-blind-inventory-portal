"""Explicit future weekly lifecycle, never an in-process scheduler.

Local callers inject collection, pressure and backup adapters. CLI requires two
disabled-by-default execution flags and all operational prerequisites. No cron
or paid worker is created. One source is reused across every due horizon.
"""
from datetime import date, datetime, timedelta, timezone
import time

from sqlalchemy import select

from .contracts import EvidenceError, TZ, stamp, validate
from .operations import Period, SCOPE_POLICY
from .codec import materialize
from .runner import forecast
from .evaluation import evaluate
from .shared import FrozenSource


def source_preflight(engine, *, now=None):
    """SELECT-only coverage check before any Square requests or evidence writes."""
    from app.models import Store as BusinessStore, PurchasingHistoryCoverage, ConsignmentSalesSyncState
    from .collection import source_session, readonly
    now = stamp(now or datetime.now(timezone.utc))
    today = now.astimezone(TZ).date()
    through = datetime.combine(today, datetime.min.time(), TZ)
    first = through - timedelta(days=84)
    with source_session(engine) as db:
        readonly(db)
        stores = [s for s in db.scalars(select(BusinessStore).where(BusinessStore.active.is_(True)))
                  if ''.join(c for c in s.name.lower() if c.isalnum()) in
                  ('andresen', 'hwy99', 'highway99', 'sr503', 'longview')]
        locations = {s.square_location_id for s in stores}
        if len(stores) != 4 or len(locations) != 4 or None in locations:
            raise EvidenceError('Four unambiguous store identities required before collection')
        scopes = {(r.source, r.location_id): r for r in db.scalars(select(PurchasingHistoryCoverage))}
        sync = db.get(ConsignmentSalesSyncState, 1)
        if sync and sync.last_result in ('FAILED', 'RUNNING'):
            raise EvidenceError('Business source synchronization is failed or active; no collection started')
        for source in ('SALES', 'MOVEMENTS'):
            for location in locations:
                row = scopes.get((source, location))
                if row is None:
                    raise EvidenceError('Required source coverage is missing; no collection started')
                # Database fixture adapters may return naive UTC timestamps.
                aware = lambda v: stamp(v.replace(tzinfo=timezone.utc) if v.tzinfo is None else v)
                if not (aware(row.start_at) <= first and through <= aware(row.through_at) <= aware(row.verified_at) <= now
                        and now - aware(row.verified_at) <= timedelta(hours=24)):
                    raise EvidenceError('Required completed-day source coverage is stale or incomplete; no collection started')
    return {'status': 'COMPLETE', 'history_start': first.isoformat(), 'through': through.isoformat()}


def run_period(store, start, *, collect_source, pressure, backup, now=None, evaluate_outcomes=True,
               notify=None, budget_seconds=1200, forecast_budget_seconds=30, scope_policy=SCOPE_POLICY):
    if pressure is None or backup is None:
        raise EvidenceError('Operational priority and verified-backup adapters are required')
    if (isinstance(forecast_budget_seconds, bool) or not isinstance(forecast_budget_seconds, (int, float))
            or not 0 < forecast_budget_seconds <= min(300, budget_seconds)):
        raise EvidenceError('Explicit bounded forecast execution budget required')
    if not isinstance(scope_policy, str) or not scope_policy or len(scope_policy) > 120:
        raise EvidenceError('Explicit bounded evidence scope required')
    began = time.monotonic()
    current = lambda: stamp(now or datetime.now(timezone.utc))
    start = date.fromisoformat(str(start))
    if current().astimezone(TZ).date() < start or current().astimezone(TZ).date() >= start + timedelta(days=7):
        raise EvidenceError('No backdated or future weekly execution')
    period = Period(store, start, scope_policy=scope_policy, now=current())
    def allowed():
        if time.monotonic() - began > budget_seconds:
            raise TimeoutError('Weekly execution deadline exhausted')
        if pressure() is not True:
            raise EvidenceError('Forecasting yields to operational business work')
    with period.lock():
        if period.state()['status'] == 'COMPLETE':
            return {'status': 'COMPLETE', 'idempotent': True, **period.publication()}
        period.claim()
        try:
            allowed()
            frozen = period.source()
            publication = period.publication()
            if frozen is None:
                if period.request_count() >= 400:
                    raise EvidenceError('Durable weekly Square budget exhausted before collection')
                snapshot = collect_source(period, 400 - period.request_count())
                validate(snapshot, now=current())
                if not all(row['sales_complete'] for row in snapshot['rows']):
                    raise EvidenceError('Weekly source sales windows are incomplete')
                allowed()
                source_id = period.freeze(snapshot)
            else:
                source_id = frozen['artifact_id']
                snapshot = materialize(store.stream_artifact(source_id))
            source = FrozenSource(snapshot, artifact=source_id, verified=True)
            if not publication:
                allowed()
                run_id = forecast(store, snapshot, now=current(), publication=period.publication_context(source_id),
                                  budget_seconds=forecast_budget_seconds)
                publication = period.publication()
                if not publication or publication['run_id'] != run_id:
                    raise EvidenceError('Weekly publication mapping is incomplete')
            results = []
            if evaluate_outcomes:
                first = date.fromisoformat(snapshot['rows'][0]['history_start'])
                # At most78retained weekly origins. Stop instead of silently
                # skipping an unexpectedly large history or scope.
                for page in range(4):
                    origins = store.list_runs(limit=20, page=page)
                    for run in origins:
                        if run['kind'] == 'SYNTHETIC':
                            continue
                        summary = store.get_run_summary(run['id'])
                        evaluation_start = date.fromisoformat(summary['snapshot']['evaluation_start'])
                        for horizon in (28, 56):
                            end = evaluation_start + timedelta(days=horizon)
                            if stamp(snapshot['cutoff']) < datetime.combine(end, datetime.min.time(), TZ):
                                continue
                            previous = store.list_evaluation_meta(run['id'], horizon=horizon, limit=1)
                            if evaluation_start < first:
                                if not previous:
                                    raise EvidenceError('A mature outcome was missed beyond the source window; certified recovery evidence required')
                                continue
                            allowed()
                            outcome = source.outcome(store, run['id'], horizon,
                                supersedes_id=previous[0]['id'] if previous else None)
                            outcome['revision'] = source_id
                            ident = evaluate(store, run['id'], outcome, now=current())
                            results.append({'run_id': run['id'], 'horizon': horizon, 'evaluation_id': ident})
                    if len(origins) < 20:
                        break
                else:
                    raise EvidenceError('Retention review required before weekly history exceeds80origins')
                period.record('EVALUATING', {'results': results})
            allowed()
            receipt = backup(period, publication)
            if not receipt.get('verified') or not receipt.get('encrypted_sha256'):
                raise EvidenceError('Encrypted evidence backup has not been verified')
            if store.production and (receipt.get('off_device_copy_verified') is not True
                                     or receipt.get('independent_key_custody_verified') is not True):
                raise EvidenceError('Production backup and independent key custody are not commissioned')
            period.record('BACKUP_VERIFIED', receipt)
            period.record('COMPLETE', {'run_id': publication['run_id'], 'evaluations': results,
                                       'source_artifact': source_id, 'requests': period.request_count()})
            return {'status': 'COMPLETE', 'run_id': publication['run_id'], 'source_artifact': source_id,
                    'evaluations': results, 'requests': period.request_count()}
        except BaseException as exc:
            try:
                period.record('FAILED', {'error_type': type(exc).__name__, 'message': str(exc)[:500],
                                          'published': period.publication() is not None})
                period.alert(type(exc).__name__, str(exc)[:500], notify)
            except Exception as cleanup:
                exc.weekly_reporting_error = type(cleanup).__name__
            raise
