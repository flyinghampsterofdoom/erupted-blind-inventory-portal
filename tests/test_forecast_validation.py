"""Shadow boundaries and independently checked arithmetic; no production source access."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone,date
from decimal import Decimal
from fractions import Fraction as F
import random,time
import pytest
from sqlalchemy import text
from app.forecast_validation.contracts import SCHEMA,MODELS,EvidenceError,Conflict
from app.forecast_validation.storage import Store
from app.forecast_validation.runner import forecast
from app.forecast_validation.demand import calculate
from app.forecast_validation.policy import store_need,aggregate
from app.forecast_validation.evaluation import evaluate

NOW=datetime(2026,7,1,18,tzinfo=timezone.utc)
def snapshot():
    return {'schema':SCHEMA,'kind':'SYNTHETIC','scope':'four-store-one-sku','origin':NOW.isoformat(),'cutoff':NOW.isoformat(),'evaluation_start':'2026-07-02','stores':{str(i):{'name':n,'location_id':f'L{i}'} for i,n in enumerate(['Andresen','HWY 99','SR 503','Longview'],1)},'rows':[{'store_id':str(i),'variation_id':'V1','product_id':'P1','sku':'DEMO-1','name':'Synthetic validation product','vendor_id':1,'category':'Demo','history_start':'2026-04-08','sales':['2']*84,'sales_complete':True,'availability':['IN_STOCK']*84,'verified_availability':['IN_STOCK']*84,'on_hand':'0','incoming':'0','unit_cost':'3.50','pack':5,'lifecycle':'ACTIVE','source_records':[{'source':'synthetic','id':f'source-{i}','version':1,'occurred_at':NOW.isoformat(),'known_at':NOW.isoformat(),'data':{'daily_sales':['2']*84}}]} for i in range(1,5)]}
@pytest.fixture
def store(tmp_path):
    result=Store('sqlite:///'+str(tmp_path/'evidence.sqlite'));result.initialize();yield result;result.engine.dispose()
def outcome(horizon=28):
    start=date(2026,7,2);end=start+timedelta(days=horizon)
    return {'horizon':horizon,'observed_at':datetime.combine(end,datetime.min.time(),timezone.utc).replace(hour=12).isoformat(),'cutoff':datetime.combine(end,datetime.min.time(),timezone.utc).replace(hour=12).isoformat(),'revision':'v1','rows':[{'store_id':str(i),'variation_id':'V1','complete':True,'days':[{'date':str(start+timedelta(days=d)),'units':'2','availability':'IN_STOCK'} for d in range(horizon)],'source_records':[{'source':'synthetic','id':str(i),'version':1,'occurred_at':str(end)+'T00:00:00Z','known_at':str(end)+'T10:00:00Z','data':{'daily_units':'2'}}]} for i in range(1,5)]}

def test_exact_models_and_policy(store):
    ident=forecast(store,snapshot(),now=NOW);payload=store.get_run(ident)['payload']
    for r in payload['forecasts']:
        for m in MODELS:assert r['models'][m]['rate']=='2' and r['models'][m]['demand28']=='56' and r['models'][m]['demand56']=='112'
    for m in MODELS:assert payload['purchases'][0]['models'][m]['quantity']==450 and F(payload['purchases'][0]['models'][m]['cost'])==1575
    assert store_need('2',{'on_hand':'56','incoming':'0'})['quantity']==56
    assert store_need('2',{'on_hand':'56.1','incoming':'0'})['quantity']==0
    assert store_need('2',{'on_hand':'-1.5','incoming':'0'})['quantity']==114

def test_audit_definitions():
    r=snapshot()['rows'][0];r['sales']=['4']*42+['0']*42;r['verified_availability']=['IN_STOCK']*42+['STOCKOUT']*42
    assert F(calculate('A_RAW84',r)['rate'])==2
    assert F(calculate('C_CONSERVATIVE',r)['rate'])==F('2.5')
    assert F(calculate('D_VERIFIED',r)['rate'])==4
    r['verified_availability']=['UNKNOWN']*71+['IN_STOCK']*13
    assert calculate('D_VERIFIED',r)['rate'] is None and F(calculate('C_CONSERVATIVE',r)['rate'])==2

def test_current_matches_production_1500_cases():
    from app.services.purchase_order_math_service import DemandEvidence,forecast_demand
    rng=random.Random(20261022)
    for i in range(1500):
        r=snapshot()['rows'][0];r['sales']=[str(rng.choice([0,0,1,2,3,8,60])) for _ in range(84)];r['availability']=[rng.choice(['IN_STOCK','STOCKOUT','UNKNOWN']) for _ in range(84)];r['on_hand']=str(rng.choice([-2,0,30,100]));r['incoming']=str(rng.choice([0,6,40]));r['product_age_days']=rng.choice([None,0,5,15,27,42,100]);r['lead_time_days']=rng.choice([None,0,50,70]);r['reliable_stock_age_days']=rng.choice([None,180]);r['lifecycle']=rng.choice(['ACTIVE']*9+['ARCHIVED'])
        expected=forecast_demand(DemandEvidence(tuple(Decimal(x) for x in r['sales']),Decimal(r['on_hand']),incoming=Decimal(r['incoming']),availability=tuple({'IN_STOCK':True,'STOCKOUT':False,'UNKNOWN':None}[v] for v in r['availability']),product_age_days=r['product_age_days'],lead_time_days=r['lead_time_days'],reliable_stock_age_days=r['reliable_stock_age_days'],lifecycle=r['lifecycle'],reorder_weeks=4,target_weeks=8))
        actual=calculate('B_CURRENT',r);policy=store_need(actual['rate'],r)
        assert policy['quantity']==expected.quantity,(i,r,actual,expected)
        if actual['rate'] is not None:
            assert Decimal(round(F(actual['rate'])*10000))/10000==expected.daily_velocity
            assert policy['target']==expected.target_level and policy['reorder_level']==expected.reorder_level

def test_retry_restart_and_conflict(store):
    s=snapshot();ident=forecast(store,s,now=NOW);assert forecast(store,s,now=NOW)==ident
    restarted=Store(str(store.url));assert restarted.get_run(ident)['payload']['snapshot']==s;assert len(restarted.list_runs())==1
    s['rows'][0]['sales'][0]='3'
    with pytest.raises(Conflict):forecast(store,s,now=NOW)
    assert store.failures()

@pytest.mark.parametrize('table',['fv_runs','fv_evaluations','fv_attempts'])
@pytest.mark.parametrize('verb',['DELETE FROM','UPDATE'])
def test_append_only(store,table,verb):
    ident=forecast(store,snapshot(),now=NOW);evaluate(store,ident,outcome(),now=datetime(2026,10,1,tzinfo=timezone.utc))
    sql=f'{verb} {table}'+(" SET id='bad'" if verb=='UPDATE' else '')
    with pytest.raises(Exception):
        with store.engine.begin() as c:c.execute(text(sql))
    assert len(store.list_runs())==1

@pytest.mark.parametrize('change',[lambda s:s.update(cutoff='2026-07-02T18:00:00Z'),lambda s:s.update(kind='PROSPECTIVE'),lambda s:s['rows'][0].update(sales=['1']),lambda s:s['rows'][0]['source_records'][0].update(known_at='2026-07-02T00:00:00Z'),lambda s:s['rows'].pop()])
def test_invalid_or_future_evidence_publishes_nothing(store,change):
    s=snapshot();change(s)
    with pytest.raises(EvidenceError):forecast(store,s,now=NOW+timedelta(days=1))
    assert not store.list_runs() and store.failures()

def test_missing_sources_explicit(store):
    s=snapshot();s['rows'][0]['sales_complete']=False;s['rows'][1]['on_hand']=None;s['rows'][2]['verified_availability']=['UNKNOWN']*84
    ident=forecast(store,s,now=NOW);p=store.get_run(ident)['payload']
    assert all(v['rate'] is None for v in p['forecasts'][0]['models'].values())
    assert p['purchases'][0]['models']['B_CURRENT']['quantity'] is None
    assert p['forecasts'][2]['models']['D_VERIFIED']['rate'] is None

def test_atomic_failure_and_retry(store):
    s=snapshot()
    with pytest.raises(TimeoutError):forecast(store,s,now=NOW,budget_seconds=-1)
    assert not store.list_runs();assert forecast(store,s,now=NOW)

def test_mature_metrics_revisions_censoring(store):
    ident=forecast(store,snapshot(),now=NOW);o=outcome()
    with pytest.raises(EvidenceError):evaluate(store,ident,o,now=NOW)
    eid=evaluate(store,ident,o,now=datetime(2026,10,1,tzinfo=timezone.utc));assert evaluate(store,ident,o,now=datetime(2026,10,1,tzinfo=timezone.utc))==eid
    e=store.list_evaluations(ident)[0]['payload'];assert len(e['cases'])==16
    assert all(m['wape']==0 for m in e['metrics'] if m['n'])
    o['revision']='v2';o['supersedes_id']=eid
    for d in o['rows'][0]['days'][:10]:d.update(units='0',availability='STOCKOUT')
    for d in o['rows'][1]['days']:d['availability']='UNKNOWN'
    evaluate(store,ident,o,now=datetime(2026,10,1,tzinfo=timezone.utc));latest=store.list_evaluations(ident)[-1]['payload']
    assert latest['classification']=='INCOMPLETE' and len(latest['cases'])==12
    assert all(c['actual']=='36' for c in latest['cases'] if c['store']=='1')
    assert store.list_evaluations(ident)[0]['payload']==e

def test_evaluation_missing_rows_never_zero(store):
    ident=forecast(store,snapshot(),now=NOW);o=outcome(56);o['rows']=[]
    evaluate(store,ident,o,now=datetime(2026,10,1,tzinfo=timezone.utc));e=store.list_evaluations(ident)[0]['payload']
    assert e['cases']==[] and e['classification']=='INCOMPLETE'

def test_business_database_rejected():
    with pytest.raises(EvidenceError):Store('postgresql+psycopg://reader@localhost/business',business_url='postgresql://writer@localhost/business')

def test_read_budget():
    from app.forecast_validation.cli import BoundedRead
    calls=[];r=BoundedRead(lambda *a,**kw:calls.append(kw),max_calls=2)
    r('/read',{});r('/read',{})
    with pytest.raises(TimeoutError):r('/read',{})
    assert len(calls)==2 and all(c['timeout_seconds']<=15 for c in calls)

def test_forward_exposure_never_backcasts_and_marks_partial():
    from app.forecast_validation.exposure import normalize,states,closing_balance
    from app.forecast_validation.contracts import TZ
    def event(kind,ident,when,qty,**fields):return {'type':kind,('physical_count' if kind=='PHYSICAL_COUNT' else 'adjustment'):{'id':ident,'location_id':'L1','catalog_object_id':'V1','quantity':str(qty),'occurred_at':when,'created_at':when,'state':'IN_STOCK',**fields}}
    changes=[event('PHYSICAL_COUNT','p','2026-04-09T10:00:00-07:00',4),event('ADJUSTMENT','a','2026-04-11T10:00:00-07:00',4,from_state='IN_STOCK',to_state='SOLD')]
    ledger=normalize(changes,NOW)[('L1','V1')]
    assert states(ledger,[date(2026,4,8)+timedelta(days=i) for i in range(5)])==['UNKNOWN','UNKNOWN','IN_STOCK','PARTIAL','STOCKOUT']
    assert closing_balance(ledger,NOW)==0
    assert states([], [date(2026,4,8)])==['UNKNOWN']


def test_inventory_history_gateway_pagination_and_boundary():
    from app.forecast_validation.exposure import EvidenceGateway
    calls=[]
    def read(path,payload):calls.append((path,payload));return {'changes':[],'cursor':'repeat'}
    gateway=EvidenceGateway(post=read)
    with pytest.raises(EvidenceError,match='Repeated'):gateway.fetch_verified_inventory_events(['L1'],['V1'],NOW-timedelta(days=84),NOW)
    assert len(calls)==2 and all(c[0]=='/v2/inventory/changes/batch-retrieve' for c in calls)
