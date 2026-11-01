from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from app.security.passwords import hash_password
from app.purchasing_v4.email_app import create_app
from test_purchasing_v4_email import email,quote,purchasing,sheet,ready,engine,template,reading,review,authorize,fresh,edit,save,pytestmark,reply,signed,SECRET

@pytest.fixture
def api(email):
 f,did,p=email;e=f[0]
 with e.begin() as c:c.execute(text('UPDATE public.principals SET password_hash=:p WHERE id=1'),{'p':hash_password('email-fixture-password')})
 app=create_app(e.url.render_as_string(hide_password=False),source_namespace='synthetic-fixture-source',owner_principal_id=1,provider=p,webhook_secret=SECRET)
 with TestClient(app,client=('127.0.0.1',50000)) as c:
  # Startup deliberately resumes authorized fixture mail asynchronously. Await
  # that work before asserting webhook imports or pausing a stable history.
  async def startup_complete():await app.state.email_resume_task
  c.portal.call(startup_complete)
  c.get('/login');assert c.post('/login',json=dict(username='v4-fixture',password='email-fixture-password'),headers={'X-CSRF-Token':c.cookies['csrf_token']}).status_code==200
  yield c,email,app
 app.state.mail_engine.dispose();app.state.worker_engine.dispose();app.state.engine.dispose()


def test_authenticated_webhook_imports_and_duplicate_returns_same_event(api):
 c,email,app=api;event,pid,aid,raw,headers=reply(email)
 assert c.post('/api/purchasing-email/resend/events',content=raw).status_code==401
 r=c.post('/api/purchasing-email/resend/events',content=raw,headers=headers);assert r.status_code==200,r.text;assert r.json()['event_id']==event
 assert c.post('/api/purchasing-email/resend/events',content=raw,headers=headers).json()==r.json()
 data=c.get('/api/purchasing-email').json();assert len(data['imports'])==1;assert data['imports'][0]['result']['outcome']=='OWNER_REVIEW_REQUIRED'
 assert c.get('/api/purchasing-email/messages/'+pid+'/original').status_code==200
 assert c.get('/api/purchasing-email/attachments/'+data['imports'][0]['attachment_id']).status_code==200


def test_email_pause_preserves_history_and_manual_workflow(api):
 c,email,app=api;before=c.get('/api/purchasing-email').json();app.state.email_writes_enabled=False;did=email[1];headers={'X-CSRF-Token':c.cookies['csrf_token']}
 assert c.post('/api/purchasing-email/deliveries/'+did+'/retry',json=dict(reason='Owner explicit recovery',interrupted_worker_confirmed=False),headers=headers).status_code==503
 assert c.post('/api/purchasing-email/resend/events',content=b'{}').status_code==503
 assert c.get('/api/purchasing-email').json()['deliveries']==before['deliveries']
 assert c.get('/api/quotation-requests/'+email[0][2]).status_code==200
 assert c.get('/api/context').json()['email_enabled'] is True


def test_recovery_requires_csrf_and_owner_authority(api):
 c,email,app=api;did=email[1];body=dict(reason='Owner reviewed original attempt',interrupted_worker_confirmed=False)
 assert c.post('/api/purchasing-email/deliveries/'+did+'/retry',json=body).status_code==403
 with email[0][0].begin() as db:
  from app.purchasing_v4.receiving import grant
  db.execute(text("INSERT INTO public.principals(id,username,password_hash,role,active) VALUES (2,'mail-receiver',:p,'MANAGER',true)"),{'p':hash_password('employee-fixture-password')});grant(db,actor_id=1,command_id=str(uuid4()),principal_id=2,kind='VIEW',enabled=True,reason='Explicit receiving-only fixture',store_id=2)
 c.cookies.delete('v4_isolated_session');r=c.post('/login',json=dict(username='mail-receiver',password='employee-fixture-password'),headers={'X-CSRF-Token':c.cookies['csrf_token']});assert r.status_code==200
 assert c.get('/api/purchasing-email').status_code==403
 assert c.post('/api/purchasing-email/deliveries/'+did+'/retry',json=body,headers={'X-CSRF-Token':c.cookies['csrf_token']}).status_code==403

def test_manual_handoff_remains_available_when_email_paused_and_prevents_later_send(api):
 c,email,app=api;did=email[1];app.state.email_writes_enabled=False;headers={'X-CSRF-Token':c.cookies['csrf_token']};body=dict(command_id=str(uuid4()),reason='Owner reports controlled manual delivery and holds future automatic dispatch',interrupted_worker_confirmed=False)
 response=c.post('/api/purchasing-email/deliveries/'+did+'/manual',json=body,headers=headers);assert response.status_code==200,response.text;assert c.post('/api/purchasing-email/deliveries/'+did+'/manual',json=body,headers=headers).json()['replayed']
 from app.purchasing_v4 import email_worker as w
 assert w.send(email[0][0],did,email[2])['state']=='HELD'
 with email[0][0].connect() as db:assert db.execute(text('SELECT count(*) FROM purchasing_v4.mail_resolution WHERE id=:id'),{'id':body['command_id']}).scalar_one()==1
