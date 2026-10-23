import json
from datetime import date,timedelta
from fractions import Fraction as F
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from test_forecast_validation import snapshot
from app.forecast_validation.product import build,ProductView,HORIZONS
from app.forecast_validation.product_backtest import rolling
from app.forecast_validation.contracts import MODELS,EvidenceError
from app.forecast_validation.demand import calculate
from app.forecast_validation.policy import store_need,aggregate
from scripts.review_forecast_product import create_review_app
from app.config import settings

@pytest.fixture
def prepared(tmp_path):
    snap=snapshot()
    for r in snap['rows']:
        r['variant']='Sample';r['inventory_observed_at']=snap['origin'];r['inventory_calculated_at']=snap['origin'];r['limitations']=[]
    forecasts=[]
    for r in snap['rows']:
        models={m:calculate(m,r) for m in MODELS}
        forecasts.append({'store_id':r['store_id'],'variation_id':r['variation_id'],'models':models,'policy':{m:store_need(x['rate'],r) for m,x in models.items()}})
    payload={'schema':'forecast-result-v1','snapshot':snap,'model_version':'test','policy_version':'minmax-4-8-exact-v1','forecasts':forecasts}
    source=tmp_path/'frozen.json';source.write_text(json.dumps(payload));before=source.read_bytes();path=tmp_path/'projection.sqlite';meta=build(source,path)
    assert source.read_bytes()==before
    return path,payload,meta

def test_readonly_projection_and_parity(prepared):
    path,p,meta=prepared;v=ProductView(path)
    assert v.listing()[0]['total']==4
    for m in MODELS:
        r=v.detail('1','V1',m);rate=F(p['forecasts'][0]['models'][m]['rate'])
        for h in HORIZONS:assert r['demand'+str(h)]==float(rate*h)
        assert r['need']==p['forecasts'][0]['policy'][m]['quantity']
        expected=aggregate([f['policy'][m] for f in p['forecasts']],p['snapshot']['rows'][0]['pack'],p['snapshot']['rows'][0]['unit_cost'])
        assert v.purchases(model=m)[0]['quantity']==expected['quantity']
    with pytest.raises(Exception):v.db.execute('DELETE FROM products')
    assert v.detail('1','bad','B_CURRENT') is None
    assert v.listing(q="' OR 1=1 --")[0]['total']==0
    assert v.listing(store='1')[0]['total']==1
    assert len(v.listing(page=1)[1])==0
    assert meta['backtests']['winner'] is None
    v.close()

def test_nulls_not_zero_and_date_anchor(prepared,tmp_path):
    path,p,_=prepared;r=p['snapshot']['rows'][0];r['on_hand']=None;r['lead_time_days']=None
    p['forecasts'][0]['models']['B_CURRENT']['rate']=None;p['forecasts'][0]['policy']['B_CURRENT']=store_need(None,r)
    src=tmp_path/'other.json';src.write_text(json.dumps(p));build(src,path)
    v=ProductView(path);x=v.detail('1','V1','B_CURRENT');assert x['days'] is None and x['need'] is None and x['demand7'] is None
    assert v.purchases()[0]['quantity'] is None
    assert v.purchases(store='2')[0]['quantity'] is not None
    assert x['detail']['stockout_date'] is None
    assert x['detail']['facts']['lead_time_days'] is None
    v.close()

@pytest.mark.parametrize('route',['','/product/1/V1','/purchasing-list'])
def test_owner_http_and_no_compute(prepared,monkeypatch,route):
    path,_,_=prepared
    for field in ('forecast_validation_enabled','forecast_product_projection_path','purchasing_owner_principal_id'):monkeypatch.setattr(settings,field,getattr(settings,field))
    app=create_review_app(path,'synthetic-local-password');c=TestClient(app)
    url='/management/forecasting'+route
    assert c.get(url).status_code==401
    c.post('/local-review',data={'password':'synthetic-local-password','account':'7'},follow_redirects=False)
    assert c.get(url).status_code==403
    c.post('/local-review',data={'password':'synthetic-local-password','account':'6'},follow_redirects=False)
    monkeypatch.setattr('app.forecast_validation.demand.calculate',lambda *a,**k:pytest.fail('HTTP must never calculate'))
    response=c.get(url)
    assert response.status_code==200,response.text
    assert response.headers['cache-control']=='no-store'
    assert 'Frozen' in response.text or 'frozen' in response.text
    assert c.post(url).status_code==405
    settings.forecast_validation_enabled=False
    assert c.get(url).status_code==404

def test_validation_filters(prepared,monkeypatch):
    for field in ('forecast_validation_enabled','forecast_product_projection_path','purchasing_owner_principal_id'):monkeypatch.setattr(settings,field,getattr(settings,field))
    c=TestClient(create_review_app(prepared[0],'pass'));c.post('/local-review',data={'password':'pass','account':'6'})
    for query in ['horizon=13','model=FAKE','page=-1','attention=write']:
        assert c.get('/management/forecasting?'+query).status_code==422
    assert c.get('/management/forecasting/product/1/missing').status_code==404
    assert c.get('/management/forecasting?q=%3Cscript%3E').status_code==200

def outcomes_for(snap,exposure='IN_STOCK'):
    start=date.fromisoformat(snap['evaluation_start'])
    return {(str(r['store_id']),r['variation_id'],(start+timedelta(days=i)).isoformat()):{'sales':'2','exposure':exposure,'complete':True} for r in snap['rows'] for i in range(56)}

def test_rolling_synthetic_math_and_censoring():
    s=snapshot();x=rolling([s],outcomes_for(s));assert x['scores']
    assert all(score['mae']==0 for score in x['scores'])
    assert {score['horizon'] for score in x['scores']}==set(HORIZONS)
    assert {score['model'] for score in x['scores']}==set(MODELS)
    assert rolling([s],outcomes_for(s,'UNKNOWN'))['scores']==[]
    assert rolling([s],outcomes_for(s,'STOCKOUT'))['scores']==[]
    assert rolling([s],{})['scores']==[]

def test_future_source_rejected():
    s=snapshot();r=s['rows'][0]
    # Real evidence contract rejects source knowledge arriving after the origin.
    s['kind']='RETROSPECTIVE';r['source_records']=[{'source':'square_catalog_metadata','id':'V1','known_at':'2099-01-01T00:00:00+00:00','occurred_at':s['origin'],'data':{}}]
    with pytest.raises(EvidenceError):rolling([s],{})

# Real SQL authorization on a disposable business DB, not the preview adapter.
from test_purchasing_receiving_extension import database_template,receiving_db

def test_product_authorization_real_db(prepared,receiving_db,monkeypatch):
    from fastapi import FastAPI
    from sqlalchemy.orm import Session
    from app.auth import get_current_principal
    from app.db import get_db
    from app.routers.forecast_product import router
    from types import SimpleNamespace
    monkeypatch.setattr(settings,'forecast_validation_enabled',True)
    monkeypatch.setattr(settings,'forecast_product_projection_path',str(prepared[0]))
    app=FastAPI();app.include_router(router);principal=SimpleNamespace(id=1)
    app.dependency_overrides[get_current_principal]=lambda:principal
    def db():
        with Session(receiving_db) as session:yield session
    app.dependency_overrides[get_db]=db
    c=TestClient(app)
    for route in ('','/product/1/V1','/purchasing-list'):
        principal.id=1;assert c.get('/management/forecasting'+route).status_code==200
        for actor in (2,3,99999):
            principal.id=actor;assert c.get('/management/forecasting'+route).status_code==403

def test_pack_rounds_once_and_categories_are_members(prepared,tmp_path):
    path,p,_=prepared
    for row,fc in zip(p['snapshot']['rows'],p['forecasts']):
        row['pack']=5;row['category']='ONE,TWO'
        for m in MODELS:fc['policy'][m]['quantity']=1
    src=tmp_path/'packs.json';src.write_text(json.dumps(p));build(src,path)
    v=ProductView(path);purchase=v.purchases()[0]
    assert purchase['unrounded']==4 and purchase['quantity']==5
    assert v.listing(category='ONE')[0]['total']==4
    assert v.listing(category='ON')[0]['total']==0
    assert v.facets()['category']==['ONE','TWO']
    assert len(v.purchases(category='TWO'))==1
    v.close()

@pytest.mark.parametrize('horizon',[7,14,28,56])
def test_horizons_exact_product_filter(prepared,horizon):
    v=ProductView(prepared[0])
    assert v.listing(variation='V1')[0]['total']==4
    assert v.purchases(variation='not-in-evidence')==[]
    assert v.detail('1','V1','B_CURRENT')['demand'+str(horizon)]==horizon*2
    v.close()
