"""Preparation must be scheduled, validated and paid across all four stores."""
from datetime import date,time
from decimal import Decimal
import pytest
from sqlalchemy import select
from app import models as m
from app.services.v2_scheduling_policy_service import materialize_coverage_positions,evaluate_assignment
from app.services.v2_scheduling_service import approve_schedule,create_draft_period,publish_schedule,SchedulingValidationError
from app.services.v2_scheduling_coverage_service import rebuild_schedule_warnings
from test_v2_scheduling_foundation import scheduling_db

DAY=date(2026,10,18)
NAMES=('Andresen','HWY 99','SR 503','Longview')

@pytest.fixture
def four_stores(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        stores=[db.get(m.Store,ids['north']),db.get(m.Store,ids['south'])]
        stores += [m.Store(name=n,square_location_id=n,active=True) for n in NAMES[2:]]
        employees=[db.get(m.Employee,ids['alex']),db.get(m.Employee,ids['blair'])]
        employees += [m.Employee(full_name=n,normalized_name=n.lower(),active=True,scheduling_active=True,visible_to_leads=True) for n in ('Third Worker','Fourth Worker')]
        db.add_all(stores+employees);db.flush()
        for store,name in zip(stores,NAMES):
            store.name=name
            db.add(m.StoreOperatingHour(store_id=store.id,day_of_week=0,opening_time=time(9),closing_time=time(22),active=True,created_by_principal_id=actor.id,updated_by_principal_id=actor.id))
            db.add(m.CoverageRequirement(store_id=store.id,day_of_week=0,start_time=time(8,45),end_time=time(22),minimum_employee_count=1,active=True,created_by_principal_id=actor.id,updated_by_principal_id=actor.id))
        db.commit()
        yield Session,actor,tuple(s.id for s in stores),tuple(e.id for e in employees)

def draft(db,actor,stores,employees):
    per=create_draft_period(db,principal=actor,week_start=DAY)
    for store,employee in zip(stores,employees):
        db.add(m.ScheduleShift(schedule_period_id=per.id,employee_id=employee,store_id=store,shift_date=DAY,start_time=time(8,45),end_time=time(22),unpaid_break_minutes=0,created_by_principal_id=actor.id,updated_by_principal_id=actor.id))
    db.flush();return per

def args(actor,stores,per):
    return dict(principal=actor,schedule_period_id=per.id,expected_version=per.version,allowed_store_ids=stores)

@pytest.mark.parametrize('index',range(4),ids=NAMES)
@pytest.mark.parametrize('override',[time(9),time(8,30)])
def test_generation_uses_opening_not_override_and_preserves_end(four_stores,index,override):
    Session,actor,stores,_=four_stores
    with Session() as db:
        source=m.StoreShift(store_id=stores[index],label='Wrong start override',start_time=override,end_time=time(21,30),active_weekdays=127,active=True,created_by_principal_id=actor.id,updated_by_principal_id=actor.id)
        db.add(source)
        per=create_draft_period(db,principal=actor,week_start=DAY)
        result=materialize_coverage_positions(db,principal=actor,period=per)
        rows=list(db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==per.id)))
        assert result['created']==len(rows)==4
        assert all(r.start_time==time(8,45) for r in rows)
        assert next(r for r in rows if r.store_id==stores[index]).end_time==time(21,30)
        assert all(r.end_time==time(22) for r in rows if r.store_id!=stores[index])
        assert source.start_time==override
        assert db.get(m.SchedulingStoreDefaults,1).standard_shift_start==time(8,45)
        assert all(r.start_time==time(8,45) and r.end_time==time(22) for r in db.scalars(select(m.CoverageRequirement)))

def test_correct_preparation_coverage_can_be_approved_and_published(four_stores):
    Session,actor,stores,employees=four_stores
    with Session() as db:
        per=draft(db,actor,stores,employees)
        warnings=rebuild_schedule_warnings(db,schedule_period_id=per.id)
        assert not {w.warning_type for w in warnings} & {'SHIFT_OUTSIDE_OPERATING_HOURS','COVERAGE_RULE_OUTSIDE_HOURS','PREOPENING_START_REQUIRED','NO_ASSIGNED_EMPLOYEE','INSUFFICIENT_COVERAGE'}
        approve_schedule(db,**args(actor,stores,per));publish_schedule(db,**args(actor,stores,per))
        assert per.status==m.SchedulePeriodStatus.PUBLISHED

@pytest.mark.parametrize('index',range(4),ids=NAMES)
@pytest.mark.parametrize('wrong',[time(8,30),time(9),time(8,46)])
def test_publication_cannot_waive_start_or_trust_cached_approval(four_stores,index,wrong):
    Session,actor,stores,employees=four_stores
    with Session() as db:
        per=draft(db,actor,stores,employees);approve_schedule(db,**args(actor,stores,per))
        row=db.scalar(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==per.id,m.ScheduleShift.store_id==stores[index]))
        row.start_time=wrong;db.flush()
        with pytest.raises(SchedulingValidationError,match='exactly 15 minutes'):
            publish_schedule(db,**args(actor,stores,per),allow_serious_warnings=True,confirmed=True,override_reason='Cannot waive preparation')
        warnings=rebuild_schedule_warnings(db,schedule_period_id=per.id)
        assert any(w.warning_type=='PREOPENING_START_REQUIRED' for w in warnings)
        if wrong>time(8,45):assert any(w.warning_type=='NO_ASSIGNED_EMPLOYEE' and w.start_time==time(8,45) for w in warnings)
        assert per.status==m.SchedulePeriodStatus.DRAFT

@pytest.mark.parametrize('index',range(4),ids=NAMES)
def test_special_opening_uses_offset_without_changing_end(four_stores,index):
    Session,actor,stores,_=four_stores
    with Session() as db:
        db.add(m.StoreSpecialHour(store_id=stores[index],calendar_date=DAY,event_name="Test special hours",opening_time=time(10),closing_time=time(22),closed_all_day=False,active=True,created_by_principal_id=actor.id,updated_by_principal_id=actor.id))
        per=create_draft_period(db,principal=actor,week_start=DAY);materialize_coverage_positions(db,principal=actor,period=per)
        row=db.scalar(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==per.id,m.ScheduleShift.store_id==stores[index]))
        assert (row.start_time,row.end_time)==(time(9,45),time(22))

@pytest.mark.parametrize('problem',['missing','ambiguous','midnight'])
def test_bad_opening_fails_before_replacing_existing_draft(four_stores,problem):
    Session,actor,stores,employees=four_stores
    with Session() as db:
        per=draft(db,actor,stores,employees)
        row=db.scalar(select(m.StoreOperatingHour).where(m.StoreOperatingHour.store_id==stores[0]))
        if problem=='missing':row.active=False
        elif problem=='midnight':row.opening_time=time(0,10)
        else:db.add(m.StoreOperatingHour(store_id=stores[0],day_of_week=0,opening_time=time(10),closing_time=time(21),active=True,created_by_principal_id=actor.id,updated_by_principal_id=actor.id))
        db.flush()
        with pytest.raises(SchedulingValidationError):materialize_coverage_positions(db,principal=actor,period=per)
        assert len(list(db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==per.id))))==4
        with pytest.raises(SchedulingValidationError):approve_schedule(db,**args(actor,stores,per))

def test_explicit_closed_date_generates_no_shift(four_stores):
    Session,actor,stores,_=four_stores
    with Session() as db:
        db.add(m.StoreSpecialHour(store_id=stores[0],calendar_date=DAY,event_name="Test special hours",closed_all_day=True,active=True,created_by_principal_id=actor.id,updated_by_principal_id=actor.id))
        per=create_draft_period(db,principal=actor,week_start=DAY)
        assert materialize_coverage_positions(db,principal=actor,period=per)['created']==3
        assert not list(db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==per.id,m.ScheduleShift.store_id==stores[0])))

def test_preparation_counts_toward_hours(four_stores):
    Session,actor,stores,employees=four_stores
    with Session() as db:
        db.add(m.EmployeeSchedulingProfile(employee_id=employees[0],maximum_weekly_hours=Decimal('13'),created_by_principal_id=actor.id,updated_by_principal_id=actor.id));db.flush()
        result=evaluate_assignment(db,employee_id=employees[0],store_id=stores[0],shift_date=DAY,start_time=time(8,45),end_time=time(22))
        assert result.resulting_hours==Decimal('13.25')
        assert any(r.code=='MAX_WEEKLY_HOURS' for r in result.reasons)


def test_all_four_stores_generate_28_correct_weekday_positions(four_stores):
    Session,actor,stores,_=four_stores
    with Session() as db:
        for store in stores:
            for weekday in range(1,7):
                db.add(m.StoreOperatingHour(store_id=store,day_of_week=weekday,opening_time=time(9),closing_time=time(22),active=True,created_by_principal_id=actor.id,updated_by_principal_id=actor.id))
                db.add(m.CoverageRequirement(store_id=store,day_of_week=weekday,start_time=time(8,45),end_time=time(22),minimum_employee_count=1,active=True,created_by_principal_id=actor.id,updated_by_principal_id=actor.id))
        per=create_draft_period(db,principal=actor,week_start=DAY)
        assert materialize_coverage_positions(db,principal=actor,period=per)['created']==28
        rows=list(db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==per.id)))
        assert len({(r.store_id,r.shift_date) for r in rows})==28
        assert all((r.start_time,r.end_time)==(time(8,45),time(22)) for r in rows)


def test_changed_actual_opening_invalidates_previous_approval(four_stores):
    Session,actor,stores,employees=four_stores
    with Session() as db:
        per=draft(db,actor,stores,employees);approve_schedule(db,**args(actor,stores,per))
        opening=db.scalar(select(m.StoreOperatingHour).where(m.StoreOperatingHour.store_id==stores[0]))
        opening.opening_time=time(10);db.flush()
        with pytest.raises(SchedulingValidationError,match='09:45'):
            publish_schedule(db,**args(actor,stores,per))
        assert per.status==m.SchedulePeriodStatus.DRAFT


def test_locked_incorrect_shift_is_preserved_but_cannot_publish(four_stores):
    Session,actor,stores,employees=four_stores
    with Session() as db:
        per=draft(db,actor,stores,employees)
        row=db.scalar(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==per.id,m.ScheduleShift.store_id==stores[0]))
        row.start_time=time(9);row.manually_locked=True;row.generated_from_coverage_requirement=True;db.flush()
        identity=row.id
        materialize_coverage_positions(db,principal=actor,period=per)
        assert db.get(m.ScheduleShift,identity).start_time==time(9)
        assert db.get(m.ScheduleShift,identity).manually_locked
        with pytest.raises(SchedulingValidationError,match='exactly 15 minutes'):
            approve_schedule(db,**args(actor,stores,per))


def test_readiness_reports_missing_openings_before_generation(four_stores):
    from app.services.v2_scheduling_readiness_service import scheduling_readiness
    Session,actor,stores,_=four_stores
    with Session() as db:
        opening=db.scalar(select(m.StoreOperatingHour).where(m.StoreOperatingHour.store_id==stores[0]))
        opening.active=False;db.flush()
        result=scheduling_readiness(db,today=date(2026,10,13))
        assert not result.can_generate
        assert any(r.code=='OPENING_CONFIGURATION_REQUIRED' for r in result.blocking)


@pytest.mark.parametrize('index',range(4),ids=NAMES)
@pytest.mark.parametrize('path',['manual','store_shift','copy','template'])
def test_every_assignment_entry_path_is_revalidated(four_stores,index,path):
    from datetime import timedelta
    from app.services.v2_scheduling_service import update_shift, ShiftInput
    from app.services.v2_store_shift_service import place_store_shift
    from app.services.v2_scheduling_template_service import (
        copy_schedule_periods, save_schedule_template, instantiate_schedule_template,
    )
    Session,actor,stores,employees=four_stores
    with Session() as db:
        per=draft(db,actor,stores,employees)
        row=db.scalar(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==per.id,m.ScheduleShift.store_id==stores[index]))
        if path=='manual':
            update_shift(db,principal=actor,schedule_period_id=per.id,shift_id=row.id,expected_version=per.version,allowed_store_ids=stores,values=ShiftInput(employees[index],stores[index],DAY,time(9),time(22)))
        elif path=='store_shift':
            db.delete(row);db.flush()
            source=m.StoreShift(store_id=stores[index],label='Legacy 09:00 start',start_time=time(9),end_time=time(22),active_weekdays=127,active=True,created_by_principal_id=actor.id,updated_by_principal_id=actor.id)
            db.add(source);db.flush()
            place_store_shift(db,principal=actor,schedule_period_id=per.id,store_shift_id=source.id,expected_version=per.version,shift_date=DAY,employee_id=employees[index],destination_store_id=stores[index],allowed_store_ids=stores,eligible_employee_ids=employees)
        else:
            row.start_time=time(9);db.flush()
            if path=='copy':
                outcome=copy_schedule_periods(db,principal=actor,source_period_ids=(per.id,),target_week_start=DAY+timedelta(days=7),allowed_store_ids=stores,mode='MERGE')
            else:
                template=save_schedule_template(db,principal=actor,name='Legacy clock template',source_period_ids=(per.id,),allowed_store_ids=stores)
                outcome=instantiate_schedule_template(db,principal=actor,schedule_template_id=template.id,target_week_start=DAY+timedelta(days=7),allowed_store_ids=stores,mode='MERGE')
            per=db.get(m.SchedulePeriod,outcome.schedule_period_ids[0])
        with pytest.raises(SchedulingValidationError,match='exactly 15 minutes'):
            approve_schedule(db,**args(actor,stores,per))
        with pytest.raises(SchedulingValidationError,match='exactly 15 minutes'):
            publish_schedule(db,**args(actor,stores,per),allow_serious_warnings=True,confirmed=True,override_reason='Cannot waive an entry-path violation')


def test_weekly_target_completion_never_copies_an_invalid_locked_start(four_stores):
    from app.services.v2_scheduling_policy_service import complete_weekly_targets
    Session,actor,stores,employees=four_stores
    with Session() as db:
        per=draft(db,actor,stores,employees)
        for row in db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==per.id)):
            row.manually_locked=True
            if row.store_id==stores[0]:row.start_time=time(9)
        reserve=m.Employee(full_name='Preparation Reserve',normalized_name='preparation reserve',active=True,scheduling_active=True)
        db.add(reserve);db.flush()
        db.add(m.EmployeeSchedulingProfile(employee_id=reserve.id,target_shifts_per_week=1,created_by_principal_id=actor.id,updated_by_principal_id=actor.id))
        for store in stores[1:]:
            db.add(m.EmployeeSchedulingStorePreference(employee_id=reserve.id,store_id=store,preference_level=m.StorePreferenceLevel.NEVER,created_by_principal_id=actor.id,updated_by_principal_id=actor.id))
        db.flush()
        assert complete_weekly_targets(db,principal=actor,period=per)==0
        assert not list(db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==per.id,m.ScheduleShift.employee_id==reserve.id)))
        assert db.scalar(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==per.id,m.ScheduleShift.store_id==stores[0])).start_time==time(9)


def test_incompatible_preserved_end_fails_without_replacing_draft(four_stores):
    Session,actor,stores,employees=four_stores
    with Session() as db:
        per=draft(db,actor,stores,employees)
        before=list(db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==per.id)))
        for row in before:row.generated_from_coverage_requirement=True
        db.add(m.StoreShift(store_id=stores[0],label='Preserved short end',start_time=time(8),end_time=time(9),active_weekdays=127,active=True,created_by_principal_id=actor.id,updated_by_principal_id=actor.id))
        db.add(m.StoreSpecialHour(store_id=stores[0],calendar_date=DAY,event_name='Later opening',opening_time=time(10),closing_time=time(22),closed_all_day=False,active=True,created_by_principal_id=actor.id,updated_by_principal_id=actor.id))
        db.flush()
        with pytest.raises(SchedulingValidationError,match='preserved shift end'):
            materialize_coverage_positions(db,principal=actor,period=per)
        after=list(db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==per.id)))
        assert {r.id for r in before}=={r.id for r in after}
        assert all(r.end_time==time(22) for r in after)
