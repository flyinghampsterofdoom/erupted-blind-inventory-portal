"""Runs in a separate recovery-code process against the rehearsal database only."""
import os
import socket
import subprocess
import sys
from urllib.parse import urlparse
from sqlalchemy.engine import make_url
url = make_url(os.environ['DATABASE_URL'])
if url.host not in {'localhost','127.0.0.1','::1'} or not url.database.startswith('erupted_rollback_rehearsal_'):
    raise SystemExit('Probe requires a disposable local rehearsal database')

# Catch accidental external integrations in the rehearsal, including login refresh.
_connect = socket.socket.connect

def local_connect(sock, address):
    if isinstance(address, tuple) and address[0] not in {'127.0.0.1', '::1', 'localhost'}:
        raise AssertionError('External network is forbidden in rollback rehearsal')
    return _connect(sock, address)

socket.socket.connect = local_connect
from fastapi.testclient import TestClient
from sqlalchemy import select, update, text
from app.main import app
from app.db import SessionLocal, engine
from app.models import Employee, ScheduleShift
from app.routers import auth
from app.schema_contract import assert_supported_schema, UnsupportedSchemaError
from app.config import settings

employee_id, shift_id, period_id = map(int,sys.argv[1:])
auth.square_data_needs_refresh = lambda db: False
checks=[]
with TestClient(app, follow_redirects=False, client=('127.0.0.1',50000)) as client:
    assert client.get('/login').status_code == 200
    def post(path, **data):
        return client.post(path,data={'csrf_token':client.cookies.get('csrf_token'),**data})
    assert post('/login',username='pending@example.test',password='anything').status_code == 401
    assert post('/login',username='recovery-admin',password='synthetic recovery password').status_code == 303
    checks.append('startup, pending-account rejection and existing Admin authentication')
    for path in ['/v2/hr/employees',f'/v2/hr/employees/{employee_id}',
                 f'/v2/scheduling/week?schedule_period_id={period_id}',
                 f'/v2/scheduling/api/board?schedule_period_id={period_id}',
                 '/v2/ordering','/v2/ordering/products','/management/users']:
        response=client.get(path)
        assert response.status_code==200,(path,response.status_code,response.text[:300])
    checks.append('HR/profile, schedule HTML/API, ordering/catalog and accounts reads')
    # Every unsafe method exposed by the old scheduling and HR routers is quarantined.
    from fastapi.routing import APIRoute
    for route in app.routes:
        if isinstance(route,APIRoute) and route.path.startswith(('/v2/scheduling','/v2/hr')):
            import re
            path=re.sub(r'\{[^}]+\}','1',route.path)
            for method in route.methods-{'GET','HEAD','OPTIONS'}:
                assert client.request(method,path,json={}).status_code==503,(method,path)
    for path in [f'/management/users/1/password','/management/password/reset','/management/stores/1/credentials']:
        assert post(path,new_password='must never be saved').status_code==503
    checks.append('all Scheduling/HR mutations and legacy credential mutation routes blocked')
    with engine.connect() as c:
        campaign_id=c.execute(text("SELECT id FROM campaigns WHERE label='Recovery synthetic campaign'")).scalar_one()
    response=post('/management/groups/create',name='Recovery synthetic count group',campaign_ids=str(campaign_id))
    assert response.status_code==303,(response.status_code,response.text[:200])
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM count_groups WHERE name='Recovery synthetic count group'")).scalar_one()==1
    checks.append('unaffected inventory count-group creation committed')
    assert post('/logout').status_code==303
with SessionLocal() as db:
    row=db.get(ScheduleShift,shift_id);row.employee_note='must not persist'
    try: db.flush()
    except RuntimeError: db.rollback()
    else: raise AssertionError('Direct ORM scheduling mutation bypassed recovery boundary')
    try: db.execute(update(Employee).where(Employee.id==employee_id).values(full_name='must not persist'))
    except RuntimeError: db.rollback()
    else: raise AssertionError('Bulk HR mutation bypassed recovery boundary')
checks.append('ORM flush and bulk DML protections')
cron_env={**os.environ, 'SCHEDULE_AUTOMATION_EXECUTION_ENABLED':'true',
          'PATH':str(__import__('pathlib').Path(sys.executable).parent)+os.pathsep+os.environ['PATH']}
result=subprocess.run(['sh','scripts/release/cron.sh'],env=cron_env,text=True,capture_output=True,check=True)
assert 'RECOVERY_DISABLED' in result.stdout
checks.append('configured cron command exits successfully without executing automation')
from app.services.v2_scheduling_policy_service import run_schedule_automation
try: run_schedule_automation(None, principal=None)
except RuntimeError: pass
else: raise AssertionError('Direct automation was not disabled')
from app.schema_contract import upgrade_database, stamp_matching_database
for operation in [lambda: upgrade_database(os.environ['DATABASE_URL']),
                  lambda: stamp_matching_database(database_url=os.environ['DATABASE_URL'], reference_url=os.environ['DATABASE_URL'])]:
    try: operation()
    except UnsupportedSchemaError: pass
    else: raise AssertionError('Recovery migration or stamping was not blocked')
checks.append('direct automation, migration and stamping entrypoints blocked')
# A forged revision label cannot bypass the object contract.
with engine.begin() as c: c.execute(text('ALTER TABLE schedule_coverage_commitments RENAME COLUMN note TO altered_note'))
try:
    try: assert_supported_schema(engine)
    except UnsupportedSchemaError: pass
    else: raise AssertionError('Schema drift was accepted')
finally:
    with engine.begin() as c: c.execute(text('ALTER TABLE schedule_coverage_commitments RENAME COLUMN altered_note TO note'))
assert_supported_schema(engine)
with engine.begin() as c: c.execute(text('ALTER TABLE schedule_generator_snapshots DISABLE TRIGGER schedule_generator_snapshots_immutable'))
try:
    try: assert_supported_schema(engine)
    except UnsupportedSchemaError: pass
    else: raise AssertionError('Disabled snapshot protection was accepted')
finally:
    with engine.begin() as c: c.execute(text('ALTER TABLE schedule_generator_snapshots ENABLE TRIGGER schedule_generator_snapshots_immutable'))
assert_supported_schema(engine)
checks.append('0031 shape drift and disabled evidence trigger rejected; restored shape accepted')
settings.schema_revision_check_enabled=False
try:
    try: assert_supported_schema(engine)
    except UnsupportedSchemaError: pass
    else: raise AssertionError('Schema checks could be disabled')
finally: settings.schema_revision_check_enabled=True
print('\n'.join(checks))
