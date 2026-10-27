"""PTO workflow acceptance and scheduling regressions in disposable databases."""
import json
from collections import Counter
from datetime import date, time, timedelta
import pytest
from sqlalchemy import select
from app import models as m
from app.auth import Principal, Role
from test_v2_scheduling_foundation import scheduling_db
from test_v2_scheduling_repair import person,period,shift
from app.services.v2_scheduling_rules_service import create_time_off_request,review_time_off_request,TimeOffInput
from app.services.v2_scheduling_policy_service import evaluate_assignment
from app.services.v2_scheduling_horizon_service import plan_schedule_horizon
from app.services.v2_scheduling_repair_service import allocate_weekly_positions
from app.services.v2_scheduling_service import approve_schedule,publish_schedule,SchedulingValidationError,SchedulingConflict

@pytest.fixture(autouse=True)
def fixed_pto_business_date(monkeypatch):
    from app.services import v2_scheduling_lead_duty_service as duty
    monkeypatch.setattr(duty,'business_today',lambda db, **kwargs:date(2026,10,20))


def setup(db,actor,ids):
    for e in db.scalars(select(m.Employee)):e.scheduling_active=False
    staff=[person(db,actor,name,lead=True,target=n) for name,n in [('Synthetic Cameron',3),('Synthetic Colleague',3),('Synthetic Reserve',1)]]
    for e in staff:
        db.scalar(select(m.EmployeeSchedulingProfile).where(m.EmployeeSchedulingProfile.employee_id==e.id)).minimum_days_off_after_max_block=1
    return staff

def request(db,actor,ids,e,start,end):
    return create_time_off_request(db,principal=actor,management_entered=False,values=TimeOffInput(
        employee_id=e.id,start_date=start,end_date=end,full_day=True,reason_category_id=ids['vacation']))

@pytest.mark.parametrize('days',[1,2,3,4,5,7])
def test_investigation_pto_days(scheduling_db,days):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        cam,other,reserve=setup(db,actor,ids)
        week=date(2026,11,8);per=period(db,actor,week)
        rows=[shift(db,actor,per,None,week+timedelta(days=i),ids['north']) for i in range(7)]
        start=week+timedelta(days=2) if days<=5 else week
        row=request(db,actor,ids,cam,start,start+timedelta(days=days-1))
        assert row.status==m.TimeOffRequestStatus.PENDING
        assert evaluate_assignment(db,employee_id=cam.id,store_id=ids['north'],shift_date=start,start_time=time(9),end_time=time(10)).eligible
        review_time_off_request(db,principal=actor,request_id=row.id,status=m.TimeOffRequestStatus.APPROVED,
            management_review_note='Synthetic controlled investigation acknowledgement.',analysis_date=date(2026,10,20))
        plan=plan_schedule_horizon(db,periods=[per],planning_date=date(2026,10,20),time_limit=5)
        if days<=4:
            assert plan['assignments'] is not None
            assert Counter(plan['assignments'].values())=={cam.id:3,other.id:3,reserve.id:1}
            assert all(plan['assignments'][r.id]!=cam.id for r in rows if row.start_date<=r.shift_date<=row.end_date)
        else:
            assert plan['assignments'] is not None
            assert all(plan['assignments'][r.id]!=cam.id for r in rows if row.start_date<=r.shift_date<=row.end_date)
        legacy=allocate_weekly_positions(db,principal=actor,period=per,planning_date=date(2026,10,20))
        if days<=4:
            assert legacy['mode']=='JOINT_CONSTRAINT_OPTIMIZATION'
            assert Counter(r.employee_id for r in rows)=={cam.id:3,other.id:3,reserve.id:1}
        if days>=5:
            assert legacy['status']=='PASS' and not legacy['exceptions']
            assert any(d['code']=='ALLOCATION_SHORTFALL' for d in legacy['diagnostics'])
            assert all(r.employee_id is not None for r in rows)
        assert not any(r.employee_id==cam.id and row.start_date<=r.shift_date<=row.end_date for r in rows)

def test_investigation_cross_week(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        cam,other,reserve=setup(db,actor,ids)
        weeks=[period(db,actor,date(2026,11,8)+timedelta(weeks=i)) for i in range(2)]
        rows=[shift(db,actor,p,None,p.week_start_date+timedelta(days=i),ids['north']) for p in weeks for i in range(7)]
        row=request(db,actor,ids,cam,date(2026,11,13),date(2026,11,16))
        review_time_off_request(db,principal=actor,request_id=row.id,status=m.TimeOffRequestStatus.APPROVED,
            management_review_note='Synthetic investigation acknowledgement.',analysis_date=date(2026,10,20))
        plan=plan_schedule_horizon(db,periods=weeks,planning_date=date(2026,10,20),time_limit=8)
        assert plan['assignments'] is not None and not plan['allocation_exceptions']
        for p in weeks:
            assert Counter(plan['assignments'][r.id] for r in rows if r.schedule_period_id==p.id)=={cam.id:3,other.id:3,reserve.id:1}
        assert all(plan['assignments'][r.id]!=cam.id for r in rows if row.start_date<=r.shift_date<=row.end_date)

def test_investigation_approval_after_draft(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        cam,other,reserve=setup(db,actor,ids)
        per=period(db,actor,date(2026,11,8))
        row=shift(db,actor,per,cam,date(2026,11,10),ids['north'])
        pto=request(db,actor,ids,cam,row.shift_date,row.shift_date)
        before=(row.employee_id,per.version)
        review_time_off_request(db,principal=actor,request_id=pto.id,status=m.TimeOffRequestStatus.APPROVED,
            management_review_note='Synthetic acknowledgement.',analysis_date=date(2026,10,20))
        assert row.employee_id==before[0]
        warnings=list(db.scalars(select(m.ScheduleWarning).where(m.ScheduleWarning.schedule_period_id==per.id)))
        assert any(w.warning_type=='APPROVED_TIME_OFF' for w in warnings)
        for gate in (approve_schedule,publish_schedule):
            with pytest.raises(SchedulingValidationError):
                gate(db,principal=actor,schedule_period_id=per.id,expected_version=per.version,allowed_store_ids=(ids['north'],ids['south']))

def test_investigation_employee_routes():
    from app.main import app
    paths=[dict(path=r.path,methods=sorted(getattr(r,'methods',[]))) for r in app.routes if 'time-off' in getattr(r,'path','') or 'time_off' in getattr(r,'path','')]
    assert any(r['path']=='/v2/scheduling/my-time-off' and 'POST' in r['methods'] for r in paths)

def test_investigation_five_employee_pto_cycle(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        for e in db.scalars(select(m.Employee)):e.scheduling_active=False
        people=[person(db,actor,f'Cycle employee {i}',lead=True,target=1) for i in range(5)]
        per=period(db,actor,date(2026,11,8))
        dates=[date(2026,11,10),date(2026,11,11),date(2026,11,12),date(2026,11,13),date(2026,11,9)]
        rows=[shift(db,actor,per,e,dates[i],ids['north'] if i%2 else ids['south']) for i,e in enumerate(people)]
        for i,e in enumerate(people):
            valid={dates[i].weekday(),dates[(i+1)%5].weekday()}
            for day in set(range(7))-valid:
                db.add(m.EmployeeSchedulingWindow(employee_id=e.id,day_of_week=(day+1)%7,
                    start_time=time(0),end_time=time(23,59),kind=m.SchedulingWindowKind.HARD_UNAVAILABLE,
                    active=True,created_by_principal_id=actor.id,updated_by_principal_id=actor.id))
        row=request(db,actor,ids,people[0],dates[0],dates[0])
        review_time_off_request(db,principal=actor,request_id=row.id,status=m.TimeOffRequestStatus.APPROVED,
            management_review_note='Synthetic cycle acknowledgement.',analysis_date=date(2026,10,20))
        plan=plan_schedule_horizon(db,periods=[per],planning_date=date(2026,10,20),time_limit=5)
        assert plan['assignments'] is not None
        assert sum(plan['assignments'][r.id]!=r.employee_id for r in rows)==1
        assert plan['assignments'][rows[0].id]!=people[0].id
        assert all(value is not None for value in plan['assignments'].values())
        from app.services.v2_scheduling_horizon_service import apply_schedule_horizon_plan
        apply_schedule_horizon_plan(db,periods=[per],plan=plan,principal=actor)

def test_investigation_manual_pto_override_rejected(scheduling_db):
    from app.services.v2_scheduling_service import create_shift,ShiftInput
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        cam,other,reserve=setup(db,actor,ids)
        per=period(db,actor,date(2026,11,8))
        row=request(db,actor,ids,cam,date(2026,11,10),date(2026,11,10))
        review_time_off_request(db,principal=actor,request_id=row.id,status=m.TimeOffRequestStatus.APPROVED,
            management_review_note='Synthetic acknowledgement.',analysis_date=date(2026,10,20))
        for override in [False,True]:
            with pytest.raises(SchedulingValidationError):
                create_shift(db,principal=actor,schedule_period_id=per.id,expected_version=per.version,
                    allowed_store_ids=(ids['north'],ids['south']),values=ShiftInput(cam.id,ids['north'],row.start_date,time(9),time(10)),
                    allow_hard_unavailability_override=override,override_reason='Cannot override PTO')

@pytest.mark.parametrize('entry',['manual','cron','regenerate'])
def test_investigation_generation_entrypoints(scheduling_db,monkeypatch,entry):
    from datetime import datetime,timezone
    from test_v2_scheduling_foundation import _coverage
    from app.services.v2_scheduling_policy_service import organization_policy,manual_generate_draft_schedule,run_schedule_automation,regenerate_period
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        cam,other,reserve=setup(db,actor,ids)
        for day in range(7):_coverage(db,actor,ids,weekday=day)
        policy=organization_policy(db,principal_id=actor.id)
        policy.schedule_length_weeks=1;policy.replenish_at_weeks=0;policy.active=True
        row=request(db,actor,ids,cam,date(2026,11,10),date(2026,11,10))
        review_time_off_request(db,principal=actor,request_id=row.id,status=m.TimeOffRequestStatus.APPROVED,
            management_review_note='Synthetic entrypoint acknowledgement.',analysis_date=date(2026,10,20))
        if entry=='regenerate':
            per=period(db,actor,date(2026,11,8))
            regenerate_period(db,principal=actor,schedule_period_id=per.id)
            result=regenerate_period(db,principal=actor,schedule_period_id=per.id)
        else:
            if entry=='cron':monkeypatch.setenv('SCHEDULE_AUTOMATION_EXECUTION_ENABLED','true')
            fn=run_schedule_automation if entry=='cron' else manual_generate_draft_schedule
            result=fn(db,principal=actor,now=datetime(2026,11,3,18,tzinfo=timezone.utc))
            per=db.scalar(select(m.SchedulePeriod).where(m.SchedulePeriod.week_start_date==date(2026,11,8)))
        rows=list(db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==per.id)))
        assert len(rows)==7
        assert Counter(r.employee_id for r in rows)=={cam.id:3,other.id:3,reserve.id:1}
        assert all(r.employee_id!=cam.id for r in rows if r.shift_date==date(2026,11,10))

@pytest.mark.parametrize('days',[5,7])
def test_investigation_exception_generation_vs_approval(scheduling_db,days):
    from test_v2_scheduling_foundation import _coverage
    from app.services.v2_scheduling_policy_service import regenerate_period
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        cam,other,reserve=setup(db,actor,ids)
        for day in range(7):_coverage(db,actor,ids,weekday=day)
        per=period(db,actor,date(2026,11,8))
        start=date(2026,11,10) if days==5 else per.week_start_date
        row=request(db,actor,ids,cam,start,start+timedelta(days=days-1))
        review_time_off_request(db,principal=actor,request_id=row.id,status=m.TimeOffRequestStatus.APPROVED,
            management_review_note='Synthetic exception acknowledgement.',analysis_date=date(2026,10,20))
        result=regenerate_period(db,principal=actor,schedule_period_id=per.id)
        approve_schedule(db,principal=actor,schedule_period_id=per.id,expected_version=per.version,
            allowed_store_ids=(ids['north'],ids['south']))
        assert result['validation_status']=='VALID'
        assert result['completion_state']=='VALID'
        assert result['unmet_shift_targets']
        assert not result['reserve_fallbacks']
        assert not result['allocation']['exceptions']

from dataclasses import replace
from test_v2_primary import primary,login,post
from app.services.v2_scheduling_pto_self_service import save_own_request,own_employee
from app.services.v2_scheduling_allocation_service import set_leave_exception,weekly_allocation,allocation_on
from app.services.v2_scheduling_rules_service import TimeOffInput


def own_account(db,employee,role=m.PrincipalRole.STORE):
    user=m.Principal(username=f'pto-{employee.id}@example.test',password_hash='unused',role=role,active=True)
    db.add(user);db.flush();employee.principal_id=user.id;db.flush()
    return Principal(user.id,user.username,Role(role.value),None,True)


def test_employee_pending_lifecycle_ownership_stale_and_audit(scheduling_db):
    Session,manager,ids,_=scheduling_db
    with Session() as db:
        employee=db.get(m.Employee,ids['alex']);actor=own_account(db,employee)
        values=TimeOffInput(employee.id,date(2026,11,10),date(2026,11,11),True,ids['vacation'])
        row=save_own_request(db,principal=actor,values=values)
        assert row.status==m.TimeOffRequestStatus.PENDING
        with pytest.raises(PermissionError):
            save_own_request(db,principal=actor,values=replace(values,employee_id=ids['blair']))
        other=own_account(db,db.get(m.Employee,ids['blair']))
        with pytest.raises(PermissionError):
            save_own_request(db,principal=other,request_id=row.id,expected_revision=row.revision,action='cancel')
        save_own_request(db,principal=actor,request_id=row.id,expected_revision=1,values=replace(values,end_date=date(2026,11,12)))
        with pytest.raises(SchedulingConflict,match='Reload'):
            save_own_request(db,principal=actor,request_id=row.id,expected_revision=1,action='cancel')
        save_own_request(db,principal=actor,request_id=row.id,expected_revision=2,action='cancel')
        assert row.status==m.TimeOffRequestStatus.CANCELLED
        events=list(db.scalars(select(m.AuditLog).where(m.AuditLog.action.in_([
            'V2:SCHEDULING:TIME_OFF_EDITED','V2:SCHEDULING:TIME_OFF_CANCELLED']))))
        assert len(events)==2 and all(e.actor_principal_id==actor.id for e in events)
        assert events[0].meta['before']['end_date']=='2026-11-11'


@pytest.mark.parametrize('cancel',[False,True])
@pytest.mark.parametrize('approve',[False,True])
def test_approved_changes_preserve_original_until_management_decides(scheduling_db,cancel,approve):
    Session,manager,ids,_=scheduling_db
    with Session() as db:
        employee=db.get(m.Employee,ids['alex']);actor=own_account(db,employee)
        values=TimeOffInput(employee.id,date(2026,11,10),date(2026,11,10),True,ids['vacation'])
        original=save_own_request(db,principal=actor,values=values)
        review_time_off_request(db,principal=manager,request_id=original.id,status=m.TimeOffRequestStatus.APPROVED,
            management_review_note='Synthetic approval')
        with pytest.raises(SchedulingConflict,match='Only pending'):
            save_own_request(db,principal=actor,request_id=original.id,expected_revision=original.revision,action='cancel')
        amendment=save_own_request(db,principal=actor,request_id=original.id,expected_revision=original.revision,
            values=replace(values,start_date=date(2026,11,12),end_date=date(2026,11,12)),
            action='request_cancel' if cancel else 'amend',note='Please review changed plans')
        assert original.status==m.TimeOffRequestStatus.APPROVED and amendment.status==m.TimeOffRequestStatus.PENDING
        assert not evaluate_assignment(db,employee_id=employee.id,store_id=ids['north'],shift_date=date(2026,11,10),start_time=time(9),end_time=time(10)).eligible
        with pytest.raises(PermissionError):
            review_time_off_request(db,principal=actor,request_id=amendment.id,status=m.TimeOffRequestStatus.APPROVED)
        review_time_off_request(db,principal=manager,request_id=amendment.id,
            status=m.TimeOffRequestStatus.APPROVED if approve else m.TimeOffRequestStatus.DENIED,
            management_review_note='Reviewed changed plans')
        assert original.status==(m.TimeOffRequestStatus.CANCELLED if approve else m.TimeOffRequestStatus.APPROVED)
        assert amendment.status==(m.TimeOffRequestStatus.CANCELLED if cancel and approve else m.TimeOffRequestStatus.APPROVED if approve else m.TimeOffRequestStatus.DENIED)
        allowed=evaluate_assignment(db,employee_id=employee.id,store_id=ids['north'],shift_date=date(2026,11,10),start_time=time(9),end_time=time(10)).eligible
        assert allowed==approve


@pytest.mark.parametrize('weeks',[1,2])
def test_explicit_full_week_leave_allocation_and_publication(scheduling_db,weeks):
    from app.services.v2_scheduling_policy_service import regenerate_period
    from test_v2_scheduling_foundation import _coverage
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        cam,other,reserve=setup(db,actor,ids)
        # Required four positions match remaining authorized capacity after leave.
        for day in (0,2,4,6):_coverage(db,actor,ids,weekday=day)
        start=date(2026,11,8)
        pto=request(db,actor,ids,cam,start,start+timedelta(days=7*weeks-1))
        review_time_off_request(db,principal=actor,request_id=pto.id,status=m.TimeOffRequestStatus.APPROVED,
            management_review_note='Approve full vacation')
        for i in range(weeks):
            week=start+timedelta(weeks=i)
            assert weekly_allocation(db,cam.id,week)==3
            exception=set_leave_exception(db,principal=actor,request_id=pto.id,week=week,reason='Authorize zero work for full-week vacation')
            assert exception.normal_shifts==3 and weekly_allocation(db,cam.id,week)==0
            per=period(db,actor,week)
            result=regenerate_period(db,principal=actor,schedule_period_id=per.id)
            assert result['validation_status']==result['completion_state']=='VALID'
            assert {r['employee_id']:r['actual_shifts'] for r in result['effective_allocations']}=={cam.id:0,other.id:3,reserve.id:1}
            args=dict(principal=actor,schedule_period_id=per.id,expected_version=per.version,allowed_store_ids=(ids['north'],ids['south']))
            approve_schedule(db,**args);publish_schedule(db,**args)
            assert per.status==m.SchedulePeriodStatus.PUBLISHED
        assert allocation_on(db,cam.id,start)==3
        assert weekly_allocation(db,cam.id,start+timedelta(weeks=weeks))==3
        assert db.scalar(select(m.EmployeeSchedulingProfile.target_shifts_per_week).where(m.EmployeeSchedulingProfile.employee_id==cam.id))==3


def test_leave_exception_permissions_validation_revocation_and_shortage(scheduling_db):
    from app.services.v2_scheduling_policy_service import regenerate_period
    from test_v2_scheduling_foundation import _coverage
    Session,manager,ids,_=scheduling_db
    with Session() as db:
        cam,other,reserve=setup(db,manager,ids);employee=own_account(db,cam)
        start=date(2026,11,8)
        pto=request(db,manager,ids,cam,start,start+timedelta(days=6))
        with pytest.raises(PermissionError):set_leave_exception(db,principal=employee,request_id=pto.id,week=start,reason='Unauthorized')
        with pytest.raises(SchedulingValidationError):set_leave_exception(db,principal=manager,request_id=pto.id,week=start,reason='Not approved')
        review_time_off_request(db,principal=manager,request_id=pto.id,status=m.TimeOffRequestStatus.APPROVED,management_review_note='Approved')
        with pytest.raises(SchedulingValidationError):set_leave_exception(db,principal=manager,request_id=pto.id,week=start+timedelta(days=1),reason='Wrong week')
        exception=set_leave_exception(db,principal=manager,request_id=pto.id,week=start,reason='Authorized leave')
        with pytest.raises(SchedulingValidationError):set_leave_exception(db,principal=manager,request_id=pto.id,week=start,reason='Duplicate')
        for day in range(7):_coverage(db,manager,ids,weekday=day)
        per=period(db,manager,start)
        result=regenerate_period(db,principal=manager,schedule_period_id=per.id)
        assert result['validation_status']=='VALID' and result['completion_state']=='VALID'
        rows=list(db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==per.id)))
        assert all(r.employee_id is not None and r.employee_id!=cam.id for r in rows)
        before=[(r.id,r.employee_id) for r in rows]
        set_leave_exception(db,principal=manager,request_id=pto.id,week=start,revoke_id=exception.id,reason='Explicit revocation')
        assert weekly_allocation(db,cam.id,start)==3 and not exception.active
        assert [(r.id,r.employee_id) for r in rows]==before
        assert per.constraints_changed_at is not None


def test_http_employee_pto_complete_flow_and_csrf(primary):
    primary.account('STORE',True)
    assert login(primary).status_code==303
    c=primary.client
    response=c.get('/v2/scheduling/my-time-off')
    assert response.status_code==200 and 'Request time off' in response.text
    values={'start_date':'2026-11-10','end_date':'2026-11-11','duration':'FULL','reason_category_id':str(primary.ids['vacation']),'employee_note':'Family trip'}
    assert c.post('/v2/scheduling/my-time-off',data=values).status_code==403
    assert post(c,'/v2/scheduling/my-time-off',**values).status_code==303
    with primary.Session() as db:
        row=db.scalar(select(m.TimeOffRequest));request_id=row.id
        assert row.employee_id==primary.ids['alex'] and row.status==m.TimeOffRequestStatus.PENDING
    assert post(c,f'/v2/scheduling/time-off/{request_id}/decision',decision='APPROVED').status_code==403
    assert post(c,f'/v2/scheduling/my-time-off/{request_id}',**{**values,'end_date':'2026-11-12','revision':'1'}).status_code==303
    assert post(c,f'/v2/scheduling/my-time-off/{request_id}',action='cancel',revision='2').status_code==303
    response=c.get('/v2/scheduling/my-time-off')
    assert response.status_code==200 and 'Cancelled' in response.text and 'Time Off Edited' in response.text


def test_source_pto_decision_revokes_exception_without_rewriting_published(scheduling_db):
    Session,manager,ids,_=scheduling_db
    with Session() as db:
        cam,other,reserve=setup(db,manager,ids)
        start=date(2026,11,8)
        pto=request(db,manager,ids,cam,start,start+timedelta(days=6))
        review_time_off_request(db,principal=manager,request_id=pto.id,status=m.TimeOffRequestStatus.APPROVED,management_review_note='Approve')
        exception=set_leave_exception(db,principal=manager,request_id=pto.id,week=start,reason='Authorized vacation')
        per=period(db,manager,start,'PUBLISHED')
        saved=shift(db,manager,per,other,start,ids['north']);before=(saved.employee_id,per.version)
        review_time_off_request(db,principal=manager,request_id=pto.id,status=m.TimeOffRequestStatus.DENIED,management_review_note='Documented reversal')
        assert not exception.active and exception.revoked_by_principal_id==manager.id
        assert weekly_allocation(db,cam.id,start)==3
        assert (saved.employee_id,per.version)==before and per.status==m.SchedulePeriodStatus.PUBLISHED


def test_explicit_permission_denials_and_partial_week_do_not_authorize_leave(scheduling_db):
    Session,manager,ids,_=scheduling_db
    with Session() as db:
        cam,other,reserve=setup(db,manager,ids);actor=own_account(db,cam)
        db.add(m.PrincipalPermissionOverride(principal_id=actor.id,permission_key='scheduling.time_off.submit_own',allowed=False))
        db.flush()
        with pytest.raises(PermissionError):own_employee(db,actor)
        pto=request(db,manager,ids,cam,date(2026,11,10),date(2026,11,14))
        review_time_off_request(db,principal=manager,request_id=pto.id,status=m.TimeOffRequestStatus.APPROVED,management_review_note='Approve partial week')
        with pytest.raises(SchedulingValidationError,match='entire scheduling week'):
            set_leave_exception(db,principal=manager,request_id=pto.id,week=date(2026,11,8),reason='Must not prorate')
        assert weekly_allocation(db,cam.id,date(2026,11,8))==3
        db.add(m.PrincipalPermissionOverride(principal_id=manager.id,permission_key='scheduling.manage_preferences',allowed=False));db.flush()
        with pytest.raises(PermissionError):set_leave_exception(db,principal=manager,request_id=pto.id,week=date(2026,11,8),reason='Denied permission')


def test_http_management_change_review_and_weekly_authorization(primary):
    primary.account('MANAGER',True)
    assert login(primary).status_code==303
    c=primary.client
    with primary.Session() as db:
        cam=db.get(m.Employee,primary.ids['alex'])
        cam.scheduling_active=True
        db.add(m.EmployeeSchedulingProfile(employee_id=cam.id,active=True,target_shifts_per_week=3,
            created_by_principal_id=primary.manager.id,updated_by_principal_id=primary.manager.id))
        pto=request(db,primary.manager,primary.ids,cam,date(2026,11,8),date(2026,11,14));pto_id=pto.id
        review_time_off_request(db,principal=primary.manager,request_id=pto.id,status=m.TimeOffRequestStatus.APPROVED,management_review_note='Vacation')
        db.commit()
    assert c.get(f'/v2/scheduling/time-off/{pto_id}').status_code==200
    fields={'week':'2026-11-08','reason':'Authorize full-week leave'}
    assert c.post(f'/v2/scheduling/time-off/{pto_id}/leave-allocation',data=fields).status_code==403
    assert post(c,f'/v2/scheduling/time-off/{pto_id}/leave-allocation',**fields).status_code==303
    with primary.Session() as db:
        exception=db.scalar(select(m.SchedulingLeaveAllocationException));exception_id=exception.id
        assert weekly_allocation(db,primary.ids['alex'],date(2026,11,8))==0
    page=c.get(f'/v2/scheduling/time-off/{pto_id}')
    assert page.status_code==200 and 'normal allocation at authorization 3' in page.text
    assert post(c,f'/v2/scheduling/time-off/{pto_id}/leave-allocation',**fields,revoke_id=str(exception_id)).status_code==303
    with primary.Session() as db:
        assert weekly_allocation(db,primary.ids['alex'],date(2026,11,8))==3


def test_additive_pto_migration_preserves_existing_records(scheduling_db):
    from alembic import command
    from app.schema_contract import _alembic_config,upgrade_database,assert_supported_schema
    from sqlalchemy import text
    import hashlib
    Session,actor,ids,engine=scheduling_db
    with Session() as db:
        cam=db.get(m.Employee,ids['alex'])
        pto=request(db,actor,ids,cam,date(2026,11,10),date(2026,11,10))
        from test_scheduling_lifecycle_integrity import complete, args
        per, rows = complete(db,actor,ids)
        approve_schedule(db,**args(per,actor,ids))
        publish_schedule(db,**args(per,actor,ids))
        db.commit()
    url=engine.url.render_as_string(hide_password=False)
    command.downgrade(_alembic_config(url),'20261020_0045')
    tables=['employees','employee_scheduling_profiles','time_off_requests','schedule_periods','schedule_shifts','schedule_approvals','schedule_generator_snapshots','audit_log']
    from sqlalchemy import inspect
    original_columns = {table: [c['name'] for c in inspect(engine).get_columns(table)] for table in tables}
    original_foreign_keys = {table: inspect(engine).get_foreign_keys(table) for table in tables}
    quote = engine.dialect.identifier_preparer.quote
    def fingerprint():
        # Preserve every original value, including fields added before this test's
        # starting revision. New defaulted columns are checked separately below.
        with engine.connect() as conn:
            return {table: hashlib.sha256(json.dumps([dict(row) for row in conn.execute(text(
                'SELECT '+','.join(quote(c) for c in original_columns[table])+
                ' FROM '+quote(table)+' ORDER BY id')).mappings()],
                sort_keys=True, default=str).encode()).hexdigest() for table in tables}
    before=fingerprint()
    upgrade_database(url)
    assert fingerprint()==before
    for table in tables:
        after_keys = inspect(engine).get_foreign_keys(table)
        assert all(key in after_keys for key in original_foreign_keys[table])
    with engine.connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM schedule_shifts WHERE is_shift_lead IS DISTINCT FROM false')).scalar_one()==0
        assert conn.execute(text('SELECT count(*) FROM schedule_coverage_commitments WHERE is_shift_lead IS DISTINCT FROM false')).scalar_one()==0
    assert_supported_schema(engine)


def test_preloaded_pending_request_cannot_edit_newly_approved_absence(scheduling_db):
    Session,manager,ids,_=scheduling_db
    with Session() as db:
        employee=db.get(m.Employee,ids['alex']);actor=own_account(db,employee)
        values=TimeOffInput(employee.id,date(2026,11,10),date(2026,11,10),True,ids['vacation'])
        row=save_own_request(db,principal=actor,values=values);request_id=row.id;db.commit()
    with Session() as stale:
        row=stale.get(m.TimeOffRequest,request_id)
        assert row.revision==1 and row.status==m.TimeOffRequestStatus.PENDING
        with Session() as management:
            review_time_off_request(management,principal=manager,request_id=request_id,
                status=m.TimeOffRequestStatus.APPROVED,management_review_note='Reviewed concurrently')
            management.commit()
        with pytest.raises(SchedulingConflict,match='Reload'):
            save_own_request(stale,principal=actor,request_id=request_id,expected_revision=1,
                values=replace(values,start_date=date(2026,11,12),end_date=date(2026,11,12)))
        stale.rollback()
    with Session() as db:
        row=db.get(m.TimeOffRequest,request_id)
        assert row.status==m.TimeOffRequestStatus.APPROVED and row.start_date==date(2026,11,10)


def test_management_cannot_approve_dates_edited_after_review_page_loaded(primary):
    primary.account('MANAGER',True)
    assert login(primary).status_code==303
    c=primary.client
    values={'start_date':'2026-11-10','end_date':'2026-11-11','duration':'FULL','reason_category_id':str(primary.ids['vacation'])}
    assert post(c,'/v2/scheduling/my-time-off',**values).status_code==303
    with primary.Session() as db:request_id=db.scalar(select(m.TimeOffRequest.id))
    page=c.get(f'/v2/scheduling/time-off/{request_id}')
    assert page.status_code==200 and 'name="expected_revision" value="1"' in page.text
    assert post(c,f'/v2/scheduling/my-time-off/{request_id}',**{**values,'revision':'1','end_date':'2026-11-12'}).status_code==303
    response=post(c,f'/v2/scheduling/time-off/{request_id}/decision',decision='APPROVED',expected_revision='1',management_reason='Reviewed old page')
    assert response.status_code==303 and 'error=' in response.headers['location']
    with primary.Session() as db:assert db.get(m.TimeOffRequest,request_id).status==m.TimeOffRequestStatus.PENDING
    assert post(c,f'/v2/scheduling/time-off/{request_id}/decision',decision='APPROVED',expected_revision='2',management_reason='Reviewed new dates').status_code==303
    with primary.Session() as db:
        row=db.get(m.TimeOffRequest,request_id)
        assert row.status==m.TimeOffRequestStatus.APPROVED and row.end_date==date(2026,11,12)


def test_http_all_statuses_foreign_request_denial_and_preview(primary):
    primary.account('STORE',True);login(primary)
    c=primary.client
    values={'start_date':'2026-11-10','end_date':'2026-11-10','duration':'FULL','reason_category_id':str(primary.ids['vacation'])}
    for note in ('Pending family trip','Approved appointment','Denied date','Cancelled plan'):
        assert post(c,'/v2/scheduling/my-time-off',**values,employee_note=note).status_code==303
    with primary.Session() as db:
        rows=list(db.scalars(select(m.TimeOffRequest).order_by(m.TimeOffRequest.id)))
        review_time_off_request(db,principal=primary.manager,request_id=rows[1].id,status=m.TimeOffRequestStatus.APPROVED,management_review_note='Approved appointment')
        review_time_off_request(db,principal=primary.manager,request_id=rows[2].id,status=m.TimeOffRequestStatus.DENIED,management_review_note='Please choose another day')
        employee=db.get(m.Employee,primary.ids['alex'])
        user=db.get(m.Principal,employee.principal_id)
        actor=Principal(user.id,user.username,Role.STORE,None,True)
        save_own_request(db,principal=actor,request_id=rows[3].id,expected_revision=1,action='cancel')
        foreign=request(db,primary.manager,primary.ids,db.get(m.Employee,primary.ids['blair']),date(2026,11,12),date(2026,11,12))
        foreign.employee_note='FOREIGN_SECRET';foreign_id=foreign.id;db.commit()
    response=c.get('/v2/scheduling/my-time-off')
    assert response.status_code==200 and 'FOREIGN_SECRET' not in response.text
    assert all(label in response.text for label in ('Pending','Approved','Denied','Cancelled'))
    assert post(c,f'/v2/scheduling/my-time-off/{foreign_id}',action='cancel',revision='1').status_code==403
    import os,re
    artifact_dir=os.environ.get('PTO_ARTIFACT_DIR')
    if artifact_dir:
        from pathlib import Path
        folder=Path(artifact_dir);folder.mkdir(parents=True,exist_ok=True)
        html=response.text
        for asset in re.findall(r'<link[^>]+href="(/v2-assets/[^"?]+\.css)"[^>]*>',html):
            css=Path('app/static/v2',asset.split('/v2-assets/')[1])
            if css.exists():
                html=re.sub(r'<link[^>]+href="'+re.escape(asset)+r'"[^>]*>',lambda _: '<style>'+css.read_text()+'</style>',html)
        # Static review copy only: no forms can submit from this artifact.
        html=html.replace('<form ', '<form onsubmit="return false" ')
        (folder/'employee-pto-preview.html').write_text(html)


def test_in_progress_approved_leave_cancellation_requires_review(scheduling_db):
    Session,manager,ids,_=scheduling_db
    with Session() as db:
        employee=db.get(m.Employee,ids['alex']);actor=own_account(db,employee)
        original=request(db,manager,ids,employee,date(2026,10,18),date(2026,10,24))
        review_time_off_request(db,principal=manager,request_id=original.id,status=m.TimeOffRequestStatus.APPROVED,management_review_note='Existing vacation')
        db.get(m.TimeOffReasonCategory,ids['vacation']).active=False;db.flush()
        amendment=save_own_request(db,principal=actor,request_id=original.id,expected_revision=original.revision,
            action='request_cancel',note='Return from vacation early; please review')
        assert amendment.status==m.TimeOffRequestStatus.PENDING and original.status==m.TimeOffRequestStatus.APPROVED
        review_time_off_request(db,principal=manager,request_id=amendment.id,status=m.TimeOffRequestStatus.APPROVED,
            management_review_note='Authorized cancellation, preserve history and correct published coverage separately')
        assert original.status==amendment.status==m.TimeOffRequestStatus.CANCELLED


def test_schedule_board_shows_authorized_counts_without_private_leave_reason(scheduling_db):
    from app.services.v2_scheduling_board_service import serialize_week_board
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        cam,other,reserve=setup(db,actor,ids)
        start=date(2026,11,8);per=period(db,actor,start)
        pto=request(db,actor,ids,cam,start,start+timedelta(days=6))
        review_time_off_request(db,principal=actor,request_id=pto.id,status=m.TimeOffRequestStatus.APPROVED,management_review_note='Approved')
        set_leave_exception(db,principal=actor,request_id=pto.id,week=start,reason='PRIVATE_LEAVE_AUTHORIZATION_REASON')
        board=serialize_week_board(db,week_start=start,selected_store_ids=(ids['north'],ids['south']),
            all_authorized_store_ids=(ids['north'],ids['south']),schedule_period_id=per.id,
            permission_flags={'scheduling.view_all':True})
        assert 'PRIVATE_LEAVE_AUTHORIZATION_REASON' not in json.dumps(board,default=str)
        row=next(row for group in board['groups'] for row in group['employees'] if row['id']==cam.id)
        assert row['target_shift_count']==0 and row['normal_target_shifts']==3
        assert row['leave_allocation_exception']['work_shifts']==0


def test_leave_exception_batch_keeps_employee_scope_and_rechecks_revoked_pto(scheduling_db):
    from app.services.read_scope import reuse_reads
    from app.services.v2_scheduling_allocation_service import leave_exception
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        cam,other,_=setup(db,actor,ids);start=date(2026,11,8)
        pto=request(db,actor,ids,cam,start,start+timedelta(days=6))
        review_time_off_request(db,principal=actor,request_id=pto.id,status=m.TimeOffRequestStatus.APPROVED,management_review_note='Approved')
        exception=set_leave_exception(db,principal=actor,request_id=pto.id,week=start,reason='Test weekly leave')
        db.commit()
        with reuse_reads(db):
            assert leave_exception(db,cam.id,start).id==exception.id
            assert leave_exception(db,other.id,start) is None
            assert weekly_allocation(db,cam.id,start)==0
            review_time_off_request(db,principal=actor,request_id=pto.id,status=m.TimeOffRequestStatus.DENIED,management_review_note='Revoke test leave')
            db.flush()
            assert leave_exception(db,cam.id,start) is None
            assert weekly_allocation(db,cam.id,start)==3
