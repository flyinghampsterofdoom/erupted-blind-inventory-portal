"""Assignment-only administration over isolated PostgreSQL; no owner data."""
from datetime import timedelta, time
from decimal import Decimal
import json
import time as timer
from sqlalchemy import select, event, func
import pytest
from app import models as m
from app.auth import Principal, Role
from app.services import scheduling_training_service as training
from app.services.v2_scheduling_service import SchedulingValidationError
from test_scheduling_training import setup, rows, NOW, START
from test_v2_employee_profiles import employee_client
from test_v2_scheduling_foundation import scheduling_db, opening_hours
from test_v2_scheduling_repair import person, period, shift


def owner(actor):
    return Principal(actor.id,actor.username,Role.ADMIN,None,True)


def snapshot(db):
    return {s.id:{c.name:getattr(s,c.name) for c in m.ScheduleShift.__table__.columns}
            for s in db.scalars(select(m.ScheduleShift))}


def forbid_engine(monkeypatch):
    from app.services import v2_scheduling_policy_service as policy
    from app.services import v2_scheduling_coverage_service as coverage
    from app.services import scheduling_owner_review as review
    def forbidden(*a,**kw):
        raise AssertionError('Assignment substitution invoked schedule-wide processing')
    for name in ['regenerate_period','manual_generate_draft_schedule','ensure_rolling_schedule_horizon']:
        monkeypatch.setattr(policy,name,forbidden)
    for name in ['rebuild_schedule_warnings','evaluate_schedule_warnings']:
        monkeypatch.setattr(coverage,name,forbidden)
    monkeypatch.setattr(review,'warning_snapshot',forbidden)


def transfer(db,actor,ids,incoming,outgoing):
    return training.save_transition(db,principal=owner(actor),employee_id=incoming.id,
        first_schedule_date=START,replacing_employee_id=outgoing.id,
        allowed_store_ids=tuple(ids.values()) if isinstance(ids,dict) else ids,now=NOW)


def test_admin_preserves_every_assignment_field_and_waives_preferences(scheduling_db,monkeypatch):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        outgoing,incoming,_,periods=setup(db,actor,ids)
        target=next(s for s in rows(db,periods[0]) if s.employee_id==outgoing.id)
        target.is_double_coverage=True;target.double_coverage_manually_assigned=True
        target.is_shift_lead=True;target.lead_of_day_manually_assigned=True
        profile=db.scalar(select(m.EmployeeSchedulingProfile).where(m.EmployeeSchedulingProfile.employee_id==incoming.id))
        profile.maximum_weekly_hours=Decimal('1');profile.preferred_workdays=1
        db.commit();before=snapshot(db)
        forbid_engine(monkeypatch)
        review=type('NoConfirmation',(),{'finish':lambda *a,**kw:pytest.fail('Extra confirmation requested')})()
        result=training.save_transition(db,principal=owner(actor),employee_id=incoming.id,
            first_schedule_date=START,replacing_employee_id=outgoing.id,
            allowed_store_ids=(ids['north'],),now=NOW,owner_review=review)
        db.commit();db.expire_all();after=snapshot(db)
        changed={r['shift_id'] for r in result['changes']}
        assert len(changed)==6
        mutable={'employee_id','manually_locked','locked_by_principal_id','locked_at','lock_reason','updated_by_principal_id','updated_at'}
        for sid,original in before.items():
            if sid not in changed:assert after[sid]==original
            else:
                assert after[sid]['employee_id']==incoming.id
                assert {k:v for k,v in after[sid].items() if k not in mutable}=={k:v for k,v in original.items() if k not in mutable}
        from app.services.scheduling_operational_exception_service import authorized_assignment_codes
        assert 'MAX_WEEKLY_HOURS' in authorized_assignment_codes(db,db.get(m.ScheduleShift,target.id))
        assert transfer(db,actor,(ids['north'],),incoming,outgoing)['unchanged']


@pytest.mark.parametrize('blocker',['overlap','different_store','availability','cannot_work','inactive','eligibility','lead','pto','evidence'])
def test_admin_hard_conflicts_rollback_all_weeks(scheduling_db,blocker):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        outgoing,incoming,enrollment,periods=setup(db,actor,ids)
        if blocker in {'overlap','different_store'}:
            shift(db,actor,periods[1],incoming,START+timedelta(days=7),
                  ids['north'] if blocker=='overlap' else ids['south'],hour=9 if blocker=='overlap' else 11)
        elif blocker=='availability':
            db.add(m.EmployeeSchedulingWindow(employee_id=incoming.id,day_of_week=0,
                kind=m.SchedulingWindowKind.HARD_UNAVAILABLE,start_time=time.min,end_time=time.max,active=True,
                created_by_principal_id=actor.id,updated_by_principal_id=actor.id))
        elif blocker=='cannot_work':
            profile=db.scalar(select(m.EmployeeSchedulingProfile).where(m.EmployeeSchedulingProfile.employee_id==incoming.id))
            profile.weekday_preferences=['CANNOT_WORK']+['NEUTRAL']*6
        elif blocker=='pto':
            db.add(m.TimeOffRequest(employee_id=incoming.id,start_date=START,end_date=START,
                full_day=True,reason_category_id=ids['vacation'],status=m.TimeOffRequestStatus.APPROVED,
                created_by_principal_id=actor.id,updated_by_principal_id=actor.id))
        elif blocker=='evidence':
            target=next(s for s in rows(db,periods[1]) if s.employee_id==outgoing.id)
            db.add(m.ScheduleCoverageCommitment(schedule_shift_id=target.id,employee_id=incoming.id,
                recorded_by_principal_id=actor.id))
        elif blocker=='inactive':incoming.scheduling_active=False
        elif blocker=='eligibility':incoming.last_effective_date=START
        else:incoming.scheduling_lead_capable=False
        db.commit();before=snapshot(db);versions=[p.version for p in periods]
        with pytest.raises(SchedulingValidationError):
            transfer(db,actor,(ids['north'],ids['south']),incoming,outgoing)
        db.commit();db.expire_all()
        assert snapshot(db)==before
        assert [p.version for p in periods]==versions
        assert incoming.first_schedule_date is None and enrollment.completed_effective_date is None


def test_admin_published_revision_preserves_original_and_unrelated_cards(scheduling_db,monkeypatch):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        outgoing,incoming,_,periods=setup(db,actor,ids)
        periods[0].status=m.SchedulePeriodStatus.PUBLISHED;periods[0].published_at=NOW
        db.commit();before=snapshot(db)
        forbid_engine(monkeypatch)
        result=transfer(db,actor,(ids['north'],),incoming,outgoing);db.commit()
        draft=db.get(m.SchedulePeriod,result['period_ids'][0])
        assert draft.status==m.SchedulePeriodStatus.DRAFT and draft.supersedes_schedule_period_id==periods[0].id
        for row in rows(db,periods[0]):assert snapshot(db)[row.id]==before[row.id]
        for copy in rows(db,draft):
            original=before[copy.source_shift_id]
            assert copy.employee_id==(incoming.id if original['employee_id']==outgoing.id else original['employee_id'])
            for key in ['store_id','shift_date','start_time','end_time','unpaid_break_minutes',
                        'generated_from_coverage_requirement','base_pattern_expected_day','base_pattern_deviation_reason',
                        'shift_type_id','is_opener','is_closer','is_double_coverage','is_shift_lead','is_lead_of_day']:
                assert getattr(copy,key)==original[key]


def seed_representative(db,actor,ids,weeks):
    """Match recorded release cardinality: 117 people, 50 periods, 1,312 shifts."""
    outgoing=person(db,actor,'Micah synthetic',lead=True,target=3)
    incoming=person(db,actor,'Transitioned synthetic',lead=True,target=3)
    people=list(db.scalars(select(m.Employee)))
    for index in range(117-len(people)):
        people.append(person(db,actor,f'Synthetic worker {index}',lead=True,target=3))
    stores=[db.get(m.Store,ids['north']),db.get(m.Store,ids['south'])]
    for index in range(4):
        store=m.Store(name=f'Synthetic store {index}',square_location_id=f'SYNTH-{index}',active=True)
        db.add(store);stores.append(store)
    db.flush()
    for store in stores:
        for weekday in range(7):opening_hours(db,actor,store.id,weekday,opening=time(9,15),closing=time(10))
    periods=[]
    for week in range(12):
        p=period(db,actor,START+timedelta(weeks=week));periods.append(p)
        for day in range(7):
            for store_index,store in enumerate(stores):
                employee=outgoing if week<weeks and day<3 and store_index==0 else people[(day*6+store_index+5)%len(people)]
                # Keep the incoming recipient free in the initial schedule.
                if employee.id in {incoming.id,outgoing.id}:employee=people[-1]
                if week<weeks and day<3 and store_index==0:employee=outgoing
                db.add(m.ScheduleShift(schedule_period_id=p.id,employee_id=employee.id,store_id=store.id,
                    shift_date=p.week_start_date+timedelta(days=day),start_time=time(9),end_time=time(10),
                    unpaid_break_minutes=0,is_lead_of_day=store_index==0,
                    created_by_principal_id=actor.id,updated_by_principal_id=actor.id))
    for index in range(38):
        p=period(db,actor,START-timedelta(weeks=index+1),'ARCHIVED');p.is_historical=True
        count=22 if index<10 else 21
        for j in range(count):
            db.add(m.ScheduleShift(schedule_period_id=p.id,employee_id=people[(j+5)%len(people)].id,
                store_id=stores[j%6].id,shift_date=p.week_start_date+timedelta(days=j%7),
                start_time=time(9),end_time=time(10),unpaid_break_minutes=0,
                created_by_principal_id=actor.id,updated_by_principal_id=actor.id))
    db.commit()
    assert db.scalar(select(func.count()).select_from(m.ScheduleShift))==1312
    return outgoing,incoming,tuple(s.id for s in stores)


@pytest.mark.parametrize('weeks,budget',[(1,2),(12,5)])
def test_substitution_production_sized_performance(scheduling_db,monkeypatch,weeks,budget):
    Session,actor,ids,engine=scheduling_db
    with Session() as db:
        outgoing,incoming,stores=seed_representative(db,actor,ids,weeks)
        before=snapshot(db)
        forbid_engine(monkeypatch)
        calls=[]
        def count(*args):calls.append(args[2])
        event.listen(engine,'before_cursor_execute',count)
        started=timer.perf_counter()
        result=transfer(db,actor,stores,incoming,outgoing)
        db.commit()
        elapsed=timer.perf_counter()-started
        event.remove(engine,'before_cursor_execute',count)
        print(json.dumps(dict(operation='substitution',fixture_employees=117,fixture_periods=50,
            fixture_shifts=1312,weeks=weeks,assignments=len(result['changes']),
            seconds=elapsed,sql_statements=len(calls),target_seconds=budget)),flush=True)
        assert len(result['changes'])==weeks*3
        assert elapsed<budget
        assert len(calls)<200
        db.expire_all();after=snapshot(db)
        changed={r['shift_id'] for r in result['changes']}
        assert {k:v for k,v in before.items() if k not in changed}=={k:v for k,v in after.items() if k not in changed}


def test_published_horizon_substitution_is_bounded_and_preserves_all_source_rows(scheduling_db,monkeypatch):
    Session,actor,ids,engine=scheduling_db
    with Session() as db:
        outgoing,incoming,stores=seed_representative(db,actor,ids,12)
        for p in db.scalars(select(m.SchedulePeriod).where(m.SchedulePeriod.status==m.SchedulePeriodStatus.DRAFT)):
            p.status=m.SchedulePeriodStatus.PUBLISHED;p.published_at=NOW
        db.commit();before=snapshot(db)
        forbid_engine(monkeypatch)
        started=timer.perf_counter()
        result=transfer(db,actor,stores,incoming,outgoing);db.commit()
        elapsed=timer.perf_counter()-started
        assert len(result['draft_revisions'])==12 and len(result['changes'])==36
        assert elapsed<5
        after=snapshot(db)
        assert {k:after[k] for k in before}==before
        print(json.dumps(dict(operation='published_horizon_substitution',weeks=12,
                              assignments=36,seconds=elapsed)),flush=True)


def test_substitution_profile_save_and_board_refresh(employee_client,monkeypatch,scheduling_clock):
    from app.services import v2_scheduling_policy_service as policy
    from urllib.parse import unquote
    scheduling_clock(START-timedelta(days=5),'app.services.scheduling_training_service')
    client,current,Session,ids=employee_client
    actor=current['principal']
    with Session() as db:
        outgoing,incoming,_,periods=setup(db,actor,ids,training=False)
        eid,oid=incoming.id,outgoing.id
        db.commit()
    current['principal']=owner(actor)
    def forbidden(*a,**kw):pytest.fail('Profile save or refresh invoked generation')
    monkeypatch.setattr(policy,'regenerate_period',forbidden)
    result=client.post(f'/v2/scheduling/employees/{eid}/training',data={
        'action':'transition','first_schedule_date':str(START),'replacing_employee_id':oid,
        'csrf_token':client.cookies.get('csrf_token')})
    assert result.status_code==303 and 'error=' not in result.headers['location'],unquote(result.headers.get('location',''))
    with Session() as db:
        assert db.get(m.Employee,eid).replacing_employee_id==oid
        assert db.scalar(select(func.count()).select_from(m.ScheduleShift).where(m.ScheduleShift.employee_id==eid))==6
    board=client.get('/v2/scheduling/api/board?start='+str(START))
    assert board.status_code==200,board.text
    assert sum(s['employee_id']==eid for s in board.json()['shifts'])==3


def test_substitution_honors_required_rest_across_adjacent_weeks(scheduling_db):
    from app.services.v2_scheduling_policy_service import MAX_ALLOWED_CONSECUTIVE_WORK_DAYS
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        outgoing,incoming,_,_=setup(db,actor,ids,training=False)
        profile=db.scalar(select(m.EmployeeSchedulingProfile).where(m.EmployeeSchedulingProfile.employee_id==incoming.id))
        profile.minimum_days_off_after_max_block=3
        previous=period(db,actor,START-timedelta(weeks=1))
        # A maximum block ends two days before the new Sunday assignment.
        for offset in range(MAX_ALLOWED_CONSECUTIVE_WORK_DAYS):
            day=START-timedelta(days=2+offset)
            # Keep each row in its actual week.
            p=previous if previous.week_start_date<=day<=previous.week_end_date else db.scalar(
                select(m.SchedulePeriod).where(m.SchedulePeriod.week_start_date==START-timedelta(weeks=2)))
            if p is None:p=period(db,actor,START-timedelta(weeks=2))
            shift(db,actor,p,incoming,day,ids['north'])
        db.commit();before=snapshot(db)
        with pytest.raises(SchedulingValidationError,match='day.*off'):
            transfer(db,actor,(ids['north'],),incoming,outgoing)
        db.commit()
        assert snapshot(db)==before


@pytest.mark.parametrize('conflict_in_draft',[False,True])
def test_substitution_validates_selected_draft_instead_of_published_source(scheduling_db,conflict_in_draft):
    from app.services.v2_scheduling_service import clone_published_revision
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        outgoing,incoming,_,periods=setup(db,actor,ids,training=False)
        published=periods[0];published.status=m.SchedulePeriodStatus.PUBLISHED;published.published_at=NOW
        if not conflict_in_draft:
            extra=shift(db,actor,published,incoming,START,ids['north'])
        db.flush()
        draft=clone_published_revision(db,principal=owner(actor),published_period_id=published.id,
            allowed_store_ids=(ids['north'],),_refresh_warnings=False)
        if conflict_in_draft:
            shift(db,actor,draft,incoming,START,ids['north'])
        else:
            for row in rows(db,draft):
                if row.employee_id==incoming.id:db.delete(row)
        db.commit()
        if conflict_in_draft:
            with pytest.raises(SchedulingValidationError,match='overlapping'):
                transfer(db,actor,(ids['north'],),incoming,outgoing)
            db.commit()
            assert db.get(m.Employee,incoming.id).first_schedule_date is None
        else:
            result=transfer(db,actor,(ids['north'],),incoming,outgoing);db.commit()
            assert len(result['changes'])==6
        assert db.info.get('scheduling_swap_periods') is None
