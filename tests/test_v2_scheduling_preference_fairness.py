"""Personal preferences never become approval/publication restrictions."""
from datetime import date,time,timedelta,datetime,timezone
import pytest
from sqlalchemy import select
from app import models as m
from app.services.v2_scheduling_policy_service import evaluate_assignment,regenerate_period
from app.services.v2_scheduling_service import approve_schedule,publish_schedule
from app.services.v2_scheduling_coverage_service import rebuild_schedule_warnings
from app.services.v2_scheduling_freshness_service import requires_regeneration
from app.services.v2_scheduling_board_service import serialize_week_board
from test_v2_scheduling_foundation import scheduling_db
from test_v2_scheduling_closure_removals import setup,close,START
from test_v2_scheduling_repair import person,period,shift


def test_preference_override_is_covered_audited_and_can_be_approved_and_published(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        stores,people,week=setup(db,actor,ids)
        for employee in people[1:]:employee.scheduling_active=False
        profile=db.scalar(select(m.EmployeeSchedulingProfile).where(m.EmployeeSchedulingProfile.employee_id==people[0].id))
        profile.max_consecutive_work_days=1
        close(db,actor,stores,[START+timedelta(days=i) for i in range(2,7)])
        close(db,actor,stores[1:],[START,START+timedelta(days=1)])
        result=regenerate_period(db,principal=actor,schedule_period_id=week.id)
        assert result['validation_status']=='VALID' and result['assigned']==2
        assert profile.max_consecutive_work_days==1
        assert not any(w.warning_type=='MAX_CONSECUTIVE_DAYS' for w in rebuild_schedule_warnings(db,schedule_period_id=week.id))
        notes=result['allocation']['management_explanations']
        assert any(n['code']=='CONSECUTIVE_PREFERENCE_OVERRIDDEN' and 'soft preference' in n['message'] for n in notes)
        audit=db.scalar(select(m.AuditLog).where(m.AuditLog.action=='V2:SCHEDULING:SCHEDULE_REGENERATED').order_by(m.AuditLog.id.desc()))
        assert audit.meta['metadata']['allocation']['management_explanations']==notes
        board=lambda flags:serialize_week_board(db,week_start=START,selected_store_ids=stores,
            all_authorized_store_ids=stores,permission_flags=flags,historical_admin=True)
        assert board({'scheduling.view_all':True,'scheduling.edit_draft_shifts':True})['optimization_notes']
        assert board({'scheduling.view_all':True})['optimization_notes']==[]
        restricted=serialize_week_board(db,week_start=START,selected_store_ids=stores[1:],
            all_authorized_store_ids=stores,
            permission_flags={'scheduling.view_all':True,'scheduling.edit_draft_shifts':True},historical_admin=True)
        assert restricted['optimization_notes']==[]
        approve_schedule(db,principal=actor,schedule_period_id=week.id,expected_version=week.version,allowed_store_ids=stores)
        publish_schedule(db,principal=actor,schedule_period_id=week.id,expected_version=week.version,allowed_store_ids=stores)
        assert week.status==m.SchedulePeriodStatus.PUBLISHED


@pytest.mark.parametrize('preferred',[1,2,7])
def test_three_day_ceiling_and_required_rest_cross_week_boundaries(scheduling_db,preferred):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        employee=person(db,actor,'Preference with mandatory rest',lead=True,target=3)
        profile=db.scalar(select(m.EmployeeSchedulingProfile).where(m.EmployeeSchedulingProfile.employee_id==employee.id))
        profile.max_consecutive_work_days=preferred;profile.minimum_days_off_after_max_block=2
        prior=period(db,actor,date(2026,10,25),'PUBLISHED')
        for day in [29,30,31]:shift(db,actor,prior,employee,date(2026,10,day),ids['north'])
        def verdict(day):
            return evaluate_assignment(db,employee_id=employee.id,store_id=ids['north'],shift_date=day,
                start_time=time(9),end_time=time(17))
        assert 'MAX_CONSECUTIVE_DAYS' in {r.code for r in verdict(date(2026,11,1)).reasons}
        assert 'REQUIRED_DAYS_OFF' in {r.code for r in verdict(date(2026,11,2)).reasons}
        assert verdict(date(2026,11,3)).eligible


def test_preference_change_does_not_invalidate_legal_draft_but_rest_change_does(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        employee=person(db,actor,'Preference freshness',lead=True,target=2)
        week=period(db,actor,START)
        shift(db,actor,week,employee,START,ids['north'])
        shift(db,actor,week,employee,START+timedelta(days=1),ids['north'])
        week.generated_at=datetime.now(timezone.utc);db.flush()
        profile=db.scalar(select(m.EmployeeSchedulingProfile).where(m.EmployeeSchedulingProfile.employee_id==employee.id))
        profile.max_consecutive_work_days=1;db.flush()
        assert not requires_regeneration(week)
        profile.minimum_days_off_after_max_block=2;db.flush();db.refresh(week)
        assert requires_regeneration(week)


def test_explanation_survives_repair_of_an_employee_outside_the_current_roster():
    from app.services.scheduling_optimization_explanations import explain_optimization
    positions=[dict(id=1,week=START,date=START,store_id=1)]
    notes=explain_optimization(positions=positions,original={1:99},assignments={1:1},
        names={1:'Current employee'},targets={(1,START):1},preferred_days={1:1},
        effective_dates={1:{START}},avoid_stores=set(),store_names={1:'Store'})
    assert len(notes)==1 and notes[0]['code']=='ALLOCATION_BALANCED'
    assert 'Current employee 0/1 → 1/1' in notes[0]['message']
