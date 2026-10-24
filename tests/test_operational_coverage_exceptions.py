"""One-action overrides leave policy/PTO intact and retain hard boundaries."""
from datetime import time,timedelta,datetime,timezone
import pytest
from sqlalchemy import select
from app import models as m
from app.auth import Principal,Role
from app.services.v2_scheduling_service import update_shift,ShiftInput,SchedulingValidationError,approve_schedule
from app.services.v2_scheduling_exception_service import record_commitment
from app.services.v2_scheduling_policy_service import evaluate_assignment
from test_atomic_employee_swaps import primary,setup,owner,scheduling_db
from test_scheduling_lifecycle_integrity import complete,args


def values(s,eid):
    return ShiftInput(employee_id=eid,store_id=s.store_id,shift_date=s.shift_date,
        start_time=s.start_time,end_time=s.end_time,unpaid_break_minutes=s.unpaid_break_minutes)


@pytest.mark.parametrize('manual_edit',[False,True])
@pytest.mark.parametrize('role',['ADMIN','LEAD','STORE'])
def test_manual_exception_is_owner_only_and_preserves_normal_rules(primary,role,manual_edit):
    a,b,_,_=setup(primary,configure_profiles=False);o=owner(primary) if role=='ADMIN' else Principal(a.id,a.username,Role(role),None,True)
    with primary.Session() as db:
        p,rows=complete(db,primary.manager,primary.ids);s=rows[0]
        db.add(m.EmployeeSchedulingStorePreference(employee_id=primary.ids['blair'],store_id=s.store_id,
            preference_level=m.StorePreferenceLevel.NEVER,created_by_principal_id=a.id,updated_by_principal_id=a.id))
        profile=db.scalar(select(m.EmployeeSchedulingProfile).where(m.EmployeeSchedulingProfile.employee_id==primary.ids['blair']));profile.maximum_weekly_hours=1
        db.commit();before=s.employee_id
        if role!='ADMIN':
            with pytest.raises(PermissionError):update_shift(db,**args(p,o,primary.ids),shift_id=s.id,values=values(s,primary.ids['blair']),manual_edit=manual_edit,operational_override=True,override_reason='Sick-call coverage')
            db.rollback();assert s.employee_id==before
        else:
            update_shift(db,**args(p,o,primary.ids),shift_id=s.id,values=values(s,primary.ids['blair']),manual_edit=manual_edit,operational_override=True,override_reason='Sick-call coverage');db.commit()
            assert s.employee_id==primary.ids['blair'] and s.manually_locked and s.is_lead_of_day
            audit=db.scalar(select(m.AuditLog).where(m.AuditLog.action=='V2:SCHEDULING:SHIFT_CHANGED').order_by(m.AuditLog.id.desc()))
            assert {r['code'] for r in audit.meta['metadata']['operational_exceptions']}=={'STORE_NEVER','MAX_WEEKLY_HOURS'}
            # Exact assignment exceptions survive refresh; an ordinary new proposal does not inherit them.
            db.expire_all();approve_schedule(db,**args(p,o,primary.ids));db.commit()
            verdict=evaluate_assignment(db,employee_id=primary.ids['blair'],store_id=s.store_id,shift_date=s.shift_date,
                start_time=s.start_time,end_time=s.end_time,exclude_shift_id=s.id,schedule_period_id=p.id)
            assert not verdict.eligible and profile.maximum_weekly_hours==1


def test_prospective_sick_call_pto_and_store_conflicts_are_warnings(primary):
    a,b,pid,sids=setup(primary,configure_profiles=False);o=owner(primary)
    with primary.Session() as db:
        source=db.get(m.ScheduleShift,sids[0]);person=primary.ids['blair']
        db.add(m.EmployeeSchedulingStorePreference(employee_id=person,store_id=source.store_id,
            preference_level=m.StorePreferenceLevel.NEVER,created_by_principal_id=o.id,updated_by_principal_id=o.id))
        db.add(m.TimeOffRequest(employee_id=person,start_date=source.shift_date,end_date=source.shift_date,full_day=True,
            status=m.TimeOffRequestStatus.APPROVED,reason_category_id=primary.ids['vacation'],
            submitted_by_principal_id=o.id,created_by_principal_id=o.id,updated_by_principal_id=o.id))
        db.commit()
        row=record_commitment(db,principal=o,shift_id=source.id,employee_id=person);db.commit()
        assert row.employee_id==person and source.employee_id==primary.ids['alex']
        assert db.scalar(select(m.TimeOffRequest)).status==m.TimeOffRequestStatus.APPROVED
        audit=db.scalar(select(m.AuditLog).where(m.AuditLog.action=='V2:SCHEDULING:COVERAGE_COMMITTED'))
        assert {'APPROVED_TIME_OFF','STORE_NEVER'} <= {r['code'] for r in audit.meta['metadata']['warnings']}


def test_owner_cannot_publish_overlap_or_missing_qualification(primary):
    from app.services.scheduling_completion_service import approve_and_publish_schedule
    a,b,_,_=setup(primary,configure_profiles=False);o=owner(primary)
    with primary.Session() as db:
        p,rows=complete(db,primary.manager,primary.ids);rows[0].is_lead_of_day=False;db.commit()
        with pytest.raises(SchedulingValidationError,match='Cannot override'):
            approve_and_publish_schedule(db,**args(p,o,primary.ids),allow_serious_warnings=True,confirmed=True,override_reason='Cannot waive qualification')
        db.rollback();assert p.status==m.SchedulePeriodStatus.DRAFT


@pytest.mark.parametrize('conflict',['hours','consecutive','availability','multiple'])
def test_sick_call_conflicts_warn_without_exception_approval(primary,conflict):
    a,b,pid,sids=setup(primary);o=owner(primary)
    with primary.Session() as db:
        source=db.get(m.ScheduleShift,sids[0]);eid=primary.ids['blair'];expected=set()
        if conflict in ('hours','multiple'):
            db.scalar(select(m.EmployeeSchedulingProfile).where(m.EmployeeSchedulingProfile.employee_id==eid)).maximum_weekly_hours=1
            expected.add('MAX_WEEKLY_HOURS')
        if conflict in ('availability','multiple'):
            db.add(m.EmployeeSchedulingWindow(employee_id=eid,day_of_week=0,start_time=time(0),end_time=time(23,59),kind=m.SchedulingWindowKind.HARD_UNAVAILABLE,created_by_principal_id=o.id,updated_by_principal_id=o.id))
            expected.add('HARD_WEEKDAY_LOCKOUT')
        if conflict=='multiple':
            db.add(m.EmployeeSchedulingStorePreference(employee_id=eid,store_id=source.store_id,preference_level=m.StorePreferenceLevel.NEVER,created_by_principal_id=o.id,updated_by_principal_id=o.id))
            expected.add('STORE_NEVER')
        if conflict=='consecutive':
            prior=m.SchedulePeriod(week_start_date=source.shift_date-timedelta(days=7),week_end_date=source.shift_date-timedelta(days=1),status=m.SchedulePeriodStatus.PUBLISHED,revision_number=1,created_by_principal_id=o.id,updated_by_principal_id=o.id)
            db.add(prior);db.flush()
            for offset in (-3,-2,-1):db.add(m.ScheduleShift(schedule_period_id=prior.id,employee_id=eid,store_id=source.store_id,shift_date=source.shift_date+timedelta(days=offset),start_time=time(9),end_time=time(17),created_by_principal_id=o.id,updated_by_principal_id=o.id))
            expected.add('MAX_CONSECUTIVE_DAYS')
        db.commit()
        row=record_commitment(db,principal=o,shift_id=source.id,employee_id=eid);db.commit()
        db.expire_all();assert db.get(m.ScheduleCoverageCommitment,row.id).employee_id==eid
        audit=db.scalar(select(m.AuditLog).where(m.AuditLog.action=='V2:SCHEDULING:COVERAGE_COMMITTED'))
        assert expected <= {r['code'] for r in audit.meta['metadata']['warnings']}
        assert audit.meta['metadata']['original_employee_id']==source.employee_id
        # Recording an exception never erases the employee's underlying restrictions.
        from app.services.v2_scheduling_exception_service import commitment_eligibility
        verdict=commitment_eligibility(db,source,eid,today=source.shift_date-timedelta(days=1))
        assert verdict.eligible and expected <= {r.code for r in verdict.reasons}


def test_manual_move_requires_separate_pto_exception_and_preserves_request(primary):
    a,_,_,_=setup(primary,configure_profiles=False);o=owner(primary)
    with primary.Session() as db:
        p,rows=complete(db,primary.manager,primary.ids);s=rows[0];before=s.employee_id
        pto=m.TimeOffRequest(employee_id=primary.ids['blair'],start_date=s.shift_date,end_date=s.shift_date,
            full_day=True,status=m.TimeOffRequestStatus.APPROVED,reason_category_id=primary.ids['vacation'],
            submitted_by_principal_id=o.id,created_by_principal_id=o.id,updated_by_principal_id=o.id)
        db.add(pto);db.commit();pto_id=pto.id
        with pytest.raises(SchedulingValidationError,match='PTO|time off'):
            update_shift(db,**args(p,o,primary.ids),shift_id=s.id,values=values(s,primary.ids['blair']),
                manual_edit=True,operational_override=True,override_reason='Emergency coverage')
        db.rollback();assert s.employee_id==before
        update_shift(db,**args(p,o,primary.ids),shift_id=s.id,values=values(s,primary.ids['blair']),
            manual_edit=True,operational_override=True,override_pto=True,override_reason='Separately confirmed PTO coverage')
        db.commit();assert s.employee_id==primary.ids['blair']
        assert db.get(m.TimeOffRequest,pto_id).status==m.TimeOffRequestStatus.APPROVED
        audit=db.scalar(select(m.AuditLog).where(m.AuditLog.action=='V2:SCHEDULING:SHIFT_CHANGED').order_by(m.AuditLog.id.desc()))
        assert audit.meta['reason']=='Separately confirmed PTO coverage'
        assert 'APPROVED_TIME_OFF' in {r['code'] for r in audit.meta['metadata']['operational_exceptions']}
