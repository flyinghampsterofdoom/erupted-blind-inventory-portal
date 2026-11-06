"""Local acceptance: manual training and a single profile save into draft scheduling."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
from threading import Event
from decimal import Decimal
import pytest
from sqlalchemy import event,select,func,inspect,text
from app import models as m
from app.auth import Principal,Role
from app.services import scheduling_training_service as t
from app.services.v2_scheduling_service import (SchedulingValidationError,ShiftInput,create_shift,update_shift,delete_shift,scheduled_paid_minutes)
from app.services.v2_scheduling_policy_service import evaluate_assignment,scheduled_weekly_hours
from app.services.v2_scheduling_coverage_service import rebuild_schedule_warnings,coverage_excesses
from test_v2_scheduling_foundation import scheduling_db,opening_hours
from test_v2_scheduling_repair import person,period,shift
from test_v2_employee_profiles import employee_client

NOW=datetime(2026,11,3,20,tzinfo=timezone.utc)
START=date(2026,11,8)


def setup(db,actor,ids,*,training=True):
    for e in db.scalars(select(m.Employee)):e.scheduling_active=False
    outgoing=person(db,actor,'Outgoing employee',lead=True,target=3)
    b=person(db,actor,'Scheduled Lead B',lead=True,target=2)
    c=person(db,actor,'Scheduled Lead C',lead=True,target=2)
    incoming=person(db,actor,'New employee',lead=True,target=3)
    for weekday in range(7):
        opening_hours(db,actor,ids['north'],weekday,opening=time(9,15),closing=time(10))
        db.add(m.CoverageRequirement(store_id=ids['north'],day_of_week=weekday,start_time=time(9,15),end_time=time(10),
            minimum_employee_count=1,active=True,created_by_principal_id=actor.id,updated_by_principal_id=actor.id))
    periods=[]
    for week in range(2):
        per=period(db,actor,START+timedelta(weeks=week));periods.append(per)
        for day in range(7):
            e=outgoing if day<3 else b if day<5 else c
            row=shift(db,actor,per,e,per.week_start_date+timedelta(days=day),ids['north']);row.is_lead_of_day=True
    enrollment=t.start_training(db,principal=actor,employee_id=incoming.id,start_date=START,now=NOW) if training else None
    db.flush();return outgoing,incoming,enrollment,periods


def rows(db,per):
    return list(db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==per.id).order_by(m.ScheduleShift.id)))


def shape(items):
    return {r.id:(r.employee_id,r.store_id,r.shift_date,r.start_time,r.end_time,r.unpaid_break_minutes,
                  r.is_training,r.is_lead_of_day,r.manually_locked) for r in items}


def manual(db,actor,ids,incoming,per,day=None,**kwargs):
    result=create_shift(db,principal=actor,schedule_period_id=per.id,expected_version=per.version,
        values=ShiftInput(employee_id=incoming.id,store_id=ids['north'],shift_date=day or per.week_start_date,
            start_time=time(9),end_time=time(10),unpaid_break_minutes=0,**kwargs),allowed_store_ids=(ids['north'],),manual_edit=True)
    return db.get(m.ScheduleShift,result.shift_id)


def train(db,actor,ids,incoming,periods):
    return [manual(db,actor,ids,incoming,per,per.week_start_date+timedelta(days=day)) for per in periods for day in range(3)]


def transition(db,actor,ids,incoming,outgoing=None,day=START+timedelta(days=3)):
    return t.save_transition(db,principal=actor,employee_id=incoming.id,first_schedule_date=day,
        replacing_employee_id=outgoing.id if outgoing else None,allowed_store_ids=(ids['north'],),now=NOW)


def test_training_click_creates_no_shifts_and_immediate_calendar_row(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        _,incoming,enrollment,periods=setup(db,actor,ids)
        assert enrollment.duration_days==14 and not any(r.employee_id==incoming.id for per in periods for r in rows(db,per))
        from app.services.v2_scheduling_board_service import serialize_week_board
        board=serialize_week_board(db,week_start=START,selected_store_ids=(ids['north'],ids['south']),all_authorized_store_ids=(ids['north'],ids['south']),permission_flags={})
        card=next(r for r in board['employees'] if r['id']==incoming.id)
        assert card['scheduled_shift_count']==0
        # A brand-new employee without a home store or scheduling profile is visible too.
        fresh=m.Employee(full_name='Fresh trainee',normalized_name='fresh trainee',active=True,scheduling_active=True)
        db.add(fresh);db.flush();t.start_training(db,principal=actor,employee_id=fresh.id,now=NOW)
        board=serialize_week_board(db,week_start=START,selected_store_ids=(ids['north'],ids['south']),all_authorized_store_ids=(ids['north'],ids['south']),permission_flags={})
        assert any(r['id']==fresh.id for r in board['employees'])
        assert t.start_training(db,principal=actor,employee_id=incoming.id,start_date=START,now=NOW).id==enrollment.id


def test_manual_calendar_shifts_paid_supplemental_no_trainer_and_no_expiry(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        _,incoming,enrollment,periods=setup(db,actor,ids);before=shape([r for per in periods for r in rows(db,per)])
        training=train(db,actor,ids,incoming,periods)
        assert len(training)==6 and sum(scheduled_paid_minutes(r) for r in training)==360
        assert all(r.is_training and r.training_id==enrollment.id and r.trainer_shift_id is None and r.trainer_employee_id is None for r in training)
        assert all(not r.is_lead_of_day and not r.is_shift_lead and not r.is_double_coverage for r in training)
        assert scheduled_weekly_hours(db,employee_id=incoming.id,shift_date=START)==Decimal('3.00')
        assert before==shape([r for per in periods for r in rows(db,per) if not r.is_training])
        assert t.training_on(db,incoming.id,START+timedelta(days=30))
        t.adjust_duration(db,principal=actor,training_id=enrollment.id,duration_days=2,now=NOW)
        assert t.training_on(db,incoming.id,START+timedelta(days=30)) and len(training)==6
        for per in periods:
            assert coverage_excesses(db,period=per)==[]
            assert not [w for w in rebuild_schedule_warnings(db,schedule_period_id=per.id) if w.warning_type in {'INVALID_TRAINING','EXCESS_COVERAGE','NO_LEAD_OF_DAY','BELOW_TARGET_SHIFTS'} and w.employee_id==incoming.id]


def test_normal_calendar_move_edit_duplicate_delete_training(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        _,incoming,_,periods=setup(db,actor,ids);per=periods[0];r=manual(db,actor,ids,incoming,per)
        update_shift(db,principal=actor,schedule_period_id=per.id,shift_id=r.id,expected_version=per.version,
            values=ShiftInput(employee_id=incoming.id,store_id=ids['north'],shift_date=START+timedelta(days=1),
                start_time=time(9,15),end_time=time(9,45),unpaid_break_minutes=0),allowed_store_ids=(ids['north'],),manual_edit=True)
        assert r.is_training and scheduled_paid_minutes(r)==30 and r.trainer_shift_id is None
        duplicated=manual(db,actor,ids,incoming,per,START+timedelta(days=2))
        assert duplicated.is_training
        delete_shift(db,principal=actor,schedule_period_id=per.id,shift_id=r.id,expected_version=per.version,
            allowed_store_ids=(ids['north'],),manual_edit=True)
        assert db.get(m.ScheduleShift,r.id) is None


def test_trainee_never_satisfies_minimum_or_lead_and_fourth_day_rejected(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        outgoing,incoming,_,periods=setup(db,actor,ids);training=train(db,actor,ids,incoming,periods)
        with pytest.raises(SchedulingValidationError):manual(db,actor,ids,incoming,periods[0],START+timedelta(days=3))
        from app.services.v2_scheduling_assignments_service import set_lead_of_day
        with pytest.raises(SchedulingValidationError,match='Training'):set_lead_of_day(db,principal=actor,shift_id=training[0].id)
        original=next(r for r in rows(db,periods[0]) if r.employee_id==outgoing.id)
        original.employee_id=None;original.is_lead_of_day=False;db.flush()
        warnings=rebuild_schedule_warnings(db,schedule_period_id=periods[0].id)
        assert any(w.warning_type in {'NO_LEAD_OF_DAY','LEAD_COVERAGE_MISSING'} for w in warnings)
        assert any(w.warning_type in {'NO_ASSIGNED_EMPLOYEE','INSUFFICIENT_COVERAGE'} for w in warnings)


def test_one_save_replacement_preserves_shape_unaffected_history_and_idempotence(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        outgoing,incoming,enrollment,periods=setup(db,actor,ids);training=train(db,actor,ids,incoming,periods)
        before=shape([r for per in periods for r in rows(db,per)])
        result=transition(db,actor,ids,incoming,outgoing)
        assert len(result['changes'])==3 and len(result['training_removals'])==3
        assert incoming.first_schedule_date==START+timedelta(days=3) and incoming.replacing_employee_id==outgoing.id
        assert enrollment.completed_effective_date==incoming.first_schedule_date
        changed={c['shift_id'] for c in result['changes']};removed={c['shift_id'] for c in result['training_removals']}
        after=shape([r for per in periods for r in rows(db,per)])
        assert {k:v for k,v in before.items() if k not in changed|removed}=={k:v for k,v in after.items() if k not in changed}
        for sid in changed:
            assert after[sid][0]==incoming.id and after[sid][1:6]==before[sid][1:6] and not after[sid][6]
        assert outgoing.active and outgoing.scheduling_active and outgoing.last_effective_date is None
        assert transition(db,actor,ids,incoming,outgoing)['unchanged']
        assert after==shape([r for per in periods for r in rows(db,per)])
        assert db.scalar(select(m.AuditLog.id).where(m.AuditLog.action=='V2:SCHEDULING:EMPLOYEE_SCHEDULE_TRANSITION_SAVED'))


def test_published_week_becomes_unpublished_draft_and_history_is_immutable(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        outgoing,incoming,_,periods=setup(db,actor,ids);train(db,actor,ids,incoming,periods)
        source=periods[1];source.status=m.SchedulePeriodStatus.PUBLISHED;source.published_at=NOW
        historical=period(db,actor,date(2026,10,25),'ARCHIVED');historical.is_historical=True
        old=shift(db,actor,historical,outgoing,date(2026,10,26),ids['north']);db.flush()
        before=shape(rows(db,source));past=shape([old])
        result=transition(db,actor,ids,incoming,outgoing)
        assert shape(rows(db,source))==before and source.status==m.SchedulePeriodStatus.PUBLISHED and shape([old])==past
        draft=db.get(m.SchedulePeriod,result['period_ids'][0])
        assert draft.status==m.SchedulePeriodStatus.DRAFT and draft.supersedes_schedule_period_id==source.id and draft.published_at is None
        assert len(rows(db,draft))==7 and sum(r.employee_id==incoming.id for r in rows(db,draft))==3


@pytest.mark.parametrize('blocker',['availability','overlap','qualification','protected','hours','inactive'])
def test_conflict_rolls_back_profile_and_all_schedule_changes(scheduling_db,blocker):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        outgoing,incoming,enrollment,periods=setup(db,actor,ids);train(db,actor,ids,incoming,periods)
        if blocker=='availability':
            db.add(m.EmployeeSchedulingWindow(employee_id=incoming.id,day_of_week=0,kind=m.SchedulingWindowKind.HARD_UNAVAILABLE,
                start_time=time.min,end_time=time.max,active=True,created_by_principal_id=actor.id,updated_by_principal_id=actor.id))
        elif blocker=='overlap':shift(db,actor,periods[1],incoming,START+timedelta(days=7),ids['north'])
        elif blocker=='qualification':incoming.scheduling_lead_capable=False
        elif blocker=='protected':next(r for r in rows(db,periods[1]) if r.employee_id==outgoing.id).manually_locked=True
        elif blocker=='hours':db.scalar(select(m.EmployeeSchedulingProfile).where(m.EmployeeSchedulingProfile.employee_id==incoming.id)).maximum_weekly_hours=Decimal('2')
        else:incoming.scheduling_active=False
        db.flush();before=shape([r for per in periods for r in rows(db,per)]);versions=[p.version for p in periods]
        count=db.scalar(select(func.count(m.AuditLog.id)))
        with pytest.raises(SchedulingValidationError):transition(db,actor,ids,incoming,outgoing)
        db.commit()
        assert incoming.first_schedule_date is None and incoming.replacing_employee_id is None and enrollment.completed_effective_date is None
        assert shape([r for per in periods for r in rows(db,per)])==before and [p.version for p in periods]==versions
        assert db.scalar(select(func.count(m.AuditLog.id)))==count


def test_failed_final_write_rolls_back_multiweek_clones_and_audit(scheduling_db,monkeypatch):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        outgoing,incoming,enrollment,periods=setup(db,actor,ids);train(db,actor,ids,incoming,periods)
        for per in periods:per.status=m.SchedulePeriodStatus.PUBLISHED;per.published_at=NOW
        db.flush();before=shape([r for per in periods for r in rows(db,per)])
        count=db.scalar(select(func.count(m.AuditLog.id)));period_count=db.scalar(select(func.count(m.SchedulePeriod.id)))
        audit=t._audit
        def fail(db,**kwargs):
            if kwargs['action']=='EMPLOYEE_SCHEDULE_TRANSITION_SAVED':raise RuntimeError('Injected final write failure')
            return audit(db,**kwargs)
        monkeypatch.setattr(t,'_audit',fail)
        with pytest.raises(RuntimeError):transition(db,actor,ids,incoming,outgoing,day=START)
        db.commit()
        assert shape([r for per in periods for r in rows(db,per)])==before
        assert db.scalar(select(func.count(m.AuditLog.id)))==count and db.scalar(select(func.count(m.SchedulePeriod.id)))==period_count
        assert incoming.first_schedule_date is None and enrollment.completed_effective_date is None


def test_no_replacement_explicit_date_ends_training_and_converts_future_manual_work(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        _,incoming,enrollment,periods=setup(db,actor,ids);training=train(db,actor,ids,incoming,periods)
        result=transition(db,actor,ids,incoming)
        assert len(result['changes'])==3 and not result['training_removals']
        assert not t.training_on(db,incoming.id,START+timedelta(days=7))
        assert t.training_on(db,incoming.id,START)
        assert all(r.is_training for r in training[:3]) and all(not r.is_training for r in training[3:])
        regular=manual(db,actor,ids,incoming,periods[0],START+timedelta(days=4))
        assert not regular.is_training and incoming.replacing_employee_id is None


def test_first_schedule_without_training_or_replacement_gates_regular_work(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        _,incoming,_,periods=setup(db,actor,ids,training=False)
        transition(db,actor,ids,incoming,day=START+timedelta(days=2))
        verdict=evaluate_assignment(db,employee_id=incoming.id,store_id=ids['north'],shift_date=START,start_time=time(9),end_time=time(10),schedule_period_id=periods[0].id)
        assert 'BEFORE_FIRST_SCHEDULE' in {r.code for r in verdict.reasons}
        assert not manual(db,actor,ids,incoming,periods[0],START+timedelta(days=2)).is_training


def test_replacement_can_be_saved_without_training_wizard_or_enrollment(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        outgoing,incoming,_,_=setup(db,actor,ids,training=False)
        result=transition(db,actor,ids,incoming,outgoing)
        assert len(result['changes'])==3


def test_role_scope_past_date_and_self_replacement_are_enforced(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        outgoing,incoming,_,_=setup(db,actor,ids)
        with pytest.raises(PermissionError):t.save_transition(db,principal=actor,employee_id=incoming.id,first_schedule_date=START,
            replacing_employee_id=outgoing.id,allowed_store_ids=(),now=NOW)
        with pytest.raises(PermissionError):t.start_training(db,principal=Principal(actor.id,actor.username,Role.STORE,None,True),employee_id=incoming.id,now=NOW)
        with pytest.raises(SchedulingValidationError):transition(db,actor,ids,incoming,incoming)
        with pytest.raises(SchedulingValidationError):transition(db,actor,ids,incoming,outgoing,day=date(2026,10,1))
        with pytest.raises(SchedulingValidationError):transition(db,actor,ids,incoming,outgoing,day=None)
        assert incoming.first_schedule_date is None


def test_manual_training_and_replacement_survive_optimizer(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        outgoing,incoming,_,periods=setup(db,actor,ids);training=train(db,actor,ids,incoming,periods)
        before=shape(training)
        from app.services.v2_scheduling_horizon_service import plan_schedule_horizon,apply_schedule_horizon_plan
        plan=plan_schedule_horizon(db,periods=periods,planning_date=START,time_limit=5)
        assert plan['assignments'] is not None
        apply_schedule_horizon_plan(db,periods=periods,plan=plan,principal=actor)
        assert shape(training)==before
        transition(db,actor,ids,incoming,outgoing)
        transferred=[r for r in rows(db,periods[1]) if r.employee_id==incoming.id]
        before=shape(transferred)
        plan=plan_schedule_horizon(db,periods=[periods[1]],planning_date=START,time_limit=5)
        assert plan['assignments'] is not None
        apply_schedule_horizon_plan(db,periods=[periods[1]],plan=plan,principal=actor)
        assert shape(transferred)==before


def test_profile_single_save_flow_and_obsolete_wizard_removed(employee_client):
    client,current,Session,ids=employee_client
    actor=current['principal']
    with Session() as db:
        outgoing,incoming,_,_=setup(db,actor,ids,training=False);db.commit();eid=incoming.id;oid=outgoing.id
    url=f'/v2/scheduling/employees/{eid}/training';profile=f'/v2/hr/employees/{eid}'
    assert client.post(url,data={'action':'start','csrf_token':'invalid'}).status_code==403
    csrf=client.cookies.get('csrf_token')
    assert client.post(url,data={'action':'start','csrf_token':csrf}).status_code==303
    page=client.get(profile)
    assert page.status_code==200 and 'First Schedule Date' in page.text and 'Replacing Employee' in page.text
    assert '/v2/scheduling/week?start=2026-11-08' in page.text
    assert 'Preview Replacement' not in page.text and 'End Training' not in page.text and 'Choose a trainer' not in page.text
    response=client.post(url,data={'action':'transition','first_schedule_date':'2026-11-11','replacing_employee_id':oid,'csrf_token':csrf})
    assert response.status_code==303 and 'error=' not in response.headers['location']
    with Session() as db:assert db.get(m.Employee,eid).first_schedule_date==date(2026,11,11)
    assert client.post(url+'/replacement/preview',data={'csrf_token':csrf}).status_code==404
    assert client.post(url+'/replacement/confirm',data={'csrf_token':csrf}).status_code==404


def test_activate_for_training_from_inactive_profile(employee_client, monkeypatch):
    client, current, Session, ids = employee_client
    actor = current['principal']
    monkeypatch.setattr(t, 'local_now', lambda db, now=None: NOW)
    with Session() as db:
        _, incoming, _, periods = setup(db, actor, ids, training=False)
        incoming.scheduling_active = False
        employee_id, period_id = incoming.id, periods[0].id
        db.commit()
    profile = f'/v2/hr/employees/{employee_id}'
    assert 'Activate for Scheduling (Training)' in client.get(profile).text
    response = client.post(f'/v2/scheduling/employees/{employee_id}/training',
        data={'action':'start','csrf_token':client.cookies.get('csrf_token')})
    assert response.status_code == 303 and 'error=' not in response.headers['location']
    assert 'Automatic scheduling skips this employee during training' in client.get(profile).text
    with Session() as db:
        incoming = db.get(m.Employee, employee_id)
        period = db.get(m.SchedulePeriod, period_id)
        assert not any(row.employee_id == employee_id for row in rows(db, period))
        assert not evaluate_assignment(db, employee_id=employee_id, store_id=ids['north'],
            shift_date=NOW.date()-timedelta(days=1), start_time=time(9), end_time=time(10)).eligible
        automated = evaluate_assignment(db, employee_id=employee_id, store_id=ids['north'],
            shift_date=START, start_time=time(9), end_time=time(10))
        assert {reason.code for reason in automated.reasons} == {'TRAINING_EMPLOYEE'}
        assert manual(db, actor, ids, incoming, period).is_training
        transition(db, actor, ids, incoming, day=START+timedelta(days=3))
        assert evaluate_assignment(db, employee_id=employee_id, store_id=ids['north'],
            shift_date=START+timedelta(days=3), start_time=time(9), end_time=time(10)).eligible


def test_training_profile_returns_promptly_when_scheduler_lock_is_held(employee_client):
    from urllib.parse import unquote
    from app.services.v2_scheduling_service import lock_scheduling

    client, _, Session, ids = employee_client
    path = f'/v2/scheduling/employees/{ids["alex"]}/training'
    with ThreadPoolExecutor(max_workers=1) as executor, Session() as db:
        lock_scheduling(db)
        future = executor.submit(client.post, path, data={'action':'start','csrf_token':client.cookies.get('csrf_token')})
        try:
            response = future.result(timeout=3)
        finally:
            db.rollback()
    assert response.status_code == 303
    assert 'Scheduling is busy with another change. Nothing was saved.' in unquote(response.headers['location'])
    with Session() as db:
        assert not t.enrollments(db, ids['alex'])


def test_training_profile_bounds_wait_for_employee_row(employee_client):
    from urllib.parse import unquote

    client, _, Session, ids = employee_client
    path = f'/v2/scheduling/employees/{ids["alex"]}/training'
    employee_lock_requested = Event()

    def observe_employee_lock(conn, cursor, statement, parameters, context, executemany):
        if 'FROM employees' in statement and 'FOR UPDATE' in statement:
            employee_lock_requested.set()

    with ThreadPoolExecutor(max_workers=2) as executor, Session() as db:
        db.scalar(select(m.Employee).where(m.Employee.id == ids['alex']).with_for_update())
        engine = db.get_bind()
        event.listen(engine, 'before_cursor_execute', observe_employee_lock)
        try:
            future = executor.submit(client.post, path, data={'action':'start','csrf_token':client.cookies.get('csrf_token')})
            assert employee_lock_requested.wait(timeout=3)
            other = executor.submit(client.get, '/missing')
            assert other.result(timeout=2).status_code == 404
            assert not future.done()
            response = future.result(timeout=6)
        finally:
            event.remove(engine, 'before_cursor_execute', observe_employee_lock)
            db.rollback()
    assert response.status_code == 303
    assert 'This employee is being updated. Nothing was saved.' in unquote(response.headers['location'])
    with Session() as db:
        assert not t.enrollments(db, ids['alex'])


def test_schema_allows_training_without_trainer_and_keeps_prior_history(scheduling_db):
    Session,actor,ids,engine=scheduling_db
    with Session() as db:
        _,incoming,enrollment,periods=setup(db,actor,ids);r=manual(db,actor,ids,incoming,periods[0])
        assert r.trainer_employee_id is None
        assert {'first_schedule_date','replacing_employee_id'} <= {c['name'] for c in inspect(engine).get_columns('employees')}
        assert db.scalar(text('SELECT version_num FROM alembic_version'))=='20261103_0055'
        assert 'scheduling_replacement_proposals' in inspect(engine).get_table_names()


def test_generation_keeps_manual_training_and_never_adds_automatic_training(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        _,incoming,_,periods=setup(db,actor,ids)
        from app.services.v2_scheduling_policy_service import regenerate_period
        per=periods[0]
        regenerate_period(db,principal=actor,schedule_period_id=per.id)
        assert not [r for r in rows(db,per) if r.employee_id==incoming.id]
        training=manual(db,actor,ids,incoming,per)
        before=shape([training])
        regenerate_period(db,principal=actor,schedule_period_id=per.id)
        assert shape([training])==before
        assert [r.id for r in rows(db,per) if r.employee_id==incoming.id]==[training.id]


def test_replacement_date_controls_future_allocation_without_terminating_employee(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        outgoing,incoming,_,periods=setup(db,actor,ids)
        transition(db,actor,ids,incoming,outgoing)
        from app.services.v2_scheduling_allocation_service import allocation_report
        report=allocation_report(db,periods[1])
        assert not any(r['employee_id']==outgoing.id for r in report)
        verdict=evaluate_assignment(db,employee_id=outgoing.id,store_id=ids['north'],shift_date=START+timedelta(days=7),
            start_time=time(9),end_time=time(10),schedule_period_id=periods[1].id)
        assert verdict.eligible  # Employment and later manual scheduling authority are retained.
        assert outgoing.active and outgoing.scheduling_active and outgoing.last_effective_date is None


def test_no_replacement_keeps_already_started_training_and_historical_snapshots(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        _,incoming,_,periods=setup(db,actor,ids)
        training=train(db,actor,ids,incoming,periods)
        from app.services.v2_scheduling_snapshot_service import serialize_schedule,canonical_checksum
        snapshot=serialize_schedule(db,periods[0]);checksum=canonical_checksum(snapshot)
        before=shape(training[:3])
        t.save_transition(db,principal=actor,employee_id=incoming.id,first_schedule_date=START+timedelta(days=3),
            allowed_store_ids=(ids['north'],),now=datetime(2026,11,11,20,tzinfo=timezone.utc))
        assert before==shape(training[:3]) and canonical_checksum(snapshot)==checksum


def test_no_competing_replacement_can_be_silently_saved(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        outgoing,incoming,_,_=setup(db,actor,ids);transition(db,actor,ids,incoming,outgoing)
        other=person(db,actor,'Another new employee',lead=True,target=3)
        with pytest.raises(SchedulingValidationError,match='already has'):
            transition(db,actor,ids,other,outgoing)
        assert other.first_schedule_date is None


def test_owner_single_profile_save_transfers_protected_work_with_locks_and_audit(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        outgoing,incoming,_,periods=setup(db,actor,ids)
        target=next(r for r in rows(db,periods[1]) if r.employee_id==outgoing.id)
        target.manually_locked=True;target.lead_of_day_manually_assigned=True
        db.flush()
        owner=Principal(actor.id,actor.username,Role.ADMIN,None,True)
        result=transition(db,owner,ids,incoming,outgoing)
        assert len(result['changes'])==3 and target.employee_id==incoming.id
        assert target.manually_locked and target.lead_of_day_manually_assigned and target.is_lead_of_day
        assert db.scalar(select(m.AuditLog.id).where(m.AuditLog.action=='V2:SCHEDULING:EMPLOYEE_SCHEDULE_TRANSITION_SAVED'))


def test_new_training_cannot_overlap_a_previously_planned_future_period(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        _,incoming,enrollment,_=setup(db,actor,ids)
        with pytest.raises(SchedulingValidationError,match='overlaps'):
            t.start_training(db,principal=actor,employee_id=incoming.id,now=NOW)
        assert t.enrollments(db,incoming.id)==[enrollment]


def test_completed_latest_enrollment_does_not_reactivate_older_training_history(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        outgoing,incoming,enrollment,_=setup(db,actor,ids)
        older=m.EmployeeTraining(employee_id=incoming.id,start_date=date(2026,10,1),duration_days=14,created_by_principal_id=actor.id)
        db.add(older);db.flush()
        assert t.training_on(db,incoming.id,START).id==enrollment.id
        transition(db,actor,ids,incoming,outgoing)
        assert t.training_on(db,incoming.id,date(2026,11,15)) is None
        assert older.completed_effective_date is None  # Historical record is retained verbatim.


def test_schema_downgrade_cannot_destroy_manual_training_history(scheduling_db):
    Session,actor,ids,engine=scheduling_db
    with Session() as db:
        _,incoming,_,periods=setup(db,actor,ids);manual(db,actor,ids,incoming,periods[0]);db.commit()
    from alembic import command
    from app.schema_contract import _alembic_config,current_revision
    with pytest.raises(RuntimeError,match='history require'):
        command.downgrade(_alembic_config(engine.url.render_as_string(hide_password=False)),'20261102_0054')
    assert current_revision(engine)=='20261103_0055'
    with Session() as db:assert db.scalar(select(func.count(m.ScheduleShift.id)).where(m.ScheduleShift.is_training.is_(True)))==1


def test_repeat_save_refreshes_stale_employee_state_before_deciding_idempotence(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        outgoing,incoming,_,_=setup(db,actor,ids);oid=outgoing.id;eid=incoming.id;db.commit()
    with Session() as stale:
        incoming=stale.get(m.Employee,eid);outgoing=stale.get(m.Employee,oid)
        assert incoming.first_schedule_date is None
        with Session() as fresh:
            transition(fresh,actor,ids,fresh.get(m.Employee,eid),fresh.get(m.Employee,oid));fresh.commit()
        result=transition(stale,actor,ids,incoming,outgoing)
        assert result['unchanged'] and incoming.first_schedule_date==date(2026,11,11)


def test_future_first_date_can_be_adjusted_without_wizard_or_changing_assignments(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        outgoing,incoming,enrollment,periods=setup(db,actor,ids);train(db,actor,ids,incoming,periods)
        transition(db,actor,ids,incoming,outgoing)
        before=shape([r for per in periods for r in rows(db,per)])
        result=transition(db,actor,ids,incoming,outgoing,day=date(2026,11,12))
        assert not result['changes'] and incoming.first_schedule_date==date(2026,11,12)
        assert before==shape([r for per in periods for r in rows(db,per)])
        with pytest.raises(SchedulingValidationError,match='existing regular assignments'):
            transition(db,actor,ids,incoming,outgoing,day=date(2026,11,17))
        assert incoming.first_schedule_date==date(2026,11,12) and enrollment.completed_effective_date==date(2026,11,12)
        assert before==shape([r for per in periods for r in rows(db,per)])
