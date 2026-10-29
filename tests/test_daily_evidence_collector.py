"""Collector-only release: real disposable PostgreSQL and injected Square reads."""
from datetime import datetime,time,timedelta,timezone
from pathlib import Path
from uuid import uuid4
import json,copy
import pytest
from sqlalchemy import create_engine,text
from app.services.daily_square_collector import Collector,Scope,run_once,ZONE
from app.services.daily_evidence_migration import install
from app.services.evidence_collector_target import ProductionTarget,ROLE
from app.services.daily_evidence_runtime import status
from test_purchasing_receiving_extension import database_template

NOW=datetime(2026,10,29,12,tzinfo=timezone.utc)
SCOPE=Scope(('FIXTURE_V1','FIXTURE_V2'),((2,'L2'),(4,'L4'),(6,'L6'),(9,'L9')))


@pytest.fixture(scope='module')
def collector_template(database_template):
    admin,url,base=database_template;name='erupted_purchasing_p1_daily_template_'+uuid4().hex[:10]
    with admin.connect() as c:
        assert not c.scalar(text('SELECT 1 FROM pg_roles WHERE rolname=:r'),{'r':ROLE})
        c.execute(text('CREATE DATABASE '+name+' TEMPLATE '+base))
    engine=create_engine(url.set(database=name))
    with engine.begin() as c:
        install(c,database_name=name,hostname=str(url.host),owner=str(url.username),password='isolated-fixture-password')
        c.exec_driver_sql("""INSERT INTO principals(id,username,password_hash,role,active) VALUES(6,'fixture-owner','not-login-capable','ADMIN',true);
        INSERT INTO stores(id,name,square_location_id,active) VALUES(2,'Andresen','L2',true),(4,'HWY 99','L4',true),(6,'Longview','L6',true),(9,'SR 503','L9',true);
        INSERT INTO ordering_catalog_identity(square_variation_id,square_item_id,sku,item_name,variation_name,last_seen_at)
        VALUES('FIXTURE_V1','FIXTURE_ITEM','fixture1','Fixture','One',now()),('FIXTURE_V2','FIXTURE_ITEM','fixture2','Fixture','Two',now());
        INSERT INTO vendors(id,square_vendor_id,name,active) VALUES(1,'FIXTURE_SUPPLIER','Fixture supplier',true);
        INSERT INTO vendor_sku_configs(id,vendor_id,sku,square_variation_id,unit_cost,pack_size,is_default_vendor,active) VALUES(1,1,'fixture1','FIXTURE_V1',3,1,true,true);
        INSERT INTO purchase_orders(id,vendor_id,status,created_by_principal_id) VALUES(1,1,'IN_TRANSIT',6);
        INSERT INTO purchase_order_lines(id,purchase_order_id,variation_id,item_name,variation_name,ordered_qty,received_qty_total) VALUES(1,1,'FIXTURE_V1','Fixture','One',100,0);""")
    engine.dispose()
    yield admin,url,name
    with admin.connect() as c:
        c.execute(text('DROP DATABASE '+name+' WITH (FORCE)'));c.execute(text('DROP ROLE '+ROLE))


@pytest.fixture
def collector(collector_template):
    admin,url,template=collector_template;name='erupted_purchasing_p1_daily_'+uuid4().hex[:10]
    with admin.connect() as c:c.execute(text('CREATE DATABASE '+name+' TEMPLATE '+template))
    owner=create_engine(url.set(database=name));worker_url=url.set(database=name,username=ROLE,password='isolated-fixture-password')
    worker=create_engine(worker_url);target=ProductionTarget(worker_url,name,str(url.host))
    yield owner,worker,target
    owner.dispose();worker.dispose()
    with admin.connect() as c:c.execute(text('DROP DATABASE '+name+' WITH (FORCE)'))


class Provider:
    def __init__(self):self.calls=[];self.missing=False;self.fail=False;self.zero=False
    def __call__(self,path,payload):
        self.calls.append(path)
        if path.endswith('counts/batch-retrieve'):
            return dict(counts=[dict(catalog_object_id=v,location_id=l,state='IN_STOCK',quantity='0' if self.zero else '10',calculated_at='2026-10-29T10:00:00Z') for v in payload['catalog_object_ids'] for l in payload['location_ids'] if not self.missing or l!='L9'])
        if path.endswith('orders/search'):return {'orders':[dict(id='REAL_SOURCE_FIXTURE',location_id='L2',state='COMPLETED',version=1,closed_at='2026-10-28T12:00:00Z',updated_at='2026-10-28T13:00:00Z',line_items=[dict(uid='source-line',catalog_object_id='FIXTURE_V1',quantity='2')])]}
        event=dict(type='ADJUSTMENT',adjustment=dict(id='MOVEMENT_FIXTURE',catalog_object_id='FIXTURE_V1',from_location_id='L2',to_location_id='L4',from_state='IN_STOCK',to_state='IN_STOCK',quantity='2',occurred_at='2026-10-28T12:00:00Z',created_at='2026-10-28T12:00:00Z'))
        return {'changes':[event],**({'cursor':'repeat'} if self.fail else {})}


def collect(pair,provider=None,key='fixture'):
    owner,worker,target=pair
    return Collector(worker,provider or Provider(),clock=lambda:NOW,target=target).collect(SCOPE,begin=datetime.combine(NOW.astimezone(ZONE).date(),time(),ZONE)-timedelta(days=126),reason='DAILY',command_key=key)


def fingerprints(engine):
    with engine.connect() as c:
        return {name:c.execute(text(f'SELECT to_jsonb(t)::text FROM {name} t ORDER BY to_jsonb(t)::text')).scalars().all() for name in ('purchase_orders','purchase_order_lines','par_levels','vendor_sku_configs','consignment_sale_facts','consignment_return_facts')}


def test_restricted_worker_and_no_business_write_permissions(collector):
    owner,worker,target=collector
    with worker.connect() as c:target.verify_privileges(c)
    before=fingerprints(owner);assert collect(collector)['qualified_counts']==8
    assert fingerprints(owner)==before
    with owner.connect() as c:
        assert c.scalar(text("SELECT to_regnamespace('purchasing_v4')")) is None
        assert c.scalar(text('SELECT version_num FROM alembic_version'))=='20261027_0053'
        assert c.scalar(text('SELECT count(*) FROM purchasing_evidence.inventory_checkpoint'))==8
        assert c.scalar(text('SELECT count(*) FROM consignment_sale_facts'))==0
        assert c.scalar(text('SELECT quantity FROM purchasing_sale_corrections'))==2
        assert c.execute(text('SELECT location_id,quantity_delta FROM purchasing_evidence.shared_inventory_events ORDER BY location_id')).all()==[('L2',-2),('L4',2)]


def test_worker_cannot_create_order_or_change_manual_settings(collector):
    from sqlalchemy.exc import DBAPIError
    for statement in ('UPDATE purchase_orders SET notes=\'forbidden\'','DELETE FROM vendor_sku_configs','UPDATE par_levels SET manual_par_level=0','UPDATE consignment_sale_facts SET quantity_sold=0'):
        with pytest.raises(DBAPIError):
            with collector[1].begin() as c:c.execute(text(statement))


def test_complete_command_replay_does_not_duplicate_sources(collector):
    provider=Provider();assert collect(collector,provider)['status']=='COMPLETE';calls=len(provider.calls)
    assert collect(collector,provider)['status']=='ALREADY_COLLECTED' and len(provider.calls)==calls
    with collector[0].connect() as c:
        assert c.scalar(text('SELECT count(*) FROM purchasing_evidence.inventory_checkpoint'))==8
        assert c.scalar(text('SELECT count(*) FROM purchasing_evidence.movement_leg'))==2


def test_failed_pagination_is_not_published_and_can_retry(collector):
    provider=Provider();provider.fail=True;before=fingerprints(collector[0])
    with pytest.raises(ValueError,match='cursor'):collect(collector,provider)
    assert fingerprints(collector[0])==before
    with collector[0].connect() as c:
        assert c.scalar(text('SELECT count(*) FROM purchasing_evidence.inventory_checkpoint'))==0
        assert c.scalar(text("SELECT count(*) FROM purchasing_evidence.collection_attempt WHERE status='FAILED'"))==1
    provider.fail=False;assert collect(collector,provider)['status']=='COMPLETE'


def test_missing_inventory_stays_unknown_then_partial_retry_recovers(collector):
    provider=Provider();provider.missing=True;assert collect(collector,provider)['unknown_counts']==2
    with collector[0].connect() as c:
        assert c.scalar(text("SELECT count(*) FROM purchasing_evidence.inventory_checkpoint WHERE state='UNKNOWN' AND quantity IS NULL"))==2
        result=status(c,now=NOW);assert result['state']=='incomplete' and result['unknown_inventory_pairs']==2
    provider.missing=False;assert collect(collector,provider)['status']=='COMPLETE'
    with collector[0].connect() as c:assert c.scalar(text('SELECT quantity FROM purchasing_sale_corrections'))==2


def test_explicit_zero_is_retained_as_known(collector):
    provider=Provider();provider.zero=True;assert collect(collector,provider)['qualified_counts']==8
    with collector[0].connect() as c:assert c.scalar(text("SELECT count(*) FROM purchasing_evidence.inventory_checkpoint WHERE state='KNOWN' AND quantity=0"))==8


def test_runtime_runner_does_not_shadow_database_target(collector):
    owner,worker,target=collector
    r=run_once(worker,Provider(),reason='RECOVERY',now=NOW,clock=lambda:NOW,target=target)
    assert r['status']=='COMPLETE'


def test_newer_employee_inventory_observation_is_not_overwritten(collector):
    from app.services.v2_ordering_inventory_repository import persist_inventory_refresh,InventoryObservation
    from app.models import OrderingInventoryRefreshRun
    from sqlalchemy.orm import Session
    owner,worker,target=collector
    with owner.begin() as c:
        db=Session(bind=c);run=OrderingInventoryRefreshRun(correlation_id=str(uuid4()),result='COMPLETE',expected_variation_count=1,active_store_count=1,expected_pair_count=1,covered_pair_count=1,missing_pair_count=0,square_request_count=1,started_at=NOW,completed_at=NOW+timedelta(minutes=1),refreshed_by_principal_id=6)
        persist_inventory_refresh(db,run=run,observations=(InventoryObservation('FIXTURE_V1',2,'L2',99,NOW),),refreshed_at=NOW+timedelta(minutes=1));db.close()
    assert collect(collector)['unknown_counts']==1
    with owner.connect() as c:
        assert c.execute(text("SELECT counted_quantity,freshness_state FROM ordering_current_inventory WHERE square_variation_id='FIXTURE_V1' AND store_id=2")).one()==(99,'FRESH')
        assert c.scalar(text("SELECT reason FROM purchasing_evidence.inventory_checkpoint WHERE variation_id='FIXTURE_V1' AND store_id=2"))=='SUPERSEDED_BY_NEWER_OBSERVATION'


def test_caller_cannot_switch_target_database_or_role(collector):
    with pytest.raises(PermissionError):ProductionTarget(collector[0].url,collector[0].url.database,str(collector[0].url.host))
    with pytest.raises(PermissionError):ProductionTarget(collector[1].url,'other-database',str(collector[1].url.host))


def test_collector_rejects_square_inventory_writes_before_transport(collector):
    from app.services.square_request_policy import SquareWriteBoundaryViolation
    provider=Provider();c=Collector(collector[1],provider,target=collector[2])
    with pytest.raises(SquareWriteBoundaryViolation):c.pages_for('/v2/inventory/changes/batch-create',{'changes':[]},'changes')
    assert provider.calls==[]


def test_run_status_distinguishes_complete_stale_and_failed(collector):
    collect(collector)
    with collector[0].connect() as c:
        r=status(c,now=NOW);assert r['state']=='current' and r['coverage_complete'] is True and r['employee_login_required'] is False
        r=status(c,now=NOW+timedelta(days=2));assert r['state']=='stale' and r['coverage_complete'] is False
    failed=Provider();failed.fail=True
    later=NOW+timedelta(minutes=1)
    with pytest.raises(ValueError):Collector(collector[1],failed,clock=lambda:later,target=collector[2]).collect(SCOPE,begin=datetime.combine(NOW.astimezone(ZONE).date(),time(),ZONE)-timedelta(days=126),reason='MANUAL',command_key='failed-after-complete')
    with collector[0].connect() as c:assert status(c,now=later)['state']=='failed'


def test_rollback_restores_original_legacy_certificates_and_retains_history(collector):
    from app.services.daily_evidence_rollback import restore_legacy_certificates
    owner,worker,target=collector
    assert collect(collector)['status']=='COMPLETE'
    with owner.begin() as c:
        pages=c.scalar(text('SELECT count(*) FROM purchasing_evidence.collection_page'))
        result=restore_legacy_certificates(c,database_name=str(owner.url.database),hostname=str(owner.url.host),owner=str(owner.url.username))
        assert result['source_facts_deleted']==0
        assert c.scalar(text('SELECT count(*) FROM purchasing_history_coverage'))==0  # Fixture began without certificates.
        assert c.scalar(text('SELECT count(*) FROM purchasing_evidence.collection_page'))==pages
        assert c.scalar(text('SELECT count(*) FROM purchasing_evidence.inventory_checkpoint'))==8
        assert c.scalar(text('SELECT count(*) FROM purchasing_evidence.movement_leg'))==2


def test_later_legacy_read_cannot_erase_native_cross_location_legs(collector):
    collect(collector)
    with collector[0].begin() as c:
        c.execute(text("""INSERT INTO purchasing_inventory_events(event_type,event_id,location_id,variation_id,occurred_at,business_date,quantity_delta,physical_count,source_updated_at,calculated_after_at,calculated_before_at,source_digest,synchronized_at)
        VALUES('ADJUSTMENT','MOVEMENT_FIXTURE','L2','FIXTURE_V1','2026-10-28T12:00Z','2026-10-28',-2,false,'2026-10-28T12:00Z','2026-10-28T12:00Z','2026-10-29T12:00Z',:digest,:later)"""),{'digest':'a'*64,'later':NOW+timedelta(hours=1)})
        assert c.scalar(text("SELECT count(*) FROM purchasing_evidence.shared_inventory_events WHERE event_id='MOVEMENT_FIXTURE'"))==2


def test_source_version_is_pinned_only_for_reads(monkeypatch):
    from app.services import square_ordering_data_service as transport
    from app.services.daily_evidence_runtime import source_post
    calls=[]
    def read(path,payload,**kwargs):calls.append(kwargs);return {}
    monkeypatch.setattr(transport,'_square_post',read)
    assert source_post('/v2/orders/search',{})=={}
    assert calls==[{'timeout_seconds':30,'read_api_version':'2026-09-16'}]
    with pytest.raises(PermissionError):transport._square_request('POST','/v2/inventory/changes/batch-create',{'changes':[{'type':'PHYSICAL_COUNT','physical_count':{'catalog_object_id':'FIXTURE_V1','location_id':'L2','quantity':'1'}}]},inventory_quantity_write=True,read_api_version='2026-09-16')


def test_prior_inventory_quantity_and_timestamp_are_retained_before_projection_update(collector):
    from app.services.v2_ordering_inventory_repository import persist_inventory_refresh,InventoryObservation
    from app.models import OrderingInventoryRefreshRun
    from sqlalchemy.orm import Session
    from decimal import Decimal
    owner,worker,target=collector;old=NOW-timedelta(days=5)
    with owner.begin() as c:
        db=Session(bind=c);run=OrderingInventoryRefreshRun(correlation_id=str(uuid4()),result='COMPLETE',expected_variation_count=1,active_store_count=1,expected_pair_count=1,covered_pair_count=1,missing_pair_count=0,square_request_count=1,started_at=old,completed_at=old,refreshed_by_principal_id=6)
        persist_inventory_refresh(db,run=run,observations=(InventoryObservation('FIXTURE_V1',2,'L2',Decimal(7),old),),refreshed_at=old);db.close()
    collect(collector)
    with owner.connect() as c:
        payload=c.scalar(text("SELECT payload FROM purchasing_evidence.collection_page WHERE payload->>'source_kind'='LEGACY_CURRENT_INVENTORY'"))
        assert len(payload['rows'])==1 and Decimal(payload['rows'][0]['counted_quantity'])==7
        assert datetime.fromisoformat(payload['rows'][0]['refreshed_at'])==old
        assert c.scalar(text("SELECT counted_quantity FROM ordering_current_inventory WHERE square_variation_id='FIXTURE_V1' AND store_id=2"))==10


def test_manual_refresh_cannot_start_large_initial_backfill_in_web_request(collector,monkeypatch):
    import app.services.daily_evidence_runtime as runtime
    calls=[]
    def invoke(*args,**kwargs):calls.append(kwargs);return {'status':'COMPLETE'}
    monkeypatch.setattr(runtime,'run_once',invoke)
    refresh=runtime.Refresh(collector[1],collector[2])
    assert refresh(reason='MANUAL')['status']=='INITIAL_RECOVERY_REQUIRED' and calls==[]
    collect(collector)
    assert refresh(reason='MANUAL')['status']=='COMPLETE' and calls[0]['reason']=='MANUAL'
