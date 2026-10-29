from uuid import uuid4
import hashlib
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from app.security.passwords import hash_password
from app.purchasing_v4.quotation_app import create_app
from test_purchasing_v4_quotations import quote,purchasing,sheet,ready,engine,template,book,final,commit,pytestmark

@pytest.fixture
def api(quote):
 e=quote[0]
 with e.begin() as c:c.execute(text('UPDATE principals SET password_hash=:p WHERE id=1'),{'p':hash_password('fixture-owner-password')})
 app=create_app(e.url.render_as_string(hide_password=False),source_namespace='synthetic-fixture-source',owner_principal_id=1)
 with TestClient(app,client=('127.0.0.1',50000)) as client:
  client.get('/login');client.post('/login',json=dict(username='v4-fixture',password='fixture-owner-password'),headers={'X-CSRF-Token':client.cookies['csrf_token']});yield client,quote,app
 app.state.worker_engine.dispose();app.state.engine.dispose()


def test_manual_upload_is_csrf_protected_private_and_creates_no_commitment(api):
 c,quote,app=api;rid=quote[2];url='/api/quotation-requests/'+rid;content=book(quote);headers={'X-CSRF-Token':c.cookies['csrf_token']}
 assert c.post(url+'/uploads?filename=vendor.xlsx',content=content).status_code==403
 response=c.post(url+'/uploads?filename=vendor.xlsx',content=content,headers=headers);assert response.status_code==200,response.text
 uploaded=response.json();assert uploaded['validation']['valid'];data=c.get(url).json();assert data['head']['selected_response_id'] is None
 original=c.get(url+'/uploads/'+uploaded['id']);assert original.content==content;assert original.headers['x-content-sha256']==hashlib.sha256(content).hexdigest()
 assert c.post(url+'/uploads?filename=vendor.xlsx',content=content,headers=headers).json()['id']==uploaded['id']
 assert c.post(url+'/decisions',json={'kind':'CANCEL'},headers=headers).status_code==422
 for path in ['/api/receive','/api/send-vendor-email','/api/email-intake','/api/store-transfers']:assert c.post(path,headers=headers).status_code==404
 c.cookies.delete('v4_isolated_session');assert c.get(url).status_code==401


def test_authorization_disabled_retains_original_workbooks_and_quarantine_history(api):
 c,quote,app=api;url='/api/quotation-requests/'+quote[2];headers={'X-CSRF-Token':c.cookies['csrf_token']};data=c.get(url).json();workbook=c.get(url+'/workbooks/'+data['head']['revision_id']);assert workbook.status_code==200
 app.state.authorization_enabled=False
 assert c.post(url+'/uploads?filename=vendor.xlsx',content=book(quote),headers=headers).status_code==503
 assert c.post(url+'/reviews',json={},headers=headers).status_code==503
 assert c.post('/api/purchase-authorizations',json={},headers=headers).status_code==503
 assert c.get(url+'/workbooks/'+data['head']['revision_id']).content==workbook.content
 assert c.get(url).status_code==200
