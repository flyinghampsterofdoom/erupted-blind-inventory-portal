"""Local-only storage, projection, provenance and weekly failure contracts."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from fractions import Fraction as F
from hashlib import sha256
import json
import os
import zlib

import pytest
from sqlalchemy import select, func, text, event

from app.forecast_validation import codec
from app.forecast_validation.storage import Store, runs, chunks, artifacts, refs, projections
from app.forecast_validation.contracts import canonical, digest, EvidenceError, Conflict, TZ
from app.forecast_validation.runner import forecast
from app.forecast_validation.evaluation import evaluate
from app.forecast_validation.operations import Period, publications, period_events, latest_alerts, missed_periods
from app.forecast_validation.weekly import run_period
from app.forecast_validation.shared import FrozenSource, verify_snapshot
from test_forecast_validation import snapshot, outcome, NOW


@pytest.fixture
def store(tmp_path):
    s = Store('sqlite:///' + str(tmp_path / 'forecast.sqlite'))
    s.initialize()
    yield s
    s.engine.dispose()


def production_source(now=NOW, quantity='2'):
    """Four fully reconstructable stores, one SKU; all Square records synthetic."""
    s = snapshot()
    s.update(kind='PROSPECTIVE', origin=now.isoformat(), cutoff=now.isoformat(), source='READ_ONLY_BUSINESS_DB_AND_SQUARE')
    today = now.astimezone(TZ).date()
    first = today - timedelta(days=84)
    s['evaluation_start'] = str(today + timedelta(days=1))
    for i, row in enumerate(s['rows'], 1):
        row.update(history_start=str(first), sales=[quantity]*84, verified_availability=['UNKNOWN']+['IN_STOCK']*83,
                   on_hand=str(1000-F(quantity)*84))
        records=[]
        def record(kind, ident, occurred, known, data):
            return {'source':kind, 'id':ident, 'version':1, 'occurred_at':occurred.isoformat(),
                    'known_at':known.isoformat(), 'data':data}
        begin = datetime.combine(first, datetime.min.time(), TZ)
        pc = {'id':f'PC{i}', 'location_id':f'L{i}', 'catalog_object_id':'V1','quantity':'1000',
              'state':'IN_STOCK','occurred_at':begin.isoformat(),'created_at':begin.isoformat()}
        records.append(record('square_absolute_inventory_event', pc['id'], begin, begin,
                              {'type':'PHYSICAL_COUNT','physical_count':pc}))
        for d in range(84):
            day = first + timedelta(days=d)
            when = datetime.combine(day, datetime.min.time(), TZ)+timedelta(hours=12)
            records.append(record('square_order_line',f'O{i}-{d}:line',when,now,
                                  {'quantity':quantity,'business_date':str(day),'location_id':f'L{i}'}))
            adjustment={'id':f'A{i}-{d}','location_id':f'L{i}','catalog_object_id':'V1','quantity':quantity,
                        'from_state':'IN_STOCK','to_state':'SOLD','occurred_at':when.isoformat(),'created_at':when.isoformat()}
            records.append(record('square_absolute_inventory_event',adjustment['id'],when,when,
                                  {'type':'ADJUSTMENT','adjustment':adjustment}))
        records.append(record('square_inventory_count',f'L{i}:V1',now,now,{'quantity':row['on_hand'],'state':'IN_STOCK'}))
        through=datetime.combine(today,datetime.min.time(),TZ)
        for kind in ('sales','movements'):
            records.append(record('coverage',kind+f':L{i}',begin,now,{'through_at':through.isoformat()}))
        row['source_records']=records
    from app.forecast_validation.collection import SOURCE_MODELS
    fingerprint={m.__tablename__:{'rows':1,'sha256':'a'*64} for m in SOURCE_MODELS}
    s['collection']={'source_before':fingerprint,'source_after':deepcopy(fingerprint),
                     'source_changes_detected':[],'source_transaction_during_network':False,
                     'history_batches':[{'offset':0,'variations':1,'pages':1,'records':340,'pagination_exhausted':True}]}
    return s


@pytest.mark.parametrize('value',[
    None, {'a':['é','\\','"','\n','😀'], 'b':[-1,2,1.23,True,None]},
    {'a':2**100,'b':1.2e50}, {'a':['x'*8192]*30},
])
def test_stream_codec_exact_and_independently_decodable(value):
    with codec.prepare(value) as prepared:
        rows=list(prepared.chunks())
        raw=b''.join(codec.decompress(row) for row in rows)
        assert raw==canonical(value).encode()
        assert prepared.sha256==digest(value)
        assert max(len(row['data']) for row in rows)<=codec.MAX_COMPRESSED
        assert codec.materialize(iter([raw[i:i+71] for i in range(0,len(raw),71)]))==value


def test_codec_refuses_truncation_and_decompression_bombs():
    with pytest.raises(EvidenceError,match='scalar'):
        with codec.prepare({'oversized':'x'*(codec.MAX_SCALAR+1)}):pass
    data=zlib.compress(b'a'*(codec.CHUNK_BYTES+1))
    with pytest.raises(EvidenceError):
        codec.decompress({'data':data,'compressed_sha256':sha256(data).hexdigest(),
                          'raw_bytes':codec.CHUNK_BYTES,'id':sha256(b'a'*codec.CHUNK_BYTES).hexdigest()})
    with codec.prepare({'a':['x'*100]*1000}) as prepared:
        row=next(prepared.chunks());row['data']=bytes([row['data'][0]^1])+row['data'][1:]
        with pytest.raises(EvidenceError):codec.decompress(row)


def test_partial_staging_is_invisible_and_retry_safe(store,monkeypatch):
    s=snapshot();s['extra']=['x'*8000]*40
    original=store._batch
    def fail(table,rows):
        original(table,rows)
        if table is refs and rows:raise RuntimeError('worker interrupted after staged chunks')
    monkeypatch.setattr(store,'_batch',fail)
    with pytest.raises(RuntimeError):forecast(store,s,now=NOW)
    assert store.list_runs()==[]
    with store.engine.connect() as c:assert c.execute(select(func.count()).select_from(artifacts)).scalar()==0
    monkeypatch.setattr(store,'_batch',original)
    ident=forecast(store,s,now=NOW)
    assert store.get_run(ident)['payload']['snapshot']==s
    before=store.get_run_meta(ident)
    assert forecast(store,s,now=NOW)==ident
    assert store.get_run_meta(ident)==before
    changed=deepcopy(s);changed['rows'][0]['incoming']='1'
    with pytest.raises(Conflict):forecast(store,changed,now=NOW)
    assert len(store.list_runs())==1


def test_no_large_sql_parameters_and_no_raw_reads_for_views(store,monkeypatch):
    sizes=[]
    @event.listens_for(store.engine,'before_cursor_execute')
    def capture(c,cur,statement,params,ctx,many):
        sizes.extend(len(v) for v in (params.values() if isinstance(params,dict) else params or ()) if isinstance(v,(str,bytes)))
    s=snapshot();s['extra']=['y'*4096]*100
    ident=forecast(store,s,now=NOW)
    monkeypatch.setattr(store,'get_run',lambda *a:pytest.fail('Summary loaded raw evidence'))
    assert store.get_run_summary(ident)['population']==4
    assert len(store.sku_rows(ident,'DEMO-1'))==4
    assert max(sizes)<=codec.MAX_COMPRESSED
    raw=b''.join(store.stream_record('run',ident))
    assert sha256(raw).hexdigest()==store.get_run_meta(ident)['sha256']
    with pytest.raises(EvidenceError):store.sku_rows(ident,'DEMO-1',limit=51)


def test_source_reconciles_sales_and_forward_availability():
    s=production_source();assert verify_snapshot(s)['population']==4
    for change in ('sales','availability','coverage','pagination','source'):
        broken=deepcopy(s)
        if change=='sales':broken['rows'][0]['sales'][3]='9'
        elif change=='availability':broken['rows'][0]['verified_availability'][3]='STOCKOUT'
        elif change=='coverage':broken['rows'][0]['source_records']=[r for r in broken['rows'][0]['source_records'] if r['source']!='coverage']
        elif change=='pagination':broken['collection']['history_batches'][0]['pagination_exhausted']=False
        else:broken['collection']['source_after']['stores']['sha256']='b'*64
        with pytest.raises(EvidenceError):verify_snapshot(broken)


def test_one_frozen_source_matches_independent_outcomes_and_revisions(store):
    original=production_source();ident=forecast(store,original,now=NOW)
    observed=NOW+timedelta(days=57)
    fresh=production_source(observed)
    artifact=store.stage_artifact(fresh)
    shared=FrozenSource(fresh,artifact=artifact,verified=True)
    for horizon in (28,56):
        a=shared.outcome(store,ident,horizon,revision=f'h{horizon}-v1')
        # An independent complete retrieval is represented by a separately
        # verified complete source object, not an existence-only cache hit.
        b=FrozenSource(deepcopy(fresh),artifact=artifact).outcome(store,ident,horizon,revision=f'h{horizon}-v1')
        assert a==b
        eid=evaluate(store,ident,a,now=observed)
        assert evaluate(store,ident,a,now=observed)==eid
        same=deepcopy(a);same['revision']='arbitrary-other-label'
        assert evaluate(store,ident,same,now=observed)==eid
        assert all(m['wape']==0 for m in store.get_evaluation(eid)['payload']['metrics'] if m['n'])
        corrupted=deepcopy(a);corrupted['rows'][0]['days'][0]['units']='999'
        with pytest.raises(EvidenceError):evaluate(store,ident,corrupted,now=observed)
        corrupted=deepcopy(a);corrupted['rows'][0]['days'][0]['availability']='STOCKOUT'
        with pytest.raises(EvidenceError):evaluate(store,ident,corrupted,now=observed)
    assert store.get_run(ident)['payload']['snapshot']==original


def test_corrected_outcomes_append_with_explicit_latest_parent(store):
    ident=forecast(store,production_source(),now=NOW)
    observed=NOW+timedelta(days=56);fresh=production_source(observed)
    artifact=store.stage_artifact(fresh);first=FrozenSource(fresh,artifact=artifact,verified=True).outcome(store,ident,28,revision='v1')
    eid=evaluate(store,ident,first,now=observed)
    changed=production_source(observed,quantity='3');new_artifact=store.stage_artifact(changed)
    revision=FrozenSource(changed,artifact=new_artifact,verified=True).outcome(store,ident,28,revision='v2')
    with pytest.raises(EvidenceError,match='parent'):evaluate(store,ident,revision,now=observed)
    revision['supersedes_id']=eid
    second=evaluate(store,ident,revision,now=observed)
    assert second!=eid
    assert store.get_evaluation(eid)['payload']['outcome']==first
    assert evaluate(store,ident,revision,now=observed)==second
    revision['supersedes_id']='not-an-evaluation'
    # Same certified content is an idempotent retry even if the caller labels it
    # differently; no new publication or changed immutable parent is written.
    assert evaluate(store,ident,revision,now=observed)==second


def test_unverified_outcome_cannot_use_a_completeness_flag(store):
    ident=forecast(store,production_source(),now=NOW)
    with pytest.raises(EvidenceError,match='reconciled'):
        evaluate(store,ident,outcome(),now=NOW+timedelta(days=56))


def test_weekly_restart_reuses_frozen_source_and_deduplicates(store,monkeypatch):
    from app.forecast_validation import weekly
    calls=[];notifications=[];s=snapshot();start=(NOW.astimezone(TZ).date()-timedelta(days=NOW.astimezone(TZ).weekday()))
    def collect(period,remaining):
        calls.append(remaining)
        period.reserve_request('/v2/test/read','a'*64)
        return s
    def backup(period,publication):return {'verified':True,'encrypted_sha256':'f'*64}
    original=weekly.forecast
    monkeypatch.setattr(weekly,'forecast',lambda *a,**kw:(_ for _ in ()).throw(RuntimeError('interrupted before publication')))
    with pytest.raises(RuntimeError):run_period(store,start,collect_source=collect,pressure=lambda:True,backup=backup,now=NOW,evaluate_outcomes=False,notify=notifications.append)
    assert store.list_runs()==[] and len(calls)==1 and notifications
    monkeypatch.setattr(weekly,'forecast',original)
    result=run_period(store,start,collect_source=lambda *a:pytest.fail('Frozen source recollected'),pressure=lambda:True,backup=backup,now=NOW,evaluate_outcomes=False)
    assert result['status']=='COMPLETE' and result['requests']==1
    restarted=Store(str(store.url))
    try:
        retry=run_period(restarted,start,collect_source=lambda *a:pytest.fail('Complete period recollected'),pressure=lambda:False,backup=backup,now=NOW,evaluate_outcomes=False)
        assert retry['status']=='COMPLETE' and retry['idempotent']
        assert len(restarted.list_runs())==1
    finally:restarted.engine.dispose()


def test_atomic_period_conflict_cannot_publish_second_run(store):
    s=snapshot();start=NOW.date()-timedelta(days=NOW.weekday());period=Period(store,start,now=NOW)
    with period.lock():
        period.claim();artifact=period.freeze(s)
        ident=forecast(store,s,now=NOW,publication=period.publication_context(artifact))
        changed=deepcopy(s);changed['origin']=(NOW+timedelta(seconds=1)).isoformat()
        with pytest.raises(Conflict):forecast(store,changed,now=NOW+timedelta(seconds=1),publication=period.publication_context(artifact))
        assert len(store.list_runs())==1 and period.publication()['run_id']==ident


def test_overlap_request_budgets_and_missed_period_alerts(store):
    from app.forecast_validation.operations import Overlap
    start=NOW.date()-timedelta(days=NOW.weekday());one=Period(store,start,now=NOW)
    with one.lock():
        one.claim()
        with pytest.raises(Overlap):
            with Period(store,start,now=NOW).lock():pass
        for n in range(20):one.reserve_request('/v2/test',str(n),retry=True)
        with pytest.raises(EvidenceError,match='retry budget'):one.reserve_request('/v2/test','overflow',retry=True)
        for n in range(380):one.reserve_request('/v2/test',str(n))
        assert one.request_count()==400
        with pytest.raises(EvidenceError,match='attempt budget'):one.reserve_request('/v2/test','overflow')
    missing=missed_periods(store,now=NOW,activation_start=start,lookback_weeks=1)
    assert missing and missing[0]['period_id']==one.id and latest_alerts(store)


def test_backup_failure_never_marks_period_successful(store):
    start=NOW.date()-timedelta(days=NOW.weekday())
    with pytest.raises(EvidenceError,match='backup'):
        run_period(store,start,collect_source=lambda *a:snapshot(),pressure=lambda:True,backup=lambda *a:{'verified':False},now=NOW,evaluate_outcomes=False)
    period=Period(store,start)
    assert period.publication() and period.state()['status']=='FAILED'
    assert len(store.list_runs())==1
    assert run_period(store,start,collect_source=lambda *a:pytest.fail('Recollected after publication'),pressure=lambda:True,
                      backup=lambda *a:{'verified':True,'encrypted_sha256':'a'*64},now=NOW,evaluate_outcomes=False)['status']=='COMPLETE'


def test_pressure_and_key_validation_fail_closed(tmp_path):
    from app.forecast_validation.priority import read_signal
    from app.forecast_validation.backup import read_key
    key=tmp_path/'key';key.write_bytes(b'x'*32);key.chmod(0o600);assert read_key(key)==b'x'*32
    key.chmod(0o644)
    with pytest.raises(EvidenceError):read_key(key)
    signal=tmp_path/'pressure';signal.write_text(json.dumps({'observed_at':NOW.isoformat(),'database_memory_bytes':300*1024**2,
        'blocked_business_sessions':0,'business_backup_active':False,'sunday_automation_active':False,'business_latency_acceptable':True}));signal.chmod(0o600)
    assert read_signal(signal,now=NOW)
    with pytest.raises(EvidenceError,match='stale'):read_signal(signal,now=NOW+timedelta(minutes=6))
    value=json.loads(signal.read_text());value['database_memory_bytes']=800*1024**2;signal.write_text(json.dumps(value))
    with pytest.raises(EvidenceError):read_signal(signal,now=NOW)


def test_authenticated_backup_and_explicit_archive_custody(tmp_path):
    from app.forecast_validation.backup import decrypted, archive_to_s3, verify_key_custody, MAGIC
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.exceptions import InvalidTag
    from io import BytesIO
    key=os.urandom(32);keyfile=tmp_path/'key';keyfile.write_bytes(key);keyfile.chmod(0o600)
    nonce=os.urandom(12);archive=tmp_path/'encrypted';content=b'private database archive'*10000
    archive.write_bytes(MAGIC+nonce+AESGCM(key).encrypt(nonce,content,MAGIC))
    with decrypted(archive,keyfile) as plain:assert plain.read_bytes()==content
    assert not plain.exists()
    broken=tmp_path/'corrupt';data=bytearray(archive.read_bytes());data[-1]^=1;broken.write_bytes(data)
    with pytest.raises(InvalidTag):
        with decrypted(broken,keyfile):pytest.fail('Unauthenticated dump exposed')
    attestation=tmp_path/'custody';attestation.write_text(json.dumps({'key_sha256':sha256(key).hexdigest(),
       'custodian':'Synthetic isolated test custodian','verified_at':NOW.isoformat(),'independent_recovery_verified':True}));attestation.chmod(0o600)
    custody=verify_key_custody(attestation,keyfile)
    class Archive:
        def put_object(self,**kw):
            assert kw['IfNoneMatch']=='*' and kw['Key'].startswith('forecast-evidence/v1/')
            self.body=kw['Body'].read()
        def get_object(self,**kw):return {'Body':BytesIO(self.body),'ContentLength':len(self.body)}
    receipt={'verified':True,'encrypted_sha256':sha256(archive.read_bytes()).hexdigest(),'encrypted_bytes':archive.stat().st_size}
    result=archive_to_s3(Archive(),'synthetic-isolated-bucket',archive,receipt,period_id='a'*64,custody=custody)
    assert result['off_device_copy_verified'] and result['independent_key_custody_verified']
    attestation.chmod(0o644)
    with pytest.raises(EvidenceError):verify_key_custody(attestation,keyfile)


def test_source_fingerprint_names_cannot_be_substituted():
    source=production_source();source['collection']['source_before']['unrelated_table']=source['collection']['source_before'].pop('stores')
    source['collection']['source_after']=deepcopy(source['collection']['source_before'])
    with pytest.raises(EvidenceError,match='snapshot proof'):verify_snapshot(source)


@pytest.mark.parametrize('command',['capture','capture-outcome','weekly','reconcile','forecast','evaluate'])
def test_disabled_worker_never_opens_any_database_or_collector(monkeypatch,command):
    from app.forecast_validation import cli
    from app.config import settings
    monkeypatch.setattr(settings,'forecast_validation_worker_enabled',False)
    monkeypatch.setattr(cli,'Store',lambda *a,**kw:pytest.fail('Disabled worker opened evidence storage'))
    monkeypatch.setattr(cli,'create_engine',lambda *a,**kw:pytest.fail('Disabled worker opened business storage'))
    monkeypatch.setattr('sys.argv',['forecast-validation',command])
    with pytest.raises(EvidenceError,match='worker is disabled'):cli.main()


@pytest.mark.parametrize('command',['weekly','reconcile'])
def test_recurring_gate_precedes_storage_even_with_manual_worker_enabled(monkeypatch,command):
    from app.forecast_validation import cli
    from app.config import settings
    monkeypatch.setattr(settings,'forecast_validation_worker_enabled',True)
    monkeypatch.setattr(settings,'forecast_validation_schedule_enabled',False)
    monkeypatch.setattr(cli,'Store',lambda *a,**kw:pytest.fail('Disabled schedule opened evidence storage'))
    monkeypatch.setattr('sys.argv',['forecast-validation',command])
    with pytest.raises(EvidenceError,match='Recurring forecasting execution is disabled'):cli.main()


def test_revision_tracks_business_date_versions_without_reobservation_noise():
    from app.forecast_validation.shared import outcome_revision
    first=outcome();record=first['rows'][0]['source_records'][0]
    record.update(source='square_line_correction',occurred_at='2026-08-10T12:00:00+00:00',version=1,
                  data={'quantity':'2','business_date':'2026-07-03'})
    original=outcome_revision(first)
    reobserved=deepcopy(first);reobserved['revision']='new-label';reobserved['observed_at']='2026-09-01T00:00:00+00:00'
    reobserved['rows'][0]['source_records'][0]['known_at']='2026-08-11T00:00:00+00:00'
    assert outcome_revision(reobserved)==original
    reobserved['rows'][0]['source_records'][0]['version']=2
    assert outcome_revision(reobserved)!=original


def test_incompressible_parameter_batches_remain_bounded(store):
    import random,base64
    rng=random.Random(20261022);sizes=[]
    @event.listens_for(store.engine,'before_cursor_execute')
    def capture(c,cur,statement,params,ctx,many):
        values=[len(v) for v in (params.values() if isinstance(params,dict) else params or ()) if isinstance(v,(str,bytes))]
        if statement.startswith('INSERT'):sizes.append((max(values or [0]),sum(values)))
    payload={'storage_stress_only':[base64.b64encode(rng.randbytes(6144)).decode() for _ in range(200)]}
    ident=store.stage_artifact(payload,project=False)
    assert codec.materialize(store.stream_artifact(ident))==payload
    assert max(v[0] for v in sizes)<=codec.MAX_COMPRESSED
    assert max(v[1] for v in sizes)<=1024*1024
