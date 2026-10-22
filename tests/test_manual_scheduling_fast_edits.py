"""Manual editing avoids optimization; current rules and read freshness remain authoritative."""
from datetime import date, time
from sqlalchemy import event, select
from app.models import Employee, ScheduleShift, ScheduleWarning, EmployeeSchedulingWindow, SchedulingWindowKind
from app.services.v2_scheduling_service import create_draft_period, create_shift, update_shift
from app.services.v2_scheduling_board_service import serialize_week_board
from app.services.v2_scheduling_coverage_service import evaluate_schedule_warnings, rebuild_schedule_warnings
from test_v2_scheduling_foundation import scheduling_db, date_hours, _shift

WEEK = date(2026, 8, 2)


def fixture(db, actor, ids):
    date_hours(db, actor, ids['north'], WEEK)
    period = create_draft_period(db, principal=actor, week_start=WEEK)
    result = create_shift(db, principal=actor, schedule_period_id=period.id,
        expected_version=period.version, values=_shift(ids['alex'], ids['north']),
        allowed_store_ids=(ids['north'],))
    db.commit()
    return period, db.get(ScheduleShift, result.shift_id)


def signature(rows):
    return sorted((r.warning_type, r.store_id, r.employee_id or 0, r.shift_id or 0,
                   r.warning_date, r.message, r.actual_count or 0) for r in rows)


def test_board_reads_fresh_rules_without_warning_maintenance(scheduling_db):
    Session, actor, ids, engine = scheduling_db
    with Session() as db:
        period, shift = fixture(db, actor, ids)
        persisted = signature(db.scalars(select(ScheduleWarning)))
        db.add(EmployeeSchedulingWindow(employee_id=ids['alex'], day_of_week=0,
            kind=SchedulingWindowKind.HARD_UNAVAILABLE, start_time=time(0), end_time=time(23,59),
            active=True, created_by_principal_id=actor.id, updated_by_principal_id=actor.id))
        db.commit()
        statements = []
        def record(conn, cursor, statement, *args): statements.append(statement.lstrip().split()[0].upper())
        event.listen(engine, 'before_cursor_execute', record)
        try:
            board = serialize_week_board(db, week_start=WEEK, selected_store_ids=(ids['north'],),
                all_authorized_store_ids=(ids['north'],), permission_flags={})
            assert any(w['type']=='HARD_UNAVAILABLE' for w in board['warnings'])
            assert not {'INSERT','UPDATE','DELETE'}.intersection(statements)
            assert not db.new and not db.dirty and not db.deleted
        finally: event.remove(engine, 'before_cursor_execute', record)
        assert signature(db.scalars(select(ScheduleWarning))) == persisted


def test_manual_move_does_not_reselect_leads_and_reuses_only_exact_revision(scheduling_db, monkeypatch):
    from app.services import v2_scheduling_assignments_service as assignments
    from app.services import v2_scheduling_coverage_service as coverage
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        period, shift = fixture(db, actor, ids)
        def forbidden(*a, **k): raise AssertionError('Manual move must not run company Lead optimization')
        monkeypatch.setattr(assignments, 'reconcile_lead_designations', forbidden)
        db.get(Employee, ids['blair']).scheduling_lead_capable = False
        db.commit()
        update_shift(db, principal=actor, schedule_period_id=period.id, shift_id=shift.id,
            expected_version=period.version, values=_shift(ids['blair'], ids['north']),
            allowed_store_ids=(ids['north'],), manual_edit=True)
        assert shift.employee_id == ids['blair'] and shift.manually_locked
        assert not shift.is_lead_of_day  # no silent replacement of responsibility
        original = coverage.evaluate_schedule_warnings
        calls=[]
        def count(*a,**k): calls.append(k['schedule_period_id']); return original(*a,**k)
        monkeypatch.setattr(coverage, 'evaluate_schedule_warnings', count)
        kw=dict(week_start=WEEK, selected_store_ids=(ids['north'],),
                all_authorized_store_ids=(ids['north'],), permission_flags={})
        reused=serialize_week_board(db, **kw, fresh_warning_revision=(period.id,period.version))
        assert not calls
        full=serialize_week_board(db, **kw)
        assert calls == [period.id]
        def normalized(b): return [{k:v for k,v in w.items() if k!='id'} for w in b['warnings']]
        assert normalized(reused)==normalized(full)
        serialize_week_board(db, **kw, fresh_warning_revision=(period.id,period.version-1))
        assert calls == [period.id, period.id]


def test_frozen_warning_facts_match_uncached_validator(scheduling_db):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        period, shift=fixture(db,actor,ids)
        cached=signature(evaluate_schedule_warnings(db,schedule_period_id=period.id))
        uncached=signature(evaluate_schedule_warnings.__wrapped__(db,schedule_period_id=period.id))
        assert cached==uncached
        assert signature(rebuild_schedule_warnings(db,schedule_period_id=period.id))==cached


def test_manual_move_retains_qualified_designated_position(scheduling_db, monkeypatch):
    from app.services import v2_scheduling_assignments_service as assignments
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        period, shift = fixture(db, actor, ids)
        assert shift.is_lead_of_day
        monkeypatch.setattr(assignments,'lead_fairness',lambda *a,**k: (_ for _ in ()).throw(AssertionError('No fairness search')))
        update_shift(db,principal=actor,schedule_period_id=period.id,shift_id=shift.id,
            expected_version=period.version,values=_shift(ids['blair'],ids['north']),
            allowed_store_ids=(ids['north'],),manual_edit=True)
        assert shift.employee_id==ids['blair'] and shift.is_lead_of_day
