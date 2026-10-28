from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from app.security.passwords import hash_password
from app.purchasing_v4.receiving_app import create_app
from app.purchasing_v4.receiving_square import InventoryAdapter
from app.purchasing_v4.receiving_simulator import install,FixtureInventoryTransport
from test_purchasing_v4_receiving import receiving,purchasing,sheet,ready,engine,template,pytestmark

@pytest.fixture
def api(receiving):
 e,po,_=receiving
 with e.begin() as c:c.execute(text('UPDATE principals SET password_hash=:p WHERE id=1'),{'p':hash_password('fixture-owner-password')})
 install(e);app=create_app(e.url.render_as_string(hide_password=False),source_namespace='synthetic-fixture-source',owner_principal_id=1,inventory_adapter=InventoryAdapter(FixtureInventoryTransport(e)))
 with TestClient(app,client=('127.0.0.1',50000)) as client:
  client.get('/login');client.post('/login',json=dict(username='v4-fixture',password='fixture-owner-password'),headers={'X-CSRF-Token':client.cookies['csrf_token']});yield client,receiving,app
 app.state.worker_engine.dispose();app.state.engine.dispose()


def test_receive_existing_po_is_one_confirmation_and_background_split(api):
 c,(e,po,_),app=api;url='/api/receiving/orders/'+str(po);h={'X-CSRF-Token':c.cookies['csrf_token']};p=c.post(url+'/open',json={},headers=h).json();assert p['snapshot']['central']['name']=='HWY 99'
 l=p['lines'][0];body=dict(command_id=str(uuid4()),expected_version=p['version'],kind='RECEIPT',lines=[dict(line_id=l['id'],good=l['supplier_outstanding'],damaged=0)],reason='Verified synthetic receipt and existing split',confirmed=True)
 assert c.post(url+'/commands',json=body).status_code==403
 response=c.post(url+'/commands',json=body,headers=h);assert response.status_code==200,response.text
 after=c.get(url).json();assert after['lines'][0]['posting_complete'];assert all(x['state']=='VERIFIED' for x in after['lines'][0]['posting'])
 assert c.post(url+'/commands',json=body,headers=h).json()['replayed']
 with e.connect() as db:assert db.execute(text('SELECT count(*) FROM purchasing_v4_simulator.adjustment')).scalar_one()==len(after['lines'][0]['posting'])
 for path in ['/api/dispatch','/api/destination-arrival','/api/receiving-location']:
  assert c.post(path,json={},headers=h).status_code==404


def test_disabled_receiving_retains_history_and_blocks_mutations(api):
 c,(e,po,_),app=api;url='/api/receiving/orders/'+str(po);h={'X-CSRF-Token':c.cookies['csrf_token']};before=c.get(url).json();app.state.receiving_writes_enabled=False
 assert c.post(url+'/commands',json={},headers=h).status_code==503
 assert c.post(url+'/inventory',json={},headers=h).status_code==503
 assert c.post(url+'/draft',json={},headers=h).status_code==503
 assert c.get(url).json()==before
 assert c.get('/receiving').status_code==200


def test_employee_authentication_does_not_grant_owner_purchasing_access(api):
 from app.purchasing_v4.receiving import grant
 c,(e,po,_),app=api
 with e.begin() as db:
  db.execute(text("INSERT INTO principals(id,username,password_hash,role,active) VALUES (2,'receiver',:password,'MANAGER',true)"),{'password':hash_password('fixture-employee-password')})
  for kind in ['VIEW','ENTER','CONFIRM']:grant(db,actor_id=1,command_id=str(uuid4()),principal_id=2,kind=kind,enabled=True,reason='Explicit isolated delegation',store_id=2)
 c.cookies.delete('v4_isolated_session');login=c.post('/login',json=dict(username='receiver',password='fixture-employee-password'),headers={'X-CSRF-Token':c.cookies['csrf_token']});assert login.status_code==200,login.text;assert login.json()['next']=='/receiving'
 assert c.get('/receiving').status_code==200
 assert c.get('/api/receiving/orders/'+str(po)).status_code==200
 assert c.get('/api/purchase-authorizations').status_code==403
 assert c.post('/api/purchase-authorizations',json={},headers={'X-CSRF-Token':c.cookies['csrf_token']}).status_code==403


def test_receiving_view_permission_does_not_expose_owner_overage_valuation(api):
 from app.purchasing_v4.receiving import grant
 c,(e,po,_),app=api;url='/api/receiving/orders/'+str(po);h={'X-CSRF-Token':c.cookies['csrf_token']};p=c.get(url).json();l=p['lines'][0];stores={k:0 for k in l['ordered']};stores['1']=1
 response=c.post(url+'/commands',json=dict(command_id=str(uuid4()),expected_version=p['version'],kind='OVERAGE',lines=[dict(line_id=l['id'],stores=stores,currency='USD',approved_total='12.34',valuation_reason='Private owner-only overage valuation evidence')],reason='Owner financial overage review',confirmed=True),headers=h);assert response.status_code==200,response.text
 assert 'approved_total' in c.get(url).text
 with e.begin() as db:
  db.execute(text("INSERT INTO principals(id,username,password_hash,role,active) VALUES (2,'receiving-viewer',:password,'MANAGER',true)"),{'password':hash_password('fixture-viewer-password')})
  grant(db,actor_id=1,command_id=str(uuid4()),principal_id=2,kind='VIEW',enabled=True,reason='Explicit view-only receiving permission',store_id=2)
 c.cookies.delete('v4_isolated_session');assert c.post('/login',json=dict(username='receiving-viewer',password='fixture-viewer-password'),headers=h).status_code==200
 response=c.get(url);assert response.status_code==200;assert 'approved_total' not in response.text and '12.34' not in response.text and 'Private owner-only' not in response.text
 assert c.post(url+'/commands',json={},headers=h).status_code==422
