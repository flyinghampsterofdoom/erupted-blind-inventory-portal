from datetime import date, datetime, time, timezone
import pytest
from sqlalchemy import select
from app import models as m
from app.services.v2_scheduling_freshness_service import REGENERATION_REQUIRED, requires_regeneration
from app.services.v2_scheduling_coverage_service import rebuild_schedule_warnings
from test_v2_scheduling_foundation import scheduling_db
from test_v2_scheduling_repair import person, period, shift


@pytest.fixture(autouse=True)
def business_date(monkeypatch):
    monkeypatch.setattr('app.services.v2_scheduling_lead_duty_service.business_today', lambda db: date(2026,10,19))


def setup_draft(db, actor, ids):
    e=person(db,actor,'Freshness worker',lead=True,target=1)
    per=period(db,actor,date(2026,11,1))
    per.generated_at=datetime(2026,10,1,tzinfo=timezone.utc)
    row=shift(db,actor,per,e,per.week_start_date,ids['north'])
    row.is_lead_of_day=True;row.manually_locked=True;db.flush()
    return e,per,row


def warnings(db, per):
    return list(db.scalars(select(m.ScheduleWarning).where(m.ScheduleWarning.schedule_period_id==per.id)))


@pytest.mark.parametrize('kind', ['availability','pto','required_rest','store_restriction','coverage','closure','eligibility','allocation'])
def test_hard_change_immediately_revalidates_without_rewriting_assignments(scheduling_db,kind):
    Session,actor,ids,_=scheduling_db
    with Session(autoflush=False) as db:
        e,per,row=setup_draft(db,actor,ids)
        other=person(db,actor,'Unaffected worker',lead=True,target=1)
        unrelated=period(db,actor,date(2026,11,8))
        shift(db,actor,unrelated,other,unrelated.week_start_date,ids['north'])
        published=period(db,actor,date(2026,11,15),'PUBLISHED')
        shift(db,actor,published,e,published.week_start_date,ids['north'])
        before=(row.employee_id,row.is_lead_of_day,row.manually_locked,row.updated_at,per.version)
        actor_fields=dict(created_by_principal_id=actor.id,updated_by_principal_id=actor.id)
        if kind=='availability':
            db.add(m.EmployeeSchedulingWindow(employee_id=e.id,day_of_week=0,start_time=time(0),end_time=time(23,59),kind=m.SchedulingWindowKind.HARD_UNAVAILABLE,active=True,**actor_fields))
        elif kind=='pto':
            db.add(m.TimeOffRequest(employee_id=e.id,start_date=per.week_start_date,end_date=per.week_start_date,full_day=True,status=m.TimeOffRequestStatus.APPROVED,reason_category_id=ids['vacation'],**actor_fields))
        elif kind=='required_rest':
            db.scalar(select(m.EmployeeSchedulingProfile).where(m.EmployeeSchedulingProfile.employee_id==e.id)).minimum_days_off_after_max_block=2
        elif kind=='store_restriction':
            db.add(m.EmployeeSchedulingStorePreference(employee_id=e.id,store_id=ids['north'],active=True,preference_level=m.StorePreferenceLevel.NEVER,**actor_fields))
        elif kind=='coverage':
            db.add(m.CoverageRequirement(store_id=ids['north'],day_of_week=0,start_time=time(9),end_time=time(10),minimum_employee_count=5,active=True,**actor_fields))
        elif kind=='closure':
            db.add(m.StoreSpecialHour(store_id=ids['north'],calendar_date=per.week_start_date,event_name='Closed',closed_all_day=True,active=True,**actor_fields))
        elif kind=='eligibility':
            e.last_effective_date=date(2026,10,31)
        elif kind=='allocation':
            db.add(m.SchedulingAllocationChange(employee_id=e.id,effective_date=per.week_start_date,shifts_per_week=2,active=True,baseline=False,created_by_principal_id=actor.id))
        db.flush()
        assert requires_regeneration(per)
        found=warnings(db,per)
        assert sum(w.warning_type==REGENERATION_REQUIRED for w in found)==1
        expected={'availability':'HARD_UNAVAILABLE','pto':'APPROVED_TIME_OFF','store_restriction':'STORE_NEVER','closure':'SHIFT_ON_CLOSED_DATE'}
        if kind in expected:
            assert any(w.warning_type==expected[kind] for w in found)
        assert (row.employee_id,row.is_lead_of_day,row.manually_locked,row.updated_at,per.version)==before
        assert published.constraints_changed_at is None
        if kind not in ('coverage','allocation'):
            assert unrelated.constraints_changed_at is None
        # Board loads and repeated checks must retain one durable stale alert.
        rebuild_schedule_warnings(db,schedule_period_id=per.id)
        assert sum(w.warning_type==REGENERATION_REQUIRED for w in warnings(db,per))==1


def test_soft_changes_do_not_stale_drafts_and_rollback_restores_freshness(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        e,per,row=setup_draft(db,actor,ids)
        profile=db.scalar(select(m.EmployeeSchedulingProfile).where(m.EmployeeSchedulingProfile.employee_id==e.id))
        profile.scheduler_note='A note';db.flush()
        assert per.constraints_changed_at is None
        profile.max_consecutive_work_days=1;db.flush()
        assert not requires_regeneration(per)
        with db.begin_nested() as tx:
            profile.minimum_days_off_after_max_block=2;db.flush()
            assert requires_regeneration(per)
            tx.rollback()
        assert not requires_regeneration(per)
        assert not any(w.warning_type==REGENERATION_REQUIRED for w in warnings(db,per))


def test_deleting_or_relaxing_hard_rules_invalidates_but_pending_pto_does_not(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        e,per,row=setup_draft(db,actor,ids)
        rule=m.EmployeeSchedulingStorePreference(employee_id=e.id,store_id=ids['north'],active=True,preference_level=m.StorePreferenceLevel.NEVER,created_by_principal_id=actor.id,updated_by_principal_id=actor.id)
        db.add(rule);db.flush()
        per.generated_at=datetime.now(timezone.utc);db.flush()
        assert not requires_regeneration(per)
        db.delete(rule);db.flush()
        assert requires_regeneration(per)
        per.generated_at=datetime.now(timezone.utc);db.flush()
        db.add(m.TimeOffRequest(employee_id=e.id,start_date=per.week_start_date,end_date=per.week_start_date,full_day=True,status=m.TimeOffRequestStatus.PENDING,reason_category_id=ids['vacation'],created_by_principal_id=actor.id,updated_by_principal_id=actor.id));db.flush()
        assert not requires_regeneration(per)


def test_single_week_regeneration_never_escalates_to_horizon(scheduling_db,monkeypatch):
    from app.services import v2_scheduling_horizon_service as h
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        e,per,row=setup_draft(db,actor,ids)
        for other in db.scalars(select(m.Employee).where(m.Employee.id != e.id)):
            other.scheduling_active=False
        db.flush()
        real=h.plan_schedule_horizon
        seen=[]
        def bounded(db,**kwargs):
            seen.append(kwargs)
            return real(db,**kwargs)
        monkeypatch.setattr(h,'plan_schedule_horizon',bounded)
        result=h.optimize_weekly_positions(db,principal=actor,period=per,planning_date=date(2026,10,19))
        assert result['status']=='PASS'
        assert len(seen)==1 and seen[0]['periods']==[per] and seen[0]['time_limit']==10
        assert row.employee_id==e.id


def test_constraint_change_rejects_an_already_calculated_plan(scheduling_db):
    from app.services.v2_scheduling_horizon_service import plan_schedule_horizon,apply_schedule_horizon_plan
    from app.services.v2_scheduling_service import SchedulingConflict
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        e,per,row=setup_draft(db,actor,ids)
        for other in db.scalars(select(m.Employee).where(m.Employee.id != e.id)):
            other.scheduling_active=False
        db.flush()
        plan=plan_schedule_horizon(db,periods=[per],planning_date=date(2026,10,19))
        assert plan['assignments']
        profile=db.scalar(select(m.EmployeeSchedulingProfile).where(m.EmployeeSchedulingProfile.employee_id==e.id))
        profile.minimum_days_off_after_max_block=2;db.flush()
        with pytest.raises(SchedulingConflict,match='changed'):
            apply_schedule_horizon_plan(db,periods=[per],plan=plan,principal=actor)


def test_fresh_week_uses_shared_planner_in_initial_generation_mode(scheduling_db,monkeypatch):
    from app.services import v2_scheduling_horizon_service as h
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        for other in db.scalars(select(m.Employee)):
            other.scheduling_active=False
        e=person(db,actor,'Fresh generation lead',lead=True,target=1)
        per=period(db,actor,date(2026,11,1))
        row=shift(db,actor,per,None,per.week_start_date,ids['north'])
        real=h.plan_schedule_horizon;seen=[]
        def initial(db,**kwargs):
            seen.append(kwargs)
            return real(db,**kwargs)
        monkeypatch.setattr(h,'plan_schedule_horizon',initial)
        result=h.optimize_weekly_positions(db,principal=actor,period=per,planning_date=date(2026,10,19))
        assert result['status']=='PASS' and row.employee_id==e.id
        assert len(seen)==1 and seen[0]['repair'] is False
        assert result['search']['optimality_scope']=='FULL_MODEL'
