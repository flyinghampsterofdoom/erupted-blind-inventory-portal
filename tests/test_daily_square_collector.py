"""Daily source continuity, isolated PostgreSQL publication and unchanged forecast rules."""
from copy import deepcopy
from datetime import datetime,date,time,timedelta,timezone
from decimal import Decimal
import json
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from app.services.daily_square_collector import (Collector,Scope,normalize_changes,daily_key,instant,run_once,ZONE,SCHEDULE)
from app.purchasing_v4.schema import migrate_daily_evidence,migrate_integration,migrate_email,migrate_worksheets
from app.purchasing_v4.evidence_rules import coverage_status,availability_projection
from app.purchasing_v4.contracts import EvidenceState,QuantityEvidence,Availability
from app.services.purchasing_history_service import purchasing_source_orders,daily_sales,certify
from test_purchasing_v4_postgres import template,engine,reading

NOW=instant('2026-10-27T12:00:00Z')
BEGIN=datetime(2026,6,23,tzinfo=ZONE)
SCOPE=Scope(('FIXTURE_V1','FIXTURE_V2'),((1,'FIXTURE_LOC1'),(2,'FIXTURE_LOC2'),(3,'FIXTURE_LOC3'),(4,'FIXTURE_LOC4')))


def event(eid='source-event',fr='FIXTURE_LOC1',to='FIXTURE_LOC2',occurred='2026-10-25T12:00:00Z',quantity='3'):
    return dict(type='ADJUSTMENT',adjustment=dict(id=eid,catalog_object_id='FIXTURE_V1',from_location_id=fr,to_location_id=to,
        from_state='IN_STOCK',to_state='IN_STOCK' if fr!=to else 'SOLD',quantity=quantity,occurred_at=occurred,created_at=occurred))


def order(version=1,quantity='3',state='COMPLETED',uid='source-line'):
    return dict(id='source-order',version=version,state=state,location_id='FIXTURE_LOC1',closed_at='2026-10-25T12:00:00Z',updated_at='2026-10-26T12:00:00Z',
        line_items=[dict(uid=uid,catalog_object_id='FIXTURE_V1',quantity=quantity)])


class Provider:
    def __init__(self):self.requests=[];self.fail=False;self.missing=False
    def __call__(self,path,payload):
        self.requests.append((path,deepcopy(payload)))
        if path.endswith('counts/batch-retrieve'):
            return {'counts':[dict(catalog_object_id=vid,location_id=loc,state='IN_STOCK',quantity='9',calculated_at='2026-10-27T10:00:00Z')
                for vid in payload['catalog_object_ids'] for loc in payload['location_ids'] if not self.missing or loc!='FIXTURE_LOC4']}
        if path=='/v2/orders/search':return {'orders':[order()]}
        if self.fail:return {'changes':[event()], 'cursor':'repeated'}
        return {'changes':[event()]}


@pytest.fixture
def collected_engine(engine):
    with engine.begin() as c:
        migrate_daily_evidence(c)
        for table in ('ordering_inventory_refresh_runs','consignment_sale_facts'):
            c.execute(text(f"SELECT setval(pg_get_serial_sequence('{table}','id'),(SELECT max(id) FROM {table}),true)"))
        c.execute(text("INSERT INTO stores(id,name,square_location_id,active) VALUES (3,'Fixture Three','FIXTURE_LOC3',true),(4,'Fixture Four','FIXTURE_LOC4',true)"))
    return engine


def collect(engine,provider=None,key='fixture-daily'):
    return Collector(engine,provider or Provider(),clock=lambda:NOW).collect(SCOPE,begin=BEGIN,reason='DAILY',command_key=key)


def source_fingerprint(engine):
    tables=['ordering_current_inventory','ordering_inventory_refresh_runs','purchasing_history_coverage','purchasing_sale_corrections','purchasing_order_source_state']
    with engine.connect() as c:return {t:c.execute(text(f'SELECT to_jsonb(t) FROM public.{t} t ORDER BY to_jsonb(t)::text')).scalars().all() for t in tables}


def test_daily_schedule_is_uninstalled_local_calendar_not_elapsed_seconds():
    assert not SCHEDULE['enabled'] and SCHEDULE['hour']==12
    assert daily_key('2026-11-01T11:59:59Z') is None
    assert daily_key('2026-11-01T12:00:00Z')=='daily:2026-11-01'
    assert daily_key('2026-03-08T11:00:00Z')=='daily:2026-03-08'
    assert daily_key('2026-10-27T11:00:00Z')=='daily:2026-10-27'


@pytest.mark.parametrize('value',[None,False,'2026-10-27','2026-10-27T12:00:00'])
def test_missing_source_timestamps_rejected(value):
    with pytest.raises(ValueError):instant(value)


def test_fractional_source_times_and_cross_location_real_ids():
    assert instant('2026-06-24T18:23:35.99Z').microsecond==990000
    rows=normalize_changes([event(),event()],scope=SCOPE,begin=BEGIN,anchor=NOW,observed=NOW)
    assert len(rows)==2 and {r['event_id'] for r in rows}=={'source-event'}
    assert {r['location_id']:r['quantity_delta'] for r in rows}=={'FIXTURE_LOC1':-3,'FIXTURE_LOC2':3}
    conflict=event(quantity='4')
    with pytest.raises(ValueError):normalize_changes([event(),conflict],scope=SCOPE,begin=BEGIN,anchor=NOW,observed=NOW)


def test_daily_checkpoints_replay_and_shared_read_projection(collected_engine):
    engine=collected_engine;provider=Provider();first=collect(engine,provider)
    assert first['status']=='COMPLETE' and first['qualified_counts']==8
    assert len(provider.requests)==5
    assert any('updated_at' in json.dumps(p) for path,p in provider.requests)
    before=source_fingerprint(engine)
    assert collect(engine,provider)['status']=='ALREADY_COLLECTED'
    assert len(provider.requests)==5 and source_fingerprint(engine)==before
    with engine.connect() as c:
        rows=c.execute(text("SELECT location_id,quantity_delta,event_id FROM purchasing_v4.shared_inventory_events WHERE event_id='source-event' ORDER BY location_id")).all()
        assert rows==[('FIXTURE_LOC1',Decimal(-3),'source-event'),('FIXTURE_LOC2',Decimal(3),'source-event')]
        assert c.scalar(text('SELECT count(*) FROM purchasing_v4.inventory_checkpoint'))==8
        assert c.scalar(text("SELECT count(*) FROM purchasing_v4.collection_page"))==6
        assert c.scalar(text("SELECT count(*) FROM purchasing_v4.collection_page WHERE payload->>'source_kind'='LEGACY_CURRENT_INVENTORY'"))==1
        assert c.scalar(text("SELECT quantity FROM purchasing_sale_corrections WHERE square_order_id='source-order'"))==3
        assert c.scalar(text("SELECT count(*) FROM consignment_sale_facts WHERE square_order_id='source-order'"))==0
        assert c.scalar(text('SELECT refreshed_by_principal_id FROM ordering_inventory_refresh_runs ORDER BY id DESC LIMIT 1')) is None
    # Existing factories/migration entry points accept the additive head without downgrading.
    with engine.begin() as c:
        for migrate in (migrate_integration,migrate_email,migrate_worksheets):migrate(c)
        assert c.scalar(text('SELECT version_num FROM purchasing_v4.alembic_version'))=='d1_0001'


def test_missing_counts_remain_unknown_without_zero_fill(collected_engine):
    provider=Provider();provider.missing=True;result=collect(collected_engine,provider)
    assert result['status']=='PARTIAL' and result['unknown_counts']==2
    with collected_engine.connect() as c:
        rows=c.execute(text("SELECT state,quantity,reason FROM purchasing_v4.inventory_checkpoint WHERE store_id=4")).all()
        assert rows==[('UNKNOWN',None,'MISSING_EXPLICIT_COUNT')]*2
        assert c.scalar(text('SELECT count(*) FROM ordering_current_inventory WHERE store_id=4'))==0


def test_partial_paginated_failure_has_no_published_source_scope_and_retries(collected_engine):
    before=source_fingerprint(collected_engine);provider=Provider();provider.fail=True
    with pytest.raises(ValueError,match='cursor'):collect(collected_engine,provider)
    assert source_fingerprint(collected_engine)==before
    with collected_engine.connect() as c:
        assert c.scalar(text('SELECT count(*) FROM purchasing_v4.inventory_checkpoint'))==0
        assert c.scalar(text("SELECT count(*) FROM purchasing_v4.collection_attempt WHERE status='FAILED'"))==1
    provider.fail=False;assert collect(collected_engine,provider)['status']=='COMPLETE'
    assert collect(collected_engine,provider)['status']=='ALREADY_COLLECTED'


def test_projection_crash_before_certification_rolls_back_every_fact(collected_engine,monkeypatch):
    import app.services.purchasing_history_service as history
    before=source_fingerprint(collected_engine)
    def crash(*args,**kwargs):raise RuntimeError('fixture crash before certification')
    with monkeypatch.context() as m:
        m.setattr(history,'certify',crash)
        with pytest.raises(RuntimeError):collect(collected_engine)
    assert source_fingerprint(collected_engine)==before
    with collected_engine.connect() as c:
        assert c.scalar(text('SELECT count(*) FROM purchasing_v4.movement_leg'))==0
        assert c.scalar(text('SELECT count(*) FROM purchasing_v4.inventory_checkpoint'))==0
    assert collect(collected_engine)['status']=='COMPLETE'


def test_checkpoint_provenance_cannot_be_changed(collected_engine):
    collect(collected_engine)
    from sqlalchemy.exc import DBAPIError
    for sql in ("UPDATE purchasing_v4.inventory_checkpoint SET quantity=0","DELETE FROM purchasing_v4.collection_page","TRUNCATE purchasing_v4.collection_attempt CASCADE"):
        with pytest.raises(DBAPIError):
            with collected_engine.begin() as c:c.execute(text(sql))


def test_quantity_only_new_changed_removed_cancelled_orders_keep_financial_facts(collected_engine):
    with collected_engine.begin() as c:
        db=Session(bind=c)
        for version,qty in ((1,'3'),(2,'7')):
            purchasing_source_orders(db,[order(version,qty)],synchronized_at=NOW,units_only=True)
            assert db.scalar(text("SELECT quantity FROM purchasing_sale_corrections WHERE square_order_id='source-order'"))==Decimal(qty)
        removed=order(3);removed['line_items']=[]
        purchasing_source_orders(db,[removed],synchronized_at=NOW,units_only=True)
        assert db.scalar(text("SELECT quantity FROM purchasing_sale_corrections WHERE square_order_id='source-order'"))==0
        purchasing_source_orders(db,[order(4,'5')],synchronized_at=NOW,units_only=True)
        purchasing_source_orders(db,[order(5,'5','CANCELED')],synchronized_at=NOW,units_only=True)
        assert db.scalar(text("SELECT quantity FROM purchasing_sale_corrections WHERE square_order_id='source-order'"))==0
        purchasing_source_orders(db,[order(2,'7')],synchronized_at=NOW,units_only=True)
        assert db.scalar(text("SELECT quantity FROM purchasing_sale_corrections WHERE square_order_id='source-order'"))==0
        assert db.scalar(text("SELECT count(*) FROM consignment_sale_facts WHERE square_order_id='source-order'"))==0
        db.close()


def test_midnight_missing_day_and_25hour_dst_freshness_fail_closed():
    through=datetime(2026,11,1,tzinfo=ZONE);verified=datetime(2026,11,1,4,tzinfo=ZONE)
    row=dict(start_at=through-timedelta(days=126),through_at=through,verified_at=verified)
    just_after_midnight=datetime(2026,11,2,0,1,tzinfo=ZONE)
    state,reason,_=coverage_status(row,False,start=row['start_at'],end=datetime(2026,11,2,tzinfo=ZONE),as_of=just_after_midnight,max_age=timedelta(days=2))
    assert state==EvidenceState.UNKNOWN and reason=='INCOMPLETE_COVERAGE'
    # Fall-back to next day's 04:00 takes 25h when the previous checkpoint precedes the transition.
    verified=datetime(2026,10,31,4,tzinfo=ZONE);asof=datetime(2026,11,1,4,tzinfo=ZONE)
    row.update(verified_at=verified.astimezone(timezone.utc),through_at=verified.astimezone(timezone.utc))
    assert (asof.astimezone(timezone.utc)-row['verified_at'])==timedelta(hours=25)
    state,reason,_=coverage_status(row,False,start=row['start_at'],end=row['through_at'],as_of=asof.astimezone(timezone.utc),max_age=timedelta(hours=24))
    assert state==EvidenceState.UNAVAILABLE and reason=='STALE_COVERAGE'


def test_equal_daily_counts_do_not_hide_intervening_stockout():
    asof=instant('2026-10-27T12:00:00Z');hand=QuantityEvidence(EvidenceState.KNOWN,Decimal(10),'fixture',observed_at=asof)
    # Equal checkpoints of 10 cannot prove this day was continuously in stock:
    # 10 sold at 10:00, then 10 received at 18:00.
    changes=[dict(event_id='sold',quantity_delta=Decimal(-10),physical_count=False,occurred_at=instant('2026-10-26T17:00:00Z'),calculated_before_at=asof,synchronized_at=asof),
        dict(event_id='restock',quantity_delta=Decimal(10),physical_count=False,occurred_at=instant('2026-10-27T01:00:00Z'),calculated_before_at=asof,synchronized_at=asof)]
    day=date(2026,10,26);sales={day:QuantityEvidence(EvidenceState.KNOWN,Decimal(10),'fixture')}
    rows=availability_projection(location='FIXTURE_LOC1',inventory=hand,anchor_row=dict(source_calculated_at=asof,started_at=asof),coverage=(EvidenceState.KNOWN,'',asof),events=changes,boundary=[],sales=sales,start_day=day,end_day=day+timedelta(days=1),as_of=asof)
    assert rows[0][1]!=Availability.AVAILABLE


def test_successful_daily_retry_supersedes_older_failed_legacy_status(collected_engine):
    from app.services.inventory_history_projection import source_failed
    collect(collected_engine)
    old=dict(last_result='FAILED',last_attempted_at=NOW-timedelta(days=1))
    with collected_engine.connect() as c:assert not source_failed(c,old,as_of=NOW)
    provider=Provider();provider.fail=True
    with pytest.raises(ValueError):collect(collected_engine,provider,key='next-manual')
    # A later failed run is recorded separately, and cannot certify a missing day.
    with collected_engine.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM purchasing_v4.collection_attempt WHERE status='FAILED'"))==1


def test_incremental_runner_searches_overlap_and_126day_initial_scope(collected_engine,monkeypatch):
    calls=[]
    actual_collect=Collector.collect
    def capture(self,scope,**kwargs):calls.append(kwargs);return {'status':'fixture'}
    monkeypatch.setattr(Collector,'collect',capture)
    run_once(collected_engine,None,reason='RECOVERY',now=NOW)
    assert (datetime.combine(NOW.astimezone(ZONE).date(),time(),ZONE)-calls[-1]['begin']).days==126
    with collected_engine.begin() as c:
        db=Session(bind=c)
        for source in ('SALES','MOVEMENTS'):
            certify(db,source=source,locations=[l for s,l in SCOPE.stores],start_at=BEGIN-timedelta(days=1),through_at=NOW-timedelta(days=1),verified_at=NOW-timedelta(hours=23))
        db.close()
    run_once(collected_engine,None,reason='DAILY',now=NOW)
    # Legacy certificates alone do not prove a retained collector checkpoint.
    assert calls[-1]['begin']==BEGIN
    assert calls[-1]['command_key']=='daily:2026-10-27'
    result=actual_collect(Collector(collected_engine,Provider(),clock=lambda:NOW),SCOPE,
        begin=BEGIN,reason='RECOVERY',command_key='verified-bootstrap')
    assert result['status']=='COMPLETE'
    run_once(collected_engine,None,reason='DAILY',now=NOW)
    completed_day=datetime.combine(NOW.astimezone(ZONE).date(),time(),ZONE)
    assert calls[-1]['begin']==completed_day-timedelta(hours=72)
    assert calls[-1]['command_key']=='daily:2026-10-27'


def test_changed_count_is_unknown_even_when_both_reads_return_numbers(collected_engine):
    provider=Provider();count_reads=0
    def post(path,payload):
        nonlocal count_reads
        response=provider(path,payload)
        if path.endswith('counts/batch-retrieve'):
            count_reads+=1
            if count_reads==2:response['counts'][0]['quantity']='10'
        return response
    outcome=collect(collected_engine,post)
    assert outcome['unknown_counts']==1
    with collected_engine.connect() as c:
        r=c.execute(text("SELECT state,quantity,reason FROM purchasing_v4.inventory_checkpoint WHERE variation_id='FIXTURE_V1' AND store_id=1")).one()
        assert r==('UNKNOWN',None,'UNQUALIFIED_OR_CHANGED_COUNT')
        # Retained old projection is explicitly stale, never silently current/zero.
        assert c.scalar(text("SELECT freshness_state FROM ordering_current_inventory WHERE square_variation_id='FIXTURE_V1' AND store_id=1"))=='STALE'


def test_partial_daily_command_can_retry_missing_counts_without_duplicate_units(collected_engine):
    provider=Provider();provider.missing=True
    assert collect(collected_engine,provider)['status']=='PARTIAL'
    provider.missing=False
    assert collect(collected_engine,provider)['status']=='COMPLETE'
    assert collect(collected_engine,provider)['status']=='ALREADY_COLLECTED'
    with collected_engine.connect() as c:
        assert c.scalar(text("SELECT quantity FROM purchasing_sale_corrections WHERE square_order_id='source-order'"))==3
        assert c.scalar(text('SELECT count(*) FROM purchasing_v4.movement_leg'))==2
        assert c.scalar(text('SELECT count(*) FROM purchasing_v4.inventory_checkpoint'))==16


def test_source_revision_conflict_rolls_back_instead_of_changing_movement_history(collected_engine):
    collect(collected_engine)
    provider=Provider()
    def conflict(path,payload):
        response=provider(path,payload)
        if 'changes' in response:response['changes'][0]['adjustment']['quantity']='4'
        return response
    with pytest.raises(ValueError,match='Conflicting movement'):collect(collected_engine,conflict,key='conflicting-source')
    with collected_engine.connect() as c:
        assert c.scalar(text("SELECT quantity_delta FROM purchasing_v4.movement_leg WHERE location_id='FIXTURE_LOC1'"))==-3


def test_reduced_recovery_capture_does_not_invent_revision_clock_or_block_full_source(collected_engine):
    with collected_engine.begin() as c:
        db=Session(bind=c);captured=order();captured.pop('updated_at')
        purchasing_source_orders(db,[captured],synchronized_at=NOW,units_only=True)
        assert db.scalar(text("SELECT count(*) FROM purchasing_order_source_state WHERE square_order_id='source-order'"))==0
        full=order();full['updated_at']=full['closed_at'];full['line_items'][0]['total_money']={'amount':123,'currency':'USD'}
        purchasing_source_orders(db,[full],synchronized_at=NOW,units_only=True)
        assert db.scalar(text("SELECT count(*) FROM purchasing_order_source_state WHERE square_order_id='source-order'"))==1
        assert db.scalar(text("SELECT quantity FROM purchasing_sale_corrections WHERE square_order_id='source-order'"))==3
        db.close()


def test_no_unversioned_recovery_import_can_invent_missing_update_time(collected_engine):
    with collected_engine.begin() as c:
        db=Session(bind=c);unversioned=order();unversioned.pop('version');unversioned.pop('updated_at')
        with pytest.raises(ValueError,match='requires updated_at'):
            purchasing_source_orders(db,[unversioned],synchronized_at=NOW,units_only=True)
        db.close()


def test_unit_collection_does_not_suppress_later_established_financial_import(collected_engine):
    from app.services.v2_consignment_facts_service import import_square_orders
    with collected_engine.begin() as c:
        db=Session(bind=c);full=order()
        full['line_items'][0].update(gross_sales_money={'amount':123,'currency':'USD'},total_money={'amount':123,'currency':'USD'})
        purchasing_source_orders(db,[full],synchronized_at=NOW,units_only=True)
        financial,_=purchasing_source_orders(db,[full],synchronized_at=NOW)
        assert len(financial)==1 and len(financial[0]['line_items'])==1
        import_square_orders(db,orders=financial,synchronized_at=NOW)
        assert db.scalar(text("SELECT net_sales_amount FROM consignment_sale_facts WHERE square_order_id='source-order'"))==Decimal('1.23')
        financial,_=purchasing_source_orders(db,[full],synchronized_at=NOW)
        assert financial==[]
        db.close()


def test_daily_and_manual_cannot_collect_concurrently(collected_engine):
    from app.services.daily_square_collector import LOCK
    provider=Provider()
    with collected_engine.connect() as c:
        c.execute(text('SELECT pg_advisory_lock(:key)'),{'key':LOCK});c.commit()
        try:
            with pytest.raises(RuntimeError,match='already running'):collect(collected_engine,provider)
            assert not provider.requests
        finally:c.execute(text('SELECT pg_advisory_unlock(:key)'),{'key':LOCK});c.commit()


def test_explicit_zero_current_count_is_known_and_not_confused_with_missing(collected_engine):
    provider=Provider()
    def zero(path,payload):
        response=provider(path,payload)
        for row in response.get('counts',[]):row['quantity']='0'
        return response
    assert collect(collected_engine,zero)['qualified_counts']==8
    with collected_engine.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM purchasing_v4.inventory_checkpoint WHERE state='KNOWN' AND quantity=0"))==8
