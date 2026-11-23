"""Window/age gates with real isolated PostgreSQL; external reads are mocked."""
from datetime import datetime,timedelta,timezone
from unittest.mock import Mock, patch
import os
from uuid import uuid4
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
import pytest
from sqlalchemy.orm import Session
from test_purchasing_receiving_extension import (
    database_template as purchasing_database_template, receiving_db,
)
from app.schema_contract import _alembic_config
from app import models
from app.config import settings
from app.services import v2_square_data_service as service
from app.services.purchasing_history_service import certify
NOW=datetime(2026,10,22,18,tzinfo=timezone.utc)
START,END=service.required_history_window(NOW)


@pytest.fixture(scope='module')
def database_template():
    """Use the launcher database without changing shared fixtures or global env."""
    raw = os.getenv('TEST_POSTGRES_ADMIN_URL')
    if os.getenv('P1_TEST_DATABASE_URL') or not raw:
        yield from purchasing_database_template.__wrapped__()
        return
    url = make_url(raw)
    assert url.get_backend_name() == 'postgresql'
    assert url.host in {'127.0.0.1', 'localhost', '::1'} and url.port == 55486
    assert not {'host', 'hostaddr', 'port', 'dbname', 'service', 'servicefile'}.intersection(url.query)
    url = url.set(database='erupted_purchasing_p1_chase')
    assert url.database.startswith('erupted_purchasing_p1_')
    admin = create_engine(url.set(database='postgres'), isolation_level='AUTOCOMMIT')
    name = 'erupted_purchasing_p1_template_' + uuid4().hex
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        command.upgrade(
            _alembic_config(url.set(database=name).render_as_string(hide_password=False)),
            'head',
        )
        yield admin, url, name
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()

def seed(db,*,through=END,success=NOW-timedelta(hours=1),result='COMPLETE'):
    state=models.ConsignmentSalesSyncState(id=1,last_successful_start_at=START,last_successful_through_at=through,last_successful_at=success,last_attempted_at=success,last_result=result,updated_by_principal_id=1);db.add(state)
    for source in ('SALES','MOVEMENTS'):
        certify(db,source=source,locations=[f'L{i}' for i in range(1,5)],start_at=START,through_at=through,verified_at=success)
    db.flush();return state

@pytest.fixture
def db(receiving_db,monkeypatch):
    monkeypatch.setattr(settings,'purchasing_v3_phase1_enabled',True)
    monkeypatch.setattr(settings,'daily_evidence_collector_enabled',False)
    with Session(receiving_db) as db:yield db

def test_fresh_complete(db):
    seed(db);s=service.square_data_status(db,now=NOW)
    assert s['state']=='current' and s['coverage_complete'] and not service.square_data_needs_refresh(db,now=NOW)
    assert s['required_through_at']==END and s['required_start_at']==START

def test_fresh_incomplete(db):
    seed(db,through=END-timedelta(hours=7));s=service.square_data_status(db,now=NOW)
    assert s['state']=='incomplete' and not s['coverage_complete'] and service.square_data_needs_refresh(db,now=NOW)

def test_old_refresh_requires_refresh_even_if_interval_is_present(db):
    seed(db);state=db.get(models.ConsignmentSalesSyncState,1);state.last_successful_at=NOW-timedelta(hours=25)
    assert service.square_data_status(db,now=NOW)['state']=='stale'

def test_pacific_midnight_advances_required_window(db):
    before=datetime(2026,10,22,6,59,59,tzinfo=timezone.utc);oldstart,oldend=service.required_history_window(before)
    state=seed(db,through=before,success=before);state.last_successful_start_at=oldstart
    for row in db.query(models.PurchasingHistoryCoverage):row.start_at=oldstart
    assert service.square_data_status(db,now=before)['state']=='current'
    after=before+timedelta(seconds=1);s=service.square_data_status(db,now=after)
    assert s['state']=='incomplete' and s['required_through_at']==after

@pytest.mark.parametrize('kind',['SALES','MOVEMENTS'])
def test_one_store_checkpoint_missing_cannot_be_current(db,kind):
    seed(db);db.delete(db.get(models.PurchasingHistoryCoverage,(kind,'L4')));db.flush()
    assert service.square_data_status(db,now=NOW)['state']=='incomplete'

@pytest.mark.parametrize('field,value',[('last_successful_through_at',None),('last_successful_start_at',None),('last_successful_through_at',NOW+timedelta(days=1)),('last_successful_start_at',END),('last_successful_at',NOW+timedelta(seconds=1))])
def test_missing_invalid_or_future_checkpoints(db,field,value):
    state=seed(db);setattr(state,field,value);db.flush()
    assert service.square_data_status(db,now=NOW)['state']!='current'

@pytest.mark.parametrize('attempted',[None,NOW-timedelta(minutes=31),NOW+timedelta(seconds=1)])
def test_interrupted_or_invalid_running_refresh(db,attempted):
    state=seed(db,result='RUNNING');state.last_attempted_at=attempted
    assert service.square_data_status(db,now=NOW)['state']=='failed'

def test_duplicate_attempt_while_running_does_not_claim(receiving_db,monkeypatch):
    monkeypatch.setattr(settings,'purchasing_v3_phase1_enabled',True)
    with Session(receiving_db) as db:seed(db,result='RUNNING',success=NOW-timedelta(minutes=1));db.commit()
    monkeypatch.setattr(service,'SessionLocal',lambda:Session(receiving_db))
    assert not service._claim_refresh(actor_id=1,force=True,now=NOW)

def test_partial_sales_pagination_never_advances_coverage(db,monkeypatch):
    from app.services.v2_consignment_facts_service import synchronize_square_facts
    class Interrupted:
        def search(self,**kw):
            yield []
            raise RuntimeError('Source page 2 unavailable')
    with pytest.raises(RuntimeError,match='page 2'):
        synchronize_square_facts(db,start_at=START,end_at=END,actor_id=1,reader=Interrupted())
    assert db.get(models.PurchasingHistoryCoverage,('SALES','L1')) is None
    assert service.square_data_status(db,now=NOW)['state']!='current'

def test_dst_window_is_calendar_days():
    from zoneinfo import ZoneInfo
    tz=ZoneInfo('America/Los_Angeles')
    for now in (datetime(2026,3,9,12,tzinfo=timezone.utc),datetime(2026,11,2,12,tzinfo=timezone.utc)):
        first,end=service.required_history_window(now)
        assert (end.astimezone(tz).date()-first.astimezone(tz).date()).days==84
        assert end.astimezone(tz).hour==first.astimezone(tz).hour==0



OLD_START = NOW - timedelta(days=20)
OLD_END = NOW - timedelta(days=13)
REQUEST = (NOW-timedelta(days=7), NOW)


@pytest.mark.parametrize('previous,success,requested,expected', [
    ((OLD_START, OLD_END), NOW, REQUEST, (OLD_END, NOW)),
    ((OLD_START, OLD_END), NOW,
     (OLD_START-timedelta(days=9), OLD_START-timedelta(days=2)),
     (OLD_START-timedelta(days=9), OLD_START)),
    ((OLD_START, OLD_END), NOW,
     (OLD_START+timedelta(days=5), NOW), (OLD_START+timedelta(days=5), NOW)),
    ((OLD_START, OLD_END), NOW,
     (OLD_START+timedelta(days=1), OLD_END-timedelta(days=1)),
     (OLD_START+timedelta(days=1), OLD_END-timedelta(days=1))),
    ((OLD_START, OLD_END), NOW, (OLD_END, NOW), (OLD_END, NOW)),
    (None, None, REQUEST, REQUEST),
    ((None, OLD_END), NOW, REQUEST, REQUEST),
    ((OLD_START, None), NOW, REQUEST, REQUEST),
    ((OLD_END, OLD_START), NOW, REQUEST, REQUEST),
    ((OLD_START, OLD_END), None, REQUEST, REQUEST),
    ((OLD_START, OLD_END), OLD_END-timedelta(seconds=1), REQUEST, REQUEST),
    ((OLD_START, OLD_END), NOW+timedelta(seconds=1), REQUEST, REQUEST),
    ((OLD_START, NOW+timedelta(days=1)), NOW+timedelta(days=2), REQUEST, REQUEST),
    ((OLD_START, OLD_END), NOW, (NOW, NOW-timedelta(days=1)), (NOW, NOW-timedelta(days=1))),
    ((OLD_START, OLD_END), NOW, (NOW, NOW), (NOW, NOW)),
], ids=['forward-gap', 'backward-gap', 'overlap', 'contained', 'touching',
        'no-state', 'missing-start', 'missing-end', 'invalid-old-envelope',
        'missing-success', 'success-before-through', 'future-success',
        'future-checkpoint', 'reversed-request', 'empty-request'])
def test_explicit_refresh_plans_only_verified_connecting_intervals(
    receiving_db, monkeypatch, previous, success, requested, expected,
):
    if previous is not None:
        with Session(receiving_db) as db:
            db.add(models.ConsignmentSalesSyncState(
                id=1, last_successful_start_at=previous[0],
                last_successful_through_at=previous[1],
                last_successful_at=success, last_result='COMPLETE',
                updated_by_principal_id=1,
            ))
            db.commit()
    monkeypatch.setattr(service, 'SessionLocal', lambda: Session(receiving_db))
    monkeypatch.setattr(service, '_now', lambda: NOW)
    assert service._refresh_bounds(start_at=requested[0], end_at=requested[1]) == expected
    # Planning a wider read must not promote the persisted financial checkpoint.
    with Session(receiving_db) as db:
        state = db.get(models.ConsignmentSalesSyncState, 1)
        if previous is None:
            assert state is None
        else:
            assert (state.last_successful_start_at, state.last_successful_through_at) == previous


@pytest.mark.parametrize('shared_state', ['current', 'incomplete', 'failed'])
@pytest.mark.parametrize('force', [True, False])
def test_shared_collector_status_cannot_steal_live_financial_refresh(
    receiving_db, monkeypatch, shared_state, force,
):
    from app.services import daily_evidence_runtime
    monkeypatch.setattr(settings, 'daily_evidence_collector_enabled', True)
    shared_status = Mock(return_value={'state': shared_state})
    monkeypatch.setattr(daily_evidence_runtime, 'status', shared_status)
    attempted_at = NOW-timedelta(minutes=1)
    with Session(receiving_db) as db:
        seed(db, result='RUNNING', success=attempted_at)
        db.commit()
    monkeypatch.setattr(service, 'SessionLocal', lambda: Session(receiving_db))
    assert not service._claim_refresh(actor_id=2, force=force, now=NOW)
    shared_status.assert_not_called()
    with Session(receiving_db) as db:
        state = db.get(models.ConsignmentSalesSyncState, 1)
        assert state.last_attempted_at == attempted_at
        assert state.updated_by_principal_id == 1


def test_shared_current_banner_does_not_prevent_abandoned_financial_recovery(
    receiving_db, monkeypatch,
):
    from app.services import daily_evidence_runtime
    monkeypatch.setattr(settings, 'daily_evidence_collector_enabled', True)
    shared_status = Mock(return_value={'state': 'current'})
    monkeypatch.setattr(daily_evidence_runtime, 'status', shared_status)
    with Session(receiving_db) as db:
        seed(db, result='RUNNING', success=NOW-timedelta(minutes=31))
        db.commit()
    monkeypatch.setattr(service, 'SessionLocal', lambda: Session(receiving_db))
    assert service._claim_refresh(actor_id=2, force=False, now=NOW)
    shared_status.assert_not_called()
    with Session(receiving_db) as db:
        state = db.get(models.ConsignmentSalesSyncState, 1)
        assert state.last_result == 'RUNNING' and state.last_attempted_at == NOW
        assert state.updated_by_principal_id == 2


def test_ci_purchasing_database_fixture_takes_precedence(monkeypatch):
    monkeypatch.setenv('TEST_POSTGRES_ADMIN_URL',
                       'postgresql+psycopg://ci@127.0.0.1:5433/postgres')
    monkeypatch.setenv('P1_TEST_DATABASE_URL',
                       'postgresql+psycopg://ci@127.0.0.1:5432/erupted_purchasing_p1_ci')
    sentinel = object()
    completed = []

    def existing_template():
        try:
            yield sentinel
        finally:
            completed.append(True)

    monkeypatch.setattr(purchasing_database_template, '__wrapped__', existing_template)
    template = database_template.__wrapped__()
    assert next(template) is sentinel
    with pytest.raises(StopIteration):
        next(template)
    assert completed == [True]
