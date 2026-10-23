"""Preparation only: all provider/mail calls are fakes; source requests are ledger fixtures."""
from datetime import datetime,timedelta,timezone
from copy import deepcopy
import base64,hashlib,hmac,json
import pytest
from sqlalchemy import text,select
from app.forecast_validation.storage import Store
from app.forecast_validation.operations import Period,period_events
from app.forecast_validation.qualification import GRANT,GRANT_ID,Capacity,QualifiedPeriod,require_execution,records
from app.forecast_validation.contracts import EvidenceError,digest,canonical
from app.forecast_validation.execution import authorize,child_environment
from app.forecast_validation.job_control import Controller,BASE_SERVICE,PLAN,job_request
from app.forecast_validation.notifications import queue_email,deliver_email,record_webhook,ResendTransport
from app.forecast_validation.observer import missed_run,observe
from app.forecast_validation.backup_verification import verified_recovery
from test_forecast_commissioning_safeguards import sample,Limits

NOW=datetime(2026,10,26,10,tzinfo=timezone.utc);SHA='a'*40

def permit():
 return {'schema':'fv-manual-execution-v1','grant_id':GRANT_ID,'action':'EXECUTE_ONE_CAPTURE','recurring':False,
         'slot':0,'deployment_sha':SHA,'approval_id':'SYNTHETIC_EXECUTION_APPROVAL',
         'period_start':'2026-10-26','scope_policy':'local-capture-0',
         'not_before':'2026-10-26T07:00:00Z','not_after':'2026-10-27T13:00:00Z'}

@pytest.fixture
def store(tmp_path):
 s=Store('sqlite:///'+str(tmp_path/'control.db'));s.initialize();yield s;s.engine.dispose()

def historical(store):
 p=Period(store,'2026-10-19',scope_policy='local-prior-fixture',now=NOW)
 with p.lock():
  p.claim()
  for i in range(311):p.reserve_request('/v2/test',str(i))

def qp(store,slot):return QualifiedPeriod(store,'2026-10-26',scope_policy='local-capture-'+str(slot),now=NOW,capacity=Capacity(store),slot=slot)
def total(store):
 with store.engine.connect() as c:return c.execute(select(__import__('sqlalchemy').func.count()).select_from(period_events).where(period_events.c.status=='SOURCE_REQUEST')).scalar_one()


def test_capacity_preparation_never_authorizes_execution():
 assert GRANT['additional_reservations']==304 and not GRANT['execution_authorized']
 for config in [{},{'execution_authorized':False},{'execution_authorized':True,'recurring_enabled':False,'deployment_sha':SHA}]:
  with pytest.raises(EvidenceError):authorize(config,permit(),now=NOW)

@pytest.mark.parametrize('field,value',[('slot',2),('slot',True),('recurring',True),('deployment_sha','b'*40),('grant_id','other'),('not_after','2026-10-28T13:00:00Z'),('not_after','2026-10-26T09:00:00Z')])
def test_execution_scope_and_tuesday_deadline_are_not_relaxed(field,value):
 p=permit();p[field]=value
 with pytest.raises(EvidenceError):require_execution(p,approved_sha256=digest(p),deployment_sha=SHA,now=NOW)


def test_global615_and_two152_slots_preserve311(store):
 historical(store)
 for slot in range(2):
  p=qp(store,slot)
  with p.lock():
   p.claim()
   for i in range(132):p.reserve_request('/v2/test','a'*64)
   with pytest.raises(EvidenceError):p.reserve_request('/v2/test','a'*64)
   for i in range(20):p.reserve_request('/v2/test','a'*64,retry=True)
   with pytest.raises(EvidenceError):p.reserve_request('/v2/test','a'*64,retry=True)
 assert total(store)==615
 with store.engine.connect() as c:
  assert len(records(c,'qualification-reservation'))==304
  assert len(records(c,'qualification-binding'))==2


def test_slot_cannot_be_rebound_or_frozen_source_recollected(store):
 historical(store);p=qp(store,0)
 with p.lock():
  p.claim();p.reserve_request('/v2/test','a'*64);p.freeze(__import__('test_forecast_stage3').production_source(NOW))
  with pytest.raises(EvidenceError,match='Frozen'):p.reserve_request('/v2/test','a'*64)
 other=QualifiedPeriod(store,'2026-10-26',scope_policy='other-scope',capacity=Capacity(store),slot=0,now=NOW)
 with other.lock():
  with pytest.raises(EvidenceError,match='conflict'):other.claim()
 assert total(store)==312


def test_reservation_and_control_receipt_are_atomic(store,monkeypatch):
 historical(store);p=qp(store,0)
 with p.lock():
  p.claim()
  def fail(*args,**kw):raise RuntimeError('synthetic storage interruption')
  monkeypatch.setattr('app.forecast_validation.qualification._event',fail)
  with pytest.raises(RuntimeError):p.reserve_request('/v2/test','a'*64)
 assert total(store)==311
 with store.engine.connect() as c:assert records(c,'qualification-reservation')==[]


def test_unknown_source_activity_is_not_an_authorized_counter_reset(store):
 historical(store);p=qp(store,0)
 with p.lock():p.claim()
 unrelated=Period(store,'2026-10-26',scope_policy='local-unattributed',now=NOW)
 with unrelated.lock():unrelated.claim();unrelated.reserve_request('/v2/test','unknown')
 with p.lock():
  with pytest.raises(EvidenceError,match='Unattributed'):p.reserve_request('/v2/test','a'*64)
 assert total(store)==312


def test_new_budget_cannot_replace_an_exhausted_baseline(store):
 p=Period(store,'2026-10-19',scope_policy='local-exhausted',now=NOW)
 with p.lock():
  p.claim()
  for i in range(400):p.reserve_request('/v2/test',str(i))
 q=qp(store,0)
 with q.lock():
  with pytest.raises(EvidenceError,match='baseline'):q.claim()
 assert total(store)==400


def test_bootstrap_environment_excludes_business_admin_and_private_keys():
 env={'PATH':'/bin','FORECAST_QUALIFICATION_SOURCE_DATABASE_URL':'source-readonly',
 'FORECAST_QUALIFICATION_EVIDENCE_DATABASE_URL':'evidence-appendonly',
 'DATABASE_URL':'business-admin','RENDER_API_KEY':'synthetic','OWNER_SESSION':'synthetic',
 'AGE_SECRET_KEY':'synthetic','RECOVERY_KEY':'synthetic','RESEND_API_KEY':'synthetic'}
 clean=child_environment(env)
 assert clean['DATABASE_URL']=='source-readonly'
 assert all(key not in clean for key in ['OWNER_SESSION','RENDER_API_KEY','AGE_SECRET_KEY','RECOVERY_KEY','RESEND_API_KEY'])
 assert clean['FORECAST_VALIDATION_SCHEDULE_ENABLED']=='false'


def ready():return dict.fromkeys(('build_compatible','billing_approved','database_authorized','backup_verified','observer_ready','source_coverage_complete','independent_monitor_ready','cron_idle'),True)
class API:
 def __init__(self,uncertain=False):self.calls=[];self.uncertain=uncertain;self.job=None
 def __call__(self,path,method='GET',body=None):
  self.calls.append((path,method,body))
  if '/deploys?' in path:return [{'deploy':{'status':'live','commit':{'id':SHA}}}]
  if method=='POST' and path.endswith('/jobs'):
   self.job={'id':'job-'+'a'*20,'serviceId':BASE_SERVICE,'planId':PLAN,'startCommand':body['startCommand'],'status':'running'}
   if self.uncertain:raise TimeoutError('synthetic response loss')
   return self.job
  if method=='POST' and path.endswith('/cancel'):return {}
  if '?' in path:return [{'job':self.job}] if self.job else []
  return self.job


def test_prepared_controller_makes_no_provider_calls(store):
 api=API();c=Controller(store,api,deployment_sha=SHA,now=lambda:NOW)
 with pytest.raises(EvidenceError):c.dispatch(permit(),readiness=ready())
 assert api.calls==[]

@pytest.mark.parametrize('missing',list(ready()))
def test_missing_prerequisite_blocks_even_an_approved_permit(store,missing):
 api=API();p=permit();c=Controller(store,api,approved_sha256=digest(p),deployment_sha=SHA,now=lambda:NOW);r=ready();r[missing]=False
 with pytest.raises(EvidenceError):c.dispatch(p,readiness=r)
 assert api.calls==[]


def test_lost_create_response_is_reconciled_without_duplicate_POST(store):
 p=permit();api=API(uncertain=True);c=Controller(store,api,approved_sha256=digest(p),deployment_sha=SHA,now=lambda:NOW)
 result=c.dispatch(p,readiness=ready());assert result['status']=='CREATE_UNCONFIRMED'
 with pytest.raises(EvidenceError):c.dispatch(p,readiness=ready())
 assert c.reconcile(digest(p))['job_id']==api.job['id']
 assert sum(m=='POST' and path.endswith('/jobs') for path,m,_ in api.calls)==1


def test_resource_failure_requests_cancel_but_does_not_claim_terminal(store):
 p=permit();api=API();c=Controller(store,api,approved_sha256=digest(p),deployment_sha=SHA,now=lambda:NOW)
 c.dispatch(p,readiness=ready())
 assert c.supervise_once(digest(p),observe=lambda:(_ for _ in ()).throw(ConnectionError()),limits=Limits())=='CANCEL_REQUESTED'
 assert api.job['status']=='running'
 api.job['status']='canceled'
 assert c.supervise_once(digest(p),observe=lambda:None,limits=Limits())=='canceled'


def test_dispatch_timeout_is_independent_of_base_service_changes(store):
 p=permit();api=API();c=Controller(store,api,approved_sha256=digest(p),deployment_sha=SHA,now=lambda:NOW)
 c.dispatch(p,readiness=ready());c.clock=lambda:NOW+timedelta(seconds=901)
 assert c.supervise_once(digest(p),observe=sample,limits=Limits())=='CANCEL_REQUESTED'


def test_email_submission_delivery_and_replay_are_distinct(store):
 ref=digest('owner@example.invalid');ident=queue_email(store,code='LOCAL_TEST',period_id='a'*64,recipient_ref=ref,now=NOW)
 assert deliver_email(store,ident,lambda *a,**kw:'email-1',now=NOW)=='ACCEPTED_NOT_DELIVERED'
 assert deliver_email(store,ident,lambda *a,**kw:pytest.fail('duplicate email'),now=NOW)=='ALREADY_SUBMITTED'
 secret=b'synthetic webhook key';raw=json.dumps({'type':'email.delivered','data':{'email_id':'email-1'}}).encode();ts=str(int(NOW.timestamp()));event='synthetic-event'
 signature=base64.b64encode(hmac.new(secret,event.encode()+b'.'+ts.encode()+b'.'+raw,hashlib.sha256).digest()).decode()
 headers={'svix-id':event,'svix-timestamp':ts,'svix-signature':'v1,'+signature}
 args=dict(signing_secret='whsec_'+base64.b64encode(secret).decode(),now=NOW)
 assert record_webhook(store,ident,raw,headers,**args)=='DELIVERED'
 assert record_webhook(store,ident,raw,headers,**args)=='DELIVERED'
 with store.engine.connect() as c:assert sum(r['status']=='DELIVERED' for r in records(c,'email:'+ident))==1
 with pytest.raises(EvidenceError):record_webhook(store,ident,raw+b' ',headers,**args)
 with pytest.raises(EvidenceError):record_webhook(store,ident,raw,headers,**{**args,'now':NOW+timedelta(seconds=301)})


def test_email_outage_is_durable_and_retry_keeps_idempotency(store):
 ident=queue_email(store,code='LOCAL_TEST',period_id='a'*64,recipient_ref=digest('owner@example.invalid'),now=NOW);keys=[]
 def fail(*a,**kw):keys.append(kw['idempotency_key']);raise TimeoutError()
 assert deliver_email(store,ident,fail,now=NOW)=='UNCONFIRMED'
 assert deliver_email(store,ident,fail,now=NOW+timedelta(seconds=61))=='UNCONFIRMED'
 assert len(set(keys))==1
 with pytest.raises(EvidenceError):deliver_email(store,ident,fail,now=NOW+timedelta(hours=24))


def test_email_default_transport_never_sends(monkeypatch):
 import httpx
 monkeypatch.setattr(httpx.Client,'post',lambda *a,**kw:pytest.fail('live mail'))
 with pytest.raises(EvidenceError):ResendTransport(api_key='synthetic',sender='sender@example.invalid',recipient='owner@example.invalid',recipient_ref=digest('owner@example.invalid'))({},idempotency_key='local')


def test_missed_run_monitor_survives_production_database_failure(store):
 now=datetime(2026,10,27,13,tzinfo=timezone.utc)
 state=lambda *a:(_ for _ in ()).throw(ConnectionError('synthetic production outage'))
 assert missed_run(store,period_start='2026-10-26',state=state,recipient_ref='a'*64,now=now)['status']=='PREPARED_NO_EXECUTION_EXPECTATION'
 assert missed_run(store,period_start='2026-10-26',state=state,recipient_ref='a'*64,now=now,expected=True)['status']=='OWNER_NOTIFICATION_QUEUED'


def signed_restore():
 from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
 from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
 key=Ed25519PrivateKey.generate();pub=base64.b64encode(key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)).decode()
 transport={'encrypted_sha256':'a'*64,'recipient':'synthetic-public-recipient','off_device_copy_verified':True,'inventory_sha256':'b'*64}
 payload={'schema':'fv-independent-restore-v1','request_id':'c'*64,'encrypted_sha256':'a'*64,'recipient':'synthetic-public-recipient','inventory_sha256':'b'*64,'restored_inventory_sha256':'b'*64,'tables':14,'all_tables_equal':True,'independent_key_recovery_verified':True,'verified_at':NOW.isoformat()}
 return key,pub,transport,payload


def test_backup_transport_alone_cannot_mean_recoverable():
 key,pub,transport,payload=signed_restore();signed={'receipt':payload,'signature':base64.b64encode(key.sign(canonical(payload).encode())).decode()}
 assert verified_recovery(transport,signed,verifier_public_key=pub,request_id='c'*64,now=NOW)['independent_restore_verified']
 with pytest.raises(EvidenceError):verified_recovery(transport,{},verifier_public_key=pub,request_id='c'*64,now=NOW)
 for field,value in [('encrypted_sha256','d'*64),('inventory_sha256','d'*64),('tables',13),('recipient','other'),('request_id','d'*64),('all_tables_equal',False)]:
  changed={**payload,field:value};bad={'receipt':changed,'signature':base64.b64encode(key.sign(canonical(changed).encode())).decode()}
  with pytest.raises(EvidenceError):verified_recovery(transport,bad,verifier_public_key=pub,request_id='c'*64,now=NOW)


def test_qualified_lifecycle_restarts_without_new_source_reservations(store):
 from app.forecast_validation.weekly import run_period
 from test_forecast_stage3 import production_source
 historical(store);capacity=Capacity(store)
 factory=lambda *a,**kw:QualifiedPeriod(*a,capacity=capacity,slot=0,**kw)
 calls=[]
 def collect(p,left):p.reserve_request('/v2/test','a'*64);calls.append(1);return production_source(NOW)
 kw=dict(collect_source=collect,pressure=lambda:True,backup=lambda *a:{'verified':True,'encrypted_sha256':'a'*64},now=NOW,evaluate_outcomes=False,scope_policy='local-capture-0',period_factory=factory)
 assert run_period(store,'2026-10-26',**kw)['status']=='COMPLETE'
 assert run_period(store,'2026-10-26',**kw)['idempotent']
 assert total(store)==312 and len(calls)==1

@pytest.mark.parametrize('principal_id,status_code',[(6,200),(1,403)])
def test_operational_status_is_owner_only_and_read_only(store,monkeypatch,principal_id,status_code):
 from fastapi import FastAPI
 from fastapi.testclient import TestClient
 from app.routers import forecast_validation as routes
 from types import SimpleNamespace
 app=FastAPI();app.include_router(routes.router)
 class DB:
  def rollback(self):pass
 app.dependency_overrides[routes.get_db]=lambda:DB()
 app.dependency_overrides[routes.get_current_principal]=lambda:SimpleNamespace(id=principal_id)
 monkeypatch.setattr(routes.settings,'forecast_validation_enabled',True)
 def owner(db,pid):
  if pid!=6:raise PermissionError()
 monkeypatch.setattr(routes,'authorize_purchasing_owner',owner)
 monkeypatch.setattr(routes,'evidence_store',lambda:store if principal_id==6 else pytest.fail('Non-Owner evidence read'))
 with TestClient(app) as client:r=client.get('/management/forecast-validation/operations')
 assert r.status_code==status_code
 if status_code==200:assert r.json()['execution_enabled'] is False and r.headers['cache-control']=='no-store'


def test_runtime_probe_is_disabled_without_public_key_and_has_no_database(monkeypatch):
 from app.routers import forecast_validation as routes
 from fastapi import FastAPI
 from fastapi.testclient import TestClient
 app=FastAPI();app.include_router(routes.router)
 monkeypatch.setattr(routes.settings,'forecast_validation_monitor_public_key','')
 monkeypatch.setattr(routes,'evidence_store',lambda:pytest.fail('No database permitted'))
 with TestClient(app) as client:assert client.get('/management/forecast-validation/runtime').status_code==404


def test_runtime_probe_signature_and_expiry():
 from app.forecast_validation.runtime_probe import authorize_probe
 from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
 from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
 key=Ed25519PrivateKey.generate();public=base64.b64encode(key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)).decode();ts=int(NOW.timestamp())
 signature=base64.b64encode(key.sign(canonical({'purpose':'fv-runtime-read-v1','timestamp':ts}).encode())).decode()
 authorize_probe(public,str(ts),signature,now=NOW)
 with pytest.raises(EvidenceError):authorize_probe(public,str(ts+1),signature,now=NOW)
 with pytest.raises(EvidenceError):authorize_probe(public,str(ts),signature,now=NOW+timedelta(seconds=31))


def test_minute_average_cannot_replace_fast_web_CPU_gate():
 def provider(name):return {'at':NOW.isoformat(),'value':400*1024**2 if 'memory' in name else .1,'unit':'bytes' if 'memory' in name else 'cores','window_seconds':60}
 with pytest.raises(EvidenceError,match='Fast web'):observe(provider,lambda:{},lambda:100,now=NOW)


def test_late_tuesday_dispatch_is_not_admitted():
 p=permit()
 with pytest.raises(EvidenceError,match='full bounded'):
  require_execution(p,approved_sha256=digest(p),deployment_sha=SHA,now=datetime(2026,10,27,12,46,tzinfo=timezone.utc))


def test_base_build_mismatch_blocks_paid_job_creation(store):
 api=API();original=api.__call__
 def stale(path,**kw):
  if '/deploys?' in path:return [{'deploy':{'status':'live','commit':{'id':'b'*40}}}]
  return original(path,**kw)
 p=permit();c=Controller(store,stale,approved_sha256=digest(p),deployment_sha=SHA,now=lambda:NOW)
 with pytest.raises(EvidenceError,match='build differs'):c.dispatch(p,readiness=ready())
 assert not any(method=='POST' for _,method,_ in api.calls)


def test_qualification_expired_origin_cannot_resume_completion(store):
 from test_forecast_stage3 import production_source
 historical(store);p=qp(store,0)
 with p.lock():p.claim();p.freeze(production_source(NOW))
 p.now=NOW+timedelta(seconds=901)
 with p.lock():
  with pytest.raises(EvidenceError,match='expired'):p.claim()
 assert total(store)==311


def test_backup_window_includes_transport_and_restoration_without_reset():
 from app.forecast_validation.execution import backup_window_deadline
 assert backup_window_deadline(100,now=300)==420
 assert backup_window_deadline(100,now=950)==1000
 with pytest.raises(TimeoutError):backup_window_deadline(100,now=1000)


def test_build_tool_rejects_unpinned_binary_and_symlink(tmp_path):
 import importlib.util
 from pathlib import Path
 spec=importlib.util.spec_from_file_location('fv_tools',Path(__file__).parents[1]/'scripts/release/prepare_forecast_tools.py')
 module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
 bad=tmp_path/'bad';bad.write_bytes(b'not the official binary')
 with pytest.raises(RuntimeError,match='binary differs'):module.install(tmp_path/'age',source=bad)
 assert not (tmp_path/'age').exists()
