"""Real shared-session V3/V4 HTTP server; all external effects disabled in its process."""
from pathlib import Path
from uuid import uuid4
from urllib.parse import unquote
import os,sys,subprocess,time,socket
import pytest,httpx
from sqlalchemy import text
from app.purchasing_v4.schema import migrate_integration
from app.purchasing_v4 import receiving as r
from app.security.passwords import hash_password
from test_purchasing_v4_email import email,quote
from test_purchasing_v4_receiving import receiving,purchasing,sheet,ready,engine,template,pytestmark

@pytest.fixture
def integrated(receiving,tmp_path):
 e,po,_=receiving
 with e.begin() as c:
  migrate_integration(c);pw=hash_password('fixture-integrated-password');c.execute(text('UPDATE public.principals SET password_hash=:p WHERE id=1'),{'p':pw})
  c.execute(text("INSERT INTO public.principals(id,username,password_hash,role,active) VALUES (2,'delegated-receiver',:pw,'MANAGER',true),(3,'other-admin',:pw,'ADMIN',true),(4,'inactive-admin',:pw,'ADMIN',false)"),{'pw':pw})
  for kind in ['VIEW','ENTER','CONFIRM']:r.grant(c,actor_id=1,command_id=str(uuid4()),principal_id=2,kind=kind,enabled=True,reason='Explicit isolated receiving delegation',store_id=2)
 with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
 env={k:v for k,v in os.environ.items() if k in ['PATH','HOME','TMPDIR','LANG']};env.update(PYTHONPATH=str(Path.cwd()),ENVIRONMENT='test',PURCHASING_V3_PHASE1_ENABLED='true',PURCHASING_V3_PHASE2_ENABLED='true')
 args=[sys.executable,'-m','app.purchasing_v4.integration_app','--isolated-url',e.url.render_as_string(hide_password=False),'--source-namespace','synthetic-fixture-source','--owner-id','1','--port',str(port),'--enable-workspace','--enable-receiving']
 log=(tmp_path/'integration.log').open('w');p=subprocess.Popen(args,env=env,stdout=log,stderr=subprocess.STDOUT)
 try:
  for _ in range(200):
   assert p.poll() is None,(tmp_path/'integration.log').read_text()
   with socket.socket() as s:s.settimeout(.1);listening=s.connect_ex(('127.0.0.1',port))==0
   if listening:break
   time.sleep(.05)
  assert listening
  with httpx.Client(base_url='http://127.0.0.1:'+str(port),follow_redirects=False,timeout=20) as client:yield client,receiving
 finally:
  p.terminate()
  try:p.wait(timeout=15)
  except subprocess.TimeoutExpired:p.kill();p.wait(timeout=10)
  log.close()


def login(client,user='v4-fixture'):
 client.cookies.clear();assert client.get('/login').status_code==200
 return client.post('/login',data=dict(username=user,password='fixture-integrated-password'),headers=csrf(client))


def csrf(client):return {'X-CSRF-Token':unquote(client.cookies['csrf_token'])}


def test_shared_legacy_session_opens_v4_and_preserves_legacy_routes(integrated):
 c,(e,po,_)=integrated;response=login(c);assert response.status_code==303 and response.headers['location']=='/purchasing-v4'
 assert 'blind_inventory_session' in c.cookies and 'v4_isolated_session' not in c.cookies
 for path in ['/purchasing-v4','/api/context','/purchasing-v4/policies','/purchasing-v4/readiness','/management/ordering-tool','/api/receiving/context']:
  response=c.get(path);assert response.status_code==200,(path,response.text[:500])
 assert c.get('/api/context').json()['generation_enabled'] is False
 legacy=c.get('/management/ordering-tool');assert '/management/ordering-tool/orders/'+str(po)+'\"' not in legacy.text
 assert '/management/ordering-tool/orders/129' in legacy.text
 assert c.get('/api/purchasing-v4/evidence-status').status_code==200
 assert c.get('/').headers['location']=='/purchasing-v4'
 assert c.post('/api/worksheets',json=dict(command_id=str(uuid4()),calculate=False)).status_code==403
 assert c.post('/api/worksheets',json=dict(command_id=str(uuid4()),calculate=False),headers=csrf(c)).status_code==200
 assert c.post('/api/worksheets',json=dict(command_id=str(uuid4()),calculate=True,max_age_seconds=0),headers=csrf(c)).status_code==503
 assert c.post('/api/commands',json={},headers=csrf(c)).status_code==503


def test_delegated_receiver_and_other_admin_cannot_gain_owner_authority(integrated):
 c,(e,po,_)=integrated
 for user in ['delegated-receiver','other-admin']:
  assert login(c,user).status_code==303
  for path in ['/purchasing-v4','/api/context','/api/manual','/api/purchase-authorizations','/api/quotation-requests','/api/purchasing-email','/purchasing-v4/readiness','/api/purchasing-v4/reconciliation','/api/purchasing-v4/evidence-status']:
   assert c.get(path).status_code==403,(user,path)
  assert c.post('/api/purchase-authorizations',json={},headers=csrf(c)).status_code==403
 assert login(c,'delegated-receiver').status_code==303
 assert c.get('/receiving').status_code==200
 assert c.get('/api/receiving/orders/'+str(po)).status_code==200
 assert c.post('/api/receiving/capabilities',json=dict(command_id=str(uuid4()),principal_id=2,kind='DISCREPANCY',enabled=True,reason='Attempted self escalation',store_id=2),headers=csrf(c)).status_code==403
 assert login(c,'other-admin').status_code==303
 assert c.get('/api/receiving/orders/'+str(po)).status_code==403


def test_signed_webhook_entry_is_not_replaced_by_login_redirect(integrated):
 c,_=integrated
 assert c.get('/api/context').status_code==303
 assert c.post('/api/purchasing-email/resend/events',content=b'{}').status_code==503
 assert c.get('/api/context').status_code==303


def test_inventory_pause_keeps_receipt_pending_and_retry_cannot_duplicate_receipt(integrated):
 c,(e,po,_)=integrated;assert login(c).status_code==303;url='/api/receiving/orders/'+str(po);state=c.get(url).json();line=state['lines'][0]
 payload=dict(command_id=str(uuid4()),expected_version=state['version'],kind='RECEIPT',lines=[dict(line_id=line['id'],good=line['supplier_outstanding'],damaged=0)],reason='Physical receipt verified; posting paused in isolated rollback test',confirmed=True)
 response=c.post(url+'/commands',json=payload,headers=csrf(c));assert response.status_code==200,response.text
 assert c.post(url+'/commands',json=payload,headers=csrf(c)).json()['replayed']
 state=c.get(url).json();assert not state['lines'][0]['posting_complete'];assert any(p['state']=='PENDING' for p in state['lines'][0]['posting'])
 assert c.post(url+'/inventory',json={},headers=csrf(c)).status_code==503
 with e.connect() as db:assert db.execute(text("SELECT count(*) FROM purchasing_v4.receiving_event WHERE purchase_order_id=:po AND kind='RECEIPT'"),{'po':po}).scalar_one()==1


def test_legacy_edit_and_receiving_endpoints_cannot_take_over_v4_po(integrated):
 c,(e,po,_)=integrated;assert login(c).status_code==303
 with e.connect() as db:before=list(db.execute(text('SELECT id,ordered_qty,received_qty_total FROM public.purchase_order_lines WHERE purchase_order_id=:po ORDER BY id'),{'po':po}));receipts=db.execute(text('SELECT count(*) FROM public.purchase_order_receipts WHERE purchase_order_id=:po'),{'po':po}).scalar_one()
 for suffix in ['/save','/submit','/received-quantities','/receive','/invoice','/add-line','/refresh-lines','/delete']:
  response=c.post('/management/ordering-tool/orders/'+str(po)+suffix,data=dict(request_key=str(uuid4()),variation_id='FIXTURE_V1',initial_qty='1',unit_cost='2.00'),headers=csrf(c))
  if response.status_code==303:assert 'error=' in response.headers['location'] and 'V4' in response.headers['location'],(suffix,response.headers['location'])
  else:assert response.status_code in (400,403,422),(suffix,response.text[:400])
 with e.connect() as db:
  assert before==list(db.execute(text('SELECT id,ordered_qty,received_qty_total FROM public.purchase_order_lines WHERE purchase_order_id=:po ORDER BY id'),{'po':po}))
  assert receipts==db.execute(text('SELECT count(*) FROM public.purchase_order_receipts WHERE purchase_order_id=:po'),{'po':po}).scalar_one()


def test_inactive_account_cannot_enter_integrated_purchasing(integrated):
 c,_=integrated;response=login(c,'inactive-admin');assert response.status_code!=303
 assert 'blind_inventory_session' not in c.cookies
 assert c.get('/api/context').status_code==303


def test_open_partial_legacy_order_keeps_original_receiving_route_and_writer(integrated):
 c,(e,po,_)=integrated;assert login(c).status_code==303
 response=c.get('/management/purchasing/orders/129/receiving');assert response.status_code==200,response.text[:500]
 assert c.post('/api/receiving/orders/129/open',json={},headers=csrf(c)).status_code in (403,422)
 with e.connect() as db:
  assert db.execute(text('SELECT writer FROM purchasing_v4.po_ownership WHERE purchase_order_id=129')).scalar_one()=='LEGACY'
  assert db.execute(text('SELECT received_qty_total FROM public.purchase_order_lines WHERE id=1')).scalar_one()==40
  assert db.execute(text('SELECT count(*) FROM purchasing_v4.receiving_order WHERE purchase_order_id=129')).scalar_one()==0

@pytest.fixture
def integrated_mail(email,tmp_path):
 e=email[0][0]
 from app.purchasing_v4.email_simulator import install
 with e.begin() as c:migrate_integration(c);c.execute(text('UPDATE public.principals SET password_hash=:pw WHERE id=1'),{'pw':hash_password('fixture-integrated-password')})
 install(e)
 with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
 env={k:v for k,v in os.environ.items() if k in ['PATH','HOME','TMPDIR','LANG']};env.update(PYTHONPATH=str(Path.cwd()),ENVIRONMENT='test',PURCHASING_V3_PHASE1_ENABLED='true',PURCHASING_V3_PHASE2_ENABLED='true')
 args=[sys.executable,'-m','app.purchasing_v4.integration_app','--isolated-url',e.url.render_as_string(hide_password=False),'--source-namespace','synthetic-fixture-source','--owner-id','1','--port',str(port),'--enable-workspace','--enable-authorization','--enable-email','--simulate-email']
 log=(tmp_path/'mail-integration.log').open('w');p=subprocess.Popen(args,env=env,stdout=log,stderr=subprocess.STDOUT)
 try:
  for _ in range(200):
   assert p.poll() is None,(tmp_path/'mail-integration.log').read_text()
   with socket.socket() as s:s.settimeout(.1);listening=s.connect_ex(('127.0.0.1',port))==0
   if listening:break
   time.sleep(.05)
  assert listening
  with httpx.Client(base_url='http://127.0.0.1:'+str(port),follow_redirects=False,timeout=20) as client:yield client,email
 finally:
  p.terminate()
  try:p.wait(timeout=15)
  except subprocess.TimeoutExpired:p.kill();p.wait(timeout=10)
  log.close()


def test_reissued_email_dispatch_and_anonymous_vendor_reply_use_shared_owner_review(integrated_mail):
 import base64,hmac,hashlib
 from test_purchasing_v4_email import reply
 from app.purchasing_v4 import quotations as q
 c,email_fixture=integrated_mail;f,old_did,provider=email_fixture;e=f[0];assert login(c).status_code==303;request=f[2]
 current=c.get('/api/quotation-requests/'+request).json()
 with e.connect() as db:snapshot=q.revision(db,current['head']['revision_id'])['snapshot']
 changes={line['row_id']:dict(pack_size=line['pack_size'],packs=line['packs']) for line in snapshot['lines']}
 response=c.post('/api/quotation-requests/'+request+'/decisions',headers=csrf(c),json=dict(command_id=str(uuid4()),expected_version=current['head']['version'],kind='REISSUE',changes=changes,reason='Owner explicitly reissues the retained request in integrated dispatch qualification'))
 assert response.status_code==200,response.text
 current=c.get('/api/quotation-requests/'+request).json();new_head=current['head'];delivery=None
 for _ in range(150):
  deliveries=c.get('/api/purchasing-email').json()['deliveries'];delivery=next((d for d in deliveries if d['revision_id']==new_head['revision_id']),None)
  if delivery and delivery['state']=='ACCEPTED':break
  time.sleep(.05)
 assert delivery and delivery['state']=='ACCEPTED';assert delivery['id']!=old_did
 new_f=(e,f[1],request,new_head);event,pid,aid,raw,_=reply((new_f,delivery['id'],provider));stamp=str(int(time.time()));key=b'isolated-fixture-webhook-key-only';signature=base64.b64encode(hmac.new(key,event.encode()+b'.'+stamp.encode()+b'.'+raw,hashlib.sha256).digest()).decode();headers={'svix-id':event,'svix-timestamp':stamp,'svix-signature':'v1,'+signature}
 with e.connect() as db:before=db.execute(text('SELECT count(*) FROM public.purchase_orders')).scalar_one()
 with httpx.Client(base_url=str(c.base_url),follow_redirects=False,timeout=20) as anonymous:
  assert anonymous.post('/api/purchasing-email/resend/events',content=raw).status_code==401
  assert anonymous.post('/api/purchasing-email/resend/events',content=raw,headers=headers).status_code==200
 for _ in range(150):
  result=c.get('/api/quotation-requests/'+request).json()
  if result['responses']:break
  time.sleep(.05)
 assert len(result['responses'])==1 and result['head']['selected_response_id'] is None
 with e.connect() as db:assert db.execute(text('SELECT count(*) FROM public.purchase_orders')).scalar_one()==before
 assert c.get('/api/purchasing-email/messages/'+pid+'/original').status_code==200
