from datetime import date, timedelta
import pytest
from sqlalchemy import select
from app import models as m
from app.services.v2_scheduling_horizon_service import plan_schedule_horizon,apply_schedule_horizon_plan
from app.services.v2_scheduling_service import SchedulingConflict
from app.services.v2_scheduling_coverage_service import rebuild_schedule_warnings
from test_v2_scheduling_foundation import scheduling_db
from test_v2_scheduling_repair import person,period,shift


def test_planning_restores_assignments_leads_and_versions(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        for e in db.scalars(select(m.Employee)):e.scheduling_active=False
        e=person(db,actor,'Horizon lead',lead=True,target=1)
        per=period(db,actor,date(2026,11,1))
        row=shift(db,actor,per,e,per.week_start_date,ids['north'])
        row.is_lead_of_day=True;db.flush()
        before=(row.employee_id,row.is_lead_of_day,per.version)
        plan=plan_schedule_horizon(db,periods=[per],planning_date=date(2026,10,19))
        assert plan['status']=='OPTIMAL'
        assert (row.employee_id,row.is_lead_of_day,per.version)==before
        apply_schedule_horizon_plan(db,periods=[per],plan=plan,principal=actor)
        assert row.employee_id==e.id


def test_published_historical_and_cleared_periods_are_refused(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        per=period(db,actor,date(2026,11,1),'PUBLISHED')
        with pytest.raises(SchedulingConflict):
            plan_schedule_horizon(db,periods=[per],planning_date=date(2026,10,19))
        per.status=m.SchedulePeriodStatus.DRAFT;per.is_historical=True
        with pytest.raises(SchedulingConflict):
            plan_schedule_horizon(db,periods=[per],planning_date=date(2026,10,19))


def test_warning_cache_contains_cross_week_consecutive_violation(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        e=person(db,actor,'One day maximum',lead=True,target=1)
        profile=db.scalar(select(m.EmployeeSchedulingProfile).where(m.EmployeeSchedulingProfile.employee_id==e.id))
        profile.max_consecutive_work_days=1
        prior=period(db,actor,date(2026,10,25),'PUBLISHED')
        per=period(db,actor,date(2026,11,1))
        shift(db,actor,prior,e,date(2026,10,31),ids['north'])
        row=shift(db,actor,per,e,date(2026,11,1),ids['north'])
        warnings=rebuild_schedule_warnings(db,schedule_period_id=per.id)
        assert any(w.warning_type=='MAX_CONSECUTIVE_DAYS' and w.shift_id==row.id for w in warnings)


def test_infeasible_protected_plan_restores_all_evidence(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        for e in db.scalars(select(m.Employee)):e.scheduling_active=False
        e=person(db,actor,'Locked lead',lead=True,target=1)
        per=period(db,actor,date(2026,11,1))
        row=shift(db,actor,per,e,per.week_start_date,ids['north'])
        row.manually_locked=True;row.is_lead_of_day=True
        db.add(m.EmployeeSchedulingWindow(employee_id=e.id,day_of_week=0,start_time=row.start_time,
            end_time=row.end_time,kind=m.SchedulingWindowKind.HARD_UNAVAILABLE,active=True,
            created_by_principal_id=actor.id,updated_by_principal_id=actor.id));db.flush()
        plan=plan_schedule_horizon(db,periods=[per],planning_date=date(2026,10,19))
        assert plan['assignments'] is None
        assert row.employee_id==e.id and row.is_lead_of_day and row.manually_locked


def test_incomplete_generation_is_not_recorded_as_success(scheduling_db):
    from app.services.v2_scheduling_prebeta_service import generation_outcome
    Session,actor,ids,_=scheduling_db
    @generation_outcome('test_incomplete')
    def generate(db, *, principal):
        return {'created_period_ids':[1],'results':[{'validation_status':'INCOMPLETE','lead_uncovered':[{'date':'2026-11-01'}]}]}
    with Session() as db:
        generate(db,principal=actor)
        assert db.scalar(select(m.ScheduleGenerationRun.status))=='INCOMPLETE'


def test_changed_schedule_rejects_stale_plan(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        for e in db.scalars(select(m.Employee)):e.scheduling_active=False
        e=person(db,actor,'Stale plan lead',lead=True,target=1)
        per=period(db,actor,date(2026,11,1))
        row=shift(db,actor,per,e,per.week_start_date,ids['north'])
        plan=plan_schedule_horizon(db,periods=[per],planning_date=date(2026,10,19))
        per.version+=1;db.flush()
        with pytest.raises(SchedulingConflict,match='changed'):
            apply_schedule_horizon_plan(db,periods=[per],plan=plan,principal=actor)


def test_scoped_effective_read_cache_clears_after_flush(scheduling_db):
    from app.services.v2_scheduling_policy_service import effective_assignment_rows
    from app.services.v2_scheduling_repair_service import _PlanningReads
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        e=person(db,actor,'Cache lead',lead=True,target=1)
        per=period(db,actor,date(2026,11,1))
        r=shift(db,actor,per,e,per.week_start_date,ids['north'])
        reads=_PlanningReads(db)
        assert len(effective_assignment_rows(reads,employee_id=e.id))==1
        r.employee_id=None;reads.flush()
        assert not effective_assignment_rows(reads,employee_id=e.id)


def test_horizon_regeneration_route_requires_generation_and_csrf():
    from app.routers.v2_scheduling import router,feature_access,generate_access
    from app.security.csrf import verify_csrf
    route=next(r for r in router.routes if r.path.endswith('/automation/regenerate-horizon'))
    dependencies={d.call for d in route.dependant.dependencies}
    assert {feature_access,generate_access,verify_csrf} <= dependencies


def test_dashboard_flags_rules_newer_than_generated_schedule(scheduling_db):
    from datetime import datetime,timezone
    from app.services.v2_scheduling_policy_service import automation_draft_dashboard
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        e=person(db,actor,'Freshness lead',lead=True,target=1)
        per=period(db,actor,date(2026,11,1))
        per.generated_at=datetime(2026,10,1,tzinfo=timezone.utc)
        shift(db,actor,per,e,per.week_start_date,ids['north'])
        db.flush()
        profile=db.scalar(select(m.EmployeeSchedulingProfile).where(m.EmployeeSchedulingProfile.employee_id==e.id))
        profile.max_consecutive_work_days=1
        db.flush()
        dashboard=automation_draft_dashboard(db,today=date(2026,10,19))
        assert next(r for r in dashboard['upcoming'] if r['period'].id==per.id)['constraints_changed']


def test_joint_horizon_targets_yield_to_coverage_without_overriding_pto(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        for e in db.scalars(select(m.Employee)):e.scheduling_active=False
        regular=person(db,actor,'Regular lead',lead=True,target=3)
        reserve=person(db,actor,'Flexible lead',lead=True,target=1)
        weeks=[period(db,actor,date(2026,11,1)+timedelta(weeks=i)) for i in range(2)]
        rows=[]
        for per in weeks:
            rows.extend(shift(db,actor,per,None,per.week_start_date+timedelta(days=i),ids['north']) for i in range(4))
        db.add(m.TimeOffRequest(employee_id=regular.id,start_date=weeks[0].week_start_date,
            end_date=weeks[0].week_start_date+timedelta(days=1),full_day=True,
            reason_category_id=ids['vacation'],status=m.TimeOffRequestStatus.APPROVED,
            created_by_principal_id=actor.id,updated_by_principal_id=actor.id));db.flush()
        plan=plan_schedule_horizon(db,periods=weeks,planning_date=date(2026,10,19))
        assert plan['assignments'] is not None
        assert all(plan['assignments'][r.id]!=regular.id for r in rows if r.shift_date <= weeks[0].week_start_date+timedelta(days=1))
        assert plan['allocation_exceptions']==[]
        assert any(d['code']=='ALLOCATION_SHORTFALL' for d in plan['diagnostics'])
        assert all(r.employee_id is None for r in rows)
        assert db.scalar(select(m.EmployeeSchedulingProfile.target_shifts_per_week).where(
            m.EmployeeSchedulingProfile.employee_id==reserve.id))==1


def test_ineligible_generated_assignee_does_not_duplicate_coverage_positions(scheduling_db):
    from app.services.v2_scheduling_policy_service import materialize_coverage_positions
    from test_v2_scheduling_foundation import _coverage
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        per=period(db,actor,date(2026,11,1))
        _coverage(db,actor,ids,weekday=0,store_id=ids['north'])
        materialize_coverage_positions(db,principal=actor,period=per)
        row=db.scalar(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==per.id))
        e=person(db,actor,'Departed generated assignee',target=1)
        e.last_effective_date=date(2026,10,31);row.employee_id=e.id;db.flush()
        original=row.id
        materialize_coverage_positions(db,principal=actor,period=per,preserve_existing=True)
        assert list(db.scalars(select(m.ScheduleShift.id).where(m.ScheduleShift.schedule_period_id==per.id)))==[original]


def test_generation_message_never_labels_incomplete_coverage_successful():
    from app.routers.v2_scheduling import _generation_message
    message=_generation_message({'created':True,'results':[{'validation_status':'INCOMPLETE',
        'uncovered':[],'lead_uncovered':[{'date':'2026-11-01'}]}]})
    assert 'remains incomplete' in message and '1 missing Lead' in message


def test_input_signature_uses_instant_not_database_timezone():
    from datetime import datetime,timezone
    from zoneinfo import ZoneInfo
    from types import SimpleNamespace
    from app.services.v2_scheduling_horizon_service import input_signature
    stamp=datetime(2026,10,20,18,tzinfo=ZoneInfo('America/Los_Angeles'))
    period=SimpleNamespace(id=1,version=2,constraints_changed_at=stamp)
    before=input_signature([], [period])
    period.constraints_changed_at=stamp.astimezone(timezone.utc)
    assert input_signature([], [period])==before
