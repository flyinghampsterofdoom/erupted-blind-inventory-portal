"""Release candidate, failure classification, durable recovery and authority boundaries."""
import copy
from email.message import Message
import hashlib
import json
import subprocess
from pathlib import Path
import pytest
from scripts.release.phase3.manifest import validate, seal, prepare, verify_source, archive, write_once
from scripts.release.phase3.verification import (login_redirect, readiness, acceptance, execute, Failure)
from scripts.release.phase3.simulation import Simulation
from scripts.release.phase3.coordinator import Coordinator
from scripts.release.phase3.rehearse import scenario, SCENARIOS, fixture_manifest
from scripts.release.phase3.journal import Journal


@pytest.mark.parametrize('name', SCENARIOS)
def test_scenario_state_and_audit_trail(name,tmp_path):
    result=scenario(name,tmp_path/name)
    assert result['assertions']=='PASS' and result['production_mutations']==0
    for path in (tmp_path/name).glob('journal/release.json'):
        data=json.loads(path.read_text())
        with Journal(path.parent,data['manifest_id']) as journal:
            assert journal.data['events'] and len(journal.data['events'])==result['journal_events']
        assert path.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize('name',['location','Location','LOCATION','lOcAtIoN'])
def test_october_24_actual_header_dictionary_regression(name):
    response=Message();response[name]='/login?return_to=%2Fv2%2Fscheduling%2Fweek'
    if name=='location':assert dict(response).get('Location','')==''  # Original false failure.
    assert login_redirect(303,response)
    assert login_redirect(303,dict(response))


def test_ambiguous_headers_are_verifier_failure_and_open_redirect_is_application_failure():
    with pytest.raises(Failure) as e:login_redirect(303,[('Location','/login'),('location','https://bad.invalid/login')])
    assert e.value.finding.category=='verification'
    with pytest.raises(Failure) as e:login_redirect(303,{'LOCATION':'https://bad.invalid/login'})
    assert e.value.finding.category=='application'


@pytest.mark.parametrize('callback,category',[
    (lambda:(_ for _ in ()).throw(AssertionError('wrong expectation')),'verification'),
    (lambda:(_ for _ in ()).throw(TypeError('defective parser')),'verification'),
    (lambda:(_ for _ in ()).throw(ConnectionError('connection reset')),'infrastructure'),
    (lambda:(_ for _ in ()).throw(TimeoutError('provider outage')),'infrastructure'),
    (lambda:{'status':'SKIP'},'verification'),(lambda:None,'verification')])
def test_failure_classification_never_silently_passes(callback,category):
    with pytest.raises(Failure) as e:execute(callback)
    assert e.value.finding.category==category and e.value.finding.application_health=='unknown'


@pytest.mark.parametrize('field,value',[
    ('revision','d'*40),('source_tree_sha256','d'*64),('schema',{'expected':'other'}),
    ('verification',{'version':'unknown'}),('artifact',{'format':'other'})])
def test_manifest_edits_after_validation_rejected(field,value):
    m=fixture_manifest();m[field]=value
    with pytest.raises(ValueError,match='changed'):validate(m)


def test_web_cron_cannot_silently_select_a_different_commit():
    m=fixture_manifest();m['roles']['cron']['revision']='9'*40
    with pytest.raises(ValueError,match='substitution'):validate(seal({k:v for k,v in m.items() if k!='manifest_id'}))


@pytest.mark.parametrize('dependency',['process','database','schema','source'])
def test_http_200_never_suffices_for_readiness(dependency,tmp_path):
    m=fixture_manifest();p=Simulation(tmp_path/'provider.json',m);c=Coordinator(m,p,tmp_path/'j')
    assert c.run()['status']=='SUCCESS'
    proof=p.readiness(c.expected());proof['dependencies'][dependency]=False
    with pytest.raises(Failure) as e:readiness(proof,c.expected(),'fixture_2')
    assert e.value.finding.category=='application'


def test_unknown_readiness_or_functional_format_is_verifier_failure(tmp_path):
    with pytest.raises(Failure) as e:readiness({'status':200},{},'fixture_1')
    assert e.value.finding.category=='verification'
    p=Simulation(tmp_path/'p.json',fixture_manifest())
    def fetch(role,path):
        r=p.fetch(role,path)
        if path=='/v2/scheduling/api/board':r['body']='<html>Unsupported response</html>'
        return r
    with pytest.raises(Failure) as e:acceptance(fetch,p.principals)
    assert e.value.finding.category=='verification'


def test_session_identity_mismatch_and_expiry_do_not_pass(tmp_path):
    p=Simulation(tmp_path/'p.json',fixture_manifest())
    def expired(role,path):return {'status':401} if path=='/session-status' else p.fetch(role,path)
    with pytest.raises(Failure) as e:acceptance(expired,p.principals)
    assert e.value.finding.category=='verification'
    def wrong(role,path):
        r=p.fetch(role,path)
        if path=='/session-status':r['body']['principal_id']=999
        return r
    with pytest.raises(Failure):acceptance(wrong,p.principals)


def test_only_exact_local_provider_class_is_allowed(tmp_path):
    class LiveLike(Simulation):pass
    m=fixture_manifest()
    with pytest.raises(ValueError,match='Simulation'):Coordinator(m,LiveLike(tmp_path/'p.json',m),tmp_path/'j')
    class Arbitrary:
        mode='simulation-only'
    with pytest.raises(ValueError):Coordinator(m,Arbitrary(),tmp_path/'j')


def test_journal_manifest_and_state_corruption_are_detected(tmp_path):
    m=fixture_manifest();p=Simulation(tmp_path/'p.json',m);c=Coordinator(m,p,tmp_path/'j');c.run()
    path=tmp_path/'j/release.json';data=json.loads(path.read_text());data['status']='BYPASSED';path.write_text(json.dumps(data))
    with pytest.raises(ValueError,match='corrupted'):c.run()


def test_success_is_idempotent_and_failed_steps_require_explicit_recovery(tmp_path):
    m=fixture_manifest();p=Simulation(tmp_path/'p.json',m);c=Coordinator(m,p,tmp_path/'j')
    assert c.run()['status']=='SUCCESS';n=len(p.state['mutations'])
    assert c.run()['status']=='SUCCESS' and len(p.state['mutations'])==n
    p=Simulation(tmp_path/'q.json',m,faults={'deploy-web':'failure'});c=Coordinator(m,p,tmp_path/'k')
    assert c.run()['status']=='FAILED';n=len(p.state['mutations'])
    assert c.run()['status']=='FAILED' and len(p.state['mutations'])==n


def test_manifest_clean_checkout_and_source_artifact_substitution(tmp_path,monkeypatch):
    root=tmp_path/'source';root.mkdir();monkeypatch.delenv('RENDER_GIT_COMMIT',raising=False)
    def git(*args):return subprocess.check_output(['git','-C',str(root),*args],text=True).strip()
    git('init','-q');(root/'.gitignore').write_text('release-identity.json\n__pycache__/\n');(root/'app').mkdir();(root/'app/code.py').write_text('value = 1\n')
    (root/'migrations/versions').mkdir(parents=True)
    (root/'migrations/versions/base.py').write_text("revision='fixture_1'\ndown_revision=None\n")
    (root/'alembic.ini').write_text('[alembic]\nscript_location=migrations\n')
    for name in ('requirements.txt','requirements-test.txt'):(root/name).write_text('# fixture lock\n')
    git('add','.');git('-c','user.name=Test','-c','user.email=test@example.invalid','commit','-qm','Source')
    revision=git('rev-parse','HEAD')
    m=prepare(root,revision,from_schema='fixture_1',baseline_revision=revision,
        baseline_supports=['fixture_1'],compatibility_review='fixture')
    artifact=tmp_path/'source.tar';artifact.write_bytes(archive(root,revision))
    assert verify_source(root,m,artifact)
    write_once(tmp_path/'manifest.json',m)
    with pytest.raises(FileExistsError):write_once(tmp_path/'manifest.json',m)
    artifact.write_bytes(b'different artifact')
    with pytest.raises(ValueError,match='Artifact'):verify_source(root,m,artifact)
    (root/'app/code.py').write_text('value = 2\n')
    with pytest.raises(ValueError,match='clean'):verify_source(root,m)


def test_shared_environment_lock_blocks_different_journal_paths_and_processes(tmp_path):
    import sys
    from scripts.release.phase3.journal import Busy
    m=fixture_manifest();p=Simulation(tmp_path/'provider.json',m)
    first=Coordinator(m,p,tmp_path/'first');second=Coordinator(m,p,tmp_path/'second')
    assert first.lock_path==second.lock_path
    code="import fcntl,sys; f=open(sys.argv[1],'a'); fcntl.flock(f,fcntl.LOCK_EX); print('locked',flush=True); sys.stdin.readline()"
    child=subprocess.Popen([sys.executable,'-c',code,str(first.lock_path)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
    try:
        assert child.stdout.readline().strip()=='locked'
        with pytest.raises(Busy):second.run()
        assert not p.state['mutations']
    finally:child.communicate('\n',timeout=10)
    assert second.run()['status']=='SUCCESS'


@pytest.mark.parametrize('field',['candidate_revision','candidate_source','candidate_artifact'])
def test_candidate_swap_after_qualification_blocks_before_any_mutation(field,tmp_path):
    m=fixture_manifest();p=Simulation(tmp_path/'p.json',m);p.state[field]='9'*len(p.state[field]);p.save()
    r=Coordinator(m,p,tmp_path/'j').run()
    assert r['status']=='FAILED' and r['failures'][0]['code']=='qualified-candidate-substitution'
    assert not p.state['mutations']


def test_final_ci_qualification_rejects_pr_substitution_and_incomplete_runs():
    from scripts.release.phase3.qualification import verify_ci
    m=fixture_manifest();run={'id':1,'app_id':15368,'html_url':'https://github.invalid/run/1','head_sha':m['revision'],
        'event':'workflow_dispatch','status':'completed','conclusion':'success'}
    names=['Plan validation','CI required',*['Validate ('+s+')' for s in (
        'fast','schedule-foundation','schedule-a','schedule-b','purchasing','inventory-access','platform-forecast','other')]]
    jobs=[{'name':name,'conclusion':'success'} for name in names]
    identity={'revision':m['revision'],'source_tree_sha256':m['source_tree_sha256']}
    assert verify_ci(m,run,jobs,identity)['status']=='PASS'
    for key,value in [('event','pull_request'),('head_sha','f'*40),('conclusion','failure'),('status','in_progress')]:
        bad={**run,key:value}
        with pytest.raises(ValueError):verify_ci(m,bad,jobs,identity)
    with pytest.raises(ValueError):verify_ci(m,run,jobs[:-1],identity)
    with pytest.raises(ValueError):verify_ci(m,run,jobs,{**identity,'revision':'b'*40})


def test_inflight_cron_prevents_migration_even_when_suspended(tmp_path):
    m=fixture_manifest();p=Simulation(tmp_path/'p.json',m);p.state['cron_inflight']=True;p.save()
    r=Coordinator(m,p,tmp_path/'j').run()
    assert r['status']=='FAILED' and r['failures'][0]['code']=='maintenance-cron-fence-not-proven'
    assert p.state['schema']=='fixture_1' and not any(x['action']=='migrate' for x in p.state['mutations'])
