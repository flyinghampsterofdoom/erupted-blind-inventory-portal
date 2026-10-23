"""Isolated append-only storage: bounded staging, atomic publication, small reads."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from contextlib import contextmanager
import json
import os
import time

from sqlalchemy import (create_engine, MetaData, Table, Column, String, Text, Integer,
                        LargeBinary, UniqueConstraint, CheckConstraint, Index,
                        select, text, func, and_, or_)
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from .contracts import canonical, digest, Conflict, EvidenceError, stamp
from . import codec
from .projections import encoded_views, MAX_VIEW_BYTES

metadata = MetaData()
runs = Table('fv_runs', metadata, Column('id', String, primary_key=True),
             Column('scope', String, nullable=False), Column('origin', String, nullable=False),
             Column('recorded_at', String, nullable=False), Column('expires_at', String, nullable=False),
             Column('kind', String, nullable=False), Column('payload', Text, nullable=False),
             Column('sha256', String, nullable=False), UniqueConstraint('scope', 'origin'))
evaluations = Table('fv_evaluations', metadata, Column('id', String, primary_key=True),
                    Column('run_id', String, nullable=False), Column('horizon', Integer, nullable=False),
                    Column('revision', String, nullable=False), Column('summary', Text, nullable=False),
                    Column('recorded_at', String, nullable=False), Column('payload', Text, nullable=False),
                    Column('sha256', String, nullable=False), UniqueConstraint('run_id', 'horizon', 'revision'))
attempts = Table('fv_attempts', metadata, Column('id', String, primary_key=True),
                 Column('recorded_at', String, nullable=False), Column('operation', String, nullable=False),
                 Column('status', String, nullable=False), Column('detail', Text, nullable=False))
chunks = Table('fv_chunks', metadata, Column('id', String, primary_key=True),
               Column('raw_bytes', Integer, nullable=False), Column('compressed_sha256', String, nullable=False),
               Column('data', LargeBinary, nullable=False),
               CheckConstraint('raw_bytes > 0 AND raw_bytes <= 65536'),
               CheckConstraint('length(data) <= 66560'))
refs = Table('fv_chunk_refs', metadata, Column('artifact_id', String, primary_key=True),
             Column('ordinal', Integer, primary_key=True), Column('chunk_id', String, nullable=False),
             Column('raw_bytes', Integer, nullable=False), Column('compressed_sha256', String, nullable=False))
artifacts = Table('fv_artifacts', metadata, Column('id', String, primary_key=True),
                  Column('codec', String, nullable=False), Column('canonical_bytes', Integer, nullable=False),
                  Column('chunks_count', Integer, nullable=False), Column('chunks_sha256', String, nullable=False),
                  Column('views_count', Integer, nullable=False), Column('views_sha256', String, nullable=False),
                  Column('recorded_at', String, nullable=False))
projections = Table('fv_projections', metadata, Column('artifact_id', String, primary_key=True),
                    Column('kind', String, primary_key=True), Column('item_key', String, primary_key=True),
                    Column('ordinal', Integer, nullable=False), Column('variation_id', String),
                    Column('sku', String), Column('store_id', String), Column('payload', Text, nullable=False),
                    Column('sha256', String, nullable=False), UniqueConstraint('artifact_id', 'ordinal'),
                    CheckConstraint('length(payload) <= 65536'))
legacy = Table('fv_legacy_index', metadata, Column('kind', String, primary_key=True),
               Column('record_id', String, primary_key=True), Column('artifact_id', String, nullable=False))
lineage = Table('fv_evaluation_lineage', metadata, Column('evaluation_id', String, primary_key=True),
                Column('run_id', String, nullable=False), Column('horizon', Integer, nullable=False),
                Column('source_sha256', String, nullable=False), Column('parent_id', String, nullable=False),
                UniqueConstraint('run_id', 'horizon', 'source_sha256'),
                UniqueConstraint('run_id', 'horizon', 'parent_id'))
Index('fv_runs_origin_id_idx', runs.c.origin, runs.c.id)
Index('fv_evaluations_run_horizon_time_idx', evaluations.c.run_id, evaluations.c.horizon,
      evaluations.c.recorded_at, evaluations.c.id)
Index('fv_views_variation_idx', projections.c.artifact_id, projections.c.kind,
      projections.c.variation_id, projections.c.store_id)
Index('fv_views_sku_idx', projections.c.artifact_id, projections.c.kind, projections.c.sku)

BATCH = 64
MAX_BATCH_BYTES = 1024 * 1024
PAGE = 20
MAX_PAGE = 50


def _bounded_page(limit, page):
    if not 1 <= limit <= MAX_PAGE or not 0 <= page <= 10_000:
        raise EvidenceError('Invalid evidence page bound')


class Store:
    def __init__(self, url, *, business_url=None, production=False):
        if not url:
            raise EvidenceError('Forecast evidence storage is not configured')
        parsed = make_url(url)
        if business_url:
            other = make_url(business_url)
            if ((parsed.get_backend_name() == other.get_backend_name() == 'postgresql'
                 and parsed.database == other.database)
                    or (parsed.host, parsed.port or 5432, parsed.database)
                    == (other.host, other.port or 5432, other.database)):
                raise EvidenceError('Evidence storage must be a different database from purchasing')
        if production and parsed.get_backend_name() != 'postgresql':
            raise EvidenceError('Production requires dedicated durable PostgreSQL storage')
        if parsed.get_backend_name() not in ('sqlite', 'postgresql'):
            raise EvidenceError('Unsupported evidence backend')
        self.url = parsed
        self.production = production
        self.engine = create_engine(url, pool_size=1, max_overflow=0, pool_timeout=5,
                                    pool_pre_ping=True, hide_parameters=True,
                                    connect_args={'options': '-c statement_timeout=10000 -c lock_timeout=2000'}
                                    if parsed.get_backend_name() == 'postgresql'
                                    else {'timeout': 5, 'check_same_thread': False})
        self.metrics = {}

    def initialize(self):
        """Explicit isolated evidence provisioning only; never called by GET/job."""
        from . import operations  # Register coordination tables; no scheduling side effects.
        if self.url.get_backend_name() == 'sqlite' and self.url.database not in (None, ':memory:'):
            path = Path(self.url.database)
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
            path.chmod(0o600)
        metadata.create_all(self.engine)
        for table in metadata.tables.values():
            for index in table.indexes:
                index.create(self.engine, checkfirst=True)
        with self.engine.begin() as c:
            if self.url.get_backend_name() == 'postgresql':
                c.execute(text("CREATE OR REPLACE FUNCTION fv_reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'Forecast evidence is append-only'; END $$"))
                for name in metadata.tables:
                    c.execute(text(f'DROP TRIGGER IF EXISTS fv_immutable ON {name}'))
                    c.execute(text(f'CREATE TRIGGER fv_immutable BEFORE UPDATE OR DELETE ON {name} FOR EACH ROW EXECUTE FUNCTION fv_reject_mutation()'))
                    c.execute(text(f'DROP TRIGGER IF EXISTS fv_no_truncate ON {name}'))
                    c.execute(text(f'CREATE TRIGGER fv_no_truncate BEFORE TRUNCATE ON {name} FOR EACH STATEMENT EXECUTE FUNCTION fv_reject_mutation()'))
            else:
                for name in metadata.tables:
                    for verb in ('UPDATE', 'DELETE'):
                        c.execute(text(f"CREATE TRIGGER IF NOT EXISTS {name}_no_{verb.lower()} BEFORE {verb} ON {name} BEGIN SELECT RAISE(ABORT,'Forecast evidence is append-only'); END"))

    def _batch(self, table, rows):
        if not rows:
            return
        if len(rows) > BATCH:
            raise EvidenceError('Evidence SQL batch exceeds bound')
        size = sum(len(v.encode()) if isinstance(v, str) else len(v) if isinstance(v, (bytes, memoryview)) else 8
                   for row in rows for v in row.values())
        if size > MAX_BATCH_BYTES:
            if len(rows) == 1:
                raise EvidenceError('Evidence SQL row exceeds its parameter budget')
            middle = len(rows) // 2
            self._batch(table, rows[:middle])
            self._batch(table, rows[middle:])
            return
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        insert = pg_insert if self.url.get_backend_name() == 'postgresql' else sqlite_insert
        began = time.monotonic()
        with self.engine.begin() as c:
            c.execute(insert(table).values(rows).on_conflict_do_nothing())
        seconds = time.monotonic() - began
        self.metrics['max_transaction_seconds'] = max(self.metrics.get('max_transaction_seconds', 0), seconds)
        self.metrics['staging_transactions'] = self.metrics.get('staging_transactions', 0) + 1

    def _artifact(self, ident):
        with self.engine.connect() as c:
            row = c.execute(select(artifacts).where(artifacts.c.id == ident)).mappings().first()
        return dict(row) if row else None

    def _raw_parts(self, ident, manifest):
        if (manifest['canonical_bytes'] > codec.MAX_BYTES or manifest['chunks_count'] > codec.MAX_CHUNKS
                or manifest['codec'] != codec.CODEC):
            raise EvidenceError('Unsupported or oversized evidence artifact')
        logical, sequence = sha256(), sha256()
        seen = total = 0
        while seen < manifest['chunks_count']:
            with self.engine.connect() as c:
                rows = c.execute(select(refs.c.ordinal, refs.c.chunk_id, refs.c.raw_bytes,
                                        refs.c.compressed_sha256, chunks.c.data,
                                        chunks.c.raw_bytes.label('actual_bytes'),
                                        chunks.c.compressed_sha256.label('actual_compressed'))
                                 .select_from(refs.join(chunks, refs.c.chunk_id == chunks.c.id))
                                 .where(refs.c.artifact_id == ident, refs.c.ordinal >= seen)
                                 .order_by(refs.c.ordinal).limit(BATCH)).mappings().all()
            if not rows:
                raise EvidenceError('Incomplete evidence artifact')
            for row in rows:
                if (row['ordinal'] != seen or seen >= manifest['chunks_count']
                        or row['raw_bytes'] != row['actual_bytes']
                        or row['compressed_sha256'] != row['actual_compressed']):
                    raise EvidenceError('Evidence chunk sequence mismatch')
                header = {'ordinal': seen, 'id': row['chunk_id'], 'raw_bytes': row['raw_bytes'],
                          'compressed_sha256': row['compressed_sha256']}
                sequence.update((canonical(header) + '\n').encode())
                raw = codec.decompress({**header, 'data': row['data']})
                total += len(raw)
                logical.update(raw)
                seen += 1
                yield raw
        if (seen != manifest['chunks_count'] or total != manifest['canonical_bytes']
                or logical.hexdigest() != ident or sequence.hexdigest() != manifest['chunks_sha256']):
            raise EvidenceError('Complete evidence digest mismatch')

    def stream_artifact(self, ident):
        manifest = self._artifact(ident)
        if not manifest:
            raise EvidenceError('Artifact is not finalized')
        yield from self._raw_parts(ident, manifest)

    def stage_artifact(self, payload, *, project=True):
        """No origin is visible until verified staging and final atomic INSERT."""
        with codec.prepare(payload) as prepared:
            self.metrics['serialization_seconds'] = prepared.seconds
            self.metrics['canonical_bytes'] = prepared.size
            # Projection output is spooled, not accumulated as a second graph.
            signature = sha256()
            count = 0
            views_file = prepared.directory / 'views.jsonl'
            with views_file.open('w') as file:
                for row in (encoded_views(payload) if project else ()):
                    row.update(artifact_id=prepared.sha256, ordinal=count,
                               sha256=sha256(row['payload'].encode()).hexdigest())
                    signature.update(canonical([count, row['kind'], row['item_key'], row['sha256']]).encode() + b'\n')
                    file.write(canonical(row) + '\n')
                    count += 1
            manifest = {'id': prepared.sha256, 'codec': codec.CODEC,
                        'canonical_bytes': prepared.size, 'chunks_count': prepared.count,
                        'chunks_sha256': prepared.chunks_sha256, 'views_count': count,
                        'views_sha256': signature.hexdigest()}
            old = self._artifact(prepared.sha256)
            if old:
                if any(old[k] != value for k, value in manifest.items()):
                    raise Conflict('Immutable artifact projection/codec conflict')
                return prepared.sha256
            began = time.monotonic()
            batch_chunks, batch_refs = [], []
            compressed_bytes = 0
            for row in prepared.chunks():
                compressed_bytes += len(row['data'])
                batch_chunks.append({k: row[k] for k in ('id', 'raw_bytes', 'compressed_sha256', 'data')})
                batch_refs.append({'artifact_id': prepared.sha256, 'ordinal': row['ordinal'],
                                   'chunk_id': row['id'], 'raw_bytes': row['raw_bytes'],
                                   'compressed_sha256': row['compressed_sha256']})
                if len(batch_chunks) == BATCH:
                    self._batch(chunks, batch_chunks)
                    self._batch(refs, batch_refs)
                    batch_chunks, batch_refs = [], []
            self._batch(chunks, batch_chunks)
            self._batch(refs, batch_refs)
            with views_file.open() as file:
                batch = []
                for line in file:
                    batch.append(json.loads(line))
                    if len(batch) == BATCH:
                        self._batch(projections, batch)
                        batch = []
                self._batch(projections, batch)
            self.metrics['staging_write_seconds'] = time.monotonic() - began
            self.metrics['compressed_bytes'] = compressed_bytes
            verify = time.monotonic()
            for _ in self._raw_parts(prepared.sha256, manifest):
                pass
            actual = sha256()
            seen = 0
            while seen < count:
                with self.engine.connect() as c:
                    rows = c.execute(select(projections).where(projections.c.artifact_id == prepared.sha256,
                                      projections.c.ordinal >= seen).order_by(projections.c.ordinal).limit(BATCH)).mappings().all()
                if not rows:
                    raise EvidenceError('Incomplete evidence projections')
                for row in rows:
                    if row['ordinal'] != seen or sha256(row['payload'].encode()).hexdigest() != row['sha256']:
                        raise EvidenceError('Projection integrity check failed')
                    actual.update(canonical([seen, row['kind'], row['item_key'], row['sha256']]).encode() + b'\n')
                    seen += 1
            if seen != count or actual.hexdigest() != signature.hexdigest():
                raise EvidenceError('Complete projection digest mismatch')
            self.metrics['verification_seconds'] = time.monotonic() - verify
            self._batch(artifacts, [{**manifest, 'recorded_at': datetime.now(timezone.utc).isoformat()}])
            final = self._artifact(prepared.sha256)
            if any(final[k] != value for k, value in manifest.items()):
                raise Conflict('Artifact finalization conflict')
            return prepared.sha256

    def _record_meta(self, table, ident):
        cols = [col for col in table.c if col.name != 'payload']
        with self.engine.connect() as c:
            row = c.execute(select(*cols).where(table.c.id == ident)).mappings().first()
        return dict(row) if row else None

    def get_run_meta(self, ident):
        return self._record_meta(runs, ident)

    def get_evaluation_meta(self, ident):
        return self._record_meta(evaluations, ident)

    def _artifact_for(self, table, ident):
        with self.engine.connect() as c:
            row = c.execute(select(table.c.sha256, func.substr(table.c.payload, 1, 240).label('head'))
                            .where(table.c.id == ident)).mappings().first()
            if not row:
                return None
            try:
                pointer = json.loads(row['head'])
            except (ValueError, TypeError):
                pointer = None
            if isinstance(pointer, dict) and pointer.get('codec') == codec.CODEC:
                if pointer.get('artifact') != row['sha256']:
                    raise EvidenceError('Record artifact binding mismatch')
                return row['sha256']
            binding = c.execute(select(legacy.c.artifact_id).where(legacy.c.kind == table.name,
                                legacy.c.record_id == ident)).scalar_one_or_none()
        if binding is not None and binding != row['sha256']:
            raise EvidenceError('Legacy evidence binding mismatch')
        return binding

    def _legacy_parts(self, table, ident, expected):
        # Explicit offline/Owner raw fallback only. Normal summary/detail reads
        # never select this column. Commissioning binds legacy rows to chunks.
        hasher, offset = sha256(), 1
        while True:
            with self.engine.connect() as c:
                part = c.execute(select(func.substr(table.c.payload, offset, codec.CHUNK_BYTES))
                                 .where(table.c.id == ident)).scalar_one_or_none()
            if part is None:
                raise EvidenceError('Legacy evidence not found')
            data = part.encode()
            if not data:
                break
            if offset + len(data) - 1 > codec.MAX_BYTES:
                raise EvidenceError('Legacy evidence exceeds import bound')
            hasher.update(data)
            yield data
            offset += len(part)
        if hasher.hexdigest() != expected:
            raise EvidenceError('Stored legacy evidence integrity check failed')

    def stream_record(self, kind, ident):
        table = runs if kind == 'run' else evaluations if kind == 'evaluation' else None
        if table is None:
            raise EvidenceError('Invalid evidence record kind')
        meta = self._record_meta(table, ident)
        if not meta:
            raise EvidenceError('Evidence record not found')
        artifact = self._artifact_for(table, ident)
        yield from self.stream_artifact(artifact) if artifact else self._legacy_parts(table, ident, meta['sha256'])

    def get_run(self, ident):
        row = self.get_run_meta(ident)
        if row is None:
            return None
        row['payload'] = codec.materialize(self.stream_record('run', ident))
        return row

    def get_evaluation(self, ident):
        row = self.get_evaluation_meta(ident)
        if row is None:
            return None
        row['payload'] = codec.materialize(self.stream_record('evaluation', ident))
        return row

    def commission_legacy(self, kind, ident):
        """Explicit offline command: append indexes/chunks, never change legacy rows."""
        table = runs if kind == 'run' else evaluations if kind == 'evaluation' else None
        if table is None:
            raise EvidenceError('Invalid legacy record kind')
        row = self._record_meta(table, ident)
        if not row:
            raise EvidenceError('Legacy evidence not found')
        payload = codec.materialize(self.stream_record(kind, ident))
        artifact = self.stage_artifact(payload)
        if artifact != row['sha256']:
            raise EvidenceError('Legacy canonical digest changed')
        self._batch(legacy, [{'kind': table.name, 'record_id': ident, 'artifact_id': artifact}])
        return ident

    def _publish(self, table, values, keys, extra=None, admission=None):
        predicate = and_(*(table.c[k] == values[k] for k in keys))
        # Metadata-only fast path; never retransmit a large payload on retry.
        with self.engine.connect() as c:
            old = c.execute(select(table.c.id, table.c.sha256).where(predicate)).mappings().first()
        if old:
            if old['sha256'] != values['sha256']:
                raise Conflict('Existing immutable record has different evidence; create a new origin/revision')
            if extra:
                with self.engine.begin() as c:
                    if admission:
                        admission()
                    extra(c, old['id'])
                    if admission:
                        admission()
            return old['id']
        try:
            began = time.monotonic()
            with self.engine.begin() as c:
                if admission:
                    admission()
                c.execute(table.insert().values(**values))
                if extra:
                    extra(c, values['id'])
                if admission:
                    admission()
            self.metrics['publication_seconds'] = time.monotonic() - began
            return values['id']
        except IntegrityError:
            with self.engine.connect() as c:
                old = c.execute(select(table.c.id, table.c.sha256).where(predicate)).mappings().first()
            if old and old['sha256'] == values['sha256']:
                if extra:
                    with self.engine.begin() as c:
                        if admission:
                            admission()
                        extra(c, old['id'])
                        if admission:
                            admission()
                return old['id']
            raise Conflict('Immutable publication or revision lineage conflict') from None

    def add_run(self, payload, *, now=None, publication=None):
        if self.production and payload['snapshot'].get('kind') == 'SYNTHETIC':
            raise EvidenceError('Synthetic evidence cannot be published by a production Store')
        began = time.monotonic()
        now = now or datetime.now(timezone.utc)
        snap = payload['snapshot']
        def admission():
            # Staging, connection waits and publication all consume freshness.
            # An injected test clock advances with elapsed time too.
            current = now + timedelta(seconds=time.monotonic() - began)
            age = (current - stamp(snap['origin'])).total_seconds()
            if snap.get('kind', 'PROSPECTIVE') == 'PROSPECTIVE' and not 0 <= age <= 900:
                raise EvidenceError('Expired/future evidence cannot be published as prospective')
        if publication:
            from .shared import stream_digest
            if stream_digest(snap) != publication['source_artifact']:
                raise Conflict('Forecast inputs differ from the immutable weekly source')
        origin = stamp(snap['origin']).isoformat()
        ident = digest({'scope': snap['scope'], 'origin': origin})
        artifact = self.stage_artifact(payload)
        values = {'id': ident, 'scope': snap['scope'], 'origin': origin, 'kind': snap.get('kind', 'PROSPECTIVE'),
                  'recorded_at': now.isoformat(), 'expires_at': (now + timedelta(days=540)).isoformat(),
                  'payload': canonical({'codec': codec.CODEC, 'artifact': artifact}), 'sha256': artifact}
        def extra(c, run_id):
            if publication:
                from .operations import publish_mapping
                publish_mapping(c, publication, run_id, artifact)
        return self._publish(runs, values, ('scope', 'origin'), extra if publication else None, admission)

    def find_evaluation_source(self, run_id, horizon, source):
        with self.engine.connect() as c:
            return c.execute(select(lineage.c.evaluation_id).where(lineage.c.run_id == run_id,
                             lineage.c.horizon == horizon, lineage.c.source_sha256 == source)).scalar_one_or_none()

    def add_evaluation(self, run_id, horizon, revision, payload):
        if not self.get_run_meta(run_id):
            raise EvidenceError('Forecast not found')
        source = payload.get('source_revision') or digest(payload['outcome'])
        old = self.find_evaluation_source(run_id, horizon, source)
        if old:
            return old
        latest = self.list_evaluation_meta(run_id, horizon=horizon, limit=1)
        parent = payload['outcome'].get('supersedes_id') or ''
        if latest and parent != latest[0]['id']:
            raise EvidenceError('A changed outcome requires the latest explicit revision parent')
        if not latest and parent:
            raise EvidenceError('First outcome cannot name a revision parent')
        ident = digest({'run': run_id, 'horizon': horizon, 'revision': revision})
        artifact = self.stage_artifact(payload)
        from .projections import evaluation_summary
        values = {'id': ident, 'run_id': run_id, 'horizon': horizon, 'revision': revision,
                  'summary': canonical(evaluation_summary(payload)), 'recorded_at': datetime.now(timezone.utc).isoformat(),
                  'payload': canonical({'codec': codec.CODEC, 'artifact': artifact}), 'sha256': artifact}
        def extra(c, eid):
            found = c.execute(select(lineage).where(lineage.c.evaluation_id == eid)).mappings().first()
            if found:
                if found['source_sha256'] != source or found['parent_id'] != parent:
                    raise Conflict('Evaluation lineage differs on retry')
                return
            c.execute(lineage.insert().values(evaluation_id=eid, run_id=run_id, horizon=horizon,
                                             source_sha256=source, parent_id=parent))
        return self._publish(evaluations, values, ('run_id', 'horizon', 'revision'), extra)

    def attempt(self, operation, status, detail):
        from uuid import uuid4
        with self.engine.begin() as c:
            c.execute(attempts.insert().values(id=uuid4().hex, recorded_at=datetime.now(timezone.utc).isoformat(),
                                              operation=operation, status=status, detail=str(detail)[:1000]))

    def list_runs(self, limit=PAGE, *, page=0):
        _bounded_page(limit, page)
        with self.engine.connect() as c:
            rows = c.execute(select(runs.c.id, runs.c.scope, runs.c.origin, runs.c.recorded_at, runs.c.kind,
                                    runs.c.expires_at).order_by(runs.c.origin.desc(), runs.c.id.desc())
                             .limit(limit).offset(page * limit)).mappings().all()
        return [dict(row) for row in rows]

    def list_evaluation_meta(self, run_id, *, horizon=None, limit=PAGE, page=0):
        _bounded_page(limit, page)
        query = select(*(col for col in evaluations.c if col.name != 'payload')).where(evaluations.c.run_id == run_id)
        if horizon is not None:
            query = query.where(evaluations.c.horizon == horizon)
        with self.engine.connect() as c:
            rows = c.execute(query.order_by(evaluations.c.recorded_at.desc(), evaluations.c.id.desc())
                             .limit(limit).offset(page * limit)).mappings().all()
        result = []
        for row in rows:
            item = dict(row)
            item['summary'] = json.loads(item['summary'])
            result.append(item)
        return result

    def list_evaluations(self, run_id, limit=PAGE, *, page=0):
        # Explicit offline compatibility API; routes/evaluator use metadata only.
        return [self.get_evaluation(row['id']) for row in reversed(self.list_evaluation_meta(run_id, limit=limit, page=page))]

    def get_view(self, kind, ident, view, key=''):
        table = runs if kind == 'run' else evaluations
        artifact = self._artifact_for(table, ident)
        if not artifact or not self._artifact(artifact):
            raise EvidenceError('Evidence summary index is not commissioned; use the explicit raw export or offline commissioning command')
        return self.artifact_view(artifact, view, key)

    def artifact_view(self, artifact, view, key=''):
        manifest = self._artifact(artifact) if artifact else None
        if not manifest:
            raise EvidenceError('Evidence artifact is not finalized')
        with self.engine.connect() as c:
            row = c.execute(select(projections.c.ordinal, projections.c.payload, projections.c.sha256).where(
                projections.c.artifact_id == artifact, projections.c.kind == view,
                projections.c.item_key == key)).mappings().first()
        if not row:
            return None
        if (not 0 <= row['ordinal'] < manifest['views_count']
                or sha256(row['payload'].encode()).hexdigest() != row['sha256']):
            raise EvidenceError('Evidence projection integrity check failed')
        return json.loads(row['payload'])

    def get_run_summary(self, ident):
        return self.get_view('run', ident, 'run-summary')

    def sku_rows(self, ident, sku, *, limit=PAGE, page=0):
        _bounded_page(limit, page)
        if not sku or len(sku) > 255:
            raise EvidenceError('Bounded exact SKU or variation required')
        artifact = self._artifact_for(runs, ident)
        manifest = self._artifact(artifact) if artifact else None
        if not manifest:
            raise EvidenceError('SKU index is not commissioned')
        with self.engine.connect() as c:
            rows = c.execute(select(projections).where(projections.c.artifact_id == artifact,
                                    projections.c.kind == 'run-row',
                                    or_(projections.c.sku == sku, projections.c.variation_id == sku))
                             .order_by(projections.c.variation_id, projections.c.store_id)
                             .limit(limit).offset(page * limit)).mappings().all()
        result = []
        for row in rows:
            if (not 0 <= row['ordinal'] < manifest['views_count']
                    or sha256(row['payload'].encode()).hexdigest() != row['sha256']):
                raise EvidenceError('SKU projection integrity check failed')
            result.append(json.loads(row['payload']))
        return result

    def iter_run_pairs(self, ident):
        artifact = self._artifact_for(runs, ident)
        if not artifact:
            raise EvidenceError('Forecast input index must be commissioned before evaluation')
        yield from self.iter_artifact_views(artifact, 'run-row')

    def iter_artifact_views(self, artifact, kind):
        manifest = self._artifact(artifact)
        if not manifest:
            raise EvidenceError('Evidence artifact is not finalized')
        cursor = -1
        while True:
            with self.engine.connect() as c:
                rows = c.execute(select(projections.c.ordinal, projections.c.payload, projections.c.sha256)
                                 .where(projections.c.artifact_id == artifact, projections.c.kind == kind,
                                        projections.c.ordinal > cursor).order_by(projections.c.ordinal)
                                 .limit(BATCH)).mappings().all()
            if not rows:
                return
            for row in rows:
                if (not 0 <= row['ordinal'] < manifest['views_count']
                        or sha256(row['payload'].encode()).hexdigest() != row['sha256']):
                    raise EvidenceError('Forecast input projection integrity check failed')
                cursor = row['ordinal']
                yield json.loads(row['payload'])

    def failures(self):
        with self.engine.connect() as c:
            rows = c.execute(select(attempts).where(attempts.c.status == 'FAILED')
                             .order_by(attempts.c.recorded_at.desc(), attempts.c.id.desc()).limit(10)).mappings().all()
        return [dict(row) for row in rows]

    def trends(self, *, page=0, limit=PAGE):
        _bounded_page(limit, page)
        with self.engine.connect() as c:
            rows = c.execute(select(runs.c.origin, runs.c.kind, evaluations.c.run_id,
                                    evaluations.c.horizon, evaluations.c.revision, evaluations.c.summary)
                             .join(evaluations, evaluations.c.run_id == runs.c.id)
                             .order_by(runs.c.origin.desc(), evaluations.c.recorded_at.desc(), evaluations.c.id.desc())
                             .limit(limit).offset(page * limit)).mappings().all()
        return [{**dict(row), 'summary': json.loads(row['summary'])} for row in rows]
