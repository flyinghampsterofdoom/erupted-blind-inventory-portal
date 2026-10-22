"""Disposable local PostgreSQL only. Production credentials are never loaded."""
import json,subprocess,time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from datetime import datetime,timedelta,timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4
import pytest
from fastapi import FastAPI,Request
from fastapi.testclient import TestClient
from sqlalchemy import text,create_engine,event,select
from sqlalchemy.orm import Session
from test_purchasing_receiving_extension import database_template,receiving_db
from test_forecast_validation import snapshot,outcome,NOW
from app.forecast_validation.storage import Store
from app.forecast_validation.runner import forecast
from app.forecast_validation.capture import capture
from app.forecast_validation.contracts import EvidenceError
from app.config import settings
from app import models

@pytest.fixture
def evidence_pg(database_template):
    admin,url,_=database_template;name='erupted_purchasing_p1_forecast_'+uuid4().hex
    with admin.connect() as c:c.execute(text(f'CREATE DATABASE "{name}"'))
    store=Store(url.set(database=name).render_as_string(hide_password=False));store.initialize()
    try:yield store
    finally:
        store.engine.dispose()
        with admin.connect() as c:c.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))

def test_pg_concurrency_immutable_backup_restore(evidence_pg,database_template,tmp_path):
    store=evidence_pg
    with ThreadPoolExecutor(max_workers=4) as pool:ids=list(pool.map(lambda _:forecast(store,snapshot(),now=NOW),range(8)))
    assert len(set(ids))==1 and len(store.list_runs())==1
    for sql in ['UPDATE fv_runs SET kind=\'RETROSPECTIVE\'','DELETE FROM fv_runs','TRUNCATE fv_runs']:
        with pytest.raises(Exception):
            with store.engine.begin() as c:c.execute(text(sql))
    admin,url,_=database_template;restored='erupted_purchasing_p1_restore_'+uuid4().hex;dump=tmp_path/'evidence.dump'
    pg='/opt/homebrew/opt/postgresql@16/bin/'
    subprocess.run([pg+'pg_dump','-Fc','-h','localhost','-U',url.username or 'justinrawlinson','-d',store.url.database,'-f',str(dump)],check=True,capture_output=True)
    with admin.connect() as c:c.execute(text(f'CREATE DATABASE "{restored}"'))
    try:
        subprocess.run([pg+'pg_restore','-h','localhost','-U',url.username or 'justinrawlinson','-d',restored,str(dump)],check=True,capture_output=True)
        recovered=Store(url.set(database=restored).render_as_string(hide_password=False))
        assert recovered.get_run(ids[0])==store.get_run(ids[0])
        with pytest.raises(Exception):
            with recovered.engine.begin() as c:c.execute(text('DELETE FROM fv_runs'))
        recovered.engine.dispose()
    finally:
        with admin.connect() as c:c.execute(text(f'DROP DATABASE "{restored}" WITH (FORCE)'))

def test_capture_read_only_business_unchanged_and_unknown(receiving_db):
    from app.services.v2_ordering_square_gateway import SquareProductMetadata,SquareInventoryCount,SquareInventoryCountReadResult
    now=datetime.now(timezone.utc);catalog={'VAR1':SquareProductMetadata('VAR1','SKU1','Test','Blue',now-timedelta(days=200),False,vendor_ids=('V1',))}
    class Gateway:
        def fetch_active_catalog(self):return catalog
        def fetch_current_inventory_counts(self,**kw):return SquareInventoryCountReadResult({(f'L{i}','VAR1'):SquareInventoryCount(f'L{i}','VAR1',Decimal(10),now) for i in range(1,5)})
    with Session(receiving_db) as db:
        db.add(models.VendorSkuConfig(vendor_id=1,sku='SKU1',square_variation_id='VAR1',unit_cost=3,pack_size=5));db.commit()
        before=db.execute(text('SELECT row_to_json(p) FROM purchase_orders p ORDER BY id')).scalars().all()
        with pytest.raises(EvidenceError,match='read-only'):capture(db,Gateway())
    read_engine=create_engine(receiving_db.url,connect_args={'options':'-c default_transaction_read_only=on'})
    seen=[]
    @event.listens_for(read_engine,'before_cursor_execute')
    def record(c,cu,statement,*args):seen.append(statement)
    try:
        with Session(read_engine) as db:s=capture(db,Gateway())
        assert len(s['rows'])==4 and all(not r['sales_complete'] and r['verified_availability']==['UNKNOWN']*84 for r in s['rows'])
        assert all(r['incoming']=='20' and r['pack']==5 for r in s['rows'])
        assert all(q.lstrip().upper().startswith(('SELECT','SHOW')) for q in seen)
        with Session(receiving_db) as db:assert db.execute(text('SELECT row_to_json(p) FROM purchase_orders p ORDER BY id')).scalars().all()==before
    finally:read_engine.dispose()

def test_owner_routes_no_source_calls_and_disabled(receiving_db,evidence_pg,monkeypatch):
    from app.routers import forecast_validation as routes
    from app.db import get_db
    from app.auth import get_current_principal
    ident=forecast(evidence_pg,snapshot(),now=NOW)
    app=FastAPI();app.include_router(routes.router)
    principal=SimpleNamespace(id=1)
    app.dependency_overrides[get_current_principal]=lambda:principal
    def db():
        with Session(receiving_db) as s:yield s
    app.dependency_overrides[get_db]=db
    monkeypatch.setattr(settings,'forecast_validation_enabled',True)
    monkeypatch.setattr(settings,'forecast_validation_database_url',str(evidence_pg.url))
    monkeypatch.setattr(routes,'evidence_store',lambda:Store(str(evidence_pg.url)))
    from app.forecast_validation import runner,capture as capture_module
    monkeypatch.setattr(runner,'forecast',lambda *a,**kw:pytest.fail('Page performed forecasting'))
    monkeypatch.setattr(capture_module,'capture',lambda *a,**kw:pytest.fail('Page accessed sources'))
    client=TestClient(app);started=time.perf_counter()
    for _ in range(10):
        response=client.get('/management/forecast-validation',params={'sku':'DEMO-1'});assert response.status_code==200
    elapsed=(time.perf_counter()-started)/10
    assert elapsed<1
    assert 'SYNTHETIC' in response.text and 'SKU Investigation' in response.text
    assert all(label in response.text for label in ('Raw 84-Day Sales','Current Production Algorithm','Conservative Adjustment','Verified Availability'))
    assert '<td>A_RAW84</td>' not in response.text
    assert client.get('/management/forecast-validation/evidence/'+ident).json()['run']['payload']['snapshot']==snapshot()
    principal.id=2;assert client.get('/management/forecast-validation').status_code==403
    principal.id=3;assert client.get('/management/forecast-validation/evidence/'+ident).status_code==403
    monkeypatch.setattr(settings,'forecast_validation_enabled',False)
    principal.id=1;assert client.get('/management/forecast-validation').status_code==404
    assert client.post('/management/forecast-validation').status_code==405
    print(f'Owner dashboard mean GET: {elapsed*1000:.1f}ms; zero source/forecast calls')

def test_ordinary_purchasing_route_has_no_added_work(receiving_db,monkeypatch):
    monkeypatch.setattr(settings,'purchasing_v3_phase2_enabled',True)
    from app.main import app as application
    from app.routers import purchasing,management,forecast_validation as shadow
    from app.db import get_db
    from app.auth import Principal,Role
    import statistics
    from app.forecast_validation import capture as source,runner
    monkeypatch.setattr(source,'capture',lambda *a,**kw:pytest.fail('Purchasing accessed shadow sources'))
    monkeypatch.setattr(runner,'forecast',lambda *a,**kw:pytest.fail('Purchasing ran shadow forecasts'))
    monkeypatch.setattr(shadow,'evidence_store',lambda:pytest.fail('Purchasing opened evidence database'))
    with Session(receiving_db) as db:
        measurements=[];statements=[]
        for include_shadow in (False,True):
            app=FastAPI();app.state.templates=application.state.templates;app.include_router(purchasing.router)
            if include_shadow:app.include_router(shadow.router)
            app.dependency_overrides[get_db]=lambda:db
            app.dependency_overrides[management.ordering_access]=lambda:Principal(1,'owner',Role.ADMIN,None,True)
            client=TestClient(app);times=[];count=[]
            def record(*args):count.append(1)
            event.listen(receiving_db,'before_cursor_execute',record)
            try:
                for i in range(35):
                    before=len(count);start=time.perf_counter();response=client.get('/management/purchasing');elapsed=time.perf_counter()-start
                    assert response.status_code==200
                    if i>=5:times.append(elapsed);statements.append((include_shadow,len(count)-before))
            finally:event.remove(receiving_db,'before_cursor_execute',record)
            measurements.append(statistics.median(times)*1000)
        baseline={n for flag,n in statements if not flag};after={n for flag,n in statements if flag}
        assert baseline==after
        # No strict microbenchmark threshold: same SQL work is the stable regression assertion.
        print(f'Ordinary purchasing GET median (30 measured after 5 warmups): baseline={measurements[0]:.2f}ms with shadow route={measurements[1]:.2f}ms; SQL/request={baseline}; shadow calls=0')

def test_certified_capture_retains_fractional_sales_and_raw_anchor(receiving_db,evidence_pg):
    from app.services.v2_ordering_square_gateway import SquareProductMetadata,SquareInventoryCount,SquareInventoryCountReadResult
    from app.forecast_validation.contracts import TZ,validate
    now=datetime.now(timezone.utc);today=now.astimezone(TZ).date();first=today-timedelta(days=84);anchor=datetime.combine(first+timedelta(days=5),datetime.min.time(),TZ)+timedelta(hours=10)
    sale=datetime.combine(today-timedelta(days=1),datetime.min.time(),TZ)+timedelta(hours=12)
    with Session(receiving_db) as db:
        db.add(models.VendorSkuConfig(vendor_id=1,sku='SKU1',square_variation_id='VAR1',unit_cost=3,pack_size=5))
        for i in range(1,5):
            for source in ('SALES','MOVEMENTS'):db.add(models.PurchasingHistoryCoverage(source=source,location_id=f'L{i}',start_at=datetime.combine(first,datetime.min.time(),TZ),through_at=now,verified_at=now))
        db.add(models.ConsignmentSaleFact(id=1,square_order_id='S1',square_line_item_uid='LINE1',square_variation_id='VAR1',square_location_id='L1',store_id=1,business_date=sale.date(),transacted_at=sale,quantity_sold=Decimal('.5'),gross_sales_amount=1,discount_amount=0,tax_amount=0,net_sales_amount=1,currency='USD',product_name_snapshot='Test',attribution_status='MISSING_COST',attribution_source='SYNTHETIC',source_synchronized_at=now,source_order_version=1))
        db.commit()
    class Gateway:
        def fetch_active_catalog(self):return {'VAR1':SquareProductMetadata('VAR1','SKU1','Test','Blue',now-timedelta(days=200),False,vendor_ids=('V1',))}
        def fetch_current_inventory_counts(self,**kw):return SquareInventoryCountReadResult({(f'L{i}','VAR1'):SquareInventoryCount(f'L{i}','VAR1',Decimal('9.5') if i==1 else Decimal(10),now) for i in range(1,5)})
        def fetch_verified_inventory_events(self,*args):
            return [{'type':'PHYSICAL_COUNT','physical_count':{'id':f'PC{i}','location_id':f'L{i}','catalog_object_id':'VAR1','quantity':'10','state':'IN_STOCK','occurred_at':anchor.isoformat(),'created_at':anchor.isoformat()}} for i in range(1,5)]+[{'type':'ADJUSTMENT','adjustment':{'id':'A1','location_id':'L1','catalog_object_id':'VAR1','quantity':'0.5','from_state':'IN_STOCK','to_state':'SOLD','occurred_at':sale.isoformat(),'created_at':sale.isoformat()}}]
    read_engine=create_engine(receiving_db.url,connect_args={'options':'-c default_transaction_read_only=on'})
    try:
        with Session(read_engine) as db:s=capture(db,Gateway())
        validate(s);assert s['rows'][0]['sales'][-1]=='0.5'
        assert all(r['sales_complete'] and r['verified_availability'].count('IN_STOCK')==78 for r in s['rows'])
        ident=forecast(evidence_pg,s);assert evidence_pg.get_run(ident)['kind']=='PROSPECTIVE'
        assert all(f['models']['D_VERIFIED']['rate'] is not None for f in evidence_pg.get_run(ident)['payload']['forecasts'])
    finally:read_engine.dispose()

def test_runtime_role_cannot_mutate_or_disable_evidence(evidence_pg):
    role='erupted_forecast_test_'+uuid4().hex
    with evidence_pg.engine.begin() as c:
        c.execute(text(f'CREATE ROLE "{role}" NOLOGIN'))
        c.execute(text(f'GRANT USAGE ON SCHEMA public TO "{role}"'))
        c.execute(text(f'GRANT SELECT,INSERT ON fv_runs,fv_evaluations,fv_attempts TO "{role}"'))
    try:
        forecast(evidence_pg,snapshot(),now=NOW)
        with evidence_pg.engine.begin() as c:
            c.execute(text(f'SET LOCAL ROLE "{role}"'));assert c.execute(text('SELECT count(*) FROM fv_runs')).scalar()==1
            c.execute(text("INSERT INTO fv_attempts VALUES ('role-test','2026-10-22','test','COMPLETE','read/append role')"))
        for sql in ['DELETE FROM fv_runs','UPDATE fv_runs SET kind=\'RETROSPECTIVE\'','TRUNCATE fv_runs','ALTER TABLE fv_runs DISABLE TRIGGER ALL']:
            with pytest.raises(Exception):
                with evidence_pg.engine.begin() as c:c.execute(text(f'SET LOCAL ROLE "{role}"'));c.execute(text(sql))
    finally:
        with evidence_pg.engine.begin() as c:
            c.execute(text(f'REVOKE ALL ON fv_runs,fv_evaluations,fv_attempts FROM "{role}"'))
            c.execute(text(f'REVOKE USAGE ON SCHEMA public FROM "{role}"'))
            c.execute(text(f'DROP ROLE "{role}"'))


def test_all_business_tables_unchanged_after_shadow_cycle(receiving_db,evidence_pg):
    def fingerprint():
        with receiving_db.connect() as c:
            names=c.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")).scalars().all()
            return {n:c.execute(text('SELECT md5(coalesce(string_agg(row_to_json(t)::text, chr(10) ORDER BY row_to_json(t)::text),\'\')) FROM '+receiving_db.dialect.identifier_preparer.quote(n)+' t')).scalar() for n in names}
    before=fingerprint();ident=forecast(evidence_pg,snapshot(),now=NOW)
    from app.forecast_validation.evaluation import evaluate
    evaluate(evidence_pg,ident,outcome(),now=datetime(2026,10,1,tzinfo=timezone.utc))
    assert fingerprint()==before
    print(f'All {len(before)} business tables retain identical row-content fingerprints after shadow forecast/evaluation')
