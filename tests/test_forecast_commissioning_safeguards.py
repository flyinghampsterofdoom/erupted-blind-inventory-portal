from datetime import datetime,timedelta,timezone
from pathlib import Path
import json,os,signal,subprocess,sys,time

import pytest
from app.forecast_validation.safeguards import (Limits,Yield,require_backup,require_capacity,
                                                private_json,supervise,linux_group_usage)

NOW=datetime(2026,10,23,1,0,tzinfo=timezone.utc)

def backup():
    return {**{n:True for n in ('remote_copy_verified','authenticated_decryption_verified',
            'all_table_restore_verified','independent_key_recovery_verified','retention_verified',
            'approved_retention_policy','isolated_destination_verified')},
            **{n:'a'*64 for n in ('encrypted_sha256','key_sha256','retention_policy_sha256')},
            'custodian':'synthetic test custodian','archive_identity':'synthetic test archive',
            'retention_days':30,'verified_at':NOW.isoformat()}

def sample():
    return {**{n:NOW.isoformat() for n in ('observed_at','web_memory_at','web_cpu_at','database_memory_at','database_cpu_at')},
            'web_memory_bytes':400*1024**2,'worker_rss_bytes':0,'database_memory_bytes':350*1024**2,
            'web_cpu_cores':.1,'database_cpu_cores':.05,'database_connections':5,'active_business_connections':1,
            'business_latency_ms':100,'blocked_business_sessions':0,'business_backup_active':False,
            'scheduling_active':False,'source_sync_active':False}

@pytest.mark.parametrize('key',list(backup()))
def test_missing_backup_prerequisite_fails_closed(key):
    value=backup();value.pop(key)
    with pytest.raises(Yield):require_backup(value,now=NOW)

@pytest.mark.parametrize('key',['remote_copy_verified','authenticated_decryption_verified','all_table_restore_verified',
                              'independent_key_recovery_verified','retention_verified','approved_retention_policy'])
def test_truthy_strings_are_not_backup_verification(key):
    value=backup();value[key]='true'
    with pytest.raises(Yield):require_backup(value,now=NOW)

def test_current_verified_backup_and_separate_key_fingerprint():
    assert require_backup(backup(),now=NOW)
    value=backup();value['verified_at']=(NOW-timedelta(days=2)).isoformat()
    with pytest.raises(Yield):require_backup(value,now=NOW)

@pytest.mark.parametrize('key',list(sample()))
def test_missing_observation_cannot_authorize_worker(key):
    value=sample();value.pop(key)
    with pytest.raises(Yield):require_capacity(value,Limits(),now=NOW)

@pytest.mark.parametrize('key,value',[
    ('web_memory_bytes',1280*1024**2),('worker_rss_bytes',900*1024**2),
    ('database_memory_bytes',750*1024**2),('web_cpu_cores',.8),('database_cpu_cores',.35),
    ('database_connections',30),('active_business_connections',8),('business_latency_ms',1500),
    ('blocked_business_sessions',1),('blocked_business_sessions',False),('business_backup_active',True),
    ('scheduling_active',True),('source_sync_active',True),('web_memory_bytes',0),
    ('database_cpu_cores',float('nan')),('web_cpu_cores',float('inf')),('database_connections',-1),
    ('business_latency_ms',False)])
def test_pressure_boundaries_stop(key,value):
    observed=sample();observed[key]=value
    with pytest.raises(Yield):require_capacity(observed,Limits(),now=NOW)

def test_freshness_is_based_on_actual_provider_sample_not_repacked_timestamp():
    value=sample();assert require_capacity(value,Limits(),now=NOW)
    value['database_memory_at']=(NOW-timedelta(seconds=91)).isoformat()
    with pytest.raises(Yield):require_capacity(value,Limits(),now=NOW)
    value=sample();value['observed_at']=(NOW+timedelta(seconds=1)).isoformat()
    with pytest.raises(Yield):require_capacity(value,Limits(),now=NOW)

def test_private_signals_reject_symlinks_and_public_modes(tmp_path):
    path=tmp_path/'signal';path.write_text(json.dumps(sample()));path.chmod(0o600)
    assert private_json(path)==sample();path.chmod(0o644)
    with pytest.raises(Yield):private_json(path)
    path.chmod(0o600);link=tmp_path/'link';link.symlink_to(path)
    with pytest.raises(OSError):private_json(link)

linux=pytest.mark.skipif(sys.platform!='linux',reason='Actual process groups, prctl and /proc require Linux')

def run(tmp_path,code,*,observe=sample,limits=None,environment=None):
    return supervise([sys.executable,'-c',code],environment=environment or {**os.environ,'FORECAST_VALIDATION_SCHEDULE_ENABLED':'false'},
        backup_receipt=backup(),observe=observe,journal=tmp_path/'journal',mutex=tmp_path/'mutex',
        limits=limits or Limits(poll_seconds=.02),now=lambda:NOW)

@linux
def test_guarded_child_inherits_hard_limits_nice_and_disabled_scheduling(tmp_path):
    out=tmp_path/'limits'
    code=f"import os,resource,json;open({str(out)!r},'w').write(json.dumps({{'as':resource.getrlimit(resource.RLIMIT_AS),'cpu':resource.getrlimit(resource.RLIMIT_CPU),'nice':os.getpriority(os.PRIO_PROCESS,0),'threads':os.getenv('OPENBLAS_NUM_THREADS'),'schedule':os.getenv('FORECAST_VALIDATION_SCHEDULE_ENABLED')}}))"
    result=run(tmp_path,code);assert result['status']=='COMPLETE';value=json.loads(out.read_text())
    assert value['as']==[1024*1024**2]*2 and value['cpu']==[120,121] and value['nice']==15
    assert value['threads']=='1' and value['schedule']=='false'

@linux
def test_backup_failure_does_not_start_a_child_or_create_qualification_journal(tmp_path):
    receipt=backup();receipt['independent_key_recovery_verified']=False;marker=tmp_path/'started'
    with pytest.raises(Yield):supervise([sys.executable,'-c',f'open({str(marker)!r},"w").close()'],environment=os.environ,
        backup_receipt=receipt,observe=sample,journal=tmp_path/'journal',mutex=tmp_path/'mutex',now=lambda:NOW)
    assert not marker.exists() and not (tmp_path/'journal').exists()

@linux
def test_pressure_change_terminates_process_group(tmp_path):
    calls=0
    pidfile=tmp_path/'pid'
    def pressure():
        nonlocal calls
        if pidfile.exists():calls+=1
        value=sample()
        if calls>8:value['scheduling_active']=True
        return value
    with pytest.raises(Yield,match='priority'):run(tmp_path,f'import os,time;open({str(pidfile)!r},"w").write(str(os.getpid()));time.sleep(60)',observe=pressure)
    pid=int(pidfile.read_text())
    with pytest.raises(ProcessLookupError):os.kill(pid,0)
    assert json.loads((tmp_path/'journal').read_text().splitlines()[-1])['status']=='YIELDED'

@linux
def test_deadline_kills_ignoring_term_child(tmp_path):
    with pytest.raises(Yield,match='deadline'):
        run(tmp_path,'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)',
            limits=Limits(deadline_seconds=.4,poll_seconds=.02,termination_seconds=.1))

@linux
def test_child_memory_failure_cannot_exhaust_host(tmp_path):
    with pytest.raises(Yield,match='child failed'):run(tmp_path,'x=bytearray(2*1024**3)')

@linux
def test_cpu_budget_pauses_and_finishes_without_throttling_supervisor(tmp_path):
    started=time.monotonic()
    assert run(tmp_path,'import time;until=time.process_time()+.3\nwhile time.process_time()<until:pass',
        limits=Limits(worker_cores=.20,poll_seconds=.02))['status']=='COMPLETE'
    assert time.monotonic()-started>=1
    rows=[json.loads(v) for v in (tmp_path/'journal').read_text().splitlines()]
    assert any(row['status']=='PAUSED_CPU' for row in rows)

@linux
def test_duplicate_local_execution_rejected_before_second_child(tmp_path):
    import fcntl
    with (tmp_path/'mutex').open('w') as file:
        os.chmod(tmp_path/'mutex',0o600)
        fcntl.flock(file,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with pytest.raises(Yield,match='already running'):run(tmp_path,'raise AssertionError("duplicate executed")')

@linux
@pytest.mark.parametrize('env',[{'FORECAST_VALIDATION_SCHEDULE_ENABLED':'true'},{'RENDER_API_KEY':'synthetic'}])
def test_recurring_enable_and_admin_credentials_are_rejected(tmp_path,env):
    with pytest.raises(Yield):run(tmp_path,'pass',environment={**os.environ,**env})

@linux
def test_exec_error_is_recorded_as_failure(tmp_path):
    with pytest.raises(Yield,match='child failed'):
        supervise(['/does/not/exist'],environment=os.environ,backup_receipt=backup(),observe=sample,
            journal=tmp_path/'journal',mutex=tmp_path/'mutex',now=lambda:NOW)
    assert json.loads((tmp_path/'journal').read_text().splitlines()[-1])['status']=='YIELDED'

@linux
def test_supervisor_death_stops_worker_in_kernel(tmp_path):
    pidfile=tmp_path/'worker-pid'
    source='''from app.forecast_validation.safeguards import supervise
from datetime import datetime
import json,os,sys
supervise([sys.executable,'-c',%r],environment=os.environ,backup_receipt=json.loads(%r),
observe=lambda:json.loads(%r),journal=%r,mutex=%r,now=lambda:datetime.fromisoformat(%r))
''' % (f'import os,time;open({str(pidfile)!r},"w").write(str(os.getpid()));time.sleep(60)',
       json.dumps(backup()),json.dumps(sample()),str(tmp_path/'journal'),str(tmp_path/'mutex'),NOW.isoformat())
    parent=subprocess.Popen([sys.executable,'-c',source])
    try:
        deadline=time.monotonic()+5
        while not pidfile.exists() and time.monotonic()<deadline:time.sleep(.02)
        assert pidfile.exists();worker=int(pidfile.read_text());parent.kill();parent.wait(timeout=2)
        while time.monotonic()<deadline:
            try:
                state=Path(f'/proc/{worker}/stat').read_text().rsplit(')',1)[1].split()[0]
                if state=='Z':break
            except FileNotFoundError:break
            time.sleep(.02)
        else:pytest.fail('Worker continued after supervisor death')
    finally:
        if parent.poll() is None:parent.kill();parent.wait()
