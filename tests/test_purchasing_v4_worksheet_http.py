import pytest
from uuid import uuid4
from fastapi.testclient import TestClient
from sqlalchemy import text
from app.security.passwords import hash_password
from app.purchasing_v4.worksheet_app import create_app
from test_purchasing_v4_worksheets import sheet,ready,engine,template,pytestmark,edit

@pytest.fixture
def client(sheet):
 engine,state=sheet
 with engine.begin() as c:c.execute(text('UPDATE principals SET password_hash=:hash WHERE id=1'),{'hash':hash_password('fixture-owner-password')})
 app=create_app(engine.url.render_as_string(hide_password=False),source_namespace='synthetic-fixture-source')
 with TestClient(app,client=('127.0.0.1',50000)) as client:
  client.get('/login');client.post('/login',json=dict(username='v4-fixture',password='fixture-owner-password'),headers={'X-CSRF-Token':client.cookies['csrf_token']});yield client,state
 app.state.engine.dispose()

def test_grid_is_authenticated_desktop_and_cannot_commit(client):
 c,state=client
 assert c.get('/').status_code==200 and 'worksheet-assets' in c.get('/').text
 for path in ['/submit-po','/authorize','/api/worksheets/'+state['session']['id']+'/submit','/inventory-post','/send-vendor-email']:assert c.post(path).status_code==404
 c.cookies.delete('v4_isolated_session');assert c.get('/api/worksheets').status_code==401

def test_patch_csrf_replay_and_selected_submission_is_preview_only(client):
 c,state=client;sid=state['session']['id'];headers={'X-CSRF-Token':c.cookies['csrf_token']};body=dict(command_id=str(uuid4()),expected_version=0,changes=edit(state))
 assert c.post('/api/worksheets/'+sid+'/commands',json=body).status_code==403
 result=c.post('/api/worksheets/'+sid+'/commands',json=body,headers=headers);assert result.status_code==200,result.text
 assert c.post('/api/worksheets/'+sid+'/commands',json=body,headers=headers).json()['replayed']
 preview=c.post('/api/worksheets/'+sid+'/submission-preview',json=dict(expected_version=1,supplier_ids=[state['rows'][0]['supplier_id']]),headers=headers).json()
 assert preview['submitted'] is False and preview['authorized'] is False and preview['commitments_created']==0 and len(preview['vendors'])==1

def test_draft_create_reopen_and_preference_cas(client):
 c,state=client;headers={'X-CSRF-Token':c.cookies['csrf_token']};body=dict(command_id=str(uuid4()),title='Owner manual draft',calculate=False)
 first=c.post('/api/worksheets',json=body,headers=headers);assert first.status_code==200,first.text
 assert c.post('/api/worksheets',json=body,headers=headers).json()['session']['id']==body['command_id']
 assert c.get('/api/worksheets/'+body['command_id']).json()['rows']==[]
 pref=dict(expected_version=0,value=dict(showCosts=False,widths={'name':300}))
 assert c.post('/api/worksheet-preferences',json=pref,headers=headers).json()['version']==1
 assert c.post('/api/worksheet-preferences',json=pref,headers=headers).status_code==409
 assert c.get('/api/worksheet-preferences').json()['value']['showCosts'] is False


def test_fresh_lifecycle_identity_rejects_different_content_and_add_replays(client):
 c,state=client;headers={'X-CSRF-Token':c.cookies['csrf_token']};cid=str(uuid4());body=dict(command_id=cid,title='Pinned creation request',calculate=False)
 assert c.post('/api/worksheets',json=body,headers=headers).status_code==200
 assert c.post('/api/worksheets',json=dict(body,title='Different request under same ID'),headers=headers).status_code==409
 # Add a verified variation to the empty manual draft. Retry uses the same pinned row identity.
 added=dict(command_id=str(uuid4()),expected_version=0,variation_id=state['rows'][0]['variation_id'])
 first=c.post('/api/worksheets/'+cid+'/add',json=added,headers=headers);assert first.status_code==200,first.text
 assert c.post('/api/worksheets/'+cid+'/add',json=added,headers=headers).json()['replayed']
 assert first.json()['added'][0]['packs']==[0,0,0,0]


def test_drafts_are_newest_first_and_all_pages_are_reachable(client):
 from app.purchasing_v4 import worksheets
 c,state=client;db=c.app.state.engine;created=[]
 with db.begin() as connection:
  for i in range(121):created.append(worksheets.create(connection,actor_id=1,command_id=str(uuid4()),title='Paged fixture draft '+str(i),rows=[])['session']['id'])
  connection.execute(text("UPDATE purchasing_v4.worksheet SET updated_at='2030-01-01T00:00Z' WHERE id=:id"),{'id':created[-1]})
 first=c.get('/api/worksheets').json();assert first['rows'][0]['id']==created[-1] and len(first['rows'])==100
 second=c.get('/api/worksheets',params={'after':first['next_cursor']}).json()
 assert len(second['rows'])==22 and second['next_cursor'] is None
 assert not ({r['id'] for r in first['rows']}&{r['id'] for r in second['rows']})


def test_inventory_get_is_readonly_and_age_is_explicit(client):
 c,state=client;url='/api/worksheets/'+state['session']['id']+'/inventory'
 result=c.get(url);assert result.status_code==200,result.text
 assert result.json()['unit']=='individual_inventory_units'
 assert all(e['value'] is None for values in result.json()['rows'].values() for e in values)
 assert c.get(url+'?max_age_seconds=-1').status_code==422
 assert c.post(url).status_code==405
 assert c.get('/api/worksheets/'+str(uuid4())+'/inventory').status_code==422
