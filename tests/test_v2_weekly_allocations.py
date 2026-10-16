"""Exact weekly allocations and evidence-based flexible coverage."""
from datetime import date, timedelta, time
from collections import Counter
from sqlalchemy import select
from app import models as m
from app.services.v2_scheduling_repair_service import allocate_weekly_positions, solve_weekly_patterns
from test_v2_scheduling_foundation import scheduling_db
from test_v2_scheduling_repair import person, period, shift


def test_exact_cover_search_reconsiders_first_choice():
    # A greedy choice for employee1 blocks employee2; exact search must move it.
    plan, search = solve_weekly_patterns({1:[0b0011,0b1100],2:[0b0011]},0b1111)
    assert plan == {2:3,1:12} and search['exhaustive']


def test_search_limit_is_not_a_constraint_certificate():
    plan, search = solve_weekly_patterns({1:[1],2:[2]},3,node_limit=1)
    assert plan is None and not search['exhaustive']


def setup(db, actor, ids, targets=(3,2,1)):
    for e in db.scalars(select(m.Employee)):e.scheduling_active=False
    people=[person(db,actor,'Allocation '+str(i),target=n) for i,n in enumerate(targets)]
    per=period(db,actor,date(2026,10,25))
    rows=[shift(db,actor,per,None,per.week_start_date+timedelta(days=i),ids['north'] if i%2 else ids['south']) for i in range(sum(targets))]
    db.flush()
    return people,per,rows


def run(db,actor,per):
    return allocate_weekly_positions(db,principal=actor,period=per,planning_date=date(2026,10,16))


def test_three_two_one_exact_across_stores(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        people,per,rows=setup(db,actor,ids)
        result=run(db,actor,per)
        assert result['status']=='PASS' and result['mode']=='EXACT_BASELINE'
        assert Counter(r.employee_id for r in rows)==Counter({e.id:n for e,n in zip(people,[3,2,1])})
        assert result['exceptions']==[]


def test_no_arbitrary_extra_for_mismatched_configuration(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        people,per,rows=setup(db,actor,ids,targets=(3,1))
        shift(db,actor,per,None,per.week_start_date+timedelta(days=4),ids['north'])
        result=run(db,actor,per)
        assert result['status']=='PARTIAL'
        assert result['diagnostics'][0]['code']=='WEEKLY_ALLOCATION_COVERAGE_MISMATCH'
        assert Counter(r.employee_id for r in rows)==Counter({people[0].id:3,people[1].id:1})


def test_pto_flex_exception_and_return_to_baseline(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        people,per,rows=setup(db,actor,ids,targets=(3,1))
        # Three-shift employee can only work two available positions this week.
        db.add(m.TimeOffRequest(reason_category_id=ids['vacation'],employee_id=people[0].id,start_date=per.week_start_date,
            end_date=per.week_start_date+timedelta(days=1),full_day=True,
            status=m.TimeOffRequestStatus.APPROVED,created_by_principal_id=actor.id,
            updated_by_principal_id=actor.id))
        db.flush()
        result=run(db,actor,per)
        assert result['status']=='PASS' and result['mode']=='CONSTRAINT_DRIVEN_FLEXIBLE_COVERAGE'
        assert Counter(r.employee_id for r in rows)==Counter({people[0].id:2,people[1].id:2})
        ex=result['exceptions'][0]
        assert ex['employee_id']==people[1].id and ex['additional_shifts']==1
        assert ex['displaced_allocations'][0]['employee_id']==people[0].id
        assert 'APPROVED_TIME_OFF' in str(ex['displaced_allocations'])
        assert db.scalar(select(m.EmployeeSchedulingProfile.target_shifts_per_week).where(m.EmployeeSchedulingProfile.employee_id==people[1].id))==1
        nextper=period(db,actor,date(2026,11,1))
        nextrows=[shift(db,actor,nextper,None,nextper.week_start_date+timedelta(days=i),ids['north']) for i in range(4)]
        again=run(db,actor,nextper)
        assert again['status']=='PASS' and again['exceptions']==[]
        assert Counter(r.employee_id for r in nextrows)==Counter({people[0].id:3,people[1].id:1})


def test_pto_alternative_days_before_flexible_exception(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        people,per,rows=setup(db,actor,ids,targets=(3,1))
        db.add(m.TimeOffRequest(reason_category_id=ids['vacation'],employee_id=people[0].id,start_date=per.week_start_date,
            end_date=per.week_start_date,full_day=True,status=m.TimeOffRequestStatus.APPROVED,
            created_by_principal_id=actor.id,updated_by_principal_id=actor.id));db.flush()
        result=run(db,actor,per)
        assert result['status']=='PASS' and result['exceptions']==[]
        assert rows[0].employee_id==people[1].id


def test_no_eligible_flexible_employee_preserves_locked_assignment(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        people,per,rows=setup(db,actor,ids,targets=(3,1))
        rows[3].employee_id=people[1].id;rows[3].manually_locked=True
        for e in people:
            db.add(m.TimeOffRequest(reason_category_id=ids['vacation'],employee_id=e.id,start_date=per.week_start_date,
                end_date=per.week_start_date+timedelta(days=1),full_day=True,
                status=m.TimeOffRequestStatus.APPROVED,created_by_principal_id=actor.id,
                updated_by_principal_id=actor.id))
        db.flush();result=run(db,actor,per)
        assert result['status']=='UNRESOLVED' and result['exceptions']==[]
        assert rows[3].employee_id==people[1].id and rows[3].manually_locked


def test_two_flexible_employees_prefer_less_prior_burden(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        people,per,rows=setup(db,actor,ids,targets=(3,1,1))
        prior=period(db,actor,date(2026,10,4))
        prior.status=m.SchedulePeriodStatus.PUBLISHED
        shift(db,actor,prior,people[1],prior.week_start_date,ids['north'])
        db.add(m.TimeOffRequest(reason_category_id=ids['vacation'],employee_id=people[0].id,
            start_date=per.week_start_date,end_date=per.week_start_date+timedelta(days=2),
            full_day=True,status=m.TimeOffRequestStatus.APPROVED,
            created_by_principal_id=actor.id,updated_by_principal_id=actor.id));db.flush()
        result=run(db,actor,per)
        assert result['status']=='PASS'
        assert Counter(r.employee_id for r in rows)==Counter({people[0].id:2,people[1].id:1,people[2].id:2})
        assert result['exceptions'][0]['employee_id']==people[2].id


def test_midweek_eligibility_boundary_is_hard(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        people,per,rows=setup(db,actor,ids,targets=(3,1))
        # Starts Tuesday: only Tuesday/Wednesday positions are eligible.
        db.add(m.SchedulingEligibilityPeriod(employee_id=people[0].id,
            start_date=per.week_start_date+timedelta(days=2),
            created_by_principal_id=actor.id,updated_by_principal_id=actor.id));db.flush()
        result=run(db,actor,per)
        assert result['status']=='PASS' and result['mode']=='CONSTRAINT_DRIVEN_FLEXIBLE_COVERAGE'
        assert all(r.shift_date>=per.week_start_date+timedelta(days=2) for r in rows if r.employee_id==people[0].id)
        assert result['exceptions'][0]['additional_shifts']==1


def test_longview_exact_primary_and_pto_flexible_cover(scheduling_db):
    from app.services.v2_scheduling_policy_service import configure_special_store
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        for e in db.scalars(select(m.Employee)):e.scheduling_active=False
        micah,renee,flex,other=[person(db,actor,'LV '+str(i),target=n) for i,n in enumerate([3,3,1,1])]
        configure_special_store(db,principal=actor,store_id=ids['south'],
            primary_employee_ids=(micah.id,renee.id),rotation_employee_ids=(flex.id,other.id))
        per=period(db,actor,date(2026,10,25))
        rows=[shift(db,actor,per,None,per.week_start_date+timedelta(days=i),ids['south']) for i in range(7)]
        rows.append(shift(db,actor,per,None,per.week_start_date+timedelta(days=3),ids['north']))
        result=run(db,actor,per)
        assert result['status']=='PASS' and result['exceptions']==[]
        assert Counter(r.employee_id for r in rows)[micah.id]==Counter(r.employee_id for r in rows)[renee.id]==3
        assert all(r.store_id==ids['south'] for r in rows if r.employee_id in (micah.id,renee.id))
        for r in rows:r.employee_id=None
        db.add(m.TimeOffRequest(reason_category_id=ids['vacation'],employee_id=renee.id,
            start_date=per.week_start_date,end_date=per.week_start_date+timedelta(days=4),
            full_day=True,status=m.TimeOffRequestStatus.APPROVED,
            created_by_principal_id=actor.id,updated_by_principal_id=actor.id));db.flush()
        result=run(db,actor,per)
        assert result['status']=='PASS' and result['mode']=='CONSTRAINT_DRIVEN_FLEXIBLE_COVERAGE'
        counts=Counter(r.employee_id for r in rows)
        assert counts[micah.id]==3 and counts[renee.id]==2
        assert sum(ex['additional_shifts'] for ex in result['exceptions'])==1
        assert all(r.store_id==ids['south'] for r in rows if r.employee_id in (micah.id,renee.id))


def test_shared_hard_day_constraint_has_explicit_witness(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        people,per,rows=setup(db,actor,ids,targets=(3,3,1))
        for e in people[:2]:
            for offset in (1,3,5,6):
                db.add(m.EmployeeSchedulingWindow(employee_id=e.id,
                    day_of_week=offset,start_time=time(0),end_time=time(23,59),
                    kind=m.SchedulingWindowKind.HARD_UNAVAILABLE,
                    created_by_principal_id=actor.id,updated_by_principal_id=actor.id))
        db.flush();result=run(db,actor,per)
        assert result['status']=='PASS'
        assert Counter(r.employee_id for r in rows)[people[2].id]==4
        assert result['exceptions'][0]['additional_shifts']==3
        assert any(loss.get('code')=='SHARED_REQUIRED_POSITION_CONFLICT' for loss in result['exceptions'][0]['displaced_allocations'])


def test_departure_last_day_inclusive_for_flexible_exception(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        people,per,rows=setup(db,actor,ids,targets=(3,1))
        people[0].last_effective_date=per.week_start_date+timedelta(days=1)
        db.flush();result=run(db,actor,per)
        assert result['status']=='PASS' and result['mode']=='CONSTRAINT_DRIVEN_FLEXIBLE_COVERAGE'
        assert all(r.shift_date<=people[0].last_effective_date for r in rows if r.employee_id==people[0].id)
        assert Counter(r.employee_id for r in rows)==Counter({people[0].id:2,people[1].id:2})


def test_anonymized_production_224_exact_allocations_nine_week_horizon(scheduling_db, monkeypatch):
    """Permanent current-data shape: ten people, four stores, eight missing weeks."""
    import json
    from pathlib import Path
    from datetime import datetime, timezone
    from app.services import v2_scheduling_policy_service as p
    from test_v2_scheduling_foundation import opening_hours
    from app.services.v2_scheduling_rules_service import create_coverage_requirement
    shape=json.loads((Path(__file__).parent/'fixtures/exact_weekly_allocations_224.json').read_text())
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        for e in db.scalars(select(m.Employee)):e.scheduling_active=False
        stores={1:ids['north'],2:ids['south']}
        for n in (3,4):
            row=m.Store(name='Allocation store '+str(n),square_location_id='ALLOC-'+str(n),active=True)
            db.add(row);db.flush();stores[n]=row.id
        people={}
        for raw in shape['employees']:
            e=person(db,actor,'Allocation employee '+str(raw['employee']),lead=raw['lead'],target=raw['target_shifts_per_week'])
            people[raw['employee']]=e
            if raw['employee']==10:
                from app.services.v2_scheduling_allocation_service import change_allocation, weekly_allocation
                baseline=db.scalar(select(m.EmployeeSchedulingProfile).where(m.EmployeeSchedulingProfile.employee_id==e.id))
                baseline.target_shifts_per_week=1;db.flush()
                change_allocation(db,principal=actor,employee_id=e.id,effective_date=date(2026,10,25),shifts_per_week=3,today=date(2026,10,16))
                assert weekly_allocation(db,e.id,date(2026,10,18))==1
                assert weekly_allocation(db,e.id,date(2026,10,25))==3
            profile=db.scalar(select(m.EmployeeSchedulingProfile).where(m.EmployeeSchedulingProfile.employee_id==e.id))
            for field in ('maximum_weekly_hours','max_consecutive_work_days','minimum_days_off_after_max_block','approval_weekly_hours'):
                setattr(profile,field,raw[field])
            if raw['eligible_start']:
                db.add(m.SchedulingEligibilityPeriod(employee_id=e.id,start_date=date.fromisoformat(raw['eligible_start']),created_by_principal_id=actor.id,updated_by_principal_id=actor.id))
        for raw in shape['preferences']:
            db.add(m.EmployeeSchedulingStorePreference(employee_id=people[raw['employee']].id,store_id=stores[raw['store']],preference_level=m.StorePreferenceLevel(raw['preference_level']),created_by_principal_id=actor.id,updated_by_principal_id=actor.id))
        for raw in shape['windows']:
            db.add(m.EmployeeSchedulingWindow(employee_id=people[raw['employee']].id,day_of_week=raw['day_of_week'],start_time=time.fromisoformat(raw['start_time']),end_time=time.fromisoformat(raw['end_time']),kind=m.SchedulingWindowKind(raw['kind']),created_by_principal_id=actor.id,updated_by_principal_id=actor.id))
        for raw in shape['pto']:
            db.add(m.TimeOffRequest(employee_id=people[raw['employee']].id,reason_category_id=ids['vacation'],start_date=date.fromisoformat(raw['start_date']),end_date=date.fromisoformat(raw['end_date']),full_day=raw['full_day'],start_time=time.fromisoformat(raw['start_time']) if raw['start_time'] else None,end_time=time.fromisoformat(raw['end_time']) if raw['end_time'] else None,status=m.TimeOffRequestStatus.APPROVED,created_by_principal_id=actor.id,updated_by_principal_id=actor.id))
        for sid in stores.values():
            for day in range(7):
                opening_hours(db,actor,sid,day)
                create_coverage_requirement(db,principal=actor,store_id=sid,day_of_week=day,start_time=time(9),end_time=time(21),minimum_employee_count=1,allowed_store_ids=tuple(stores.values()))
        primary=tuple(people[r['employee']].id for r in shape['employees'] if r['primary'])
        rotation=tuple(e.id for e in people.values() if e.id not in primary)
        p.configure_special_store(db,principal=actor,store_id=stores[3],primary_employee_ids=primary,rotation_employee_ids=rotation)
        published=period(db,actor,date(2026,10,18));published.status=m.SchedulePeriodStatus.PUBLISHED
        for raw in shape['previous_published_shifts']:
            db.add(m.ScheduleShift(schedule_period_id=published.id,employee_id=people[raw['employee']].id,store_id=stores[raw['store']],shift_date=date.fromisoformat(raw['shift_date']),start_time=time.fromisoformat(raw['start_time']),end_time=time.fromisoformat(raw['end_time']),unpaid_break_minutes=raw['unpaid_break_minutes'],created_by_principal_id=actor.id,updated_by_principal_id=actor.id))
        org=p.organization_policy(db,principal_id=actor.id);org.schedule_length_weeks=9;org.replenish_at_weeks=8
        db.flush()
        original=[(r.id,r.employee_id,r.store_id,r.shift_date) for r in db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==published.id))]
        monkeypatch.setattr(p,'scheduling_execution_enabled',lambda:True)
        now=datetime(2026,10,16,16,tzinfo=timezone.utc)
        result=p.run_schedule_automation(db,principal=actor,now=now)
        assert len(result['generated_period_ids'])==8 and result['published_period_ids']==[]
        for pid in result['generated_period_ids']:
            per=db.get(m.SchedulePeriod,pid);assert per.status==m.SchedulePeriodStatus.DRAFT
            rows=list(db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==pid)))
            assert len(rows)==28 and all(r.employee_id is not None and r.start_time==time(8,45) for r in rows)
            counts=Counter(r.employee_id for r in rows)
            for raw in shape['employees']:assert counts[people[raw['employee']].id]==raw['target_shifts_per_week']
            assert sum(r.store_id==stores[3] and r.employee_id not in primary for r in rows)==1
            assert all(r.store_id==stores[3] for r in rows if r.employee_id in primary)
            for r in rows:
                assert p.evaluate_assignment(db,employee_id=r.employee_id,store_id=r.store_id,shift_date=r.shift_date,start_time=r.start_time,end_time=r.end_time,exclude_shift_id=r.id,schedule_period_id=per.id).eligible
        assert [(r.id,r.employee_id,r.store_id,r.shift_date) for r in db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==published.id))]==original
        again=p.run_schedule_automation(db,principal=actor,now=now)
        assert again['generated_period_ids']==again['published_period_ids']==[]
        assert len(list(db.scalars(select(m.SchedulePeriod))))==9


def test_two_shift_profile_is_automatically_flexible(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        people,per,rows=setup(db,actor,ids,targets=(3,2))
        db.add(m.TimeOffRequest(reason_category_id=ids['vacation'],employee_id=people[0].id,
            start_date=per.week_start_date,end_date=per.week_start_date+timedelta(days=2),
            full_day=True,status=m.TimeOffRequestStatus.APPROVED,
            created_by_principal_id=actor.id,updated_by_principal_id=actor.id));db.flush()
        result=run(db,actor,per)
        assert result['status']=='PASS'
        assert Counter(r.employee_id for r in rows)==Counter({people[0].id:2,people[1].id:3})
        assert result['exceptions'][0]['configured_shifts']==2
        assert result['exceptions'][0]['additional_shifts']==1


def test_existing_nonoverlapping_positions_have_no_invented_daily_cap(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        people,per,rows=setup(db,actor,ids,targets=(3,2))
        rows[4].shift_date=rows[3].shift_date;rows[4].start_time=time(11);rows[4].end_time=time(12)
        db.add(m.TimeOffRequest(reason_category_id=ids['vacation'],employee_id=people[1].id,
            start_date=per.week_start_date,end_date=per.week_start_date+timedelta(days=2),
            full_day=True,status=m.TimeOffRequestStatus.APPROVED,
            created_by_principal_id=actor.id,updated_by_principal_id=actor.id))
        db.add(m.EmployeeSchedulingWindow(employee_id=people[0].id,day_of_week=3,
            start_time=time.min,end_time=time.max,kind=m.SchedulingWindowKind.HARD_UNAVAILABLE,
            created_by_principal_id=actor.id,updated_by_principal_id=actor.id));db.flush()
        result=run(db,actor,per)
        assert result['status']=='PASS' and result['exceptions']==[]
        assert rows[3].employee_id==rows[4].employee_id==people[1].id
        assert Counter(r.employee_id for r in rows)==Counter({people[0].id:3,people[1].id:2})


def test_whole_week_departure_uses_dated_loss_without_phantom_target(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        people,per,rows=setup(db,actor,ids,targets=(3,3,1))
        people[0].last_effective_date=per.week_start_date-timedelta(days=2)
        db.flush();result=run(db,actor,per)
        assert result['status']=='PASS' and result['mode']=='CONSTRAINT_DRIVEN_FLEXIBLE_COVERAGE'
        assert Counter(r.employee_id for r in rows)==Counter({people[1].id:3,people[2].id:4})
        assert all(x['employee_id']!=people[0].id for x in result['weekly_allocations'])
        ex=result['exceptions'][0];assert ex['employee_id']==people[2].id and ex['additional_shifts']==3
        assert ex['displaced_allocations'][0]['employee_id']==people[0].id
        assert ex['displaced_allocations'][0]['known_last_eligible_date']=='2026-10-23'
        assert 'DATED_ELIGIBILITY_CAPACITY_LOSS' in str(ex)


def test_invalid_protected_lead_cannot_be_silently_replaced(scheduling_db):
    import pytest
    from app.services import v2_scheduling_policy_service as p
    from app.services.v2_scheduling_service import create_shift, ShiftInput, SchedulingValidationError
    from app.services.v2_scheduling_assignments_service import set_lead_of_day
    from app.services.v2_scheduling_rules_service import upsert_employee_profile
    from test_v2_scheduling_foundation import _coverage
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        for key in ('alex','blair'):
            upsert_employee_profile(db,principal=actor,employee_id=ids[key],home_store_id=ids['north'],
                target_weekly_hours=13,target_shifts_per_week=1,allowed_store_ids=(ids['north'],ids['south']))
        _coverage(db,actor,ids,count=2)
        per=period(db,actor,date(2026,10,25))
        original=create_shift(db,principal=actor,schedule_period_id=per.id,expected_version=per.version,
            values=ShiftInput(employee_id=ids['alex'],store_id=ids['north'],shift_date=per.week_start_date,start_time=time(8,45),end_time=time(22),unpaid_break_minutes=0),
            allowed_store_ids=(ids['north'],ids['south']))
        row=db.get(m.ScheduleShift,original.shift_id)
        set_lead_of_day(db,principal=actor,shift_id=row.id)
        db.add(m.TimeOffRequest(reason_category_id=ids['vacation'],employee_id=ids['alex'],start_date=per.week_start_date,end_date=per.week_start_date,full_day=True,status=m.TimeOffRequestStatus.APPROVED,created_by_principal_id=actor.id,updated_by_principal_id=actor.id));db.flush()
        version=per.version
        with pytest.raises(SchedulingValidationError,match='Protected assignments require'):
            p.regenerate_period(db,principal=actor,schedule_period_id=per.id)
        assert row.employee_id==ids['alex'] and row.is_lead_of_day and row.lead_of_day_manually_assigned
        assert per.version==version
        assert list(db.scalars(select(m.ScheduleShift.id).where(m.ScheduleShift.schedule_period_id==per.id)))==[row.id]
