"""Independent build/runtime and infrastructure/application failure boundaries."""
import hashlib,json,urllib.error
from pathlib import Path
import pytest
from app.services.runtime_identity_service import capture_identity,tree_digest
from scripts.release.verify_runtime import (verify_web,verify_cron_logs,validate_deploy,GateFailure,InfrastructureUnavailable)

REV='a'*40;OLD='b'*40;DIGEST='c'*64

def proof(role='web',service='srv-test'):
 return {'status':'VERIFIED','revision':REV,'platform_revision':REV,'source_tree_sha256':DIGEST,'service_id':service,'role':role,'files_still_match':True,'principal_id':6,'owner_authorized':True,'schema':'20261018_0038','engine_version':'purchasing-v3-p2-exact-20261018'}

def test_actual_file_proof_cannot_be_replaced_by_expected_environment(tmp_path,monkeypatch):
 monkeypatch.setenv('RENDER_GIT_COMMIT',REV)
 assert capture_identity(tmp_path)['status']=='UNVERIFIED'
 (tmp_path/'code.py').write_text('original code')
 files={'code.py':hashlib.sha256((tmp_path/'code.py').read_bytes()).hexdigest()}
 (tmp_path/'release-identity.json').write_text(json.dumps({'revision':REV,'files':files,'source_tree_sha256':tree_digest(files)}))
 assert capture_identity(tmp_path)['status']=='VERIFIED'
 (tmp_path/'code.py').write_text('different code')
 assert capture_identity(tmp_path)['status']=='INVALID'
 (tmp_path/'code.py').write_text('original code');monkeypatch.setenv('RENDER_GIT_COMMIT',OLD)
 assert capture_identity(tmp_path)['status']=='INVALID'

class Clock:
 def __init__(self):self.now=0
 def __call__(self):return self.now
 def sleep(self,seconds):self.now+=seconds

def verify(fetch,clock=None):
 c=clock or Clock()
 return verify_web(fetch,revision=REV,previous_revision=OLD,digest=DIGEST,service_id='srv-test',schema='20261018_0038',owner_id=6,clock=c,sleep=c.sleep)

def test_transient_connectivity_and_old_runtime_stabilize_without_ssh():
 values=[urllib.error.URLError('disconnected'),{'revision':OLD},urllib.error.HTTPError('url',503,'unavailable',{},None),proof()]
 def fetch(timeout):
  assert 0<timeout<=15
  value=values.pop(0)
  if isinstance(value,Exception):raise value
  return value
 result=verify(fetch);assert result['status']=='PASS' and len(result['observations'])==3

@pytest.mark.parametrize('field,value',[('source_tree_sha256','wrong'),('revision','d'*40),('status','INVALID'),('files_still_match',False),('schema','wrong'),('owner_authorized',False),('principal_id',7),('service_id','wrong')])
def test_real_application_failure_stops_immediately(field,value):
 p=proof();p[field]=value;c=Clock()
 with pytest.raises(GateFailure):verify(lambda timeout:p,c)
 assert c.now==0

@pytest.mark.parametrize('status',[401,403,303,500])
def test_auth_and_application_http_errors_are_not_retried(status):
 c=Clock()
 def fetch(timeout):raise urllib.error.HTTPError('url',status,'error',{},None)
 with pytest.raises(GateFailure):verify(fetch,c)
 assert c.now==0

def test_infrastructure_deadline_is_exactly_bounded():
 c=Clock()
 def fetch(timeout):raise urllib.error.URLError('offline')
 with pytest.raises(InfrastructureUnavailable):verify(fetch,c)
 assert c.now==120

def test_deployed_sha_is_not_cron_runtime_proof():
 validate_deploy({'status':'live','commit':{'id':REV}},REV)
 with pytest.raises(GateFailure):verify_cron_logs([],revision=REV,digest=DIGEST,service_id='crn-test',deployed_at='2026-10-18T10:00:00Z',last_success=None)
 p={**proof('cron','crn-test'),'event':'ERUPTED_RUNTIME_IDENTITY'}
 logs=[{'message':json.dumps(p),'timestamp':'2026-10-18T10:02:00Z','labels':[{'name':'resource','value':'crn-test'}]}]
 with pytest.raises(GateFailure):verify_cron_logs(logs,revision=REV,digest=DIGEST,service_id='crn-test',deployed_at='2026-10-18T10:03:00Z',last_success='2026-10-18T10:04:00Z')
 with pytest.raises(GateFailure):verify_cron_logs(logs,revision=REV,digest=DIGEST,service_id='crn-test',deployed_at='2026-10-18T10:00:00Z',last_success='2026-10-18T10:01:00Z')
 assert verify_cron_logs(logs,revision=REV,digest=DIGEST,service_id='crn-test',deployed_at='2026-10-18T10:00:00Z',last_success='2026-10-18T10:04:00Z')['status']=='PASS'


def test_build_identity_comes_from_clean_git_checkout(tmp_path,monkeypatch):
 import subprocess
 from scripts.release.build_identity import build
 monkeypatch.delenv('RENDER_GIT_COMMIT',raising=False)
 def git(*args):return subprocess.check_output(['git',*args],cwd=tmp_path,text=True)
 git('init','-q');(tmp_path/'app').mkdir();(tmp_path/'app/main.py').write_text('print("code")\n')
 git('add','app/main.py');git('-c','user.name=Local Test','-c','user.email=test@example.invalid','commit','-qm','Synthetic source')
 expected=git('rev-parse','HEAD').strip();result=build(tmp_path)
 assert result['revision']==expected and capture_identity(tmp_path)['status']=='VERIFIED'
 (tmp_path/'app/main.py').write_text('changed')
 with pytest.raises(RuntimeError,match='uncommitted'):build(tmp_path)
 monkeypatch.setenv('RENDER_GIT_COMMIT','f'*40)
 with pytest.raises(RuntimeError,match='Checkout differs'):build(tmp_path)
