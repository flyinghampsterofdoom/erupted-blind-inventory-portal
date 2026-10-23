"""No source reads: publication deadline and independent group CPU safety."""
from datetime import timedelta
import sys
import pytest
from sqlalchemy import text
from app.forecast_validation.storage import Store
from app.forecast_validation.runner import forecast
from app.forecast_validation.contracts import EvidenceError
from test_forecast_stage3 import production_source
from test_forecast_validation import NOW
from test_forecast_commissioning_safeguards import backup,sample,NOW as SAFETY_NOW
from app.forecast_validation.safeguards import supervise,Limits,Yield


def test_staging_cannot_publish_a_prospective_run_after_freshness_deadline(tmp_path,monkeypatch):
    store=Store('sqlite:///'+str(tmp_path/'evidence.db'));store.initialize()
    elapsed=[0.0];monkeypatch.setattr('app.forecast_validation.storage.time.monotonic',lambda:elapsed[0])
    stage=store.stage_artifact
    def slow_stage(payload):
        artifact=stage(payload);elapsed[0]+=2;return artifact
    monkeypatch.setattr(store,'stage_artifact',slow_stage)
    with pytest.raises(EvidenceError,match='prospective'):
        forecast(store,production_source(),now=NOW+timedelta(seconds=899))
    with store.engine.connect() as db:
        assert db.execute(text('SELECT count(*) FROM fv_runs')).scalar_one()==0
        assert db.execute(text('SELECT count(*) FROM fv_artifacts')).scalar_one()==1
    store.engine.dispose()


@pytest.mark.skipif(sys.platform!='linux',reason='Private process-group supervision requires Linux')
def test_group_CPU_limit_is_independent_of_per_process_rlimit(tmp_path):
    import os
    # Aggregate sampled usage exceeds 120 even though no individual process
    # reports that CPU consumption to its per-process kernel RLIMIT_CPU.
    with pytest.raises(Yield,match='CPU budget'):
        supervise([sys.executable,'-c','import time;time.sleep(.2)'],environment=os.environ,
            backup_receipt=backup(),observe=sample,journal=tmp_path/'journal',mutex=tmp_path/'mutex',
            limits=Limits(poll_seconds=.02),now=lambda:SAFETY_NOW,
            usage=lambda pid:{'rss_bytes':1024,'cpu_seconds':121})

@pytest.mark.skipif(sys.platform!='linux',reason='Private process-group supervision requires Linux')
def test_isolated_worker_CPU_is_not_subtracted_from_web_pressure(tmp_path):
    import os,time,json
    began=time.monotonic()
    def observe():
        s=sample();s['web_cpu_usage_seconds']=.7*min(time.monotonic()-began,1.2);return s
    result=supervise([sys.executable,'-c','import time;time.sleep(.35)'],environment=os.environ,
        backup_receipt=backup(),observe=observe,journal=tmp_path/'journal',mutex=tmp_path/'mutex',
        limits=Limits(poll_seconds=.02,business_quiet_seconds=.3),now=lambda:SAFETY_NOW,
        usage=lambda pid:{'rss_bytes':1024,'cpu_seconds':.6*min(time.monotonic()-began,1.2)},
        web_cpu_includes_worker=False)
    rows=[json.loads(s) for s in (tmp_path/'journal').read_text().splitlines()]
    assert any(s['status']=='PAUSED_BUSINESS' for s in rows)
    assert result['elapsed_seconds']>=1.2


def test_expiry_during_publication_rolls_back_metadata(tmp_path,monkeypatch):
    from sqlalchemy import event
    store=Store('sqlite:///'+str(tmp_path/'evidence.db'));store.initialize()
    elapsed=[0.0];monkeypatch.setattr('app.forecast_validation.storage.time.monotonic',lambda:elapsed[0])
    @event.listens_for(store.engine,'after_cursor_execute')
    def delay(db,cursor,statement,parameters,context,many):
        if statement.startswith('INSERT INTO fv_runs'):elapsed[0]+=2
    with pytest.raises(EvidenceError,match='prospective'):
        forecast(store,production_source(),now=NOW+timedelta(seconds=899))
    with store.engine.connect() as db:
        assert db.execute(text('SELECT count(*) FROM fv_runs')).scalar_one()==0
    store.engine.dispose()


@pytest.mark.skipif(sys.platform!='linux',reason='Real /proc accounting requires Linux')
def test_reaped_children_remain_in_group_CPU_accounting(tmp_path):
    import subprocess,time,os
    from app.forecast_validation.safeguards import linux_group_usage
    marker=tmp_path/'reaped'
    code="import subprocess,sys,time;from pathlib import Path;subprocess.run([sys.executable,'-c','import time;end=time.process_time()+.25\\nwhile time.process_time()<end:pass'],check=True);Path(%r).touch();time.sleep(5)"%str(marker)
    child=subprocess.Popen([sys.executable,'-c',code],start_new_session=True)
    try:
        deadline=time.monotonic()+10
        while not marker.exists() and time.monotonic()<deadline:time.sleep(.02)
        assert marker.exists()
        assert linux_group_usage(child.pid)['cpu_seconds']>=.24
    finally:
        child.terminate();child.wait(timeout=2)

@pytest.mark.parametrize('age,expired',[(100,False),(901,True)])
def test_prospective_restart_preserves_frozen_origin_and_read_reservations(tmp_path,age,expired):
    from app.forecast_validation.operations import Period
    from app.forecast_validation.weekly import run_period
    store=Store('sqlite:///'+str(tmp_path/'evidence.db'));store.initialize()
    start=NOW.date()-timedelta(days=NOW.weekday());period=Period(store,start,now=NOW)
    source=production_source()
    with period.lock():
        period.claim();period.reserve_request('/v2/test/read','a'*64);artifact=period.freeze(source)
    def forbidden(*args):pytest.fail('Restart attempted a new source read')
    args=dict(collect_source=forbidden,pressure=lambda:True,backup=lambda *args:{'verified':True,'encrypted_sha256':'a'*64},now=NOW+timedelta(seconds=age),evaluate_outcomes=False)
    if expired:
        with pytest.raises(EvidenceError,match='prospective'):run_period(store,start,**args)
        assert store.list_runs()==[]
    else:assert run_period(store,start,**args)['status']=='COMPLETE'
    assert period.request_count()==1 and period.source()['artifact_id']==artifact
    from app.forecast_validation.codec import materialize
    assert materialize(store.stream_artifact(artifact))==source
    store.engine.dispose()


def test_independent_missed_run_check_detects_worker_that_never_started(tmp_path):
    from app.forecast_validation.operations import missed_periods,latest_alerts
    path='sqlite:///'+str(tmp_path/'evidence.db');store=Store(path);store.initialize();store.engine.dispose()
    independent=Store(path);notifications=[];start=NOW.date()-timedelta(days=NOW.weekday())
    missing=missed_periods(independent,now=NOW,activation_start=start,lookback_weeks=1,notify=notifications.append)
    assert len(missing)==len(notifications)==1
    assert notifications[0]['code']=='MISSED_OR_INCOMPLETE'
    assert latest_alerts(independent)[0]['period_id']==missing[0]['period_id']
    independent.engine.dispose()


def test_notification_outage_keeps_durable_alert_for_independent_retry(tmp_path):
    from app.forecast_validation.operations import missed_periods,latest_alerts
    store=Store('sqlite:///'+str(tmp_path/'evidence.db'));store.initialize();start=NOW.date()-timedelta(days=NOW.weekday())
    def offline(message):raise ConnectionError('Synthetic notification outage')
    args=dict(now=NOW,activation_start=start,lookback_weeks=1)
    with pytest.raises(ConnectionError):missed_periods(store,notify=offline,**args)
    first=latest_alerts(store);assert len(first)==1
    delivered=[];missed_periods(store,notify=delivered.append,**args)
    assert len(delivered)==1 and latest_alerts(store)==first
    store.engine.dispose()
