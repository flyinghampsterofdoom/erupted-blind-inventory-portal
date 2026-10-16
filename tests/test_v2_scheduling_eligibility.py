"""Dated departures and returns retain one identity and existing authorization."""
from datetime import date, time
from decimal import Decimal
import pytest
from sqlalchemy import select, func, text
from sqlalchemy.exc import IntegrityError
from app.models import Employee, SchedulingEligibilityPeriod, ScheduleShift, AuditLog
from app.services.v2_scheduling_lifecycle_service import (
    eligible_on, eligible_during, scheduling_status, change_eligibility)
from app.services.v2_scheduling_policy_service import evaluate_assignment, regenerate_period
from app.services.v2_scheduling_service import create_draft_period, create_shift, ShiftInput
from app.services.v2_scheduling_rules_service import upsert_employee_profile, create_coverage_requirement
from app.services.v2_scheduling_roster_service import list_scheduling_candidates, set_last_effective_date
from tests.test_v2_scheduling_foundation import scheduling_db, opening_hours


@pytest.mark.parametrize('day,expected', [
    (date(2026,10,27), False), (date(2026,10,28), True),
    (date(2026,10,29), True), (date(2026,11,1), True), (date(2026,11,2), False)])
def test_inclusive_dates(scheduling_db, day, expected):
    Session, manager, ids, _ = scheduling_db
    with Session() as db:
        e = db.get(Employee, ids['inactive'])
        change_eligibility(db, principal=manager, employee_id=e.id, action='reactivate', day=date(2026,10,28))
        change_eligibility(db, principal=manager, employee_id=e.id, action='end', day=date(2026,11,1))
        assert eligible_on(e, day) is expected
        assert e.active is False  # Scheduling actions never reactivate accounts.
        assert eligible_during(e,date(2026,10,25),date(2026,10,31))


def test_rehire_preserves_identity_unknown_start_and_gap(scheduling_db):
    Session, manager, ids, _ = scheduling_db
    with Session() as db:
        e=db.get(Employee, ids['alex']); e.last_effective_date=date(2026,9,29)
        old=(e.id,e.principal_id,e.active,e.scheduling_active,e.scheduling_lead_capable)
        change_eligibility(db, principal=manager, employee_id=e.id, action='reactivate', day=date(2026,10,28))
        assert e.eligibility_periods[0].start_date is None
        assert eligible_on(e,date(2026,9,29)) and not eligible_on(e,date(2026,9,30))
        assert not eligible_on(e,date(2026,10,27)) and eligible_on(e,date(2026,10,28))
        assert (e.id,e.principal_id,e.active,e.scheduling_active,e.scheduling_lead_capable)==old
        assert scheduling_status(e,date(2026,10,15))=='Scheduled Return'
        assert scheduling_status(e,date(2026,10,28))=='Active'
        assert e in list_scheduling_candidates(db)
        with pytest.raises(ValueError, match='Use End Scheduling'):
            set_last_effective_date(db, principal=manager, employee_id=e.id,last_effective_date=None)
        assert db.scalar(select(func.count(AuditLog.id)).where(AuditLog.action=='V2:SCHEDULING:SCHEDULING_ELIGIBILITY_CHANGED'))==1


def test_future_departure_and_status(scheduling_db):
    Session, manager, ids, _ = scheduling_db
    with Session() as db:
        e=change_eligibility(db,principal=manager,employee_id=ids['alex'],action='end',day=date(2026,10,23))
        assert scheduling_status(e,date(2026,10,15))=='Scheduled Departure'
        assert eligible_on(e,date(2026,10,23)) and not eligible_on(e,date(2026,10,24))
        assert scheduling_status(e,date(2026,10,24))=='Inactive'


def test_database_rejects_overlap_and_inclusive_same_day(scheduling_db):
    Session, manager, ids, _ = scheduling_db
    with Session() as db:
        e=db.get(Employee,ids['inactive'])
        change_eligibility(db,principal=manager,employee_id=e.id,action='reactivate',day=date(2026,10,28));db.commit()
        with pytest.raises(ValueError,match='cannot overlap'):
            change_eligibility(db,principal=manager,employee_id=e.id,action='reactivate',day=date(2026,11,1))
        db.add(SchedulingEligibilityPeriod(employee_id=e.id,start_date=date(2026,10,1),end_date=date(2026,10,28)))
        with pytest.raises(IntegrityError,match='scheduling_eligibility_no_overlap'): db.flush()


@pytest.mark.parametrize('published',[False,True])
def test_departure_conflicts_block_without_modifying_assignment(scheduling_db,published):
    from app.models import SchedulePeriodStatus
    Session,manager,ids,_=scheduling_db
    with Session() as db:
        p=create_draft_period(db,principal=manager,week_start=date(2027,1,3))
        opening_hours(db,manager,ids['north'],1)
        result=create_shift(db,principal=manager,schedule_period_id=p.id,expected_version=p.version,
            values=ShiftInput(store_id=ids['north'],employee_id=ids['alex'],shift_date=date(2027,1,4),
            start_time=time(8,45),end_time=time(22)),allowed_store_ids=(ids['north'],ids['south']))
        if published:p.status=SchedulePeriodStatus.PUBLISHED
        db.flush(); version=p.version
        with pytest.raises(ValueError,match=f'assignment #{result.shift_id}'):
            change_eligibility(db,principal=manager,employee_id=ids['alex'],action='end',day=date(2027,1,3),today=date(2027,1,1))
        assert db.get(Employee,ids['alex']).eligibility_periods==[]
        assert db.get(ScheduleShift,result.shift_id).employee_id==ids['alex'] and p.version==version


@pytest.mark.parametrize('return_day', [28, 29])
def test_generator_midweek_return_before_return_date(scheduling_db, return_day):
    Session,manager,ids,_=scheduling_db
    with Session() as db:
        for key in ('alex','blair'):db.get(Employee,ids[key]).scheduling_active=False
        e=db.get(Employee,ids['inactive'])
        change_eligibility(db,principal=manager,employee_id=e.id,action='reactivate',day=date(2026,10,return_day))
        upsert_employee_profile(db,principal=manager,employee_id=e.id,home_store_id=ids['north'],
            target_weekly_hours=Decimal('0'),target_shifts_per_week=4,approval_weekly_hours=Decimal('60'),
            allowed_store_ids=(ids['north'],ids['south']))
        for weekday in range(7):
            opening_hours(db,manager,ids['north'],weekday)
            create_coverage_requirement(db,principal=manager,store_id=ids['north'],day_of_week=weekday,
                start_time=time(9),end_time=time(22),minimum_employee_count=1,
                allowed_store_ids=(ids['north'],ids['south']))
        p=create_draft_period(db,principal=manager,week_start=date(2026,10,25))
        regenerate_period(db,principal=manager,schedule_period_id=p.id)
        rows=list(db.scalars(select(ScheduleShift).where(ScheduleShift.schedule_period_id==p.id)))
        assert {s.shift_date for s in rows if s.employee_id==e.id}=={date(2026,10,d) for d in range(return_day, return_day+3)}
        assert all(s.employee_id is None for s in rows if s.shift_date<date(2026,10,return_day))
        assert not evaluate_assignment(db,employee_id=e.id,store_id=ids['north'],shift_date=date(2026,10,27),start_time=time(8,45),end_time=time(22)).eligible


def test_legacy_unknown_dates_not_reinterpreted(scheduling_db):
    Session,manager,ids,_=scheduling_db
    with Session() as db:
        assert eligible_on(db.get(Employee,ids['alex']),date(1990,1,1))
        assert not eligible_on(db.get(Employee,ids['inactive']),date(2026,10,28))
        assert db.scalar(select(func.count(SchedulingEligibilityPeriod.id)))==0


def test_permissions_denial_preserves_employee(scheduling_db):
    from app.models import PrincipalPermissionOverride
    Session,manager,ids,_=scheduling_db
    with Session() as db:
        db.add(PrincipalPermissionOverride(principal_id=manager.id,permission_key='scheduling.manage_preferences',allowed=False));db.flush()
        with pytest.raises(PermissionError):
            change_eligibility(db,principal=manager,employee_id=ids['alex'],action='end',day=date(2026,10,23))
        assert db.get(Employee,ids['alex']).eligibility_periods==[]


def test_concurrent_rehires_do_not_create_overlapping_periods(scheduling_db):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    Session,manager,ids,_=scheduling_db
    barrier=Barrier(2)
    def attempt(day):
        with Session() as db:
            barrier.wait()
            try:
                change_eligibility(db,principal=manager,employee_id=ids['inactive'],action='reactivate',day=day)
                db.commit();return 'saved'
            except ValueError:
                db.rollback();return 'rejected'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(attempt,[date(2026,10,28),date(2026,10,29)]))
    assert sorted(results)==['rejected','saved']
    with Session() as db:assert len(db.get(Employee,ids['inactive']).eligibility_periods)==1


def test_ineligible_gap_does_not_accumulate_weekend_opportunities(scheduling_db):
    from app.services.v2_scheduling_request_pattern_service import _raw_pattern
    Session,manager,ids,_=scheduling_db
    with Session() as db:
        e=db.get(Employee,ids['alex']);e.last_effective_date=date(2026,8,15)
        change_eligibility(db,principal=manager,employee_id=e.id,action='reactivate',day=date(2026,10,28))
        raw=_raw_pattern(db,employee=e,profile=None,start=date(2026,8,16),end=date(2026,10,27))
        assert raw['saturday'].eligible_dates==() and raw['sunday'].eligible_dates==()


@pytest.mark.parametrize('day,allowed',[(27,False),(28,True),(31,True)])
def test_callout_and_manual_workflows_use_return_date(scheduling_db,day,allowed):
    from app.services.v2_scheduling_exception_service import commitment_eligibility
    from tests.test_v2_lead_duty import period, shift
    from app.services.v2_scheduling_service import SchedulingValidationError
    Session,manager,ids,_=scheduling_db
    with Session() as db:
        e=db.get(Employee,ids['blair']);e.last_effective_date=date(2026,9,29)
        change_eligibility(db,principal=manager,employee_id=e.id,action='reactivate',day=date(2026,10,28))
        p=period(db,manager,date(2026,10,day),status='DRAFT')
        row=shift(db,manager,p,ids['alex'],ids['north'],date(2026,10,day),lead=False)
        assert commitment_eligibility(db,row,e.id,today=date(2026,10,15)).eligible is allowed
        opening_hours(db,manager,ids['south'],(date(2026,10,day).weekday()+1)%7)
        def manual():return create_shift(db,principal=manager,schedule_period_id=p.id,expected_version=p.version,
            values=ShiftInput(store_id=ids['south'],employee_id=e.id,shift_date=date(2026,10,day),start_time=time(8,45),end_time=time(22)),
            allowed_store_ids=(ids['north'],ids['south']))
        if allowed:assert manual().shift_id
        else:
            with pytest.raises(SchedulingValidationError,match='not eligible'):manual()


def test_nonoverlap_database_constraint_serializes_raw_concurrent_writes(scheduling_db):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    Session,manager,ids,_=scheduling_db
    barrier=Barrier(2)
    def insert(day):
        with Session() as db:
            barrier.wait()
            try:
                db.execute(text('INSERT INTO scheduling_eligibility_periods(employee_id,start_date) VALUES (:id,:day)'),{'id':ids['inactive'],'day':day})
                db.commit();return 'saved'
            except IntegrityError:
                db.rollback();return 'rejected'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(insert,[date(2026,10,28),date(2026,10,29)]))==['rejected','saved']


@pytest.mark.parametrize('override',[False,True])
def test_approval_publication_cannot_override_dated_eligibility(scheduling_db,override):
    from app.services.v2_scheduling_service import approve_schedule,publish_schedule,SchedulingValidationError
    Session,manager,ids,_=scheduling_db
    with Session() as db:
        e=db.get(Employee,ids['alex']);e.last_effective_date=date(2026,9,29)
        change_eligibility(db,principal=manager,employee_id=e.id,action='reactivate',day=date(2026,10,28))
        p=create_draft_period(db,principal=manager,week_start=date(2026,10,25))
        opening_hours(db,manager,ids['north'],2)
        row=ScheduleShift(schedule_period_id=p.id,employee_id=e.id,store_id=ids['north'],shift_date=date(2026,10,27),
            start_time=time(8,45),end_time=time(22),created_by_principal_id=manager.id,updated_by_principal_id=manager.id)
        db.add(row);db.flush()
        with pytest.raises(SchedulingValidationError,match='not eligible'):
            approve_schedule(db,principal=manager,schedule_period_id=p.id,expected_version=p.version,allowed_store_ids=(ids['north'],ids['south']))
        with pytest.raises(SchedulingValidationError,match='not eligible'):
            publish_schedule(db,principal=manager,schedule_period_id=p.id,expected_version=p.version,
                allowed_store_ids=(ids['north'],ids['south']),allow_serious_warnings=override,confirmed=override,override_reason='Cannot bypass dates')
        assert row.employee_id==e.id


def test_rehire_detects_existing_assignments_during_gap(scheduling_db):
    from tests.test_v2_lead_duty import period,shift
    Session,manager,ids,_=scheduling_db
    with Session() as db:
        e=db.get(Employee,ids['blair']);e.scheduling_active=False;e.last_effective_date=date(2026,9,29)
        row=shift(db,manager,period(db,manager,date(2026,10,20)),e.id,ids['north'],date(2026,10,20))
        with pytest.raises(ValueError,match=f'assignment #{row.id}'):
            change_eligibility(db,principal=manager,employee_id=e.id,action='reactivate',day=date(2026,10,28),today=date(2026,10,15))
        assert e.eligibility_periods==[] and not e.scheduling_active


def test_rehire_retains_disabled_profile_restrictions_without_extra_toggle(scheduling_db):
    from app.models import EmployeeSchedulingProfile
    Session,manager,ids,_=scheduling_db
    with Session() as db:
        e=db.get(Employee,ids['blair']);e.scheduling_active=False;e.last_effective_date=date(2026,9,29)
        profile=EmployeeSchedulingProfile(employee_id=e.id,active=False,maximum_weekly_hours=Decimal('10'),
            target_weekly_hours=Decimal('0'),target_shifts_per_week=3,
            created_by_principal_id=manager.id,updated_by_principal_id=manager.id)
        db.add(profile);db.flush()
        change_eligibility(db,principal=manager,employee_id=e.id,action='reactivate',day=date(2026,10,28))
        result=evaluate_assignment(db,employee_id=e.id,store_id=ids['north'],shift_date=date(2026,10,28),start_time=time(8,45),end_time=time(22))
        assert 'MAX_WEEKLY_HOURS' in {reason.code for reason in result.reasons}
        assert not profile.active and not e.scheduling_active
