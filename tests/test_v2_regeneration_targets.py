"""Targets never override hard rules, and partial drafts retain audit evidence."""
from datetime import timedelta
import pytest
from sqlalchemy import select,func
from app import models as m
from app.services.v2_scheduling_policy_service import regenerate_period
from app.services.v2_scheduling_service import approve_schedule,SchedulingValidationError,SchedulingConflict
from app.services.v2_scheduling_horizon_service import regenerate_schedule_horizon
from app.services.v2_scheduling_board_service import serialize_week_board
from test_v2_scheduling_foundation import scheduling_db
from test_v2_scheduling_closure_removals import setup,remove,START


def full_pto(db,actor,employee,ids):
    row=m.TimeOffRequest(employee_id=employee.id,start_date=START,end_date=START+timedelta(days=6),
        full_day=True,reason_category_id=ids['vacation'],status=m.TimeOffRequestStatus.APPROVED,
        created_by_principal_id=actor.id,updated_by_principal_id=actor.id)
    db.add(row);db.flush();return row


def test_full_week_pto_allows_shortfall_without_reauthorizing_leave(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        stores,people,week=setup(db,actor,ids)
        pto=full_pto(db,actor,people[0],ids)
        result=regenerate_period(db,principal=actor,schedule_period_id=week.id)
        assert result['validation_status']=='VALID',result
        assert not db.scalar(select(m.ScheduleShift.id).where(m.ScheduleShift.schedule_period_id==week.id,m.ScheduleShift.employee_id==people[0].id))
        assert pto.status==m.TimeOffRequestStatus.APPROVED
        assert any(d['code']=='ALLOCATION_SHORTFALL' and d['employee_id']==people[0].id and 'APPROVED_TIME_OFF' in str(d) for d in result['allocation']['diagnostics'])
        assert db.scalar(select(func.count()).select_from(m.SchedulingLeaveAllocationException))==0
        approve_schedule(db,principal=actor,schedule_period_id=week.id,expected_version=week.version,allowed_store_ids=stores)


@pytest.mark.parametrize('horizon',[False,True])
def test_impossible_coverage_saves_usable_draft_with_explanations(scheduling_db,horizon):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        stores,people,week=setup(db,actor,ids)
        regenerate_period(db,principal=actor,schedule_period_id=week.id)
        for employee in people:full_pto(db,actor,employee,ids)
        if horizon:
            result=regenerate_schedule_horizon(db,principal=actor,period_ids=[week.id],planning_date=START,time_limit=5)
            diagnostics=result['diagnostics']
        else:
            result=regenerate_period(db,principal=actor,schedule_period_id=week.id)
            diagnostics=result['allocation']['diagnostics']
        assert result['validation_status']=='INCOMPLETE'
        assert week.status==m.SchedulePeriodStatus.DRAFT
        assert len(list(db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==week.id,m.ScheduleShift.employee_id.is_(None)))))==14
        vacancies=[d for d in diagnostics if d['code']=='UNFILLED_COVERAGE']
        assert len(vacancies)==14 and all('APPROVED_TIME_OFF' in str(d) for d in vacancies)
        with pytest.raises(SchedulingValidationError):
            approve_schedule(db,principal=actor,schedule_period_id=week.id,expected_version=week.version,allowed_store_ids=stores)
        wid=week.id;db.commit()
    with Session() as db:
        assert db.get(m.SchedulePeriod,wid).status==m.SchedulePeriodStatus.DRAFT
        board=serialize_week_board(db,week_start=START,selected_store_ids=stores,
            all_authorized_store_ids=stores,permission_flags={'scheduling.view_all':True,'scheduling.edit_draft_shifts':True},historical_admin=True)
        assert any('Approved time off' in w['message'] or 'Approved PTO' in w['message'] for w in board['warnings']),board['warnings']
        assert db.scalar(select(func.count()).select_from(m.ScheduleGeneratorSnapshot).where(m.ScheduleGeneratorSnapshot.schedule_period_id==wid))==2


def test_locked_removal_requires_explicit_unlock(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        stores,people,week=setup(db,actor,ids)
        regenerate_period(db,principal=actor,schedule_period_id=week.id)
        row=db.scalar(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==week.id))
        row.manually_locked=True;db.flush();before=(row.employee_id,week.version)
        with pytest.raises(SchedulingConflict,match='Unlock'):
            remove(db,actor,week,row)
        assert (row.employee_id,week.version)==before


def test_locked_assignments_survive_target_shortfall(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        stores,people,week=setup(db,actor,ids)
        regenerate_period(db,principal=actor,schedule_period_id=week.id)
        row=db.scalar(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==week.id))
        row.manually_locked=True;db.flush()
        before=(row.id,row.employee_id,row.shift_date,row.start_time,row.end_time,row.lock_reason)
        profile=db.scalar(select(m.EmployeeSchedulingProfile).where(m.EmployeeSchedulingProfile.employee_id==row.employee_id))
        profile.target_shifts_per_week=0;db.flush()
        result=regenerate_period(db,principal=actor,schedule_period_id=week.id)
        assert result['validation_status']=='VALID'
        assert (row.id,row.employee_id,row.shift_date,row.start_time,row.end_time,row.lock_reason)==before


def test_entirely_closed_special_store_needs_no_traveler(scheduling_db):
    from app.services.v2_scheduling_policy_service import configure_special_store
    from test_v2_scheduling_closure_removals import close
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        stores,people,week=setup(db,actor,ids)
        configure_special_store(db,principal=actor,store_id=stores[0],
            primary_employee_ids=tuple(e.id for e in people[:2]),rotation_employee_ids=tuple(e.id for e in people[2:]))
        close(db,actor,stores,[START+timedelta(days=i) for i in range(7)])
        result=regenerate_period(db,principal=actor,schedule_period_id=week.id)
        assert result['validation_status']=='VALID',result
        assert result['positions']==0 or not list(db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==week.id)))
        approve_schedule(db,principal=actor,schedule_period_id=week.id,expected_version=week.version,allowed_store_ids=stores)


def test_invalid_removal_does_not_clear_designations(scheduling_db):
    from app.services.v2_scheduling_service import update_shift,ShiftInput
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        stores,people,week=setup(db,actor,ids)
        regenerate_period(db,principal=actor,schedule_period_id=week.id)
        row=db.scalar(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==week.id,m.ScheduleShift.is_lead_of_day.is_(True)))
        row.lead_of_day_manually_assigned=True;db.flush()
        before=(row.employee_id,row.is_lead_of_day,row.lead_of_day_manually_assigned,week.version)
        with pytest.raises(SchedulingValidationError):
            update_shift(db,principal=actor,schedule_period_id=week.id,shift_id=row.id,expected_version=week.version,
                allowed_store_ids=stores,values=ShiftInput(employee_id=None,store_id=row.store_id,shift_date=row.shift_date,
                    start_time=row.end_time,end_time=row.start_time))
        db.flush()
        assert (row.employee_id,row.is_lead_of_day,row.lead_of_day_manually_assigned,week.version)==before
