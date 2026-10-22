"""Window/age gates with real isolated PostgreSQL; external reads are mocked."""
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
import pytest
from sqlalchemy.orm import Session
from test_purchasing_receiving_extension import database_template,receiving_db
from app import models
from app.config import settings
from app.services import v2_square_data_service as service
from app.services.purchasing_history_service import certify
NOW=datetime(2026,10,22,18,tzinfo=timezone.utc)
START,END=service.required_history_window(NOW)

def seed(db,*,through=END,success=NOW-timedelta(hours=1),result='COMPLETE'):
    state=models.ConsignmentSalesSyncState(id=1,last_successful_start_at=START,last_successful_through_at=through,last_successful_at=success,last_attempted_at=success,last_result=result,updated_by_principal_id=1);db.add(state)
    for source in ('SALES','MOVEMENTS'):
        certify(db,source=source,locations=[f'L{i}' for i in range(1,5)],start_at=START,through_at=through,verified_at=success)
    db.flush();return state

@pytest.fixture
def db(receiving_db,monkeypatch):
    monkeypatch.setattr(settings,'purchasing_v3_phase1_enabled',True)
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
