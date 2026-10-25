from datetime import date
from collections import Counter
import pytest
from sqlalchemy import select, func
from app import models as m
from app.auth import Principal, Role
from app.services.v2_scheduling_allocation_service import allocation_on, weekly_allocation, allocation_history, change_allocation
from app.services.v2_scheduling_service import SchedulingValidationError
from test_v2_scheduling_foundation import scheduling_db
from test_v2_scheduling_repair import person
from test_v2_weekly_allocations import setup, run
TODAY=date(2026,10,16)
START=date(2026,10,25)

def schedule(db,actor,employee,**kwargs):
    return change_allocation(db,principal=actor,employee_id=employee.id,today=TODAY,effective_date=kwargs.pop('effective_date',START),shifts_per_week=kwargs.pop('shifts_per_week',3),**kwargs)

def test_boundary_and_undated_baseline(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        employee=person(db,actor,'Returning employee',target=1)
        schedule(db,actor,employee)
        for day in (date(2025,1,1),date(2026,10,18),date(2026,10,24)):
            assert allocation_on(db,employee.id,day)==1
            assert weekly_allocation(db,employee.id,day)==1
        assert weekly_allocation(db,employee.id,START)==3
        assert weekly_allocation(db,employee.id,date(2026,10,28))==3
        rows=allocation_history(db,employee.id)
        assert len(rows)==2 and rows[0].baseline and rows[0].effective_date is None
        assert db.scalar(select(m.EmployeeSchedulingProfile.target_shifts_per_week).where(m.EmployeeSchedulingProfile.employee_id==employee.id))==1

def test_edit_cancel_retain_original_revisions_and_audit(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        employee=person(db,actor,'Dated employee',target=1)
        first=schedule(db,actor,employee)
        corrected=schedule(db,actor,employee,change_id=first.id,expected_revision=first.revision,shifts_per_week=2)
        assert corrected.id!=first.id and corrected.replaces_id==first.id and not first.active
        assert weekly_allocation(db,employee.id,START)==2
        schedule(db,actor,employee,change_id=corrected.id,expected_revision=corrected.revision,cancel=True)
        assert weekly_allocation(db,employee.id,START)==1
        assert len(allocation_history(db,employee.id))==3
        assert db.scalar(select(func.count()).select_from(m.AuditLog).where(m.AuditLog.action=='V2:scheduling:ALLOCATION_CHANGED'))==3

@pytest.mark.parametrize('day,value,message',[(date(2026,10,28),3,'Sunday'),(date(2026,10,11),3,'future'),(START,8,'zero and seven')])
def test_invalid_changes_rejected(scheduling_db,day,value,message):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        employee=person(db,actor,'Invalid change',target=1)
        with pytest.raises(SchedulingValidationError,match=message):schedule(db,actor,employee,effective_date=day,shifts_per_week=value)
        assert allocation_history(db,employee.id)==[]

def test_duplicates_and_stale_edits_rejected(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        employee=person(db,actor,'Concurrent change',target=1)
        row=schedule(db,actor,employee)
        with pytest.raises(SchedulingValidationError,match='already exists'):schedule(db,actor,employee)
        with pytest.raises(SchedulingValidationError,match='another user'):schedule(db,actor,employee,change_id=row.id,expected_revision=99)
        assert len(allocation_history(db,employee.id))==2

def test_midweek_legacy_boundary_blocks_generation(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        employee=person(db,actor,'Midweek allocation',target=1)
        db.add(m.SchedulingAllocationChange(employee_id=employee.id,effective_date=date(2026,10,28),shifts_per_week=3,created_by_principal_id=actor.id));db.flush()
        with pytest.raises(SchedulingValidationError,match='not prorated'):weekly_allocation(db,employee.id,START)

def test_generation_resolves_future_quota_and_flexible_classification(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        people,per,rows=setup(db,actor,ids,targets=(3,1))
        schedule(db,actor,people[0],shifts_per_week=1)
        schedule(db,actor,people[1],shifts_per_week=3)
        result=run(db,actor,per)
        assert result['status']=='PASS' and result['exceptions']==[]
        assert Counter(s.employee_id for s in rows)=={people[0].id:1,people[1].id:3}
        assert weekly_allocation(db,people[0].id,START)<3
        assert weekly_allocation(db,people[1].id,START)==3

def test_store_account_cannot_change_allocation(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        employee=person(db,actor,'Permission protected',target=1)
        store=Principal(id=actor.id,username=actor.username,role=Role.STORE,active=True,store_id=ids['north'])
        with pytest.raises(PermissionError):schedule(db,store,employee)
        assert allocation_history(db,employee.id)==[]

@pytest.mark.parametrize('today', [date(2026, 10, 24), START, date(2026, 10, 26), date(2027, 1, 3)])
def test_profile_editor_displays_current_and_future_separately(scheduling_db, today, scheduling_clock):
    scheduling_clock(today, 'app.routers.v2_scheduling')
    from starlette.requests import Request
    from app.main import app
    from app.routers.v2_scheduling import employee_policy_page
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        employee=person(db,actor,'Allocation UI employee',target=1)
        schedule(db,actor,employee)
        request=Request({'type':'http','method':'GET','scheme':'http','path':'/v2/hr/employees/'+str(employee.id),
            'query_string':b'','headers':[],'client':('test',1),'server':('test',80),'app':app})
        html=employee_policy_page(employee.id,request,_feature=actor,principal=actor,db=db).body.decode()
        current = '1 shift per week' if today < START else '3 shifts per week'
        assert f'Current: <strong>{current}</strong>' in html
        assert '2026-10-25 · 3 shifts' in html
        assert ('2026-10-25 → 3 shifts per week' in html) == (today < START)
        assert 'Schedule Change' in html
        assert ('Edit Change' in html) == (today < START)
        assert ('Cancel Change' in html) == (today < START)
        assert 'Historical start not recorded (baseline)' in html


def test_allocation_changes_preserve_published_assignments_and_account(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        people,per,rows=setup(db,actor,ids,targets=(3,1))
        per.status=m.SchedulePeriodStatus.PUBLISHED
        rows[0].employee_id=people[0].id
        rows[0].manually_locked=True
        db.flush()
        original=[(s.id,s.employee_id,s.store_id,s.shift_date,s.manually_locked) for s in rows]
        identity=(people[0].id,people[0].principal_id,people[0].active,people[0].scheduling_active)
        schedule(db,actor,people[0],shifts_per_week=1)
        assert original==[(s.id,s.employee_id,s.store_id,s.shift_date,s.manually_locked) for s in rows]
        assert per.status==m.SchedulePeriodStatus.PUBLISHED
        assert identity==(people[0].id,people[0].principal_id,people[0].active,people[0].scheduling_active)


def test_recorded_baseline_cannot_be_overwritten_by_legacy_editor(scheduling_db):
    from app.services.v2_scheduling_rules_service import upsert_employee_profile
    from decimal import Decimal
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        employee=person(db,actor,'Baseline protected',target=1)
        with pytest.raises(SchedulingValidationError,match='Schedule Change'):
            upsert_employee_profile(db,principal=actor,employee_id=employee.id,home_store_id=None,
                target_weekly_hours=Decimal(0),target_shifts_per_week=3,allowed_store_ids=(ids['north'],ids['south']))
        assert allocation_on(db,employee.id,date(2026,10,18))==1

def test_explicit_manager_denial_is_preserved(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        employee=person(db,actor,'Explicit denial protected',target=1)
        db.add(m.PrincipalPermissionOverride(principal_id=actor.id,permission_key='scheduling.manage_preferences',allowed=False));db.flush()
        with pytest.raises(PermissionError):schedule(db,actor,employee)
        assert allocation_history(db,employee.id)==[]


def test_database_rejects_duplicate_active_dates(scheduling_db):
    from sqlalchemy.exc import IntegrityError
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        employee=person(db,actor,'Database dates',target=1)
        schedule(db,actor,employee)
        with pytest.raises(IntegrityError), db.begin_nested():
            db.add(m.SchedulingAllocationChange(employee_id=employee.id,effective_date=START,shifts_per_week=2,created_by_principal_id=actor.id));db.flush()
        assert len(allocation_history(db,employee.id))==2


def test_past_changes_cannot_be_cancelled_or_rewritten(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        employee=person(db,actor,'Retained history',target=1)
        row=schedule(db,actor,employee)
        with pytest.raises(SchedulingValidationError,match='already in effect'):
            change_allocation(db,principal=actor,employee_id=employee.id,change_id=row.id,
                expected_revision=row.revision,cancel=True,today=date(2026,10,26))
        assert row.active and allocation_on(db,employee.id,START)==3
