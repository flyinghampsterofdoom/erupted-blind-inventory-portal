"""Configured demand bounds every prospective position, regardless of origin."""
from collections import Counter
from datetime import date, time

import pytest
from sqlalchemy import select, func, delete
from app import models as m
from app.services.v2_scheduling_policy_service import regenerate_period
from app.services.v2_scheduling_coverage_service import rebuild_schedule_warnings, coverage_excesses
from app.services.v2_scheduling_rules_service import create_coverage_requirement
from app.services.v2_scheduling_service import (
    create_draft_period, create_shift, ShiftInput, approve_schedule, publish_schedule,
    SchedulingValidationError,
)
from test_v2_scheduling_foundation import scheduling_db, opening_hours
from test_v2_scheduling_repair import person


def setup_day(db, actor, ids, headcount=1):
    for employee in db.scalars(select(m.Employee)):
        employee.scheduling_active = False
    store = ids['north']
    opening_hours(db, actor, store, 0, closing=time(10))
    create_coverage_requirement(db, principal=actor, store_id=store, day_of_week=0,
        start_time=time(8,45), end_time=time(10), minimum_employee_count=headcount,
        allowed_store_ids=(store,))
    db.get(m.SchedulingStoreDefaults,1).standard_shift_end=time(10)
    week=create_draft_period(db,principal=actor,week_start=date(2027,1,3))
    return week,store


def manual(db, actor, week, employee, store):
    return create_shift(db,principal=actor,schedule_period_id=week.id,expected_version=week.version,
        values=ShiftInput(employee_id=employee.id,store_id=store,shift_date=week.week_start_date,
                          start_time=time(8,45),end_time=time(10),unpaid_break_minutes=0),
        allowed_store_ids=(store,))


def test_four_store_week_is_bounded_despite_excessive_targets(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        for employee in db.scalars(select(m.Employee)):employee.scheduling_active=False
        stores=[db.get(m.Store,ids['north']),db.get(m.Store,ids['south'])]
        stores[0].name='Andresen';stores[1].name='HWY 99'
        for name in ('SR 503','Longview'):
            row=m.Store(name=name,square_location_id='exact-'+name,active=True);db.add(row);stores.append(row)
        db.flush()
        employees=[person(db,actor,'Exact '+str(i),lead=True,target=7) for i in range(6)]
        for store in stores:
            for weekday in range(7):
                opening_hours(db,actor,store.id,weekday,closing=time(10))
                create_coverage_requirement(db,principal=actor,store_id=store.id,day_of_week=weekday,
                    start_time=time(8,45),end_time=time(10),minimum_employee_count=1,
                    allowed_store_ids=tuple(s.id for s in stores))
        db.get(m.SchedulingStoreDefaults,1).standard_shift_end=time(10)
        week=create_draft_period(db,principal=actor,week_start=date(2027,1,3))
        for attempt in range(2):
            if attempt:
                from app.services.v2_scheduling_snapshot_service import serialize_schedule
                before=serialize_schedule(db,week)
                with pytest.raises(SchedulingValidationError,match='left unchanged'):
                    regenerate_period(db,principal=actor,schedule_period_id=week.id)
                assert serialize_schedule(db,week)==before
            else:result=regenerate_period(db,principal=actor,schedule_period_id=week.id)
            rows=list(db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==week.id)))
            assert len(rows)==28 and set(Counter((s.store_id,s.shift_date) for s in rows).values())=={1}
            assert all(s.start_time==time(8,45) and s.end_time==time(10) for s in rows)
            assert result['unmet_shift_targets']
            assert len(result['uncovered'])==28
            assert result['allocation']['diagnostics'][0]['code']=='WEEKLY_ALLOCATION_COVERAGE_MISMATCH'
            assert result['allocation']['diagnostics'][0]['configured_shifts']==42
            assert all(r.employee_id is None for r in rows)
            assert all(r['shortfall']==r['target_shifts']-r['assigned_shifts'] for r in result['unmet_shift_targets'])


def test_configured_two_positions_no_third_and_zero_target_is_visible(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        week,store=setup_day(db,actor,ids,2)
        employees=[person(db,actor,'Two '+str(i),lead=True,target=3) for i in range(3)]
        # Overlapping requirements use maximum demand, not their sum.
        create_coverage_requirement(db,principal=actor,store_id=store,day_of_week=0,
            start_time=time(9),end_time=time(10),minimum_employee_count=1,allowed_store_ids=(store,))
        result=regenerate_period(db,principal=actor,schedule_period_id=week.id)
        assert db.scalar(select(func.count()).select_from(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==week.id))==2
        zero=next(r for r in result['unmet_shift_targets'] if r['assigned_shifts']==0)
        warnings=rebuild_schedule_warnings(db,schedule_period_id=week.id)
        assert any(w.warning_type=='BELOW_TARGET_SHIFTS' and w.employee_id==zero['employee_id']
                   and w.actual_count==0 and w.severity==m.ScheduleWarningSeverity.INFO for w in warnings)
        from app.services.v2_scheduling_board_service import serialize_week_board
        board=serialize_week_board(db,week_start=week.week_start_date,selected_store_ids=(store,),
            all_authorized_store_ids=(store,),permission_flags={'scheduling.view_all':True})
        zero_name=db.get(m.Employee,zero['employee_id']).full_name
        assert any(w['type']=='BELOW_TARGET_SHIFTS' and zero_name in w['message']
                   and w['actual_count']==0 for w in board['warnings'])
        # Invalid allocations cannot silently drop employees and publish output.
        with pytest.raises(SchedulingValidationError,match='Unfilled assignments'):
            approve_schedule(db,principal=actor,schedule_period_id=week.id,expected_version=week.version,allowed_store_ids=(store,))
        for i,e in enumerate(employees):
            db.scalar(select(m.EmployeeSchedulingProfile).where(m.EmployeeSchedulingProfile.employee_id==e.id)).target_shifts_per_week=1 if i<2 else 0
        result=regenerate_period(db,principal=actor,schedule_period_id=week.id)
        assert result['allocation']['status']=='PASS' and result['uncovered']==[]
        approve_schedule(db,principal=actor,schedule_period_id=week.id,expected_version=week.version,allowed_store_ids=(store,))
        publish_schedule(db,principal=actor,schedule_period_id=week.id,expected_version=week.version,allowed_store_ids=(store,))
        assert week.status==m.SchedulePeriodStatus.PUBLISHED


def test_manual_excess_regeneration_rolls_back_without_losing_rows_or_evidence(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        week,store=setup_day(db,actor,ids)
        employees=[person(db,actor,'Locked '+str(i),lead=True) for i in range(2)]
        for employee in employees:manual(db,actor,week,employee,store)
        original=[(r.id,r.employee_id,r.manually_locked) for r in db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==week.id))]
        version=week.version;audit_count=db.scalar(select(func.count()).select_from(m.AuditLog))
        with pytest.raises(SchedulingValidationError,match='Configured coverage exceeded'):
            regenerate_period(db,principal=actor,schedule_period_id=week.id)
        assert original==[(r.id,r.employee_id,r.manually_locked) for r in db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==week.id))]
        assert week.version==version and db.scalar(select(func.count()).select_from(m.AuditLog))==audit_count
        assert db.scalar(select(func.count()).select_from(m.ScheduleGeneratorSnapshot))==0
        assert any(w.warning_type=='EXCESS_COVERAGE' for w in rebuild_schedule_warnings(db,schedule_period_id=week.id))
        from app.services.v2_scheduling_board_service import serialize_week_board
        board=serialize_week_board(db,week_start=week.week_start_date,selected_store_ids=(store,),
            all_authorized_store_ids=(store,),permission_flags={'scheduling.view_all':True})
        assert any(w['type']=='EXCESS_COVERAGE' for w in board['warnings'])
        assert board['summary']['coverage_warning_count']>=1


@pytest.mark.parametrize('override',[False,True])
def test_approval_and_publication_reject_persisted_excess_with_stale_warning_cache(scheduling_db,override):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        week,store=setup_day(db,actor,ids)
        for i in range(2):manual(db,actor,week,person(db,actor,'Publish '+str(i),lead=True),store)
        db.execute(delete(m.ScheduleWarning).where(m.ScheduleWarning.schedule_period_id==week.id))
        with pytest.raises(SchedulingValidationError,match='Configured coverage exceeded'):
            approve_schedule(db,principal=actor,schedule_period_id=week.id,expected_version=week.version,allowed_store_ids=(store,))
        assert db.scalar(select(func.count()).select_from(m.ScheduleApproval))==0
        db.add(m.ScheduleApproval(schedule_period_id=week.id,version=week.version,actor_principal_id=actor.id));db.flush()
        with pytest.raises(SchedulingValidationError,match='Configured coverage exceeded'):
            publish_schedule(db,principal=actor,schedule_period_id=week.id,expected_version=week.version,
                allowed_store_ids=(store,),allow_serious_warnings=override,confirmed=True,override_reason='Attempted waiver')
        assert week.status==m.SchedulePeriodStatus.DRAFT
        assert db.scalar(select(func.count()).select_from(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==week.id))==2


def test_missing_requirement_is_not_an_unlimited_manual_budget(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        week,store=setup_day(db,actor,ids)
        manual(db,actor,week,person(db,actor,'No demand',lead=True),store)
        for rule in db.scalars(select(m.CoverageRequirement)):rule.active=False
        db.flush()
        with pytest.raises(SchedulingValidationError,match='0 configured'):
            approve_schedule(db,principal=actor,schedule_period_id=week.id,expected_version=week.version,allowed_store_ids=(store,))


def test_historical_evidence_not_reinterpreted_by_current_capacity(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        week,store=setup_day(db,actor,ids)
        for i in range(2):manual(db,actor,week,person(db,actor,'History '+str(i),lead=True),store)
        week.is_historical=True;db.flush()
        assert coverage_excesses(db,period=week)==[]
        assert db.scalar(select(func.count()).select_from(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==week.id))==2


def test_disposable_supplemental_output_is_replaced_but_copied_excess_is_preserved(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        week,store=setup_day(db,actor,ids)
        for i in range(2):
            outcome=manual(db,actor,week,person(db,actor,'Disposable '+str(i),lead=True,target=1 if i==0 else 0),store)
            row=db.get(m.ScheduleShift,outcome.shift_id)
            row.manually_locked=False;row.generated_from_coverage_requirement=True
        db.flush()
        regenerate_period(db,principal=actor,schedule_period_id=week.id)
        rows=list(db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==week.id)))
        assert len(rows)==1
        # Source-linked copied proposals are not permission to erase excess.
        source=rows[0]
        copied=m.ScheduleShift(schedule_period_id=week.id,store_id=store,shift_date=source.shift_date,
            employee_id=None,start_time=source.start_time,end_time=source.end_time,unpaid_break_minutes=0,
            source_shift_id=source.id,generated_from_coverage_requirement=False,
            created_by_principal_id=actor.id,updated_by_principal_id=actor.id)
        db.add(copied);db.flush()
        source.generated_from_coverage_requirement=False;db.flush()
        before=[r.id for r in db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==week.id))]
        with pytest.raises(SchedulingValidationError,match='Configured coverage exceeded'):
            regenerate_period(db,principal=actor,schedule_period_id=week.id)
        assert before==[r.id for r in db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==week.id))]


def test_required_coverage_and_lead_remain_unfilled_when_hard_store_exclusion_blocks_everyone(scheduling_db):
    from app.services.v2_scheduling_rules_service import set_store_preference
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        week,store=setup_day(db,actor,ids)
        employee=person(db,actor,'Excluded lead',lead=True,target=3)
        set_store_preference(db,principal=actor,employee_id=employee.id,store_id=store,
            preference_rank=None,preference_level=m.StorePreferenceLevel.NEVER,allowed_store_ids=(store,))
        result=regenerate_period(db,principal=actor,schedule_period_id=week.id)
        assert len(result['uncovered'])==1 and result['lead_uncovered']
        assert result['unmet_shift_targets']==[{**result['shift_targets'][0],'shortfall':3}]
        assert db.scalar(select(m.ScheduleShift.employee_id).where(m.ScheduleShift.schedule_period_id==week.id)) is None


def test_edit_after_approval_is_revalidated_for_excess_before_publication(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        week,store=setup_day(db,actor,ids)
        manual(db,actor,week,person(db,actor,'Approved lead',lead=True),store)
        approve_schedule(db,principal=actor,schedule_period_id=week.id,expected_version=week.version,allowed_store_ids=(store,))
        manual(db,actor,week,person(db,actor,'Later excess',lead=True),store)
        with pytest.raises(SchedulingValidationError,match='Configured coverage exceeded'):
            publish_schedule(db,principal=actor,schedule_period_id=week.id,expected_version=week.version,allowed_store_ids=(store,))
        assert week.status==m.SchedulePeriodStatus.DRAFT


def test_concurrent_publication_and_excess_manual_edit_cannot_publish_two(scheduling_db):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from app.services.v2_scheduling_service import SchedulingConflict
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        week,store=setup_day(db,actor,ids)
        first=person(db,actor,'Concurrent first',lead=True)
        second=person(db,actor,'Concurrent second',lead=True,target=0)
        manual(db,actor,week,first,store)
        approve_schedule(db,principal=actor,schedule_period_id=week.id,expected_version=week.version,allowed_store_ids=(store,))
        week_id,employee_id,version=week.id,second.id,week.version
        db.commit()
    barrier=Barrier(2)
    def run(publish):
        with Session() as db:
            barrier.wait(timeout=10)
            try:
                if publish:
                    publish_schedule(db,principal=actor,schedule_period_id=week_id,expected_version=version,allowed_store_ids=(store,))
                else:
                    create_shift(db,principal=actor,schedule_period_id=week_id,expected_version=version,
                        values=ShiftInput(employee_id=employee_id,store_id=store,shift_date=date(2027,1,3),
                            start_time=time(8,45),end_time=time(10),unpaid_break_minutes=0),allowed_store_ids=(store,))
                db.commit();return 'committed'
            except (SchedulingValidationError,SchedulingConflict):
                db.rollback();return 'rejected'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(run,[True,False]))
    assert sorted(results)==['committed','rejected']
    with Session() as db:
        week=db.get(m.SchedulePeriod,week_id)
        count=db.scalar(select(func.count()).select_from(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==week_id))
        assert (week.status,count) in [(m.SchedulePeriodStatus.PUBLISHED,1),(m.SchedulePeriodStatus.DRAFT,2)]
