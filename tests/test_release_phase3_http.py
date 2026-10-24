"""Actual app routes and disposable PostgreSQL; no production HTTP/credentials."""
from contextlib import ExitStack
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from app.config import settings
from app import models as m
from app.security.passwords import hash_password
from scripts.release.phase3.verification import acceptance
from test_v2_primary import primary, PASSWORD, post
from test_v2_scheduling_foundation import scheduling_db


def test_readonly_acceptance_and_owner_readiness(primary, monkeypatch):
    from app.routers import deployment_verification as routes
    from app.schema_contract import HEAD_REVISION
    monkeypatch.setattr(settings,'purchasing_v3_phase2_enabled',True)
    owner=primary.account('ADMIN',linked=False)
    monkeypatch.setattr(settings,'purchasing_owner_principal_id',owner)
    with primary.Session() as db:
        principals={'owner':owner}
        for role in ('LEAD','STORE'):
            p=m.Principal(username=role.lower()+'@release.test',role=m.PrincipalRole(role),active=True,
                store_id=primary.ids['north'] if role=='STORE' else None,password_hash=hash_password(PASSWORD))
            db.add(p);db.flush();principals[role.lower()]=p.id
        db.commit()
    identity={'status':'VERIFIED','revision':'a'*40,'source_tree_sha256':'c'*64}
    monkeypatch.setattr(routes,'capture_identity',lambda:identity)
    monkeypatch.setattr(primary.app.state,'runtime_identity',identity)
    with ExitStack() as stack:
        clients={role:stack.enter_context(TestClient(primary.app,follow_redirects=False,client=('127.0.0.1',51000+n))) for n,role in enumerate(('anonymous','owner','lead','store'))}
        monkeypatch.setattr(primary.app.state,'runtime_identity',identity)
        for role in ('owner','lead','store'):
            client=clients[role];client.get('/login')
            username='person@example.test' if role=='owner' else role+'@release.test'
            assert post(client,'/login',username=username,password=PASSWORD).status_code==303
        writes=[]
        engine=primary.Session.kw['bind']
        def observe(conn,cursor,statement,*_):
            if statement.lstrip().upper().startswith(('INSERT ','UPDATE ','DELETE ')):
                writes.append(statement.split()[1:3])
        event.listen(engine,'before_cursor_execute',observe)
        try:
            def fetch(role,path):
                response=clients[role].get(path)
                body=response.json() if response.headers.get('content-type','').startswith('application/json') else response.text
                return {'status':response.status_code,'headers':response.headers,'body':body}
            report=acceptance(fetch,principals)
            assert report['status']=='PASS' and len(report['checks'])==10
            response=clients['owner'].get('/management/deployment/readiness')
            assert response.status_code==200,response.text
            body=response.json();assert body['schema']==HEAD_REVISION and all(body['dependencies'].values())
            assert clients['lead'].get('/management/deployment/readiness').status_code==403
            assert clients['store'].get('/management/deployment/readiness').status_code==403
            assert clients['anonymous'].get('/management/deployment/readiness').status_code==303
        finally:event.remove(engine,'before_cursor_execute',observe)
        # Normal middleware renews ordinary sessions; business writes are forbidden.
        assert all('web_sessions' in ' '.join(row).lower() for row in writes),writes
        identity['status']='INVALID'
        response=clients['owner'].get('/management/deployment/readiness')
        assert response.status_code==503 and not response.json()['dependencies']['source']


def test_readiness_database_error_is_503_without_connection_details(monkeypatch):
    from types import SimpleNamespace
    import json
    from sqlalchemy.exc import OperationalError
    from app.routers import deployment_verification as routes
    class Broken:
        def execute(self,*args):raise OperationalError('secret database URL',{},Exception('password'))
        def rollback(self):pass
    monkeypatch.setattr(routes,'capture_identity',lambda:{'status':'UNVERIFIED'})
    request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(runtime_identity={})))
    response=routes.readiness(request,principal=SimpleNamespace(id=1),db=Broken())
    assert response.status_code==503 and 'password' not in response.body.decode() and 'secret' not in response.body.decode()
    assert json.loads(response.body)['dependencies']['database'] is False
