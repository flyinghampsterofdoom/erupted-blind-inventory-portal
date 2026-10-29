"""Owner inspection uses existing auth, the worksheet schema, and no effect workers."""
from pathlib import Path
from urllib.parse import unquote
from uuid import uuid4
import os,sys,socket,subprocess,time
import httpx
import pytest
from sqlalchemy import text
from app.security.passwords import hash_password
from app.purchasing_v4.production_target import OwnerInspectionTarget
from test_purchasing_v4_worksheets import sheet,ready,engine,template,pytestmark


@pytest.fixture
def inspection_site(sheet,tmp_path):
    e,state=sheet
    with e.begin() as c:
        password=hash_password('owner-inspection-fixture')
        c.execute(text('UPDATE principals SET password_hash=:pw WHERE id=1'),{'pw':password})
        c.execute(text("INSERT INTO principals(id,username,password_hash,role,active) VALUES (3,'other-admin',:pw,'ADMIN',true)"),{'pw':password})
        before={t:c.scalar(text('SELECT count(*) FROM '+t)) for t in ('purchase_orders','purchase_order_lines','funding_ledger_entries','par_levels')}
        assert c.scalar(text("SELECT to_regclass('purchasing_v4.purchase_authorization')")) is None
    with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
    env={k:v for k,v in os.environ.items() if k in ('PATH','HOME','TMPDIR','LANG')}
    env.update(ENVIRONMENT='test',PYTHONPATH=str(Path.cwd()),DATABASE_URL=e.url.render_as_string(hide_password=False),
        PURCHASING_OWNER_PRINCIPAL_ID='1',PURCHASING_V4_OWNER_INSPECTION_ENABLED='true',
        PURCHASING_V4_DATABASE_NAME=e.url.database,PURCHASING_V4_DATABASE_HOST='127.0.0.1')
    log=(tmp_path/'server.log').open('w')
    process=subprocess.Popen([sys.executable,'-m','uvicorn','app.main:app','--host','127.0.0.1','--port',str(port)],env=env,stdout=log,stderr=subprocess.STDOUT)
    try:
        for _ in range(200):
            assert process.poll() is None,(tmp_path/'server.log').read_text()[-4000:]
            with socket.socket() as s:s.settimeout(.1);listening=s.connect_ex(('127.0.0.1',port))==0
            if listening:break
            time.sleep(.05)
        assert listening
        with httpx.Client(base_url=f'http://127.0.0.1:{port}',follow_redirects=False,timeout=20) as client:yield client,e,before
    finally:
        process.terminate()
        try:process.wait(timeout=10)
        except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
        log.close()


def csrf(client):return {'X-CSRF-Token':unquote(client.cookies['csrf_token'])}
def login(client,user='v4-fixture'):
    client.cookies.clear();client.get('/login')
    return client.post('/login',data={'username':user,'password':'owner-inspection-fixture'},headers=csrf(client))


def test_owner_can_inspect_and_edit_new_manual_drafts_without_reserved_capabilities(inspection_site):
    c,e,before=inspection_site
    assert c.get('/purchasing-v4').status_code==303
    assert login(c).status_code==303
    for path in ('/purchasing-v4','/purchasing-v4/policies','/purchasing-v4/readiness','/api/catalog','/api/legacy-manual','/api/purchasing-v4/evidence-status','/api/purchasing-v4/reconciliation','/management/ordering-tool'):
        response=c.get(path);assert response.status_code==200,(path,response.text[:400])
    context=c.get('/api/context').json()
    for key in ('generation_enabled','authorization_enabled','submission_enabled','quotation_enabled','receiving_enabled','email_enabled'):assert context[key] is False,key
    response=c.post('/api/worksheets',json=dict(command_id=str(uuid4()),calculate=False),headers=csrf(c));assert response.status_code==200,response.text
    sid=response.json()['session']['id']
    assert c.get('/api/worksheets/'+sid).status_code==200
    with e.connect() as db:
        variation=str(db.scalar(text("SELECT variation_id FROM purchasing_v4.variation_reference WHERE square_variation_id='FIXTURE_V1'")))
    added=c.post('/api/worksheets/'+sid+'/add',json=dict(command_id=str(uuid4()),expected_version=0,variation_id=variation),headers=csrf(c))
    assert added.status_code==200,added.text
    state=c.get('/api/worksheets/'+sid).json();row=state['rows'][0]
    from app.purchasing_v4.worksheets import FIELDS
    working={key:row[key] for key in FIELDS};working['packs']=[2,0,0,0];working['touched']=[True]*4
    saved=c.post('/api/worksheets/'+sid+'/commands',json=dict(command_id=str(uuid4()),expected_version=state['session']['version'],changes=[dict(id=row['id'],value=working)]),headers=csrf(c))
    assert saved.status_code==200,saved.text
    assert c.get('/api/worksheets/'+sid).json()['rows'][0]['packs']==[2,0,0,0]
    assert c.post('/api/worksheets',json=dict(command_id=str(uuid4()),calculate=True,max_age_seconds=172800),headers=csrf(c)).status_code==503
    assert c.post('/api/commands',json={},headers=csrf(c)).status_code==503
    assert c.post('/api/suppliers',json={},headers=csrf(c)).status_code==503
    with e.connect() as db:
        assert before=={t:db.scalar(text('SELECT count(*) FROM '+t)) for t in before}
        assert db.scalar(text("SELECT version_num FROM purchasing_v4.alembic_version"))=='p4_0003'
        assert db.scalar(text("SELECT to_regclass('purchasing_v4.purchase_authorization')")) is None


def test_other_admin_and_missing_csrf_cannot_write_owner_workspace(inspection_site):
    c,e,before=inspection_site
    assert login(c,'other-admin').status_code==303
    for path in ('/purchasing-v4','/api/context','/api/catalog','/api/worksheets','/api/manual'):
        assert c.get(path).status_code==403,path
    assert c.post('/api/worksheets',json=dict(command_id=str(uuid4()),calculate=False),headers=csrf(c)).status_code==403
    login(c)
    assert c.post('/api/worksheets',json=dict(command_id=str(uuid4()),calculate=False)).status_code==403


def test_owner_target_rejects_url_overrides_and_wrong_principal(engine):
    target=OwnerInspectionTarget(engine.url.database,'127.0.0.1',1,engine.url.username)
    for url in (engine.url.set(database='another_database'),engine.url.set(query={'host':'elsewhere'}),engine.url.set(host='elsewhere')):
        with pytest.raises(PermissionError):target.verify_url(url)
