import copy,json,urllib.error,io
from fractions import Fraction
from pathlib import Path
import pytest
from scripts.release.rfc3339 import timestamp
from scripts.release.verify_runtime import (verify_cron_logs,verify_web,GateFailure,
    PendingEvidence,ProviderEvidenceError,AuthenticationRequired,InfrastructureUnavailable)
from scripts.release.render_contract import deploy_state,collect_logs,get_json,check_session
from scripts.release.cron_recovery import recover,RecoveryJournal,verify_legacy_restoration
from test_runtime_identity import Clock,proof,verify

FIX=Path(__file__).parent/'fixtures/render'
def fixture(name):return json.loads((FIX/name).read_text())
REV='e95f3fe45106577f0a90fc7fea69b91e65eed500';OLD='8d227f84272a5ade80cbc56b87da9f0aaa3f3b2f';SID='crn-da6pd2qd0e5s73dbudr0'
DIGEST='38d7d4e418b7d510dee711b8e676a276f6edbc57890d05ab555c39c5416671b8'

@pytest.mark.parametrize('value',[
 '2026-10-19T05:00:45Z','2026-10-19T05:00:45.0Z','2026-10-19T05:00:45.000Z',
 '2026-10-19T05:00:45.000000Z','2026-10-19T05:00:45.000000000Z',
 '2026-10-18T22:00:45-07:00','2026-10-19T10:30:45+05:30','2026-10-19t05:00:45z',
 '2026-10-19T05:00:45-00:00'])
def test_valid_timestamp_equivalence(value):assert timestamp(value)==timestamp('2026-10-19T05:00:45Z')

@pytest.mark.parametrize('digits',['1','12','123','1234','12345','123456','1234567','12345678','123456789','123456789123'])
def test_arbitrary_fraction_is_exact(digits):
 assert timestamp('2026-10-19T00:00:00.'+digits+'Z')-timestamp('2026-10-19T00:00:00Z')==Fraction(int(digits),10**len(digits))

@pytest.mark.parametrize('value',[None,123,'','2026-10-19','2026-10-19T05:00:45','2026-10-19 05:00:45Z',
 '2026-02-30T00:00:00Z','2026-10-19T24:00:00Z','2026-10-19T00:60:00Z',
 '2026-10-19T00:00:00.Z','2026-10-19T00:00:00.12xZ','2026-10-19T00:00:00+24:00',
 '2026-10-19T00:00:00+00:60','2026-10-19T00:00:00Zjunk'])
def test_malformed_or_unusable_timestamps_fail(value):
 with pytest.raises(ValueError):timestamp(value)


def cron(logs=None,**kw):
 values=dict(revision=REV,digest=DIGEST,service_id=SID,deployed_at=fixture('cron-deploy.json')['finishedAt'],last_success='2026-10-19T05:01:22Z');values.update(kw)
 return verify_cron_logs(fixture('nanosecond-cron-logs.json')['logs'] if logs is None else logs,**values)


def test_captured_render_run_that_previously_false_failed_now_passes():
 result=cron();assert result['status']=='PASS' and result['log_timestamp']=='2026-10-19T05:00:45.020951672Z'


def test_one_nanosecond_boundary_is_not_rounded_away():
 logs=fixture('nanosecond-cron-logs.json')['logs'];row=next(x for x in logs if 'ERUPTED_RUNTIME_IDENTITY' in x['message'])
 assert cron([row],deployed_at='2026-10-19T05:00:45.020951671Z')['status']=='PASS'
 with pytest.raises(PendingEvidence):cron([row],deployed_at='2026-10-19T05:00:45.020951673Z')

@pytest.mark.parametrize('field',['timestamp','message','labels'])
def test_malformed_relevant_proof_is_infrastructure_failure_not_silently_skipped(field):
 logs=fixture('nanosecond-cron-logs.json')['logs'];row=next(x for x in logs if 'ERUPTED_RUNTIME_IDENTITY' in x['message']);row[field]='{"event":"ERUPTED_RUNTIME_IDENTITY",' if field=='message' else 'malformed'
 with pytest.raises(ProviderEvidenceError):cron([row])

@pytest.mark.parametrize('field,value',[('revision','f'*40),('source_tree_sha256','bad'),('role','web'),('service_id','wrong'),('platform_revision','f'*40),('status','INVALID')])
def test_actual_cron_identity_mismatch_stops(field,value):
 logs=fixture('nanosecond-cron-logs.json')['logs'];row=next(x for x in logs if 'ERUPTED_RUNTIME_IDENTITY' in x['message']);p=json.loads(row['message']);p[field]=value;row['message']=json.dumps(p)
 with pytest.raises(GateFailure):cron([row])


def test_expected_wait_is_distinct_from_failed_application():
 with pytest.raises(PendingEvidence):cron([])
 with pytest.raises(PendingEvidence):cron(last_success=None)
 with pytest.raises(ProviderEvidenceError):cron(last_success='not a date')

@pytest.mark.parametrize('status',['created','queued','build_in_progress','pre_deploy_in_progress','update_in_progress'])
def test_all_provider_progress_states_wait(status):
 d=fixture('web-deploy.json');d['status']=status;assert deploy_state(d,REV)=='WAIT'

@pytest.mark.parametrize('status',['build_failed','pre_deploy_failed','update_failed','canceled','deactivated'])
def test_terminal_status_stops(status):
 d=fixture('web-deploy.json');d['status']=status
 with pytest.raises(GateFailure):deploy_state(d,REV)


def test_unknown_or_malformed_metadata_never_passes():
 d=fixture('web-deploy.json');d['status']='future-provider-status'
 with pytest.raises(ProviderEvidenceError):deploy_state(d,REV)
 with pytest.raises(ProviderEvidenceError):deploy_state([],REV)
 with pytest.raises(GateFailure):deploy_state(fixture('web-deploy.json'),OLD)
 assert deploy_state(fixture('web-deploy.json'),REV)=='PASS'


def test_log_pagination_accepts_real_final_page_and_refuses_stuck_cursor():
 captured=fixture('nanosecond-cron-logs.json');assert collect_logs(lambda path:captured,{'ownerId':'owner'})==captured['logs']
 stuck={'logs':[],'hasMore':True,'nextStartTime':'2026-10-19T00:00:00.000000001Z','nextEndTime':'2026-10-19T01:00:00Z'}
 with pytest.raises(ProviderEvidenceError):collect_logs(lambda path:stuck,{})
 with pytest.raises(ProviderEvidenceError):collect_logs(lambda path:{'logs':[],'hasMore':True},{})


def test_get_network_interruptions_retry_boundedly_without_mutation():
 c=Clock();items=[urllib.error.URLError('reset'),urllib.error.HTTPError('u',429,'limit',{'Retry-After':'2'},None),io.BytesIO(b'{"ok":true}')];methods=[]
 def open_url(request,timeout):
  methods.append(request.get_method());v=items.pop(0)
  if isinstance(v,Exception):raise v
  return v
 assert get_json('https://provider.invalid',clock=c,sleep=c.sleep,open_url=open_url)=={'ok':True}
 assert methods==['GET']*3 and c.now==5
 c=Clock()
 def down(*args,**kw):raise TimeoutError()
 with pytest.raises(InfrastructureUnavailable):get_json('https://provider.invalid',timeout=12,clock=c,sleep=c.sleep,open_url=down)
 assert c.now==12

@pytest.mark.parametrize('status',[301,302,303,307,308,401,403])
def test_expired_auth_never_retried_as_rollover(status):
 def fetch(timeout):raise urllib.error.HTTPError('u',status,'auth',{},None)
 with pytest.raises(AuthenticationRequired):verify(fetch)


def test_session_preflight_uses_real_expiry_not_default_assumption():
 now='2026-10-19T04:54:21.361965Z'
 with pytest.raises(AuthenticationRequired):check_session({'authenticated':True,'principal_id':6,'expires_at':now},owner_id=6,now=now)
 assert check_session({'authenticated':True,'principal_id':6,'expires_at':'2026-10-19T05:24:21Z'},owner_id=6,now=now)['status']=='PASS'
 with pytest.raises(AuthenticationRequired):check_session({'authenticated':False},owner_id=6,now=now)


def test_captured_recovery_matches_actual_subsequent_natural_job():
 result=verify_legacy_restoration(service=fixture('restored-service.json'),deploy=fixture('restored-deploy.json'),deployments=fixture('restored-deployments.json'),events=fixture('restored-natural-events.json'),revision=OLD)
 assert result['status']=='PASS' and result['revision']==OLD
 assert result['runtime_source_digest'].startswith('UNAVAILABLE')


def test_recovery_acceptance_requires_successful_real_run_not_accepted_deploy():
 args=dict(service=fixture('restored-service.json'),deploy=fixture('restored-deploy.json'),deployments=fixture('restored-deployments.json'),events=[],revision=OLD)
 with pytest.raises(PendingEvidence):verify_legacy_restoration(**args)
 args['revision']=REV
 with pytest.raises(GateFailure):verify_legacy_restoration(**args)


class FakeRender:
 def __init__(self):
  self.service={'id':SID,'type':'cron_job','branch':'release','autoDeploy':'no','serviceDetails':{'schedule':'0 * * * *','envSpecificDetails':{'startCommand':'existing cron'}}};self.calls=[];self.fail_post=False;self.statuses=['build_in_progress','pre_deploy_in_progress','live'];self.response_revision=OLD
 def __call__(self,path,method='GET',body=None):
  self.calls.append((path,method,body))
  if path.endswith('/rollback') or path.endswith('/runs'):raise AssertionError('Forbidden native rollback/manual cron run')
  if method=='PATCH':self.service['branch']=body['branch'];return copy.deepcopy(self.service)
  if method=='POST':
   if self.fail_post:raise TimeoutError('Ambiguous POST result')
   assert body=={'clearCache':'do_not_clear'};return {'id':'recovery','commit':{'id':self.response_revision}}
  if '/deploys/' in path:return {'id':'recovery','commit':{'id':OLD},'status':self.statuses.pop(0),'finishedAt':'2026-10-19T05:04:13.122396Z'}
  return copy.deepcopy(self.service)


def run_recovery(fake,tmp_path,**kw):
 clock=Clock();values=dict(service_id=SID,revision=OLD,recovery_branch='unique-baseline',branch_tip=lambda branch:OLD,config_fingerprint=lambda s:json.dumps({k:s[k] for k in ['autoDeploy','serviceDetails']},sort_keys=True),verify_restored_run=lambda d:{'status':'PASS','revision':OLD,'run_id':'natural-run'},journal=RecoveryJournal(tmp_path/'journal.json'),clock=clock,sleep=clock.sleep)
 values.update(kw);return recover(fake,**values)


def test_supported_cron_recovery_restores_config_with_no_duplicate_jobs(tmp_path):
 fake=FakeRender();assert run_recovery(fake,tmp_path)['status']=='PASS';assert fake.service['branch']=='release';assert fake.service['serviceDetails']['schedule']=='0 * * * *'
 assert sum(method=='POST' for _,method,_ in fake.calls)==1
 with pytest.raises(GateFailure,match='already submitted'):run_recovery(fake,tmp_path)


def test_ambiguous_recovery_submission_never_retried_and_branch_restored(tmp_path):
 fake=FakeRender();fake.fail_post=True
 with pytest.raises(TimeoutError):run_recovery(fake,tmp_path)
 assert fake.service['branch']=='release'
 with pytest.raises(GateFailure,match='already submitted'):run_recovery(fake,tmp_path)
 assert sum(m=='POST' for _,m,_ in fake.calls)==1


def test_branch_drift_and_wrong_recovery_image_stop(tmp_path):
 fake=FakeRender()
 with pytest.raises(GateFailure,match='not pinned'):run_recovery(fake,tmp_path,branch_tip=lambda b:REV)
 assert not any(m=='POST' for _,m,_ in fake.calls)
 fake.response_revision=REV
 with pytest.raises(GateFailure,match='unexpected commit'):run_recovery(fake,tmp_path)
 assert fake.service['branch']=='release'


def test_provider_shaped_end_to_end_rollover_and_recovery(tmp_path):
 # No SSH, real API or real scheduler. Exact captured proof that caused rollback
 # is accepted only after web passes and the natural cron run has completed.
 states=['queued','build_in_progress','pre_deploy_in_progress','update_in_progress','live']
 for status in states:
  d=fixture('web-deploy.json');d['status']=status;assert deploy_state(d,REV)==('PASS' if status=='live' else 'WAIT')
 values=[{'revision':'b'*40},urllib.error.URLError('rollover'),proof()]
 def fetch(timeout):
  v=values.pop(0)
  if isinstance(v,Exception):raise v
  return v
 assert verify(fetch)['status']=='PASS'
 with pytest.raises(PendingEvidence):cron([])
 assert cron()['status']=='PASS'
 assert run_recovery(FakeRender(),tmp_path)['status']=='PASS'


def test_actual_cron_cli_replays_provider_response_and_returns_wait_before_natural_run(tmp_path,monkeypatch):
 from scripts.release import verify_render_cron as cli
 manifest=tmp_path/'manifest.json';manifest.write_text(json.dumps({'revision':REV,'source_tree_sha256':DIGEST}));output=tmp_path/'result.json'
 monkeypatch.setenv('RENDER_API_KEY','synthetic-not-a-credential')
 monkeypatch.setattr('sys.argv',['verify_render_cron','--service-id',SID,'--deploy-id',fixture('cron-deploy.json')['id'],'--manifest',str(manifest),'--output',str(output)])
 def get(url,**kw):
  if '/logs?' in url:return fixture('nanosecond-cron-logs.json')
  if '/deploys/' in url:return fixture('cron-deploy.json')
  return {'id':SID,'type':'cron_job','ownerId':'synthetic-owner','serviceDetails':{'schedule':'0 * * * *','lastSuccessfulRunAt':'2026-10-19T05:01:22Z'}}
 monkeypatch.setattr(cli,'get_json',get);assert cli.main()==0 and json.loads(output.read_text())['status']=='PASS'
 def pending(url,**kw):
  if '/logs?' in url:return {'logs':[],'hasMore':False}
  return get(url,**kw)
 monkeypatch.setattr(cli,'get_json',pending);assert cli.main()==2 and json.loads(output.read_text())['status']=='WAIT'
 def malformed(url,**kw):
  if '/logs?' in url:
   page=fixture('nanosecond-cron-logs.json');page['logs'][2]['timestamp']='invalid';return page
  return get(url,**kw)
 monkeypatch.setattr(cli,'get_json',malformed);assert cli.main()==1 and json.loads(output.read_text())['category']=='ProviderEvidenceError'


def test_actual_web_cli_checks_fresh_session_and_rejects_expiry(tmp_path,monkeypatch):
 import http.cookiejar
 from datetime import datetime,timedelta,timezone
 from scripts.release import verify_runtime as cli
 manifest=tmp_path/'manifest.json';manifest.write_text(json.dumps({'revision':'a'*40,'source_tree_sha256':'c'*64}))
 deploy=tmp_path/'deploy.json';deploy.write_text(json.dumps({'status':'live','commit':{'id':'a'*40}}))
 cookie=tmp_path/'cookies';jar=http.cookiejar.MozillaCookieJar(str(cookie));jar.save();cookie.chmod(0o600);output=tmp_path/'result.json'
 monkeypatch.setattr('sys.argv',['verify_runtime','--url','https://synthetic.invalid','--cookie-file',str(cookie),'--manifest',str(manifest),'--previous-revision','b'*40,'--service-id','srv-test','--deploy-evidence',str(deploy),'--owner-id','6','--schema','20261018_0038','--output',str(output)])
 class Opener:
  def __init__(self):self.expired=False;self.paths=[]
  def open(self,request,timeout):
   self.paths.append(request.full_url)
   if '/session-status' in request.full_url:
    if self.expired:raise urllib.error.HTTPError(request.full_url,401,'expired',{},None)
    obj={'authenticated':True,'principal_id':6,'expires_at':(datetime.now(timezone.utc)+timedelta(minutes=30)).isoformat()}
   else:obj=proof()
   return io.BytesIO(json.dumps(obj).encode())
 opener=Opener();monkeypatch.setattr(cli.urllib.request,'build_opener',lambda *args:opener)
 assert cli.main()==0 and json.loads(output.read_text())['status']=='PASS'
 opener.expired=True;opener.paths=[]
 assert cli.main()==1 and json.loads(output.read_text())['category']=='AuthenticationRequired'
 assert len(opener.paths)==1 and opener.paths[0].endswith('/session-status')


def test_truncated_network_read_retries_only_get():
 import http.client
 clock=Clock();items=[http.client.IncompleteRead(b'{'),io.BytesIO(b'{"ok":true}')]
 def open_url(request,timeout):
  value=items.pop(0)
  if isinstance(value,Exception):raise value
  return value
 assert get_json('https://synthetic.invalid',clock=clock,sleep=clock.sleep,open_url=open_url)=={'ok':True}


def test_string_booleans_and_short_revisions_cannot_pass():
 for field in ['owner_authorized','files_still_match']:
  obj=proof();obj[field]='true'
  with pytest.raises(GateFailure):verify(lambda timeout:obj)
 from scripts.release.verify_runtime import validate_identity
 with pytest.raises(ProviderEvidenceError):validate_identity(proof(),revision='abcdef',digest='c'*64,service_id='srv-test',role='web')


def test_same_second_completion_requires_matching_ordered_success_marker():
 logs=fixture('nanosecond-cron-logs.json')['logs'];row=next(x for x in logs if 'ERUPTED_RUNTIME_IDENTITY' in x['message'])
 with pytest.raises(PendingEvidence):cron([row],last_success='2026-10-19T05:00:45Z')
 complete={'labels':row['labels'],'message':'Cron job run finished successfully','timestamp':'2026-10-19T05:00:45.999999999Z'}
 assert cron([row,complete],last_success='2026-10-19T05:00:45Z')['status']=='PASS'
 fail=dict(complete,message='Cron job run finished unsuccessfully')
 with pytest.raises(GateFailure):cron([row,fail],last_success='2026-10-19T05:01:22Z')
 later=dict(complete,message='Cron job run started',timestamp='2026-10-19T05:00:45.5Z')
 with pytest.raises(PendingEvidence):cron([row,later,complete],last_success='2026-10-19T05:00:45Z')


def test_recovery_configuration_fingerprint_includes_credentials_without_exposing_them():
 from scripts.release.cron_recovery import configuration_fingerprint
 service=FakeRender().service;before=configuration_fingerprint(service,{'DATABASE_URL':'synthetic secret one'})
 assert 'synthetic' not in before
 assert before!=configuration_fingerprint(service,{'DATABASE_URL':'synthetic secret two'})
 changed=copy.deepcopy(service);changed['serviceDetails']['schedule']='*/30 * * * *'
 assert before!=configuration_fingerprint(changed,{'DATABASE_URL':'synthetic secret one'})


def test_recovery_rejects_nonexact_sha_before_any_provider_call(tmp_path):
 fake=FakeRender()
 with pytest.raises(GateFailure,match='full approved'):run_recovery(fake,tmp_path,revision='HEAD')
 assert not fake.calls


def test_direct_script_entrypoint_catches_shared_authentication_error(tmp_path,monkeypatch):
 # runpy uses __main__, which must share exception identities with imported
 # contract helpers. Import-only CLI tests do not exercise this boundary.
 import runpy,http.cookiejar
 from scripts.release import verify_runtime as cli
 manifest=tmp_path/'manifest.json';manifest.write_text(json.dumps({'revision':'a'*40,'source_tree_sha256':'c'*64}));deploy=tmp_path/'deploy.json';deploy.write_text(json.dumps({'status':'live','commit':{'id':'a'*40}}))
 cookie=tmp_path/'cookies';http.cookiejar.MozillaCookieJar(str(cookie)).save();cookie.chmod(0o600);output=tmp_path/'result.json'
 monkeypatch.setattr('sys.argv',['verify_runtime','--url','https://synthetic.invalid','--cookie-file',str(cookie),'--manifest',str(manifest),'--previous-revision','b'*40,'--service-id','srv-test','--deploy-evidence',str(deploy),'--owner-id','6','--schema','20261018_0038','--output',str(output)])
 class Opener:
  def open(self,request,timeout):return io.BytesIO(b'{"authenticated":false}')
 monkeypatch.setattr(cli.urllib.request,'build_opener',lambda *args:Opener())
 with pytest.raises(SystemExit) as ex:runpy.run_path(cli.__file__,run_name='__main__')
 assert ex.value.code==1 and json.loads(output.read_text())['category']=='AuthenticationRequired'
