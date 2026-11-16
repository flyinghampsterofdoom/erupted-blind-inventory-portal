"""Production composition over disposable fixtures; never contacts production."""
import json,os,subprocess,sys
from uuid import uuid4
import pytest
from sqlalchemy import text
from app.purchasing_v4.production_submission_schema import install
from app.purchasing_v4.production_submission import ProductionSubmissionTarget
from app.purchasing_v4.production_target import OwnerInspectionTarget
from test_purchasing_v4_worksheets import sheet,ready,engine,template,pytestmark
from app.security.passwords import hash_password


@pytest.fixture
def approved(sheet):
    e,state=sheet
    with e.begin() as c:
        protected=['public.vendors','public.purchase_orders','public.purchase_order_lines','public.purchase_order_store_allocations','public.purchase_order_receipts','public.square_sync_events']
        before={t:list(c.scalars(text('SELECT row_to_json(x)::text FROM '+t+' x ORDER BY row_to_json(x)::text'))) for t in protected}
        install(c,owner_principal_id=1,approval_reference='Isolated fixture approval only')
        assert before=={t:list(c.scalars(text('SELECT row_to_json(x)::text FROM '+t+' x ORDER BY row_to_json(x)::text'))) for t in protected}
        password=hash_password('production-auth-fixture')
        c.execute(text('UPDATE principals SET password_hash=:password WHERE id=1'),dict(password=password))
        for id,name,role,active in [(3,'other-admin','ADMIN',True),(4,'ordinary-user','STORE',True),(5,'inactive-admin','ADMIN',False)]:
            c.execute(text('INSERT INTO principals(id,username,password_hash,role,active) VALUES (:id,:name,:password,CAST(:role AS principal_role),:active)'),dict(id=id,name=name,password=password,role=role,active=active))
    return e,state


def run_script(e,state,script,**env):
    configured=dict(os.environ,DATABASE_URL=e.url.render_as_string(hide_password=False),ENVIRONMENT='test',
        PURCHASING_OWNER_PRINCIPAL_ID='1',PURCHASING_V4_DATABASE_NAME=e.url.database,
        PURCHASING_V4_DATABASE_HOST='127.0.0.1',PYTHONPATH=os.getcwd(),FIXTURE=json.dumps(state))
    configured.update(env)
    result=subprocess.run([sys.executable,'-B','-c',script],env=configured,text=True,capture_output=True,timeout=150)
    assert result.returncode==0,result.stdout+'\n'+result.stderr
    return result.stdout


SCRIPT=r"""
import os,json
from uuid import uuid4
from urllib.parse import unquote
from fastapi.testclient import TestClient
from sqlalchemy import text
from app.config import settings
# Compose the same production target with the synthetic source namespace.
settings.purchasing_v4_owner_inspection_enabled=False
settings.purchasing_v4_production_submission_enabled=False
settings.purchasing_v4_receiving_enabled=True
from app import main
from app.purchasing_v4.integration_app import create_app,Capabilities
from app.purchasing_v4.production_submission import ProductionSubmissionTarget
from app.purchasing_v4.production_target import OwnerInspectionTarget
from sqlalchemy.engine import make_url
state=json.loads(os.environ['FIXTURE'])
url=make_url(settings.database_url_normalized)
production=os.environ.get('INSPECTION_ONLY')!='true'
target=(ProductionSubmissionTarget if production else OwnerInspectionTarget)(url.database,url.host,1,url.username)
app=create_app(url,source_namespace='synthetic-fixture-source',owner_principal_id=1,target=target,
 capabilities=Capabilities(workspace=True,authorization=production))
def csrf(c):return {'X-CSRF-Token':unquote(c.cookies['csrf_token'])}
def login(c,username='v4-fixture'):
 c.cookies.clear();c.get('/login')
 return c.post('/login',data=dict(username=username,password='production-auth-fixture'),headers=csrf(c))
e=app.state.purchasing_v4.state.engine
def counts():
 with e.connect() as c:return {t:c.scalar(text('SELECT count(*) FROM '+t)) for t in ['public.purchase_orders','public.purchase_order_receipts','public.square_sync_events','purchasing_v4.purchase_authorization']}
with TestClient(app,follow_redirects=False,client=('127.0.0.1',50000)) as c:
 before=counts()
 assert c.get('/api/context').status_code==303
 assert c.post('/api/purchase-authorizations',json={}).status_code in (303,401,403,404)
 assert login(c).status_code==303
 context=c.get('/api/context');assert context.status_code==200,context.text
 assert context.json()['submission_enabled'] is production
 assert context.json()['authorization_enabled'] is production
 assert not any(context.json()[k] for k in ('email_enabled','quotation_enabled','receiving_enabled'))
 if not production:
  assert c.post('/api/purchase-authorizations',json={},headers=csrf(c)).status_code in (404,503)
  assert c.post('/api/submission-reviews',json={},headers=csrf(c)).status_code in (404,503)
  assert counts()==before
 else:
  row=state['rows'][0]
  request=dict(review_id=str(uuid4()),session_id=state['session']['id'],expected_version=0,
   supplier_ids=[row['supplier_id']],currency='USD',
   valuations={row['id']:dict(source='EXPECTED' if row['unit_cost'] is not None else 'OWNER_EXPLICIT',unit_value=row['unit_cost'] if row['unit_cost'] is not None else '2.50',reason='Explicit fixture valuation')},
   incremental_reason='Explicit isolated additional purchase after inspecting original incoming supply',
   public_header=dict(purchaser='Fixture owner',ship_to='Fixture pickup'))
  assert c.post('/api/submission-reviews',json=request).status_code==403
  r=c.post('/api/submission-reviews',json=request,headers=csrf(c));assert r.status_code==200,r.text
  assert not r.json()['blocking'],r.text
  assert counts()==before
  command=dict(command_id=str(uuid4()),review_id=r.json()['id'],acknowledged=True)
  assert c.post('/api/purchase-authorizations',json=command).status_code==403
  assert c.post('/api/purchase-authorizations',json=dict(command,acknowledged=False),headers=csrf(c)).status_code==422
  for username in ('other-admin','ordinary-user'):
   assert login(c,username).status_code==303
   assert c.get('/api/context').status_code==403
   assert c.post('/api/submission-reviews',json=request,headers=csrf(c)).status_code==403
   assert c.post('/api/purchase-authorizations',json=command,headers=csrf(c)).status_code==403
  assert login(c,'inactive-admin').status_code!=303
  assert counts()==before
  assert login(c).status_code==303
  with e.begin() as db:db.execute(text("UPDATE public.principals SET role='STORE' WHERE id=1"))
  assert c.post('/api/purchase-authorizations',json=command,headers=csrf(c)).status_code==403
  with e.begin() as db:db.execute(text("UPDATE public.principals SET role='ADMIN' WHERE id=1"))
  first=c.post('/api/purchase-authorizations',json=command,headers=csrf(c));assert first.status_code==200,first.text
  repeated=c.post('/api/purchase-authorizations',json=command,headers=csrf(c));assert repeated.status_code==200 and repeated.json()['replayed'],repeated.text
  assert c.post('/api/purchase-authorizations',json=dict(command,command_id=str(uuid4())),headers=csrf(c)).json()['authorization_id']==first.json()['authorization_id']
  assert c.get('/api/authorization-commands/'+command['command_id']).json()['authorization_id']==first.json()['authorization_id']
  child=first.json()['children'][0];po=child['purchase_order_id'];assert po
  with e.connect() as db:
   assert db.scalar(text("SELECT to_regclass('purchasing_v4.po_ownership')")) is None
   assert db.scalar(text('SELECT count(*) FROM purchasing_v4.purchase_authorization'))==1
   assert db.scalar(text('SELECT count(*) FROM public.purchase_orders'))==before['public.purchase_orders']+1
   assert db.scalar(text('SELECT count(*) FROM public.square_sync_events'))==before['public.square_sync_events']
   assert db.scalar(text('SELECT count(*) FROM purchasing_v4.integration_command WHERE kind=:kind'),{'kind':'DELIVER_DOCUMENTS'})==0
  status=c.get('/api/vendor-authorizations/'+child['id']);assert status.status_code==200,status.text
  files=c.post('/api/vendor-authorizations/'+child['id']+'/documents',headers=csrf(c));assert files.status_code==200,files.text
  assert len(files.json())==2
  assert c.post('/api/vendor-authorizations/'+child['id']+'/documents',headers=csrf(c)).json()==files.json()
  doc=c.get('/api/vendor-artifacts/'+files.json()[0]['id']);assert doc.status_code==200 and doc.content
  # Existing legacy order and the newly submitted order use the same receiving service.
  assert c.get('/api/purchasing-v4/receiving/orders/129').status_code==200
  from app.services import purchase_order_admin_service as legacy
  from app.services.square_request_policy import enforce_square_request_policy
  sent=[]
  def fixture_post(path,payload,*,inventory_quantity_write):
   enforce_square_request_policy('POST',path,payload,inventory_quantity_write=inventory_quantity_write)
   sent.append(payload);return {'counts':[]}
  legacy._square_post=fixture_post
  endpoint='/api/purchasing-v4/receiving/orders/'+str(po)
  receiving=c.get(endpoint);assert receiving.status_code==200,receiving.text
  current=receiving.json();line=current['rows'][0]
  shipment=dict(request_key=str(uuid4()),version=current['version'],cells=[[line['id'],current['stores'][0]['id'],1]])
  for _ in range(2):
   response=c.post(endpoint+'/shipments',json=shipment,headers=csrf(c));assert response.status_code==200,response.text
  assert len(sent)==1
  received=c.get(endpoint).json();assert received['previous']==1
  response=c.post(endpoint+'/close',json=dict(request_key=str(uuid4()),version=received['version']),headers=csrf(c));assert response.status_code==200,response.text
  assert c.get(endpoint).json()['status']=='Closed — Unfulfilled'
  # Pausing new submissions preserves command recovery and original documents.
  app.state.purchasing_v4.state.authorization_enabled=False
  assert not c.get('/api/context').json()['authorization_enabled']
  assert c.post('/api/purchase-authorizations',json=command,headers=csrf(c)).status_code==503
  assert c.get('/api/authorization-commands/'+command['command_id']).status_code==200
  assert c.get('/api/vendor-artifacts/'+files.json()[0]['id']).content==doc.content
print('production auth fixture PASS')
"""


def test_real_sessions_owner_admin_only_csrf_exact_receipts_and_recovery(approved):
    e,state=approved
    assert 'PASS' in run_script(e,state,SCRIPT)


def test_inspection_context_stays_non_committing_with_ledger_installed(approved):
    e,state=approved
    assert 'PASS' in run_script(e,state,SCRIPT,INSPECTION_ONLY='true')


def test_opt_in_fails_closed_without_the_explicit_ledger(sheet):
    e,state=sheet
    from app.purchasing_v4.production_target import attach
    target=ProductionSubmissionTarget(e.url.database,'127.0.0.1',1,e.url.username)
    attach(e,target)
    with e.connect() as c:
        with pytest.raises(RuntimeError,match='separately approved ledger'):
            target.verify(c)


def test_explicit_migration_is_replayable_and_preserves_existing_records(approved):
    e,state=approved
    with e.begin() as c:
        before={t:c.scalar(text('SELECT count(*) FROM '+t)) for t in ['public.vendors','public.purchase_orders','public.purchase_order_lines','public.purchase_order_receipts','public.square_sync_events']}
        install(c,owner_principal_id=1,approval_reference='Repeated isolated fixture qualification')
        assert before=={t:c.scalar(text('SELECT count(*) FROM '+t)) for t in before}
        assert c.scalar(text('SELECT version_num FROM purchasing_v4.alembic_version'))=='p4_0003'
        assert c.scalar(text("SELECT to_regclass('purchasing_v4.po_ownership')")) is None


def test_main_entrypoint_uses_opt_in_target_and_pause_preserves_reads(approved):
    e,state=approved
    code=r"""
from app import main
from fastapi.testclient import TestClient
from app.purchasing_v4.production_submission import ProductionSubmissionTarget
from urllib.parse import unquote
assert isinstance(main.app.state.purchasing_v4.state.engine._purchasing_v4_owner_target,ProductionSubmissionTarget)
with TestClient(main.app,follow_redirects=False,client=('127.0.0.1',50000)) as c:
 c.get('/login')
 r=c.post('/login',data=dict(username='v4-fixture',password='production-auth-fixture'),headers={'X-CSRF-Token':unquote(c.cookies['csrf_token'])});assert r.status_code==303,r.text
 context=c.get('/api/context');assert context.status_code==200,context.text
 assert context.json()['submission_enabled']
 assert not context.json()['authorization_enabled']
 assert c.get('/api/purchase-authorizations').status_code==200
 assert c.post('/api/purchase-authorizations',json={},headers={'X-CSRF-Token':unquote(c.cookies['csrf_token'])}).status_code==503
print('paused production binding PASS')
"""
    assert 'PASS' in run_script(e,state,code,PURCHASING_V4_OWNER_INSPECTION_ENABLED='true',
        PURCHASING_V4_PRODUCTION_SUBMISSION_ENABLED='true',PURCHASING_V4_SUBMISSION_PAUSED='true')


def test_enable_without_workspace_is_rejected_before_mounting(approved):
    e,state=approved
    code=r"""
try:
 from app import main
except RuntimeError as e:
 assert 'explicitly bound owner workspace' in str(e)
else:raise AssertionError('Unbound production purchasing was enabled')
print('invalid configuration denied PASS')
"""
    assert 'PASS' in run_script(e,state,code,PURCHASING_V4_OWNER_INSPECTION_ENABLED='false',
        PURCHASING_V4_PRODUCTION_SUBMISSION_ENABLED='true')


def test_concurrent_reviews_and_changed_supply_do_not_duplicate_orders(approved):
    from concurrent.futures import ThreadPoolExecutor
    from app.purchasing_v4 import production_submission as production,controls
    from test_purchasing_v4_submission import review
    e,state=approved
    reviews=[review(approved),review(approved)]
    def authorize(r):
        try:
            with e.begin() as c:
                return production.authorize(c,actor_id=1,command_id=str(uuid4()),review_id=r['id'],acknowledged=True,source_namespace='synthetic-fixture-source')
        except controls.Conflict:return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(authorize,reviews))
    assert sum(isinstance(r,dict) for r in results)==1
    with e.connect() as c:
        assert c.scalar(text('SELECT count(*) FROM purchasing_v4.purchase_authorization'))==1
        assert c.scalar(text('SELECT count(*) FROM purchasing_v4.authorization_command'))==1


def test_financial_failure_rolls_back_the_complete_decision(approved,monkeypatch):
    from app.purchasing_v4 import production_submission as production
    from app.services import v2_order_payments_service as payments
    from test_purchasing_v4_submission import review
    e,state=approved;r=review(approved)
    def fail(**kwargs):raise RuntimeError('Isolated configured-finance failure')
    monkeypatch.setattr(payments,'initialize_new_order_if_configured',lambda *args,**kwargs:fail(**kwargs))
    with e.connect() as c:before=c.scalar(text('SELECT count(*) FROM public.purchase_orders'))
    with pytest.raises(RuntimeError,match='configured-finance'):
        with e.begin() as c:production.authorize(c,actor_id=1,command_id=str(uuid4()),review_id=r['id'],acknowledged=True,source_namespace='synthetic-fixture-source')
    with e.connect() as c:
        assert c.scalar(text('SELECT count(*) FROM public.purchase_orders'))==before
        for table in ['purchase_authorization','authorized_vendor','authorization_command','worksheet_seal','purchasing_intent']:
            assert c.scalar(text('SELECT count(*) FROM purchasing_v4.'+table))==0
        assert c.scalar(text('SELECT version FROM purchasing_v4.worksheet WHERE id=:id'),{'id':state['session']['id']})==0


def test_database_guards_preserve_approved_costs_and_block_unreviewed_overlap(approved):
    from app.purchasing_v4 import production_submission as production
    from test_purchasing_v4_submission import review
    e,state=approved;r=review(approved)
    with e.begin() as c:
        result=production.authorize(c,actor_id=1,command_id=str(uuid4()),review_id=r['id'],acknowledged=True,source_namespace='synthetic-fixture-source')
        po=result['children'][0]['purchase_order_id']
    attempts=[
        ('UPDATE public.purchase_order_lines SET unit_cost=0 WHERE purchase_order_id=:po','Reviewed quantities'),
        ('UPDATE public.purchase_order_lines SET ordered_qty=ordered_qty+1 WHERE purchase_order_id=:po','Reviewed quantities'),
        ('UPDATE public.purchase_order_lines SET ordered_qty=ordered_qty+1 WHERE id=1','separately reviewed incremental'),
        ('UPDATE public.purchase_order_lines SET purchase_order_id=:po WHERE id=1','unreviewed lines'),
        ('UPDATE public.purchase_order_store_allocations SET purchase_order_line_id=1 WHERE purchase_order_line_id IN (SELECT id FROM public.purchase_order_lines WHERE purchase_order_id=:po)','Reviewed store allocations')]
    for sql,message in attempts:
        with pytest.raises(Exception,match=message):
            with e.begin() as c:c.execute(text(sql),dict(po=po))
    with e.connect() as c:
        assert c.scalar(text('SELECT received_qty_total FROM public.purchase_order_lines WHERE id=1'))==40


def test_stale_review_rejected_after_canonical_receiving_changes(approved):
    from app.purchasing_v4 import production_submission as production,controls
    from test_purchasing_v4_submission import review
    e,state=approved;r=review(approved)
    with e.begin() as c:
        c.execute(text('UPDATE public.purchase_order_store_allocations SET store_received_qty=store_received_qty+1 WHERE id=1'))
        c.execute(text('UPDATE public.purchase_order_lines SET received_qty_total=received_qty_total+1 WHERE id=1'))
    with pytest.raises(controls.Conflict):
        with e.begin() as c:production.authorize(c,actor_id=1,command_id=str(uuid4()),review_id=r['id'],acknowledged=True,source_namespace='synthetic-fixture-source')
    with e.connect() as c:assert c.scalar(text('SELECT count(*) FROM purchasing_v4.purchase_authorization'))==0

def test_po_writer_role_cannot_insert_an_unreviewed_order(approved):
    e,_=approved
    with pytest.raises(Exception,match='exact owner authorization'):
        with e.begin() as c:
            c.execute(text('SET LOCAL ROLE erupted_v4_po_writer'))
            c.execute(text("INSERT INTO public.purchase_orders(vendor_id,status,created_by_principal_id,submitted_by_principal_id) VALUES (1,'IN_TRANSIT',1,1)"))
