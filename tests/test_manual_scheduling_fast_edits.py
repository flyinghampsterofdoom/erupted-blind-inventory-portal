"""Manual editing avoids optimization; current rules and read freshness remain authoritative."""
from datetime import date, time
from sqlalchemy import event, select
from app.models import Employee, ScheduleShift, ScheduleWarning, EmployeeSchedulingWindow, SchedulingWindowKind
from app.services.v2_scheduling_service import create_draft_period, create_shift, update_shift
from app.services.v2_scheduling_board_service import serialize_week_board
from app.services.v2_scheduling_coverage_service import evaluate_schedule_warnings, rebuild_schedule_warnings
from test_v2_scheduling_foundation import scheduling_db, date_hours, _shift
from test_v2_primary import primary

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


def test_compact_move_snapshot_matches_full_canonical_state(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        period,shift=fixture(db,actor,ids)
        update_shift(db,principal=actor,schedule_period_id=period.id,shift_id=shift.id,
            expected_version=period.version,values=_shift(ids['blair'],ids['north']),
            allowed_store_ids=(ids['north'],),manual_edit=True)
        db.flush()
        args=dict(week_start=WEEK,selected_store_ids=(ids['north'],),all_authorized_store_ids=(ids['north'],),
            permission_flags={},fresh_warning_revision=(period.id,period.version))
        full=serialize_week_board(db,**args)
        compact=serialize_week_board(db,**args,mutation_shift_ids=(shift.id,))
        for key in ('period','summary','labor','warnings','actions','editable','mode','mode_label','requires_regeneration','optimization_notes'):
            assert compact[key]==full[key],key
        assert compact['shifts']==[s for s in full['shifts'] if s['id']==shift.id]
        for employee in compact['employees']:
            original=next(e for e in full['employees'] if e['id']==employee['id'])
            assert all(original[key]==value for key,value in employee.items())
        assert compact['groups']==[] and all('days' not in e for e in compact['employees'])


def test_http_move_delta_and_legacy_response_are_both_authoritative(primary):
    from test_v2_primary import login
    primary.account('ADMIN',linked=False);assert login(primary).status_code==303
    week=date(2027,6,6)
    with primary.Session() as db:
        date_hours(db,primary.manager,primary.ids['north'],week)
        period=create_draft_period(db,principal=primary.manager,week_start=week)
        outcome=create_shift(db,principal=primary.manager,schedule_period_id=period.id,
            expected_version=period.version,values=_shift(primary.ids['alex'],primary.ids['north'],day=week),
            allowed_store_ids=(primary.ids['north'],))
        db.commit();pid=period.id;sid=outcome.shift_id;version=period.version
    path=f'/v2/scheduling/api/periods/{pid}/shifts/{sid}?start={week}'
    payload=dict(employee_id=primary.ids['blair'],store_id=primary.ids['north'],shift_date=str(week),
        start_time='09:00',end_time='17:00',unpaid_break_minutes=30,expected_version=version)
    assert primary.client.patch(path,json=payload).status_code==403
    headers={'X-CSRF-Token':primary.client.cookies.get('csrf_token'),'X-Scheduling-Response':'delta'}
    response=primary.client.patch(path,json=payload,headers=headers)
    assert response.status_code==200,response.text
    result=response.json();assert 'board' not in result and result['save_outcome']=='local_saved'
    delta=result['delta'];assert delta['base_version']==version and delta['period']['version']==version+1
    assert len(delta['shifts'])==1 and delta['shifts'][0]['employee_id']==primary.ids['blair']
    full=primary.client.get(f'/v2/scheduling/api/board?start={week}').json()
    assert delta['summary']==full['summary'] and delta['labor']==full['labor']
    for field in ('employee_id','store_id','shift_date','start_time','end_time','is_lead_of_day','manually_locked'):
        assert delta['shifts'][0][field]==full['shifts'][0][field]
    payload.update(employee_id=primary.ids['alex'],expected_version=version+1)
    del headers['X-Scheduling-Response']
    response=primary.client.patch(path,json=payload,headers=headers)
    assert response.status_code==200 and response.json()['board']['shifts'][0]['employee_id']==primary.ids['alex']


def test_frozen_assignment_history_is_resolved_once_across_date_ranges(scheduling_db,monkeypatch):
    from datetime import timedelta
    from app.services import v2_scheduling_policy_service as policy
    from app.services.read_scope import reuse_reads
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        period,shift=fixture(db,actor,ids)
        ranges=[(None,None),(WEEK,WEEK),(WEEK+timedelta(days=1),WEEK+timedelta(days=6)),(WEEK-timedelta(days=7),WEEK-timedelta(days=1))]
        def signature(first,last):
            return [(s.id,p.id) for s,p in policy._load_effective_assignment_rows_with_period(
                db,employee_id=ids['alex'],start_date=first,end_date=last)]
        reference=[signature(*r) for r in ranges]
        loads=[];original=policy._load_effective_assignment_facts
        def count(*a,**kw):loads.append(kw);return original(*a,**kw)
        monkeypatch.setattr(policy,'_load_effective_assignment_facts',count)
        with reuse_reads(db):
            assert [signature(*r) for r in ranges]==reference
            assert signature(WEEK,WEEK)==reference[1]
        assert loads==[{'start_date':None,'end_date':None}]


def test_empty_exception_evidence_does_not_load_archived_shift_ancestry(scheduling_db):
    from datetime import timedelta
    from app import models as m
    from app.services.v2_scheduling_policy_service import _load_effective_assignment_rows_with_period
    Session,actor,ids,engine=scheduling_db
    with Session() as db:
        period,shift=fixture(db,actor,ids)
        parent=m.SchedulePeriod(week_start_date=WEEK-timedelta(days=7),week_end_date=WEEK-timedelta(days=1),
            status=m.SchedulePeriodStatus.ARCHIVED,revision_number=1,created_by_principal_id=actor.id,updated_by_principal_id=actor.id)
        db.add(parent);db.flush()
        ancestor=m.ScheduleShift(schedule_period_id=parent.id,employee_id=ids['alex'],store_id=ids['north'],
            shift_date=parent.week_start_date,start_time=time(9),end_time=time(17),created_by_principal_id=actor.id,updated_by_principal_id=actor.id)
        db.add(ancestor);db.flush();shift.source_shift_id=ancestor.id;period.status=m.SchedulePeriodStatus.PUBLISHED;db.commit()
        db.expunge(ancestor)
        statements=[]
        def record(conn,cursor,statement,*args):statements.append(statement)
        event.listen(engine,'before_cursor_execute',record)
        try:
            rows=_load_effective_assignment_rows_with_period(db,employee_id=ids['alex'])
            assert [s.id for s,p in rows]==[shift.id]
            assert not any('WHERE schedule_shifts.id =' in sql for sql in statements)
        finally:event.remove(engine,'before_cursor_execute',record)


def test_labor_summary_batches_effective_rates_without_changing_scope_or_missing_values(scheduling_db):
    from decimal import Decimal
    from app.models import EmployeeCompensationRate
    from app.services.v2_scheduling_rules_service import estimate_labor_cost
    Session, actor, ids, engine = scheduling_db
    with Session() as db:
        period = create_draft_period(db, principal=actor, week_start=WEEK)
        for employee, store, day, end, unpaid in [
            (ids['alex'], ids['north'], date(2026, 8, 3), time(11), 30),
            (ids['alex'], ids['north'], date(2026, 8, 4), time(11), 0),
            (ids['blair'], ids['north'], date(2026, 8, 4), time(10), 0),
            (ids['alex'], ids['south'], date(2026, 8, 5), time(11), 0),
            (None, ids['north'], date(2026, 8, 5), time(11), 0),
        ]:
            db.add(ScheduleShift(schedule_period_id=period.id, employee_id=employee,
                store_id=store, shift_date=day, start_time=time(9), end_time=end,
                unpaid_break_minutes=unpaid, created_by_principal_id=actor.id,
                updated_by_principal_id=actor.id))
        for start, end, rate, active in [
            (date(2026, 8, 1), date(2026, 8, 3), '20.00', True),
            (date(2026, 8, 4), None, '25.00', True),
            (date(2026, 8, 2), None, '99.00', False),
        ]:
            db.add(EmployeeCompensationRate(employee_id=ids['alex'],
                effective_start_date=start, effective_end_date=end,
                hourly_rate=Decimal(rate), active=active,
                created_by_principal_id=actor.id, updated_by_principal_id=actor.id))
        db.commit()
        statements = []
        def record(conn, cursor, statement, *args):
            if 'FROM employee_compensation_rates' in statement: statements.append(statement)
        event.listen(engine, 'before_cursor_execute', record)
        try:
            estimate = estimate_labor_cost(db, schedule_period_id=period.id,
                permitted=True, allowed_store_ids=(ids['north'],))
        finally: event.remove(engine, 'before_cursor_execute', record)
        assert estimate.estimated_cost == Decimal('80.00')
        assert estimate.costed_paid_hours == Decimal('3.50')
        assert estimate.missing_rate_paid_hours == Decimal('1.00')
        assert estimate.missing_rate_shift_count == 1
        assert len(statements) == 1
