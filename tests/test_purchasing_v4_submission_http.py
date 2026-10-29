"""Actual owner session/CSRF and artifact boundary over disposable PostgreSQL."""
from fastapi.testclient import TestClient
from uuid import uuid4
from sqlalchemy import text
import pytest
from app.security.passwords import hash_password
from app.purchasing_v4.submission_app import create_app
from test_purchasing_v4_submission import purchasing,sheet,ready,engine,template,pytestmark,review

@pytest.fixture
def api(purchasing):
 e,state=purchasing
 with e.begin() as c:c.execute(text('UPDATE principals SET password_hash=:p WHERE id=1'),{'p':hash_password('fixture-owner-password')})
 app=create_app(e.url.render_as_string(hide_password=False),source_namespace='synthetic-fixture-source',owner_principal_id=1)
 with TestClient(app,client=('127.0.0.1',50000)) as client:
  client.get('/login');client.post('/login',json=dict(username='v4-fixture',password='fixture-owner-password'),headers={'X-CSRF-Token':client.cookies['csrf_token']});yield client,purchasing
 app.state.worker_engine.dispose();app.state.engine.dispose()


def test_preparation_is_not_authorization_and_exact_commit_is_csrf_protected(api):
 c,p=api;state=p[1];headers={'X-CSRF-Token':c.cookies['csrf_token']};body=dict(review_id=str(uuid4()),session_id=state['session']['id'],expected_version=0,supplier_ids=[state['rows'][0]['supplier_id']])
 assert c.post('/api/submission-reviews',json=body).status_code==403
 initial=c.post('/api/submission-reviews',json=body,headers=headers);assert initial.status_code==200,initial.text
 assert initial.json()['commitments_created']==0 and initial.json()['blocking']
 r=review(p);cmd=dict(command_id=str(uuid4()),review_id=r['id'],acknowledged=True)
 assert c.post('/api/purchase-authorizations',json=cmd).status_code==403
 result=c.post('/api/purchase-authorizations',json=cmd,headers=headers);assert result.status_code==200,result.text
 assert c.post('/api/purchase-authorizations',json=cmd,headers=headers).json()['replayed']
 assert c.get('/api/authorization-commands/'+cmd['command_id']).json()['authorization_id']==result.json()['authorization_id']
 child=result.json()['children'][0];files=c.post('/api/vendor-authorizations/'+child['id']+'/documents',headers=headers);assert files.status_code==200,files.text
 for f in files.json():
  doc=c.get('/api/vendor-artifacts/'+f['id']);assert doc.status_code==200 and doc.headers['x-content-sha256']==f['sha256'];assert 'private' in doc.headers['cache-control']
 c.cookies.delete('v4_isolated_session');assert c.get('/api/vendor-artifacts/'+files.json()[0]['id']).status_code==401


def test_unreviewed_valuations_or_implicit_authorization_are_rejected(api):
 c,p=api;headers={'X-CSRF-Token':c.cookies['csrf_token']};r=review(p)
 body=dict(command_id=str(uuid4()),review_id=r['id'],acknowledged=False)
 assert c.post('/api/purchase-authorizations',json=body,headers=headers).status_code==422
 assert c.post('/api/purchase-authorizations',json=dict(body,acknowledged=True,valuation='0'),headers=headers).status_code==422
 assert c.post('/api/submission-reviews',json={},headers=headers).status_code==422
 for path in ['/inventory-post','/send-vendor-email','/api/vendor-responses']:
  assert c.post(path,headers=headers).status_code==404


def test_rollback_profile_stops_new_authorizations_but_preserves_original_documents(api):
 c,p=api;headers={'X-CSRF-Token':c.cookies['csrf_token']};r=review(p);result=c.post('/api/purchase-authorizations',json=dict(command_id=str(uuid4()),review_id=r['id'],acknowledged=True),headers=headers).json();child=result['children'][0]
 files=c.post('/api/vendor-authorizations/'+child['id']+'/documents',headers=headers).json();original=c.get('/api/vendor-artifacts/'+files[0]['id']).content
 c.app.state.authorization_enabled=False
 assert c.get('/api/context').json()['authorization_enabled'] is False
 assert c.post('/api/purchase-authorizations',json=dict(command_id=str(uuid4()),review_id=r['id'],acknowledged=True),headers=headers).status_code==503
 assert c.get('/api/vendor-artifacts/'+files[0]['id']).content==original
 assert len(c.get('/api/purchase-authorizations').json())==1
