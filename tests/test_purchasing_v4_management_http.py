"""Existing password/session validation, owner restriction and CSRF at isolated HTTP boundary."""
import json
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from app.security.passwords import hash_password
from app.purchasing_v4.management_app import create_app
from test_purchasing_v4_controls import template,engine,ready,pytestmark,global_policy,change

@pytest.fixture
def client(ready):
    engine,vid,sid=ready
    with engine.begin() as c:
        c.execute(text('UPDATE principals SET password_hash=:hash WHERE id=1'),{'hash':hash_password('fixture-owner-password')})
        c.execute(text("INSERT INTO principals(id,username,password_hash,role,active) VALUES (2,'fixture-manager',:hash,'MANAGER',true)"),{'hash':hash_password('fixture-owner-password')})
    app=create_app(engine.url.render_as_string(hide_password=False),source_namespace='synthetic-fixture-source')
    with TestClient(app,client=('127.0.0.1',50000)) as c:
        c.get('/login');yield c,vid,sid
    app.state.engine.dispose()


def signin(c):
    return c.post('/login',json=dict(username='v4-fixture',password='fixture-owner-password'),headers={'X-CSRF-Token':c.cookies['csrf_token']})


def test_login_owner_and_csrf_guards(client):
    c,vid,sid=client
    assert c.get('/api/context').status_code==401
    assert c.post('/login',json=dict(username='v4-fixture',password='fixture-owner-password')).status_code==403
    assert c.post('/login',json=dict(username='fixture-manager',password='fixture-owner-password'),headers={'X-CSRF-Token':c.cookies['csrf_token']}).status_code==403
    assert signin(c).status_code==200
    assert c.get('/api/context').json()['recommendations_active'] is False
    body=dict(command_id=str(uuid4()),reason='Owner explicitly reviews synthetic settings',changes=[change('GLOBAL','global',global_policy())])
    assert c.post('/api/commands',json=body).status_code==403
    response=c.post('/api/commands',json=body,headers={'X-CSRF-Token':c.cookies['csrf_token']});assert response.status_code==200,response.text
    assert c.post('/api/commands',json=body,headers={'X-CSRF-Token':c.cookies['csrf_token']}).json()['replayed'] is True
    changed=dict(body,command_id=str(uuid4()))
    assert c.post('/api/commands',json=changed,headers={'X-CSRF-Token':c.cookies['csrf_token']}).status_code==409


def test_no_client_actor_or_conversion_bypass(client):
    c,vid,sid=client;assert signin(c).status_code==200
    value=dict(variation_id=vid,store_id=1,state='AUTOMATIC',par_units=5,level_units=20)
    response=c.post('/api/commands',json=dict(actor_id=999,conversion=True,command_id=str(uuid4()),reason='Forged conversion flag',changes=[change('MANUAL',vid+':1',value)]),headers={'X-CSRF-Token':c.cookies['csrf_token']})
    assert response.status_code==422
    assert c.post('/api/use-automatic',json=dict(command_id=str(uuid4()),reason='Empty selection',check_id=str(uuid4()),selected_keys=[]),headers={'X-CSRF-Token':c.cookies['csrf_token']}).status_code==422


def test_bounded_search_and_distinct_management_pages(client):
    c,vid,sid=client;signin(c)
    home=c.get('/');assert 'Manual Par/Levels' in home.text and 'purchasing worksheet' not in home.text
    result=c.get('/api/catalog?search=Fixture&limit=1');assert result.status_code==200,result.text
    assert len(result.json()['rows'])==1 and result.json()['next_cursor']
    assert c.get('/api/catalog?limit=10000').status_code==422
    assert c.get('/api/manual').status_code==200
    assert c.get('/api/legacy-manual').status_code==200
    assert c.get('/api/suppliers').status_code==200
    suggestion=c.get('/api/supplier-suggestions');assert suggestion.status_code==200,suggestion.text
    assert suggestion.json()['rows'] and 'source_config_id' in suggestion.json()['rows'][0]
    assert c.get('/api/reviews').status_code==200
    assert c.get('/api/audit').status_code==200
    for path in ('/submit-po','/inventory-post','/activate','/send-vendor-email'):
        assert c.post(path).status_code==404


def test_isolated_factory_rejects_production_urls():
    with pytest.raises(PermissionError):create_app('postgresql://postgres@production.invalid/production',source_namespace='x')
