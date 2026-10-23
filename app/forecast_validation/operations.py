"""Append-only weekly coordination. No scheduler, cron creation or activation.

A session advisory lock uses autocommit and a separate bounded connection: it
never keeps a source transaction open during Square requests. Durable records
survive worker/database restarts; the mutex prevents concurrent attempts.
"""
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from uuid import uuid4
import json
import logging
import os

from sqlalchemy import Table, Column, String, Text, Integer, UniqueConstraint, select, func, text, create_engine
from sqlalchemy.pool import NullPool

from .storage import metadata, artifacts
from .contracts import canonical, digest, EvidenceError, Conflict, TZ, stamp

periods = Table('fv_periods', metadata, Column('id', String, primary_key=True),
                Column('family', String, nullable=False), Column('period_start', String, nullable=False),
                Column('scope_policy', String, nullable=False), Column('recorded_at', String, nullable=False),
                UniqueConstraint('family', 'period_start', 'scope_policy'))
period_events = Table('fv_period_events', metadata, Column('id', String, primary_key=True),
                      Column('period_id', String, nullable=False), Column('sequence', Integer, nullable=False),
                      Column('attempt_id', String, nullable=False), Column('status', String, nullable=False),
                      Column('recorded_at', String, nullable=False), Column('detail', Text, nullable=False),
                      UniqueConstraint('period_id', 'sequence'))
frozen = Table('fv_period_sources', metadata, Column('period_id', String, primary_key=True),
               Column('artifact_id', String, nullable=False), Column('origin', String, nullable=False))
publications = Table('fv_period_publications', metadata, Column('period_id', String, primary_key=True),
                     Column('run_id', String, nullable=False), Column('source_artifact', String, nullable=False),
                     Column('forecast_artifact', String, nullable=False), Column('recorded_at', String, nullable=False))
alerts = Table('fv_alerts', metadata, Column('id', String, primary_key=True),
               Column('period_id', String, nullable=False), Column('code', String, nullable=False),
               Column('recorded_at', String, nullable=False), Column('detail', Text, nullable=False))

FAMILY = 'four-model-weekly-v1'
SCOPE_POLICY = 'four-stores-active-catalog-v1'
MAX_ATTEMPTS = 400
STATUSES = frozenset({'CLAIMED', 'SOURCE_REQUEST', 'SOURCE_FROZEN', 'PUBLISHED',
                      'EVALUATING', 'BACKUP_VERIFIED', 'COMPLETE', 'FAILED', 'YIELDED'})


class Overlap(EvidenceError):
    pass


def period_identity(start, scope_policy=SCOPE_POLICY):
    start = date.fromisoformat(str(start))
    if start.weekday() != 0:
        raise EvidenceError('Weekly period must begin on a Pacific Monday')
    return digest({'family': FAMILY, 'period_start': str(start), 'scope_policy': scope_policy})


def _event(c, period_id, attempt_id, status, detail, now=None):
    if status not in STATUSES:
        raise EvidenceError('Invalid weekly state')
    encoded = canonical(detail)
    if len(encoded) > 16 * 1024:
        raise EvidenceError('Weekly state detail exceeds bound')
    sequence = c.execute(select(func.coalesce(func.max(period_events.c.sequence), 0))
                         .where(period_events.c.period_id == period_id)).scalar_one() + 1
    if sequence > 1024:
        raise EvidenceError('Weekly event bound exhausted')
    c.execute(period_events.insert().values(id=uuid4().hex, period_id=period_id, sequence=sequence,
              attempt_id=attempt_id, status=status, recorded_at=stamp(now or datetime.now(timezone.utc)).isoformat(),
              detail=encoded))


def publish_mapping(c, publication, run_id, artifact):
    period_id, source = publication['period_id'], publication['source_artifact']
    claimed = c.execute(select(periods.c.id).where(periods.c.id == period_id)).scalar_one_or_none()
    frozen_source = c.execute(select(frozen.c.artifact_id).where(frozen.c.period_id == period_id)).scalar_one_or_none()
    if not claimed or frozen_source != source:
        raise EvidenceError('Publication requires the immutable period source')
    if not c.execute(select(artifacts.c.id).where(artifacts.c.id == artifact)).scalar_one_or_none():
        raise EvidenceError('Publication artifact is incomplete')
    old = c.execute(select(publications).where(publications.c.period_id == period_id)).mappings().first()
    if old:
        if old['run_id'] != run_id or old['forecast_artifact'] != artifact or old['source_artifact'] != source:
            raise Conflict('Weekly origin already published with different immutable evidence')
        return
    c.execute(publications.insert().values(period_id=period_id, run_id=run_id, source_artifact=source,
              forecast_artifact=artifact, recorded_at=datetime.now(timezone.utc).isoformat()))
    _event(c, period_id, publication['attempt_id'], 'PUBLISHED', {'run_id': run_id, 'sha256': artifact})


class Period:
    def __init__(self, store, start, *, scope_policy=SCOPE_POLICY, now=None):
        self.store, self.start, self.scope_policy = store, str(start), scope_policy
        self.id = period_identity(start, scope_policy)
        self.attempt_id = uuid4().hex
        self.now = now
        self.locked = False

    @contextmanager
    def lock(self):
        if self.locked:
            raise Overlap('Worker already owns a period lock')
        if self.store.url.get_backend_name() == 'postgresql':
            engine = create_engine(self.store.url, poolclass=NullPool, hide_parameters=True,
                                   isolation_level='AUTOCOMMIT', connect_args={
                                       'options': '-c statement_timeout=10000 -c lock_timeout=2000'})
            # One infrastructure budget is shared by ordinary and explicitly
            # isolated commissioning scopes. Preserve the established lock key
            # so a manual namespace cannot bypass a normal worker's mutex.
            key = int.from_bytes(sha256((FAMILY + SCOPE_POLICY).encode()).digest()[:8], 'big', signed=True)
            try:
                with engine.connect() as c:
                    if not c.execute(text('SELECT pg_try_advisory_lock(:key)'), {'key': key}).scalar_one():
                        raise Overlap('Another forecast worker is active; no collection started')
                    self.locked = True
                    try:
                        yield self
                    finally:
                        self.locked = False
                        c.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': key})
            finally:
                engine.dispose()
        else:
            import fcntl
            path = Path(self.store.url.database).with_suffix('.forecast.lock')
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise Overlap('Another local forecast worker is active') from exc
                self.locked = True
                try:
                    yield self
                finally:
                    self.locked = False
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _require_lock(self):
        if not self.locked:
            raise EvidenceError('Weekly transitions require the overlap lock')

    def claim(self):
        self._require_lock()
        if self.state()['status'] == 'COMPLETE':
            return self.id
        self.store._batch(periods, [{'id': self.id, 'family': FAMILY, 'period_start': self.start,
                                    'scope_policy': self.scope_policy,
                                    'recorded_at': stamp(self.now or datetime.now(timezone.utc)).isoformat()}])
        self.record('CLAIMED', {'attempt_id': self.attempt_id})
        return self.id

    def record(self, status, detail):
        self._require_lock()
        with self.store.engine.begin() as c:
            _event(c, self.id, self.attempt_id, status, detail, self.now)

    def request_count(self):
        with self.store.engine.connect() as c:
            return c.execute(select(func.count()).select_from(period_events).where(
                period_events.c.period_id == self.id, period_events.c.status == 'SOURCE_REQUEST')).scalar_one()

    def retry_count(self):
        with self.store.engine.connect() as c:
            return c.execute(select(func.count()).select_from(period_events).where(
                period_events.c.period_id == self.id, period_events.c.status == 'SOURCE_REQUEST',
                period_events.c.detail.contains('"retry":true'))).scalar_one()

    def remaining_source_seconds(self, *, now=None):
        with self.store.engine.connect() as c:
            first = c.execute(select(period_events.c.recorded_at).where(period_events.c.period_id == self.id,
                period_events.c.status == 'SOURCE_REQUEST').order_by(period_events.c.sequence).limit(1)).scalar_one_or_none()
        if first is None:
            return 900
        return max(0, 900 - (stamp(now or datetime.now(timezone.utc)) - stamp(first)).total_seconds())

    def reserve_request(self, path, query_sha, *, retry=False):
        self._require_lock()
        if self.source():
            raise EvidenceError('Frozen weekly source cannot be recollected')
        if self.request_count() >= MAX_ATTEMPTS:
            raise EvidenceError('Durable weekly Square attempt budget exhausted')
        if retry and self.retry_count() >= 20:
            raise EvidenceError('Durable weekly retry budget exhausted')
        self.record('SOURCE_REQUEST', {'path': path, 'query_sha256': query_sha,
                                      'attempt': self.request_count() + 1, 'retry': bool(retry)})

    def source(self):
        with self.store.engine.connect() as c:
            row = c.execute(select(frozen).where(frozen.c.period_id == self.id)).mappings().first()
        return dict(row) if row else None

    def freeze(self, snapshot):
        self._require_lock()
        artifact = self.store.stage_artifact(snapshot)
        old = self.source()
        if old:
            if old['artifact_id'] != artifact:
                raise Conflict('Weekly source is immutable across retries')
            return artifact
        with self.store.engine.begin() as c:
            c.execute(frozen.insert().values(period_id=self.id, artifact_id=artifact, origin=snapshot['origin']))
            _event(c, self.id, self.attempt_id, 'SOURCE_FROZEN', {'artifact_id': artifact}, self.now)
        return artifact

    def publication(self):
        with self.store.engine.connect() as c:
            row = c.execute(select(publications).where(publications.c.period_id == self.id)).mappings().first()
        return dict(row) if row else None

    def state(self):
        with self.store.engine.connect() as c:
            row = c.execute(select(period_events.c.status, period_events.c.detail, period_events.c.recorded_at)
                            .where(period_events.c.period_id == self.id)
                            .order_by(period_events.c.sequence.desc()).limit(1)).mappings().first()
        return {**dict(row), 'detail': json.loads(row['detail'])} if row else {'status': 'MISSING'}

    def publication_context(self, source):
        self._require_lock()
        return {'period_id': self.id, 'source_artifact': source, 'attempt_id': self.attempt_id}

    def alert(self, code, detail, notify=None):
        message = {'period_id': self.id, 'code': code, 'detail': str(detail)[:1000]}
        ident = digest(message)
        self.store._batch(alerts, [{'id': ident, **message,
                                  'recorded_at': datetime.now(timezone.utc).isoformat()}])
        logging.getLogger(__name__).error('Forecast period failure: %s', canonical(message))
        if notify:
            notify(message)
        return ident


def latest_alerts(store, limit=20):
    if not 1 <= limit <= 50:
        raise EvidenceError('Alert page bound exceeded')
    with store.engine.connect() as c:
        return [dict(row) for row in c.execute(select(alerts).order_by(
            alerts.c.recorded_at.desc(), alerts.c.id).limit(limit)).mappings()]


def missed_periods(store, *, now=None, lookback_weeks=12, activation_start=None, notify=None):
    """Explicit reconciliation, never an installed scheduler or a source call."""
    now = stamp(now or datetime.now(timezone.utc))
    if not 1 <= lookback_weeks <= 78:
        raise EvidenceError('Weekly reconciliation bound exceeded')
    local = now.astimezone(TZ)
    monday = local.date() - timedelta(days=local.weekday())
    activation_start = date.fromisoformat(str(activation_start)) if activation_start else monday
    result = []
    for weeks in range(lookback_weeks):
        start = monday - timedelta(weeks=weeks)
        if start < activation_start:
            continue
        due = datetime.combine(start + timedelta(days=1), datetime.min.time(), TZ) + timedelta(hours=6)
        if now < due:
            continue
        period = Period(store, start)
        state = period.state()
        if state['status'] != 'COMPLETE':
            item = {'period_start': str(start), 'state': state['status'],
                    'published': period.publication() is not None, 'period_id': period.id}
            result.append(item)
            period.alert('MISSED_OR_INCOMPLETE', canonical(item), notify)
    return result
