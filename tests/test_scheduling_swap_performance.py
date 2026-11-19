"""Synthetic four-store swap latency and assignment-integrity regression."""
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
import json
from time import perf_counter
from sqlalchemy import event, func, select
import pytest
from app import models as m
from app.auth import Principal, Role
from app.services import v2_scheduling_swap_service as swaps
from app.services import v2_scheduling_coverage_service as coverage
from app.services.scheduling_owner_review import OwnerScheduleReview, OwnerReviewRequired
from app.services.v2_scheduling_board_service import serialize_week_board
from test_v2_scheduling_foundation import scheduling_db, opening_hours
from test_v2_scheduling_repair import person, period
from test_v2_scheduling_prebeta import ready_period
from app.services.v2_scheduling_service import SchedulingValidationError, SchedulingConflict

WEEK = date(2027, 1, 3)
NOW = datetime(2026, 12, 30, 20, tzinfo=timezone.utc)


def representative(db, actor, ids, *, published=False):
    for employee in db.scalars(select(m.Employee)):
        employee.scheduling_active = False
    people = [person(db, actor, f'Swap worker {i}', lead=True, target=3) for i in range(114)]
    stores = [db.get(m.Store, ids['north']), db.get(m.Store, ids['south'])]
    for i in range(2):
        store = m.Store(name=f'Swap store {i}', square_location_id=f'SWAP-{i}', active=True)
        db.add(store); stores.append(store)
    db.flush()
    for store in stores:
        for weekday in range(7):
            opening_hours(db, actor, store.id, weekday, opening=time(9, 15), closing=time(10))
            db.add(m.CoverageRequirement(store_id=store.id, day_of_week=weekday,
                start_time=time(9, 15), end_time=time(10), minimum_employee_count=1,
                active=True, created_by_principal_id=actor.id, updated_by_principal_id=actor.id))
    future = []
    for week in range(12):
        per = period(db, actor, WEEK + timedelta(weeks=week), 'PUBLISHED' if published else 'DRAFT')
        if published: per.published_at = NOW
        future.append(per)
        for day in range(7):
            for index, store in enumerate(stores):
                db.add(m.ScheduleShift(schedule_period_id=per.id,
                    employee_id=people[day*4+index].id, store_id=store.id,
                    shift_date=per.week_start_date+timedelta(days=day),
                    start_time=time(9), end_time=time(10), unpaid_break_minutes=0,
                    is_lead_of_day=index==0, created_by_principal_id=actor.id,
                    updated_by_principal_id=actor.id))
    for week in range(38):
        per = period(db, actor, WEEK-timedelta(weeks=week+1), 'ARCHIVED')
        per.is_historical = True
        for index in range(26 if week < 26 else 25):
            db.add(m.ScheduleShift(schedule_period_id=per.id,
                employee_id=people[index].id, store_id=stores[index%4].id,
                shift_date=per.week_start_date+timedelta(days=index%7),
                start_time=time(9), end_time=time(10), unpaid_break_minutes=0,
                created_by_principal_id=actor.id, updated_by_principal_id=actor.id))
    db.commit()
    for model, count in [(m.Employee,117),(m.SchedulePeriod,50),(m.ScheduleShift,1312)]:
        assert db.scalar(select(func.count()).select_from(model)) == count
    rows = list(db.scalars(select(m.ScheduleShift).where(
        m.ScheduleShift.schedule_period_id == future[0].id).order_by(m.ScheduleShift.shift_date, m.ScheduleShift.store_id)))
    return future[0], rows, tuple(s.id for s in stores)


def assignment_snapshot(db):
    return {r.id: {c.name: getattr(r, c.name) for c in m.ScheduleShift.__table__.columns}
            for r in db.scalars(select(m.ScheduleShift))}


@pytest.mark.parametrize('owner', [True, False], ids=['owner', 'manager'])
@pytest.mark.parametrize('published', [False, True], ids=['draft', 'published'])
def test_four_store_swap_processing(scheduling_db, monkeypatch, owner, published):
    Session, actor, ids, engine = scheduling_db
    principal = Principal(actor.id, actor.username, Role.ADMIN if owner else Role.MANAGER, None, True)
    with Session() as db:
        per, rows, stores = representative(db, actor, ids, published=published)
        before = assignment_snapshot(db)
        original_evaluate = coverage.evaluate_schedule_warnings
        evaluations = []
        def evaluate(*args, **kwargs):
            evaluations.append(kwargs['schedule_period_id'])
            return original_evaluate(*args, **kwargs)
        monkeypatch.setattr(coverage, 'evaluate_schedule_warnings', evaluate)
        queries = []
        sql_ms = [0.0]
        def started(conn, cursor, statement, parameters, context, many):
            queries.append(statement.lstrip().split()[0].upper())
            context._swap_test_started = perf_counter()
        def finished(conn, cursor, statement, parameters, context, many):
            sql_ms[0] += (perf_counter()-context._swap_test_started)*1000
        event.listen(engine, 'before_cursor_execute', started)
        event.listen(engine, 'after_cursor_execute', finished)
        try:
            review = OwnerScheduleReview(principal, 'synthetic-swap', {}, '') if owner else None
            start = perf_counter()
            result = swaps.change_assignments(db, principal=principal, shift_id=rows[0].id,
                other_shift_id=rows[1].id, kind='STORE', reason='Synthetic measured swap',
                expected_versions={str(per.id): per.version}, allowed_store_ids=stores,
                now=NOW, owner_review=review)
            db.commit()
            service_ms = (perf_counter()-start)*1000
            statement_count, statement_sql_ms = len(queries), sql_ms[0]
            warning_count = len(evaluations)
            pid = result['period_ids'][0]
            current = db.get(m.SchedulePeriod, pid)
            start = perf_counter()
            board = serialize_week_board(db, week_start=WEEK, selected_store_ids=stores,
                all_authorized_store_ids=stores, permission_flags={}, principal=principal,
                schedule_period_id=pid, fresh_warning_revision=(pid, current.version))
            board_ms = (perf_counter()-start)*1000
        finally:
            event.remove(engine, 'before_cursor_execute', started)
            event.remove(engine, 'after_cursor_execute', finished)
        print(json.dumps(dict(operation='four_store_swap', owner=owner, published=published,
            employees=117, periods=50, shifts=1312, service_commit_ms=round(service_ms,3),
            sql_count=statement_count, sql_ms=round(statement_sql_ms,3),
            warning_evaluations=warning_count, board_ms=round(board_ms,3))), flush=True)
        after = assignment_snapshot(db)
        if published:
            assert {sid:after[sid] for sid in before} == before
            assert per.status == (m.SchedulePeriodStatus.PUBLISHED if owner else m.SchedulePeriodStatus.ARCHIVED)
        else:
            changed = {rows[0].id, rows[1].id}
            assert {sid:r for sid,r in after.items() if sid not in changed} == {sid:r for sid,r in before.items() if sid not in changed}
        current_rows = list(db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==pid)))
        by_source = {r.source_shift_id if published else r.id:r for r in current_rows}
        assert by_source[rows[0].id].store_id == before[rows[1].id]['store_id']
        assert by_source[rows[1].id].store_id == before[rows[0].id]['store_id']
        assert len(current_rows)==28 and board['period']['id']==pid
        assert warning_count == (2 if owner else 1)
        assert service_ms < 1000
        assert statement_count < 800
        assert current.status == (m.SchedulePeriodStatus.DRAFT if owner or not published else m.SchedulePeriodStatus.PUBLISHED)
        assert not [w for w in board['warnings'] if w['type'] in {'EMPLOYEE_OVERLAP','EXCESS_COVERAGE','INSUFFICIENT_COVERAGE'}]
        assert Counter((r.shift_date,r.store_id) for r in current_rows)==Counter((r.shift_date,r.store_id) for r in rows)


@pytest.mark.parametrize('owner', [True, False])
@pytest.mark.parametrize('blocker', ['stale', 'inactive', 'pto', 'overlap', 'scope'])
def test_swap_failure_preserves_both_assignments_and_versions(scheduling_db, owner, blocker):
    Session, actor, ids, _ = scheduling_db
    principal = Principal(actor.id, actor.username, Role.ADMIN if owner else Role.MANAGER, None, True)
    with Session() as db:
        per, rows = ready_period(db, actor, ids, day=WEEK, two=True)
        if blocker == 'inactive':
            db.get(m.Employee, rows[1].employee_id).active = False
        elif blocker == 'pto':
            db.add(m.TimeOffRequest(employee_id=rows[1].employee_id, start_date=WEEK,
                end_date=WEEK, full_day=True, status=m.TimeOffRequestStatus.APPROVED,
                reason_category_id=ids['vacation'], submitted_by_principal_id=actor.id,
                created_by_principal_id=actor.id, updated_by_principal_id=actor.id))
        elif blocker == 'overlap':
            db.add(m.ScheduleShift(schedule_period_id=per.id, employee_id=rows[1].employee_id,
                store_id=rows[0].store_id, shift_date=WEEK, start_time=time(9),
                end_time=time(10), created_by_principal_id=actor.id, updated_by_principal_id=actor.id))
        db.commit()
        before = assignment_snapshot(db); version = per.version
        count = db.scalar(select(func.count()).select_from(m.AuditLog))
        with pytest.raises((SchedulingValidationError, SchedulingConflict, PermissionError)):
            swaps.change_assignments(db, principal=principal, shift_id=rows[0].id,
                other_shift_id=rows[1].id, kind='STORE', reason='Failure must be atomic',
                expected_versions={str(per.id):version-1 if blocker=='stale' else version},
                allowed_store_ids=(ids['north'],) if blocker=='scope' else (ids['north'],ids['south']),
                now=NOW, owner_review=OwnerScheduleReview(principal,'test',{},'') if owner else None)
        db.commit(); db.expire_all()
        assert assignment_snapshot(db) == before and per.version == version
        assert db.scalar(select(func.count()).select_from(m.AuditLog)) == count
        assert 'scheduling_swap_periods' not in db.info
        assert 'atomic_publication_validation' not in db.info


def test_owner_review_and_persisted_warnings_use_same_final_state(scheduling_db):
    Session, actor, ids, _ = scheduling_db
    principal = Principal(actor.id, actor.username, Role.ADMIN, None, True)
    with Session() as db:
        per, rows = ready_period(db, actor, ids, day=WEEK)
        incoming = person(db, actor, 'Nonlead replacement', lead=False, target=0)
        db.commit()
        fields = dict(shift_id=rows[0].id, replacement_employee_id=incoming.id, kind='SHIFT',
            reason='Reviewed replacement', expected_versions={str(per.id):per.version})
        before = assignment_snapshot(db)
        with pytest.raises(OwnerReviewRequired) as needed:
            swaps.change_assignments(db, principal=principal, allowed_store_ids=(ids['north'],ids['south']),
                now=NOW, owner_review=OwnerScheduleReview(principal,'test',fields,''), **fields)
        db.commit(); assert assignment_snapshot(db)==before
        result = swaps.change_assignments(db, principal=principal,
            allowed_store_ids=(ids['north'],ids['south']), now=NOW,
            owner_review=OwnerScheduleReview(principal,'test',fields,needed.value.token), **fields)
        db.commit()
        assert rows[0].employee_id==incoming.id and result['period_id']==per.id
        assert result['shift_id']==rows[0].id
        assert result['period_versions']=={str(per.id):per.version}
        persisted = list(db.scalars(select(m.ScheduleWarning).where(m.ScheduleWarning.schedule_period_id==per.id)))
        fresh = coverage.evaluate_schedule_warnings(db,schedule_period_id=per.id)
        def signature(values):
            return sorted((w.warning_type,w.store_id,w.employee_id or 0,w.shift_id or 0,
                w.warning_date,w.message) for w in values)
        assert signature(persisted)==signature(fresh)
        assert any(w.warning_type=='NO_LEAD_OF_DAY' for w in persisted)


def published_pair(db, actor, ids):
    from app.services.v2_scheduling_service import approve_schedule, publish_schedule
    first, left = ready_period(db,actor,ids,day=WEEK)
    second, right = ready_period(db,actor,ids,day=WEEK+timedelta(weeks=1))
    right[0].employee_id=ids['blair']
    for per in (first,second):
        approve_schedule(db,principal=actor,schedule_period_id=per.id,
            expected_version=per.version,allowed_store_ids=(ids['north'],ids['south']))
        publish_schedule(db,principal=actor,schedule_period_id=per.id,
            expected_version=per.version,allowed_store_ids=(ids['north'],ids['south']))
    db.commit()
    return first,left,second,right


def test_joint_cross_week_swap_validates_both_before_publication(scheduling_db, monkeypatch):
    from app.services import scheduling_approval_validation as gate
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        first,left,second,right=published_pair(db,actor,ids)
        before=assignment_snapshot(db)
        original=gate._validate_complete_schedule; validated=[]
        def validate(*a,**kw):
            validated.append(kw['period'].id); return original(*a,**kw)
        monkeypatch.setattr(gate,'_validate_complete_schedule',validate)
        result=swaps.change_assignments(db,principal=actor,shift_id=right[0].id,
            other_shift_id=left[0].id,kind='SHIFT',reason='Joint final state',
            expected_versions={str(first.id):first.version,str(second.id):second.version},
            allowed_store_ids=(ids['north'],ids['south']),now=NOW)
        db.commit()
        assert len(validated)==2 and len(set(validated))==2
        after=assignment_snapshot(db)
        assert {sid:after[sid] for sid in before}==before
        assert first.status==second.status==m.SchedulePeriodStatus.ARCHIVED
        assert db.get(m.SchedulePeriod,result['period_id']).week_start_date==second.week_start_date
        assert db.get(m.ScheduleShift,result['shift_id']).employee_id==ids['alex']
        assert all(db.get(m.SchedulePeriod,pid).status==m.SchedulePeriodStatus.PUBLISHED for pid in result['period_ids'])
        assert 'atomic_publication_validation' not in db.info


def test_late_second_publication_failure_rolls_back_both_weeks(scheduling_db, monkeypatch):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        first,left,second,right=published_pair(db,actor,ids)
        before=assignment_snapshot(db);versions=(first.version,second.version)
        original=swaps.publish_schedule; calls=[]
        def fail_second(*a,**kw):
            result=original(*a,**kw);calls.append(result.id)
            if len(calls)==2: raise RuntimeError('Injected late publication failure')
            return result
        monkeypatch.setattr(swaps,'publish_schedule',fail_second)
        with pytest.raises(RuntimeError,match='late publication'):
            swaps.change_assignments(db,principal=actor,shift_id=left[0].id,
                other_shift_id=right[0].id,kind='SHIFT',reason='Rollback both weeks',
                expected_versions={str(first.id):first.version,str(second.id):second.version},
                allowed_store_ids=(ids['north'],ids['south']),now=NOW)
        db.commit();db.expire_all()
        assert assignment_snapshot(db)==before
        assert (first.version,second.version)==versions
        assert first.status==second.status==m.SchedulePeriodStatus.PUBLISHED
        assert 'scheduling_swap_periods' not in db.info
        assert 'atomic_publication_validation' not in db.info


def test_lead_swap_retains_stricter_assignment_completeness(scheduling_db):
    Session, actor, ids, _ = scheduling_db
    principal=Principal(actor.id,actor.username,Role.LEAD,None,True)
    with Session() as db:
        per,rows=ready_period(db,actor,ids,day=WEEK,two=True)
        # Lead publication supports partial weeks; swaps have always required
        # every existing nonclosed position to be assigned before approval.
        rows[1].employee_id=None;db.commit()
        before=assignment_snapshot(db);version=per.version
        with pytest.raises(SchedulingValidationError,match='Unfilled assignments'):
            swaps.change_assignments(db,principal=principal,shift_id=rows[0].id,
                replacement_employee_id=ids['blair'],kind='SHIFT',reason='Preserve complete swap gate',
                expected_versions={str(per.id):version},
                allowed_store_ids=(ids['north'],ids['south']),now=NOW)
        db.commit()
        assert assignment_snapshot(db)==before and per.version==version


def test_batched_training_and_allocation_facts_match_direct_reads_and_invalidate(scheduling_db):
    from app.services.read_scope import reuse_reads
    from app.services.scheduling_training_service import training_on, replacement_on
    from app.services.v2_scheduling_allocation_service import allocation_on
    Session, actor, ids, engine = scheduling_db
    with Session() as db:
        people = [person(db,actor,f'Frozen facts {i}',lead=True,target=i) for i in range(4)]
        enrollment = m.EmployeeTraining(employee_id=people[0].id,start_date=WEEK,
            completed_effective_date=WEEK+timedelta(days=2),created_by_principal_id=actor.id)
        people[3].replacing_employee_id=people[1].id
        people[3].first_schedule_date=WEEK+timedelta(days=3)
        db.add(enrollment);db.commit()
        def values():
            return [(training_on(db,p.id,WEEK+timedelta(days=d)).id if training_on(db,p.id,WEEK+timedelta(days=d)) else None,
                     replacement_on(db,p.id,WEEK+timedelta(days=d)).id if replacement_on(db,p.id,WEEK+timedelta(days=d)) else None,
                     allocation_on(db,p.id,WEEK+timedelta(days=d)))
                    for p in people for d in range(7)]
        direct=values()
        statements=[]
        def count(conn,cursor,statement,*a):statements.append(statement)
        event.listen(engine,'before_cursor_execute',count)
        try:
            with reuse_reads(db):
                assert values()==direct
                # One batch each for training, replacements, allocation history
                # and baseline profiles, plus Employee's existing eligibility
                # relationship load; independent of people/date count.
                assert len(statements)==5
                enrollment.completed_effective_date=None
                db.flush()
                assert training_on(db,people[0].id,WEEK+timedelta(days=6)).id==enrollment.id
                people[3].first_schedule_date=WEEK+timedelta(days=8)
                db.flush()
                assert replacement_on(db,people[1].id,WEEK+timedelta(days=6)) is None
                profile=db.scalar(select(m.EmployeeSchedulingProfile).where(
                    m.EmployeeSchedulingProfile.employee_id==people[2].id))
                profile.target_shifts_per_week=6;db.flush()
                assert allocation_on(db,people[2].id,WEEK)==6
        finally:
            event.remove(engine,'before_cursor_execute',count)
        assert '_operation_read_scope' not in db.info
