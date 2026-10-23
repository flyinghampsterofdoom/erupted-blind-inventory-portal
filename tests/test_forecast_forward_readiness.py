from copy import deepcopy
from pathlib import Path
from datetime import datetime,timedelta,timezone,date
import hashlib,json,sqlite3
import pytest
from fastapi.testclient import TestClient
from test_forecast_product import prepared
from test_forecast_validation import NOW
from app.forecast_validation.forward_validation import register,evaluate,read
from app.forecast_validation.product_diagnostics import attention,abstention_audit
from app.forecast_validation.product_release import verify_file
from app.forecast_validation.product import ProductView,build
from app.forecast_validation.contracts import EvidenceError,TZ
from scripts.review_forecast_product import create_review_app
from app.config import settings

@pytest.fixture
def prospective(prepared,tmp_path):
    p=deepcopy(prepared[1]);p['snapshot']['kind']='PROSPECTIVE';registry=tmp_path/'registry';registration=register(registry,p,now=NOW)
    start=date.fromisoformat(p['snapshot']['evaluation_start']);observed=datetime.combine(start+timedelta(days=7),datetime.min.time(),TZ)
    rows=[]
    for row in p['snapshot']['rows']:
        days=[]
        for i in range(7):
            day=(start+timedelta(days=i)).isoformat();records=[]
            for source,data in [('daily_sales_coverage',{'date':day,'complete':True,'units':'2'}),('daily_exposure',{'date':day,'availability':'IN_STOCK'}),('square_order_line',{'date':day,'quantity':'2'})]:
                records.append({'source':source,'id':source+day+row['store_id'],'version':1,'known_at':observed.isoformat(),'occurred_at':observed.isoformat(),'data':data})
            days.append({'date':day,'units':'2','availability':'IN_STOCK','complete':True,'source_records':records})
        rows.append({'store_id':row['store_id'],'variation_id':row['variation_id'],'days':days})
    outcome={'registration':registration,'horizon':7,'revision':'synthetic-v1','cutoff':observed.isoformat(),'observed_at':observed.isoformat(),'rows':rows}
    return registry,registration,outcome,observed,p

def test_forward_exact_error_and_immutable_retry(prospective):
    registry,registration,outcome,now,p=prospective
    result=evaluate(registry,registration,outcome,now=now);assert evaluate(registry,registration,outcome,now=now)==result
    r=read(registry,result);assert r['coverage']=={'CONTINUOUS':4}
    assert len(r['individual_cohorts'])>0 and all(x['mae']==0 and x['bias_units']==0 for x in r['individual_cohorts'])
    assert r['winner'] is None and not r['automatic_promotion']
    assert r['common_four_model_cohort']

@pytest.mark.parametrize('kind',['RETROSPECTIVE','SYNTHETIC'])
def test_not_prospective_cannot_register(prospective,kind):
    reg,_,_,_,p=prospective;p['snapshot']['kind']=kind
    with pytest.raises(EvidenceError):register(reg,p,now=NOW)

def test_freshness_and_late_registration(prospective):
    reg,_,_,_,p=prospective
    with pytest.raises(EvidenceError):register(reg,p,now=NOW+timedelta(seconds=901))
    with pytest.raises(EvidenceError):register(reg,p,now=NOW+timedelta(days=1))

@pytest.mark.parametrize('problem',['immature','future','duplicate','outside','negative','proof','transaction','known_after_cutoff','foreign_scope','wrong_registration'])
def test_forward_rejects_invalid_evidence(prospective,problem):
    reg,ident,o,now,p=prospective;day=o['rows'][0]['days'][0]
    if problem=='immature':o['cutoff']=(now-timedelta(seconds=1)).isoformat()
    elif problem=='future':o['observed_at']=(now+timedelta(seconds=1)).isoformat()
    elif problem=='duplicate':o['rows'].append(deepcopy(o['rows'][0]))
    elif problem=='outside':day['date']='2099-01-01'
    elif problem=='negative':day['units']='-1'
    elif problem=='proof':day['source_records']=[]
    elif problem=='transaction':day['source_records']=day['source_records'][:2]
    elif problem=='known_after_cutoff':day['source_records'][0]['known_at']=(now+timedelta(seconds=1)).isoformat()
    elif problem=='foreign_scope':o['rows'][0]['store_id']='unscoped'
    elif problem=='wrong_registration':o['registration']='0'*64
    with pytest.raises(EvidenceError):evaluate(reg,ident,o,now=now)

@pytest.mark.parametrize('exposure',['STOCKOUT','UNKNOWN','PARTIAL'])
def test_censoring_and_common_cohort(prospective,exposure):
    reg,ident,o,now,p=prospective
    for row in o['rows']:
        day=row['days'][0];day['availability']=exposure;day['source_records'][1]['data']['availability']=exposure
    r=read(reg,evaluate(reg,ident,o,now=now))
    assert not r['individual_cohorts'] and not r['common_four_model_cohort']
    assert r['coverage']=={'CENSORED':4}
    assert all(e['lost_demand'] is None for e in r['stockout_effects'])

def test_tamper_detected(prospective):
    reg,ident,*_=prospective;path=reg/(ident+'.json');path.chmod(0o600);path.write_text('{}')
    with pytest.raises(EvidenceError):read(reg,ident)

def test_priority_overlaps_and_abstentions(prepared):
    _,p,_=prepared;r=p['snapshot']['rows'][0];r['lead_time_days']=None
    a=attention(r,{'rate':'1'},{'quantity':4},2,False,False)
    assert a['primary']=='immediate' and a['insufficient'] and a['near_term']
    assert 'lead_time' in a['missing']
    a=attention(r,{'rate':None},{'quantity':None},None,False,False);assert a['primary']=='evidence'
    a=abstention_audit(p);assert a['models']['B_CURRENT']['denominator_store_sku']==4

def test_pinned_projection_integrity(prepared,tmp_path):
    path,_,_=prepared;sha=hashlib.sha256(path.read_bytes()).hexdigest();verify_file(path,sha,True)
    with pytest.raises(ValueError):verify_file(path,'',True)
    path.chmod(0o600)
    with path.open('ab') as f:f.write(b'tampered')
    with pytest.raises(ValueError):verify_file(path,sha,True)
    other=tmp_path/'business.sqlite';sqlite3.connect(other).close()
    with pytest.raises(ValueError):ProductView(other)

def test_failed_rebuild_keeps_last_good_view(prepared,tmp_path):
    path,p,_=prepared;before=hashlib.sha256(path.read_bytes()).hexdigest();p['snapshot']['rows'][0]['sales']=[]
    source=tmp_path/'bad.json';source.write_text(json.dumps(p))
    with pytest.raises(EvidenceError):build(source,path)
    assert hashlib.sha256(path.read_bytes()).hexdigest()==before
    v=ProductView(path);assert v.listing()[0]['total']==4;v.close()

def test_production_gate_and_corruption_are_route_scoped(prepared,monkeypatch):
    for field in ('forecast_validation_enabled','forecast_product_projection_path','purchasing_owner_principal_id','environment','forecast_product_projection_sha256'):monkeypatch.setattr(settings,field,getattr(settings,field))
    app=create_review_app(prepared[0],'local');c=TestClient(app);c.post('/local-review',data={'password':'local','account':'6'})
    monkeypatch.setattr(settings,'environment','production');monkeypatch.setattr(settings,'forecast_product_projection_sha256','')
    assert c.get('/management/forecasting').status_code==503
    sha=hashlib.sha256(prepared[0].read_bytes()).hexdigest();monkeypatch.setattr(settings,'forecast_product_projection_sha256',sha)
    assert c.get('/management/forecasting').status_code==200
    monkeypatch.setattr(settings,'forecast_product_projection_sha256','0'*64)
    assert c.get('/management/forecasting').status_code==503
    assert c.get('/local-review').status_code==200

def test_interrupted_publication_never_replaces_good_projection(prepared,tmp_path,monkeypatch):
    path,p,_=prepared;before=path.read_bytes();src=tmp_path/'valid.json';src.write_text(json.dumps(p))
    def interrupt(*a):raise OSError('simulated publish interruption')
    monkeypatch.setattr('app.forecast_validation.product.os.replace',interrupt)
    with pytest.raises(OSError):build(src,path)
    assert path.read_bytes()==before

def test_create_only_registry_atomic_publish(prospective,monkeypatch):
    from app.forecast_validation.forward_validation import _append
    reg,*_=prospective
    def interrupt(*a):raise OSError('simulated publish interruption')
    monkeypatch.setattr('app.forecast_validation.forward_validation.os.link',interrupt)
    before=set(reg.iterdir())
    with pytest.raises(OSError):_append(reg,{'new':'record'})
    assert set(reg.iterdir())==before

from test_purchasing_receiving_extension import database_template,receiving_db

def test_purchasing_concurrency_and_business_integrity(prepared,receiving_db,monkeypatch):
    import os,time,statistics
    from concurrent.futures import ThreadPoolExecutor
    from fastapi import FastAPI
    from sqlalchemy import text
    from sqlalchemy.orm import Session
    from app.main import app as main
    from app.auth import Principal,Role,get_current_principal
    from app.db import get_db
    from app.routers import purchasing,management,forecast_product
    from app.forecast_validation import demand,runner,capture
    monkeypatch.setattr(settings,'purchasing_v3_phase2_enabled',True)
    monkeypatch.setattr(settings,'forecast_validation_enabled',True)
    monkeypatch.setattr(settings,'forecast_product_projection_path',os.getenv('STAGE4_PROJECTION',str(prepared[0])))
    for module,function in ((demand,'calculate'),(runner,'forecast'),(capture,'capture')):
        monkeypatch.setattr(module,function,lambda *a,**kw:pytest.fail('HTTP invoked model/collector'))
    app=FastAPI();app.state.templates=main.state.templates;app.include_router(purchasing.router);app.include_router(forecast_product.router)
    actor=Principal(1,'local-owner',Role.ADMIN,None,True)
    app.dependency_overrides[get_current_principal]=lambda:actor
    app.dependency_overrides[management.ordering_access]=lambda:actor
    def db():
        with Session(receiving_db) as session:
            session.execute(text('SET TRANSACTION READ ONLY'));yield session
    app.dependency_overrides[get_db]=db
    client=TestClient(app)
    tables=['purchase_orders','purchase_order_lines','purchase_order_store_allocations','purchase_order_receipts','purchase_order_receipt_allocations','vendors','stores']
    def fingerprint():
        with receiving_db.connect() as c:
            return {t:c.execute(text('SELECT md5(coalesce(jsonb_agg(to_jsonb(t) ORDER BY id)::text,\'[]\')) FROM '+t+' t')).scalar_one() for t in tables}
    before=fingerprint()
    def request(url):
        t=time.perf_counter();r=client.get(url);assert r.status_code==200,r.text[:100];return (time.perf_counter()-t)*1000
    for i in range(3):request('/management/purchasing')
    baseline=[request('/management/purchasing') for _ in range(30)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        concurrent=[];forecasts=[]
        for i in range(30):
            f=pool.submit(request,'/management/forecasting?store=2' if i%2 else '/management/forecasting/purchasing-list')
            b=pool.submit(request,'/management/purchasing');concurrent.append(b.result());forecasts.append(f.result())
    recovery=[request('/management/purchasing') for _ in range(30)]
    def summary(v):return {'n':len(v),'p50_ms':statistics.median(v),'p95_ms':sorted(v)[int(.95*(len(v)-1))],'max_ms':max(v)}
    result={'baseline':summary(baseline),'concurrent_business':summary(concurrent),'concurrent_dashboard':summary(forecasts),'recovery':summary(recovery),'errors':0,'business_hashes_unchanged':before==fingerprint(),'scope':'Actual purchasing handler + prepared full-size forecast view; local PostgreSQL synthetic business fixture. Not a hosted or full business workload qualification.'}
    assert result['business_hashes_unchanged']
    if os.getenv('STAGE4_RESULTS'):
        Path(os.environ['STAGE4_RESULTS']).write_text(json.dumps(result,indent=2))


def test_full_reader_capacity_rejects_immediately(prepared,monkeypatch):
    from app.routers.forecast_product import slots
    for field in ('forecast_validation_enabled','forecast_product_projection_path','purchasing_owner_principal_id'):
        monkeypatch.setattr(settings,field,getattr(settings,field))
    c=TestClient(create_review_app(prepared[0],'bounded'))
    c.post('/local-review',data={'password':'bounded','account':'6'},follow_redirects=False)
    slots.acquire();slots.acquire()
    try:
        response=c.get('/management/forecasting')
        assert response.status_code==503 and response.headers['retry-after']=='1'
    finally:slots.release();slots.release()
