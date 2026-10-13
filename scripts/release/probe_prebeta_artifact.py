"""Runtime acceptance against only a named disposable loopback rehearsal DB."""
import json,os,socket,subprocess,sys
from pathlib import Path
from sqlalchemy.engine import make_url
url=make_url(os.environ['DATABASE_URL'])
if url.host not in {'localhost','127.0.0.1','::1'} or not url.database.startswith('erupted_prebeta_release_'):
    raise SystemExit('Artifact probes require a disposable loopback rehearsal database.')
# No provider requests may escape the probe, even with retained DB integrations.
connect=socket.socket.connect
external=[]
def local_only(self,address):
    if isinstance(address,tuple) and address[0] not in {'localhost','127.0.0.1','::1'}:
        external.append('blocked');raise AssertionError('External network forbidden in artifact rehearsal.')
    return connect(self,address)
socket.socket.connect=local_only
from sqlalchemy import select,text,update
from app.db import engine,SessionLocal
from app import models as m
from app.config import settings
from app.schema_contract import assert_supported_schema,UnsupportedSchemaError
from app.security.passwords import hash_password
from app.auth import Principal,Role
from app.services.blind_count_service import save_draft
from fastapi.testclient import TestClient
from app.routers import auth
from app.main import app
# The real transport is forbidden; synthetic login need not refresh provider caches.
auth.square_data_needs_refresh=lambda db:False
mode=sys.argv[1];checks=[]
if mode=='candidate':
    with SessionLocal() as db:
        sid=db.scalar(select(m.Store.id).where(m.Store.active.is_(True)).order_by(m.Store.id))
        admin=m.Principal(username='release-rehearsal-admin@example.test',password_hash=hash_password('Synthetic-rehearsal-only-2026!'),role=m.PrincipalRole.ADMIN,active=True)
        staff=m.Principal(username='release-rehearsal-store@example.test',password_hash=hash_password('Synthetic-rehearsal-only-2026!'),role=m.PrincipalRole.STORE,store_id=sid,active=True)
        db.add_all([admin,staff]);db.flush()
        campaign=m.Campaign(label='Synthetic release rehearsal');db.add(campaign);db.flush()
        count=m.CountSession(store_id=sid,campaign_id=campaign.id,employee_name='Synthetic release rehearsal',created_by_principal_id=staff.id)
        db.add(count);db.flush();db.add(m.SnapshotLine(session_id=count.id,variation_id='REHEARSAL',item_name='Synthetic fixture',variation_name='Rehearsal'))
        eid=db.scalar(select(m.Employee.id).order_by(m.Employee.id));period=db.scalar(select(m.SchedulePeriod.id).order_by(m.SchedulePeriod.id))
        db.commit()
        state={'count':count.id,'employee':eid,'period':period}
        Path(os.environ['REHEARSAL_PRIVATE_STATE']).write_text(json.dumps(state))
else:state=json.loads(Path(os.environ['REHEARSAL_PRIVATE_STATE']).read_text())
assert_supported_schema();checks.append('schema 0032 accepted')
with TestClient(app,headers={'X-Forwarded-For':'127.0.0.1'}) as client:
    assert client.get('/login').status_code==200
    response=client.post('/login',data={'username':'release-rehearsal-admin@example.test','password':'Synthetic-rehearsal-only-2026!','csrf_token':client.cookies.get('csrf_token')},follow_redirects=False)
    assert response.status_code==303
    checks.append('startup and synthetic Admin authentication')
    board=client.get('/v2/scheduling/week',params={'period_id':state['period']})
    assert board.status_code==200
    checks.append('existing scheduling period reads')
    assert client.get('/v2/hr/employees').status_code in (200,404)
    if mode=='recovery':
        assert board.headers['X-Erupted-Recovery']=='schema-0032-bounded'
        assert 'Recovery mode:' in board.text
        from app.prebeta_recovery_guard import blocked_mutation_path
        for route in app.routes:
            if blocked_mutation_path(route.path):
                for method in getattr(route,'methods',set()) & {'POST','PATCH','PUT','DELETE'}:
                    path=__import__('re').sub(r'\{[^}]+\}','1',route.path)
                    assert client.request(method,path,json={}).status_code==503,(method,route.path)
        checks.append('all registered scheduling/HR/account mutation routes reject with 503')
        assert client.get('/v2/scheduling/automation').status_code==503
        from app.prebeta_recovery_guard import assert_contract
        assert_contract();checks.append('strict object contract and recovery banner')
    # Existing counts remain accessible in their established store scope.
    response=client.post('/logout',data={'csrf_token':client.cookies.get('csrf_token')},follow_redirects=False)
    assert response.status_code==303
    response=client.post('/login',data={'username':'release-rehearsal-store@example.test','password':'Synthetic-rehearsal-only-2026!','csrf_token':client.cookies.get('csrf_token')},follow_redirects=False)
    assert response.status_code==303
    assert client.get('/store/daily-count').status_code==200
    count=client.get('/store/sessions/'+str(state['count']));assert count.status_code==200
    with SessionLocal() as db:revision=db.get(m.CountSession,state['count']).draft_revision
    response=client.post('/store/sessions/'+str(state['count'])+'/draft',json={'revision':revision,'operation_id':'release-'+mode,'changes':{'REHEARSAL':{'front_qty':'2','back_qty':'3'}}},headers={'X-CSRF-Token':client.cookies.get('csrf_token'),'X-Working-Context':'legacy'})
    assert response.status_code==200
    checks.append('unchanged store-scoped inventory read and synthetic draft save')
if mode=='recovery':
    with SessionLocal() as db:
        person=db.get(m.Employee,state['employee']);person.full_name='Forbidden recovery mutation'
        try:db.flush()
        except RuntimeError:db.rollback()
        else:raise AssertionError('ORM recovery boundary failed')
        try:db.execute(update(m.InventoryStoreAccessGrant).values(reason='Forbidden grant mutation'))
        except RuntimeError:db.rollback()
        else:raise AssertionError('Bulk grant recovery boundary failed')
    checks.append('ORM employee and bulk emergency-grant writes blocked')
    from app.services.v2_scheduling_policy_service import run_schedule_automation
    try:run_schedule_automation(None,principal=None)
    except RuntimeError:pass
    else:raise AssertionError('Direct recovery automation was enabled')
    from app.schema_contract import upgrade_database,stamp_matching_database
    for operation in (lambda:upgrade_database(os.environ['DATABASE_URL']),lambda:stamp_matching_database(database_url=os.environ['DATABASE_URL'],reference_url=os.environ['DATABASE_URL'])):
        try:operation()
        except UnsupportedSchemaError:pass
        else:raise AssertionError('Recovery migration/stamping allowed')
    checks.append('direct automation, migration and stamping blocked')
    # Falsified version labels cannot conceal missing new columns or disabled evidence protection.
    with engine.begin() as c:c.execute(text('ALTER TABLE inventory_store_access_grants RENAME COLUMN context_version TO altered_context_version'))
    try:
        try:assert_contract()
        except UnsupportedSchemaError:pass
        else:raise AssertionError('Missing 0032 column accepted')
    finally:
        with engine.begin() as c:c.execute(text('ALTER TABLE inventory_store_access_grants RENAME COLUMN altered_context_version TO context_version'))
    with engine.begin() as c:c.execute(text('ALTER TABLE schedule_generator_snapshots DISABLE TRIGGER schedule_generator_snapshots_immutable'))
    try:
        try:assert_contract()
        except UnsupportedSchemaError:pass
        else:raise AssertionError('Disabled snapshot protection accepted')
    finally:
        with engine.begin() as c:c.execute(text('ALTER TABLE schedule_generator_snapshots ENABLE TRIGGER schedule_generator_snapshots_immutable'))
    old=settings.schema_revision_check_enabled;settings.schema_revision_check_enabled=False
    try:
        try:assert_contract()
        except UnsupportedSchemaError:pass
        else:raise AssertionError('Recovery schema checks could be disabled')
    finally:settings.schema_revision_check_enabled=old
    assert_contract();checks.append('shape drift, disabled evidence trigger and disabled schema validation rejected')
cron_env={**os.environ,'SCHEDULE_AUTOMATION_EXECUTION_ENABLED':'true' if mode=='recovery' else 'false'}
for command in ([sys.executable,'scripts/run_schedule_automation.py'],['sh','scripts/release/cron.sh']):
    result=subprocess.run(command,env=cron_env,text=True,capture_output=True)
    assert result.returncode==0
    assert ('RECOVERY_DISABLED' if mode=='recovery' else 'SCHEDULE_AUTOMATION_DISABLED') in result.stdout
checks.append('exact cron command and wrapper inert under required gates')
assert not external,'A provider request was attempted'
print(json.dumps({'mode':mode,'checks':checks,'external_provider_attempts':0,'status':'passed'}))
