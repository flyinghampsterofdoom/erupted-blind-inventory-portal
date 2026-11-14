"""Direct company-wide Lead designation over isolated synthetic schedules."""
from datetime import date,timedelta,time
from time import perf_counter
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import pytest
from sqlalchemy import select,event
from app import models as m
from app.auth import Principal,Role
from app.services.scheduling_daily_lead_service import assign_daily_lead
from app.services.v2_scheduling_service import SchedulingConflict
from test_v2_employee_profiles import employee_client,_admin
from test_v2_scheduling_foundation import scheduling_db
from test_v2_scheduling_repair import person,period,shift


@pytest.fixture
def lead_board(employee_client):
    client,current,Session,ids=employee_client
    day=date.today()+timedelta(days=14)
    start=day-timedelta(days=(day.weekday()+1)%7)
    day=start+timedelta(days=1)
    with Session() as db:
        actor=current['principal']=_admin(db)
        a=person(db,actor,'Parker synthetic',lead=True)
        b=person(db,actor,'Mikey synthetic',lead=True)
        c=person(db,actor,'Chelsea synthetic',lead=False)
        other=person(db,actor,'Unrelated synthetic',lead=True)
        per=period(db,actor,start)
        first=shift(db,actor,per,a,day,ids['north']);first.is_lead_of_day=True
        second=shift(db,actor,per,b,day,ids['south'])
        untouched=shift(db,actor,per,other,day+timedelta(days=1),ids['north'])
        db.commit()
        ids.update(period=per.id,first=first.id,second=second.id,other_shift=untouched.id,
            outgoing=a.id,incoming=c.id,day=day)
    def post(sid=ids['second'],assigned=True,version=None,csrf=True,scope=None):
        if version is None:
            with Session() as db:version=db.get(m.SchedulePeriod,ids['period']).version
        return client.post(f"/v2/scheduling/api/periods/{ids['period']}/shifts/{sid}/daily-lead",
            json={'assigned':assigned,'expected_version':version},
            headers={'X-CSRF-Token':client.cookies.get('csrf_token')} if csrf else {},
            params={'store_id':scope} if scope else None)
    return client,current,Session,ids,post


def snapshot(db):
    return {s.id:{c.name:getattr(s,c.name) for c in m.ScheduleShift.__table__.columns}
            for s in db.scalars(select(m.ScheduleShift))}


def forbid_processing(monkeypatch):
    from app.services import v2_scheduling_policy_service as policy
    from app.services import v2_scheduling_coverage_service as coverage
    from app.services import scheduling_training_service as replacement
    from app.routers import v2_scheduling as routes
    def forbidden(*a,**kw):raise AssertionError('Direct Lead assignment invoked scheduling processing')
    for name in ('evaluate_assignment','regenerate_period','ensure_rolling_schedule_horizon'):
        monkeypatch.setattr(policy,name,forbidden)
    for name in ('evaluate_schedule_warnings','rebuild_schedule_warnings'):
        monkeypatch.setattr(coverage,name,forbidden)
    monkeypatch.setattr(replacement,'save_transition',forbidden)
    monkeypatch.setattr(routes,'_board',forbidden)


def test_atomic_cross_store_transfer_is_lightweight_and_persists(lead_board,monkeypatch):
    client,current,Session,ids,post=lead_board
    with Session() as db:before=snapshot(db)
    forbid_processing(monkeypatch)
    began=perf_counter();response=post();elapsed=perf_counter()-began
    assert response.status_code==200,response.text
    receipt=response.json()
    assert 'board' not in receipt and not receipt['lead_gap']
    assert {r['id'] for r in receipt['changes']}=={ids['first'],ids['second']}
    with Session() as db:
        after=snapshot(db)
        assert not after[ids['first']]['is_lead_of_day']
        assert after[ids['second']]['is_lead_of_day']
        assert sum(r['is_lead_of_day'] for r in after.values())==1
        mutable={'is_lead_of_day','lead_of_day_manually_assigned','updated_at','updated_by_principal_id'}
        for sid,original in before.items():
            assert {k:v for k,v in after[sid].items() if k not in mutable}=={k:v for k,v in original.items() if k not in mutable}
        assert after[ids['other_shift']]==before[ids['other_shift']]
        assert db.get(m.Employee,ids['incoming']).scheduling_lead_capable is False
    assert elapsed<1
    print({'operation':'direct_daily_lead','seconds':elapsed})


def test_remove_lead_preserves_shift_and_reports_gap(lead_board,monkeypatch):
    _,_,Session,ids,post=lead_board
    with Session() as db:before=snapshot(db)
    forbid_processing(monkeypatch)
    response=post(ids['first'],False)
    assert response.status_code==200 and response.json()['lead_gap']
    assert 'Lead coverage is required' in response.json()['message']
    with Session() as db:
        after=snapshot(db)
        assert not any(r['is_lead_of_day'] for r in after.values())
        for sid,old in before.items():
            for key in ('employee_id','store_id','shift_date','start_time','end_time','unpaid_break_minutes','is_shift_lead'):
                assert after[sid][key]==old[key]


@pytest.mark.parametrize('role',['MANAGER','LEAD','STORE'])
def test_nonadmin_cannot_use_new_operation(lead_board,role):
    _,current,Session,ids,post=lead_board
    actor=current['principal']
    current['principal']=Principal(actor.id,actor.username,Role(role),None,True)
    with Session() as db:before=snapshot(db)
    assert post().status_code==403
    with Session() as db:assert snapshot(db)==before


def test_csrf_stale_version_scope_and_unqualified_employee_rejected(lead_board):
    _,current,Session,ids,post=lead_board
    assert post(csrf=False).status_code==403
    assert post(version=999).status_code==409
    with Session() as db:
        row=db.get(m.ScheduleShift,ids['second'])
        row.employee_id=ids['incoming'];db.commit()
        before=snapshot(db)
    assert post().status_code==422
    assert post(ids['first'],False,scope=ids['south']).status_code==403
    with Session() as db:assert snapshot(db)==before


def test_published_source_and_all_unrelated_assignments_are_immutable(lead_board,monkeypatch):
    _,_,Session,ids,post=lead_board
    with Session() as db:
        p=db.get(m.SchedulePeriod,ids['period'])
        p.status=m.SchedulePeriodStatus.PUBLISHED
        from datetime import datetime,timezone
        p.published_at=datetime.now(timezone.utc);db.commit()
        before=snapshot(db)
    forbid_processing(monkeypatch)
    response=post()
    assert response.status_code==200,response.text
    receipt=response.json();assert receipt['revised'] and receipt['period_id']!=ids['period']
    with Session() as db:
        assert {sid:snapshot(db)[sid] for sid in before}==before
        copies=list(db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==receipt['period_id'])))
        assert len(copies)==len(before)
        assert [s.source_shift_id for s in copies if s.is_lead_of_day]==[ids['second']]
        assert db.get(m.SchedulePeriod,ids['period']).status==m.SchedulePeriodStatus.PUBLISHED


def test_replacement_gap_can_be_resolved_without_replacement_processing(lead_board,monkeypatch):
    _,current,Session,ids,post=lead_board
    from app.services.scheduling_training_service import save_transition
    with Session() as db:
        result=save_transition(db,principal=current['principal'],employee_id=ids['incoming'],
            first_schedule_date=ids['day'],replacing_employee_id=ids['outgoing'],
            allowed_store_ids=(ids['north'],ids['south']))
        db.commit()
        assert len(result['changes'])==1 and len(result['lead_reassignments'])==1
    forbid_processing(monkeypatch)
    assert post().status_code==200
    with Session() as db:
        assert db.get(m.ScheduleShift,ids['first']).employee_id==ids['incoming']
        assert db.get(m.ScheduleShift,ids['second']).is_lead_of_day
        assert not db.get(m.Employee,ids['incoming']).scheduling_lead_capable


def test_concurrent_selections_never_create_multiple_daily_leads(lead_board):
    _,current,Session,ids,_=lead_board
    barrier=Barrier(2)
    def save(sid):
        with Session() as db:
            barrier.wait(timeout=10)
            try:
                result=assign_daily_lead(db,principal=current['principal'],period_id=ids['period'],
                    shift_id=sid,expected_version=1,assigned=True,allowed_store_ids=(ids['north'],ids['south']))
                db.commit();return result
            except SchedulingConflict:
                db.rollback();return None
    with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(save,[ids['first'],ids['second']]))
    with Session() as db:
        assert sum(s.is_lead_of_day for s in db.scalars(select(m.ScheduleShift)).all())==1


def test_filtered_calendar_can_replace_authorized_lead_at_another_store(lead_board):
    _,_,Session,ids,post=lead_board
    response=post(scope=ids['south'])
    assert response.status_code==200,response.text
    with Session() as db:
        assert not db.get(m.ScheduleShift,ids['first']).is_lead_of_day
        assert db.get(m.ScheduleShift,ids['second']).is_lead_of_day
