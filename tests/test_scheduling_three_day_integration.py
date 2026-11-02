from datetime import date, timedelta
from sqlalchemy import select
from app import models as m
from app.services.scheduling_preferences import prefers_three_day_blocks
from app.services.v2_scheduling_policy_service import consecutive_preference_score
from app.services.v2_scheduling_horizon_service import plan_schedule_horizon, apply_schedule_horizon_plan
from app.services.v2_scheduling_assignments_service import reconcile_lead_designations
from test_v2_scheduling_foundation import scheduling_db
from test_v2_scheduling_repair import person,period,shift


def test_stored_three_day_preference_reaches_planner_and_survives_lead_reconciliation(scheduling_db,monkeypatch):
    from app.services import scheduling_horizon_solver as solver
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        for e in db.scalars(select(m.Employee)):e.scheduling_active=False
        lexi=person(db,actor,'Three day employee',lead=True,target=3)
        other=person(db,actor,'Default employee',lead=True,target=3)
        profile=db.scalar(select(m.EmployeeSchedulingProfile).where(m.EmployeeSchedulingProfile.employee_id==other.id))
        profile.max_consecutive_work_days=None
        per=period(db,actor,date(2026,11,1))
        for offset in range(1,7):shift(db,actor,per,None,per.week_start_date+timedelta(days=offset),ids['north'])
        db.flush()
        seen=[];real=solver.solve_horizon
        def capture(**kwargs):
            seen.append(kwargs)
            return real(**kwargs)
        monkeypatch.setattr(solver,'solve_horizon',capture)
        plan=plan_schedule_horizon(db,periods=[per],planning_date=date(2026,11,2),repair=False)
        assert seen[0]['limits'][lexi.id]['prefer_three_day_blocks']
        assert not seen[0]['limits'][other.id]['prefer_three_day_blocks']
        apply_schedule_horizon_plan(db,periods=[per],plan=plan,principal=actor)
        before={r.id:r.employee_id for r in db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==per.id))}
        dates={r.shift_date for r in db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==per.id,m.ScheduleShift.employee_id==lexi.id))}
        assert len(dates)==3 and max(dates)-min(dates)==timedelta(days=2)
        assert reconcile_lead_designations(db,schedule_period_id=per.id,planning_date=date(2026,11,2))==[]
        assert before=={r.id:r.employee_id for r in db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==per.id))}
        repair=plan_schedule_horizon(db,periods=[per],planning_date=date(2026,11,2),repair=True)
        assert repair['assignments']==before
        assert db.scalar(select(m.EmployeeSchedulingProfile.max_consecutive_work_days).where(m.EmployeeSchedulingProfile.employee_id==lexi.id))==3


def test_legacy_candidate_score_rewards_completing_three_instead_of_splitting(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        e=person(db,actor,'Three day candidate')
        per=period(db,actor,date(2026,11,1))
        for offset in [1,2]:shift(db,actor,per,e,per.week_start_date+timedelta(days=offset),ids['north'])
        complete=consecutive_preference_score(db,employee_id=e.id,shift_date=per.week_start_date+timedelta(days=3))
        scattered=consecutive_preference_score(db,employee_id=e.id,shift_date=per.week_start_date+timedelta(days=5))
        assert complete==100 and scattered==0
        assert not prefers_three_day_blocks(None)
        profile=db.scalar(select(m.EmployeeSchedulingProfile).where(m.EmployeeSchedulingProfile.employee_id==e.id))
        assert prefers_three_day_blocks(profile)
        for preference in [None,1,2,7]:
            profile.max_consecutive_work_days=preference
            assert not prefers_three_day_blocks(profile)
