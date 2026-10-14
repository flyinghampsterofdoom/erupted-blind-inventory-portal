"""Historical reconstruction acceptance using real PostgreSQL and the weekly board."""
from datetime import date, datetime, time, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import pytest
from sqlalchemy import select, func, text
from sqlalchemy.exc import DBAPIError
from app import models as m
from app.auth import Principal, Role
from app.services.v2_scheduling_history_service import (
    open_historical_week, lock_historical_week, unlock_historical_week, historical_workload_minutes)
from app.services.v2_scheduling_service import (
    create_shift, update_shift, delete_shift, create_draft_period, approve_schedule,
    publish_schedule, ShiftInput, SchedulingConflict, SchedulingValidationError)
from app.services.v2_scheduling_policy_service import (
    longview_rotation_fairness, weekend_fairness, regenerate_period,
    effective_assignment_rows, configure_special_store)
from app.services.v2_scheduling_assignments_service import set_lead_of_day, lead_fairness
from app.services.v2_scheduling_attendance_service import record_attendance_event
from app.services.v2_scheduling_board_service import serialize_week_board
from app.services.inventory_working_store_service import working_context
from test_v2_scheduling_foundation import scheduling_db
from test_v2_primary import primary, login

WEEK=date(2026,9,27)
TODAY=date(2026,10,14)

@pytest.fixture
def history(scheduling_db,monkeypatch):
    Session,actor,ids,engine=scheduling_db
    monkeypatch.setattr('app.services.v2_scheduling_lead_duty_service.business_today',lambda db:TODAY)
    with Session() as db:
        row=db.get(m.Principal,actor.id);row.role=m.PrincipalRole.ADMIN;db.commit()
    return Session,Principal(actor.id,actor.username,Role.ADMIN,None,True),ids,engine

def open_week(db,actor,week=WEEK):
    return open_historical_week(db,principal=actor,week_start=week,reason='Reconstructed from manager records')

def add(db,actor,ids,period,employee='alex',store='north',day=0):
    result=create_shift(db,principal=actor,schedule_period_id=period.id,expected_version=period.version,
        values=ShiftInput(ids[employee],ids[store],period.week_start_date+timedelta(days=day),time(8,45),time(17)),
        allowed_store_ids=(ids['north'],ids['south']))
    return db.get(m.ScheduleShift,result.shift_id)

def lock(db,actor,p):
    return lock_historical_week(db,principal=actor,period_id=p.id,expected_version=p.version,
        reason='I attest to the reconstructed work and explicit Lead responsibility')

def board(db,ids,**kwargs):
    return serialize_week_board(db,week_start=WEEK,selected_store_ids=(ids['north'],ids['south']),
        all_authorized_store_ids=(ids['north'],ids['south']),permission_flags={'scheduling.edit_draft_shifts':True,
        'scheduling.generate':True,'scheduling.publish':True,'scheduling.approve':True},**kwargs)

@pytest.mark.parametrize('role,active',[(Role.LEAD,True),(Role.MANAGER,True),(Role.STORE,True),(Role.ADMIN,False)])
def test_only_active_admin_can_open_lock_unlock_and_edit(history,role,active):
    Session,actor,ids,_=history
    bad=Principal(actor.id,actor.username,role,None,active)
    with Session() as db:
        with pytest.raises(PermissionError):open_week(db,bad)
        p=open_week(db,actor);shift=add(db,actor,ids,p)
        with pytest.raises(PermissionError):add(db,bad,ids,p,employee='blair',day=1)
        with pytest.raises(PermissionError):lock(db,bad,p)
        lock(db,actor,p)
        with pytest.raises(PermissionError):unlock_historical_week(db,principal=bad,period_id=p.id,expected_version=p.version,reason='Correction')

@pytest.mark.parametrize('week',[date(2026,10,11),date(2026,10,18),date(2026,10,25)])
def test_current_future_weeks_rejected(history,week):
    Session,actor,ids,_=history
    with Session() as db:
        with pytest.raises(SchedulingValidationError,match='completed week'):open_week(db,actor,week)
        assert db.scalar(select(func.count()).select_from(m.SchedulePeriod))==0

def test_board_manual_lock_backward_navigation_and_no_fake_publication(history):
    Session,actor,ids,_=history
    with Session() as db:
        empty=board(db,ids,historical_admin=True);assert empty['actions']['open_history']
        p=open_week(db,actor);shift=add(db,actor,ids,p)
        b=board(db,ids,historical_admin=True)
        assert b['editable'] and b['actions']['lock_history'] and b['mode']=='HISTORICAL_ENTRY'
        assert not b['actions']['generate'] and not b['actions']['publish'] and not b['actions']['approve']
        assert not shift.is_lead_of_day
        lock(db,actor,p);db.commit()
        b=board(db,ids,historical_admin=True)
        assert b['period']['id']==p.id and b['mode']=='HISTORICAL_LOCKED' and not b['editable']
        assert b['actions']['unlock_history'] and b['period']['historical_reason']
        assert p.published_at is None and p.published_by_principal_id is None and p.generated_at is None
        previous=date.fromisoformat(b['week']['previous_start']);assert previous==WEEK-timedelta(days=7)
        p2=open_week(db,actor,previous);add(db,actor,ids,p2);lock(db,actor,p2)
        assert db.scalar(select(func.count()).select_from(m.SchedulingNotification))==0
        assert db.scalar(select(func.count()).select_from(m.ScheduleAttendanceEvent))==0
        assert db.scalar(select(func.count()).select_from(m.ScheduleGenerationRun))==0

def test_reconstruction_includes_former_employees_without_current_constraints(history):
    Session,actor,ids,_=history
    with Session() as db:
        p=open_week(db,actor);add(db,actor,ids,p,employee='inactive');lock(db,actor,p)
        b=board(db,ids,historical_admin=True)
        assert ids['inactive'] in {e['id'] for e in b['employees']}

def test_unlocked_entries_have_no_fairness_or_future_reservation_credit(history):
    Session,actor,ids,_=history
    with Session() as db:
        p=open_week(db,actor);add(db,actor,ids,p)
        f=longview_rotation_fairness(db,employee_id=ids['alex'],store_id=ids['north'],before_date=TODAY,as_of_date=TODAY)
        assert f.historical_assignment_count==f.planned_future_assignment_count==0
        assert not effective_assignment_rows(db,employee_id=ids['alex'])

def test_longview_employee_week_weekend_and_explicit_lead_credit(history):
    Session,actor,ids,_=history
    with Session() as db:
        configure_special_store(db,principal=actor,store_id=ids['south'],primary_employee_ids=(),rotation_employee_ids=(ids['alex'],))
        p=open_week(db,actor)
        add(db,actor,ids,p,store='south',day=1);add(db,actor,ids,p,store='south',day=2)
        sunday=add(db,actor,ids,p,day=0);add(db,actor,ids,p,day=6)
        assert lead_fairness(db,employee_id=ids['alex'],before_date=TODAY).historical_assignment_count==0
        set_lead_of_day(db,principal=actor,shift_id=sunday.id);lock(db,actor,p)
        f=longview_rotation_fairness(db,employee_id=ids['alex'],store_id=ids['south'],before_date=TODAY,as_of_date=TODAY)
        assert f.historical_assignment_count==1 and f.planned_future_assignment_count==0
        assert all(d['evidence_class']=='MANAGEMENT_ATTESTED' for d in f.credit_details)
        for weekday in (5,6):assert weekend_fairness(db,employee_id=ids['alex'],weekday=weekday,before_date=TODAY,as_of_date=TODAY).historical_assignment_count==1
        assert lead_fairness(db,employee_id=ids['alex'],before_date=TODAY).historical_assignment_count==1
        assert historical_workload_minutes(db,employee_id=ids['alex'],start_date=WEEK,end_date=WEEK+timedelta(days=6))==4*495

def test_unlock_preserves_original_lock_and_recomputes_only_after_relocking(history):
    Session,actor,ids,_=history
    with Session() as db:
        p=open_week(db,actor);original=add(db,actor,ids,p);lock(db,actor,p);db.commit()
        original_version=p.version;original_time=p.historical_locked_at
        correction=unlock_historical_week(db,principal=actor,period_id=p.id,expected_version=p.version,reason='Manager identified the wrong employee')
        copied=db.scalar(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==correction.id))
        update_shift(db,principal=actor,schedule_period_id=correction.id,shift_id=copied.id,expected_version=correction.version,
            values=ShiftInput(ids['blair'],ids['north'],WEEK,time(8,45),time(17)),allowed_store_ids=(ids['north'],ids['south']))
        assert longview_rotation_fairness(db,employee_id=ids['alex'],store_id=ids['north'],before_date=TODAY).historical_assignment_count==1
        lock(db,actor,correction)
        assert longview_rotation_fairness(db,employee_id=ids['alex'],store_id=ids['north'],before_date=TODAY).historical_assignment_count==0
        assert longview_rotation_fairness(db,employee_id=ids['blair'],store_id=ids['north'],before_date=TODAY).historical_assignment_count==1
        assert original.employee_id==ids['alex'] and p.version==original_version and p.historical_locked_at==original_time
        actions=set(db.scalars(select(m.AuditLog.action)))
        assert {'V2:SCHEDULING:HISTORICAL_WEEK_UNLOCKED','V2:SCHEDULING:HISTORICAL_WEEK_LOCKED','V2:SCHEDULING:SHIFT_CHANGED'}<=actions
        with pytest.raises(SchedulingConflict):unlock_historical_week(db,principal=actor,period_id=p.id,expected_version=p.version,reason='Old version')

@pytest.mark.parametrize('operation',['generate','publish','approve','create','update','delete','normal_draft'])
def test_locked_history_rejects_ordinary_operations(history,operation):
    Session,actor,ids,_=history
    with Session() as db:
        p=open_week(db,actor);shift=add(db,actor,ids,p);lock(db,actor,p)
        args=dict(principal=actor,schedule_period_id=p.id,expected_version=p.version,allowed_store_ids=(ids['north'],ids['south']))
        with pytest.raises((SchedulingConflict,SchedulingValidationError)):
            if operation=='generate':regenerate_period(db,principal=actor,schedule_period_id=p.id)
            elif operation=='publish':publish_schedule(db,**args)
            elif operation=='approve':approve_schedule(db,**args)
            elif operation=='create':add(db,actor,ids,p,day=1)
            elif operation=='update':update_shift(db,**args,shift_id=shift.id,values=ShiftInput(ids['blair'],ids['north'],WEEK,time(9),time(17)))
            elif operation=='delete':delete_shift(db,principal=actor,schedule_period_id=p.id,shift_id=shift.id,expected_version=p.version,allowed_store_ids=args['allowed_store_ids'])
            else:create_draft_period(db,principal=actor,week_start=WEEK)

@pytest.mark.parametrize('sql',['UPDATE schedule_shifts SET employee_id=NULL WHERE id=:id',
    'DELETE FROM schedule_shifts WHERE id=:id','UPDATE schedule_periods SET notes=\'changed\' WHERE id=:id',
    'DELETE FROM schedule_periods WHERE id=:id'])
def test_database_guards_preserve_locked_evidence(history,sql):
    Session,actor,ids,_=history
    with Session() as db:
        p=open_week(db,actor);shift=add(db,actor,ids,p);lock(db,actor,p);db.commit()
        with pytest.raises(DBAPIError,match='immutable'):
            db.execute(text(sql),{'id':p.id if 'schedule_periods' in sql else shift.id})
        db.rollback()

@pytest.mark.parametrize('event_type',[m.AttendanceEventType.CALLED_OUT,m.AttendanceEventType.COVERED_SHIFT,
                                     m.AttendanceEventType.WORKED_AS_SCHEDULED,m.AttendanceEventType.LATE])
def test_actual_attendance_precedence_and_no_duplicate_credit(history,event_type):
    Session,actor,ids,_=history
    with Session() as db:
        original=create_draft_period(db,principal=actor,week_start=WEEK)
        s=add(db,actor,ids,original)
        original.status=m.SchedulePeriodStatus.PUBLISHED;original.published_at=datetime(2026,9,25,tzinfo=timezone.utc);db.flush()
        event=record_attendance_event(db,principal=actor,shift_id=s.id,event_type=event_type,
            event_at=datetime(2026,9,27,20,tzinfo=timezone.utc),today=TODAY,
            replacement_employee_id=ids['blair'] if event_type==m.AttendanceEventType.COVERED_SHIFT else None)
        db.flush();event_id=event.event.id
        p=open_week(db,actor);lock(db,actor,p)
        for person in ('alex','blair'):
            f=longview_rotation_fairness(db,employee_id=ids[person],store_id=ids['north'],before_date=TODAY,as_of_date=TODAY)
            expected=int(event_type==m.AttendanceEventType.WORKED_AS_SCHEDULED and person=='alex' or event_type==m.AttendanceEventType.COVERED_SHIFT and person=='blair')
            assert f.historical_assignment_count==expected
            w=weekend_fairness(db,employee_id=ids[person],weekday=6,before_date=TODAY,as_of_date=TODAY)
            assert w.historical_assignment_count==expected
        assert db.get(m.ScheduleAttendanceEvent,event_id).voided_at is None
        assert db.scalar(select(func.count()).select_from(m.ScheduleAttendanceEvent))==1
        assert original.status==m.SchedulePeriodStatus.PUBLISHED

def test_history_changes_neither_working_store_nor_future_publication(history):
    Session,actor,ids,_=history
    now=datetime(2026,10,14,18,tzinfo=timezone.utc)
    with Session() as db:
        account=m.Principal(username='linked',password_hash='unused',role=m.PrincipalRole.STORE,store_id=ids['north'],active=True)
        db.add(account);db.flush();employee=db.get(m.Employee,ids['alex']);employee.principal_id=account.id
        user=Principal(account.id,account.username,Role.STORE,account.store_id,True)
        future=create_draft_period(db,principal=actor,week_start=date(2026,10,11))
        current=add(db,actor,ids,future,store='south',day=3)
        future.status=m.SchedulePeriodStatus.PUBLISHED;future.published_at=now;db.flush()
        context=working_context(db,principal=user,now=now);version=future.version;context_version=employee.working_context_version
        p=open_week(db,actor);add(db,actor,ids,p);lock(db,actor,p)
        assert working_context(db,principal=user,now=now)==context
        assert context['store_id']==ids['south'] and employee.working_context_version==context_version
        assert future.version==version and current.store_id==ids['south']
        assert db.scalar(select(func.count()).select_from(m.InventoryStoreAccessGrant))==0
        assert db.scalar(select(func.count()).select_from(m.SchedulingNotification))==0

def test_history_http_admin_controls_csrf_and_backward_week(primary):
    primary.account(role='ADMIN',linked=False);assert login(primary).status_code==303
    client=primary.client
    url='/v2/scheduling/week?start=2026-09-27'
    html=client.get(url);assert html.status_code==200 and 'Open historical entry week' in html.text
    assert 'start=2026-09-20' in html.text
    payload={'week_start_date':'2026-09-27','reason':'Manager records'}
    assert client.post('/v2/scheduling/api/history/open',json=payload).status_code==403
    headers={'X-CSRF-Token':client.cookies.get('csrf_token')}
    response=client.post('/v2/scheduling/api/history/open',json=payload,headers=headers)
    assert response.status_code==200,response.text
    assert 'Lock Historical Week' in client.get(url).text


def test_concurrent_unlocks_create_one_correction_and_keep_original(history):
    Session,actor,ids,_=history
    with Session() as db:
        p=open_week(db,actor);add(db,actor,ids,p);lock(db,actor,p);db.commit();pid=p.id;version=p.version
    barrier=Barrier(2)
    def attempt():
        with Session() as db:
            barrier.wait()
            try:
                result=unlock_historical_week(db,principal=actor,period_id=pid,expected_version=version,reason='Concurrent correction request')
                db.commit();return result.id
            except SchedulingConflict:
                db.rollback();return None
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:attempt(),range(2)))
    assert len([r for r in results if r])==1
    with Session() as db:
        assert db.get(m.SchedulePeriod,pid).historical_locked_at
        assert db.scalar(select(func.count()).select_from(m.SchedulePeriod).where(m.SchedulePeriod.status==m.SchedulePeriodStatus.DRAFT))==1


def test_concurrent_lock_and_edit_never_commit_a_stale_attestation(history):
    Session,actor,ids,_=history
    with Session() as db:
        p=open_week(db,actor);s=add(db,actor,ids,p);db.commit();pid=p.id;version=p.version;sid=s.id
    barrier=Barrier(2)
    def attempt(kind):
        with Session() as db:
            barrier.wait()
            try:
                if kind=='lock':lock_historical_week(db,principal=actor,period_id=pid,expected_version=version,reason='Verified original version')
                else:update_shift(db,principal=actor,schedule_period_id=pid,shift_id=sid,expected_version=version,
                    values=ShiftInput(ids['blair'],ids['north'],WEEK,time(8,45),time(17)),allowed_store_ids=(ids['north'],ids['south']))
                db.commit();return kind
            except SchedulingConflict:
                db.rollback();return None
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(attempt,['lock','edit']))
    assert len([r for r in results if r])==1
    with Session() as db:
        p=db.get(m.SchedulePeriod,pid);s=db.get(m.ScheduleShift,sid)
        assert (p.historical_locked_at is not None and s.employee_id==ids['alex']) or (p.historical_locked_at is None and s.employee_id==ids['blair'])


def test_overlap_rejected_and_stale_lock_version_rejected(history):
    Session,actor,ids,_=history
    with Session() as db:
        p=open_week(db,actor);first_version=p.version;add(db,actor,ids,p)
        with pytest.raises(SchedulingValidationError):add(db,actor,ids,p,store='south')
        with pytest.raises(SchedulingConflict):lock_historical_week(db,principal=actor,period_id=p.id,expected_version=first_version,reason='Old browser state')


def test_locked_history_never_generates_or_approves_even_after_unlock(history):
    Session,actor,ids,_=history
    with Session() as db:
        p=open_week(db,actor);add(db,actor,ids,p);lock(db,actor,p)
        p=unlock_historical_week(db,principal=actor,period_id=p.id,expected_version=p.version,reason='Correction')
        with pytest.raises(SchedulingConflict):regenerate_period(db,principal=actor,schedule_period_id=p.id)
        args=dict(principal=actor,schedule_period_id=p.id,expected_version=p.version,allowed_store_ids=(ids['north'],ids['south']))
        with pytest.raises(SchedulingConflict):approve_schedule(db,**args)
        with pytest.raises(SchedulingConflict):publish_schedule(db,**args)
        assert not db.scalar(select(func.count()).select_from(m.ScheduleGenerationRun))


def test_lead_exception_precedence_and_no_implicit_lead_credit(history):
    from app.services.v2_scheduling_lead_duty_service import record_lead_duty
    Session,actor,ids,_=history
    with Session() as db:
        original=create_draft_period(db,principal=actor,week_start=WEEK);s=add(db,actor,ids,original)
        original.status=m.SchedulePeriodStatus.PUBLISHED;original.published_at=datetime(2026,9,25,tzinfo=timezone.utc);db.flush()
        record_lead_duty(db,principal=actor,shift_id=s.id,outcome='PERFORMED',employee_id=ids['blair'],reason='Actual Lead duty documented',today=TODAY)
        p=open_week(db,actor);copied=db.scalar(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==p.id))
        assert not copied.is_lead_of_day
        set_lead_of_day(db,principal=actor,shift_id=copied.id);lock(db,actor,p)
        assert lead_fairness(db,employee_id=ids['alex'],before_date=TODAY).historical_assignment_count==0
        assert lead_fairness(db,employee_id=ids['blair'],before_date=TODAY).historical_assignment_count==1


@pytest.mark.parametrize('role',['MANAGER','LEAD','STORE'])
def test_history_http_non_admin_denied(primary,role):
    primary.account(role=role,linked=False);login(primary)
    client=primary.client;headers={'X-CSRF-Token':client.cookies.get('csrf_token')}
    response=client.post('/v2/scheduling/api/history/open',json={'week_start_date':'2026-09-27','reason':'Attempt'},headers=headers)
    assert response.status_code==403
    assert 'Open historical entry week' not in client.get('/v2/scheduling/week?start=2026-09-27').text


def test_migration_0033_to_0034_preserves_original_columns_and_evidence(history):
    from alembic import command
    from app.schema_contract import _alembic_config,current_revision
    from app.services.v2_scheduling_history_service import lock_historical_week
    Session,actor,ids,engine=history
    # An empty fresh schema may downgrade for the upgrade rehearsal, never a populated history.
    cfg=_alembic_config(engine.url.render_as_string(hide_password=False))
    command.downgrade(cfg,'20261013_0033')
    with engine.begin() as c:
        c.execute(text("INSERT INTO schedule_periods (week_start_date,week_end_date,status,revision_number,created_by_principal_id,updated_by_principal_id) VALUES ('2026-09-27','2026-10-03','DRAFT',1,:actor,:actor)"),{'actor':actor.id})
        before=c.execute(text('SELECT row_to_json(p) FROM schedule_periods p')).scalar_one()
        employees=c.execute(text('SELECT row_to_json(e) FROM employees e ORDER BY id')).scalars().all()
        audits=c.execute(text('SELECT row_to_json(a) FROM audit_log a ORDER BY id')).scalars().all()
    command.upgrade(cfg,'head')
    with engine.connect() as c:
        after=c.execute(text('SELECT row_to_json(p) FROM schedule_periods p')).scalar_one()
        assert {k:after[k] for k in before}==before
        assert c.execute(text('SELECT row_to_json(e) FROM employees e ORDER BY id')).scalars().all()==employees
        assert c.execute(text('SELECT row_to_json(a) FROM audit_log a ORDER BY id')).scalars().all()==audits
    with Session() as db:
        p=open_week(db,actor);add(db,actor,ids,p);lock(db,actor,p);db.commit()
    with pytest.raises(RuntimeError,match='Historical evidence'):command.downgrade(cfg,'20261013_0033')
    assert current_revision(engine)=='20261014_0034'


def test_attendance_and_separate_backfilled_double_coverage_both_receive_credit(history):
    Session,actor,ids,_=history
    with Session() as db:
        original=create_draft_period(db,principal=actor,week_start=WEEK);s=add(db,actor,ids,original)
        original.status=m.SchedulePeriodStatus.PUBLISHED;original.published_at=datetime(2026,9,25,tzinfo=timezone.utc);db.flush()
        record_attendance_event(db,principal=actor,shift_id=s.id,event_type=m.AttendanceEventType.WORKED_AS_SCHEDULED,
            event_at=datetime(2026,9,27,20,tzinfo=timezone.utc),today=TODAY)
        p=open_week(db,actor);add(db,actor,ids,p,employee='blair');lock(db,actor,p)
        for person in ('alex','blair'):
            assert longview_rotation_fairness(db,employee_id=ids[person],store_id=ids['north'],before_date=TODAY).historical_assignment_count==1
            assert historical_workload_minutes(db,employee_id=ids[person],start_date=WEEK,end_date=WEEK+timedelta(days=6))==495


def test_existing_committed_coverage_precedes_reconstruction_without_duplicate_credit(history):
    from app.services.v2_scheduling_exception_service import record_commitment
    Session,actor,ids,_=history
    with Session() as db:
        original=create_draft_period(db,principal=actor,week_start=WEEK);s=add(db,actor,ids,original)
        original.status=m.SchedulePeriodStatus.PUBLISHED;original.published_at=datetime(2026,9,25,tzinfo=timezone.utc);db.flush()
        commitment=record_commitment(db,principal=actor,shift_id=s.id,employee_id=ids['blair'],note='Existing committed replacement',today=WEEK-timedelta(days=1))
        p=open_week(db,actor);add(db,actor,ids,p,employee='blair');lock(db,actor,p)
        assert longview_rotation_fairness(db,employee_id=ids['alex'],store_id=ids['north'],before_date=TODAY).historical_assignment_count==0
        assert longview_rotation_fairness(db,employee_id=ids['blair'],store_id=ids['north'],before_date=TODAY).historical_assignment_count==1
        assert historical_workload_minutes(db,employee_id=ids['blair'],start_date=WEEK,end_date=WEEK+timedelta(days=6))==495
        assert commitment.voided_at is None


def test_historical_lead_http_can_clear_and_audit_deleted_draft_entries(primary):
    pid=primary.account(role='ADMIN',linked=False);login(primary)
    actor=Principal(pid,'person@example.test',Role.ADMIN,None,True)
    with primary.Session() as db:
        p=open_week(db,actor);shift=add(db,actor,primary.ids,p);db.commit();period_id=p.id;shift_id=shift.id;version=p.version
    client=primary.client;headers={'X-CSRF-Token':client.cookies.get('csrf_token')}
    path=f'/v2/scheduling/api/history/{period_id}/lead/{shift_id}'
    result=client.post(path,json={'expected_version':version,'reason':'Explicit Lead responsibility'},headers=headers)
    assert result.status_code==200,result.text
    with primary.Session() as db:version=db.get(m.SchedulePeriod,period_id).version
    result=client.post(path,json={'expected_version':version,'reason':'Correction: responsibility not established','assigned':False},headers=headers)
    assert result.status_code==200,result.text
    with primary.Session() as db:
        p=db.get(m.SchedulePeriod,period_id);shift=db.get(m.ScheduleShift,shift_id)
        assert not shift.is_lead_of_day
        delete_shift(db,principal=actor,schedule_period_id=period_id,shift_id=shift_id,expected_version=p.version,
                     allowed_store_ids=(primary.ids['north'],primary.ids['south']))
        db.commit()
    html=client.get(f'/v2/scheduling/periods/{period_id}/history')
    assert html.status_code==200 and 'SHIFT_DELETED' in html.text and 'HISTORICAL_LEAD_CLEARED' in html.text
    assert 'No original publication or employee acknowledgment is asserted' in html.text


def test_reconstruction_and_correction_preserve_completed_and_unfinished_inventory(history):
    from unittest.mock import Mock
    from decimal import Decimal
    from app.services.blind_count_service import save_draft,submit_round
    from app.services.v2_scheduling_policy_service import organization_policy
    Session,actor,ids,_=history
    with Session() as db:
        organization_policy(db,principal_id=actor.id)
        user=m.Principal(username='retained-count-worker',password_hash='unused',role=m.PrincipalRole.STORE,store_id=ids['north'],active=True)
        campaign=m.Campaign(label='Retained synthetic inventory');db.add_all([user,campaign]);db.flush()
        worker=Principal(user.id,user.username,Role.STORE,ids['north'],True)
        for completed in (False,True):
            count=m.CountSession(store_id=ids['north'],campaign_id=campaign.id,employee_name='Retained Worker',created_by_principal_id=user.id)
            db.add(count);db.flush();db.add(m.SnapshotLine(session_id=count.id,variation_id='HISTORY',item_name='Synthetic',variation_name='Test'));db.flush()
            revision=save_draft(db,principal=worker,session_id=count.id,revision=0,changes={'HISTORY':{'front_qty':'2','back_qty':'3'}},operation_id=str(completed))['revision']
            if completed:
                provider=Mock();provider.fetch_current_on_hand.return_value={'HISTORY':Decimal('5')}
                submit_round(db,principal=worker,session_id=count.id,revision=revision,provider=provider)
        db.flush()
        models=(m.Employee,m.Principal,m.CountSession,m.Entry,m.SnapshotLine,m.CountObservation,
                m.ScheduleAttendanceEvent,m.ScheduleCoverageCommitment,m.LeadDutyOutcome,m.SchedulingOrganizationPolicy)
        def retained():return {model.__tablename__:[dict(r) for r in db.execute(select(model.__table__).order_by(*model.__table__.primary_key.columns)).mappings()] for model in models}
        before=retained();assert before['count_observations']
        p=open_week(db,actor);add(db,actor,ids,p);lock(db,actor,p)
        correction=unlock_historical_week(db,principal=actor,period_id=p.id,expected_version=p.version,reason='Documented historical correction')
        lock(db,actor,correction)
        assert retained()==before
        assert db.scalar(select(func.count()).select_from(m.SchedulingNotification))==0


def test_existing_weekly_workload_reporting_counts_attested_history_without_future_reservations(history):
    from decimal import Decimal
    from app.services.v2_scheduling_policy_service import scheduled_weekly_hours,scheduled_weekly_shift_count,weekly_work_pattern
    Session,actor,ids,_=history
    with Session() as db:
        p=open_week(db,actor);add(db,actor,ids,p,employee='inactive');lock(db,actor,p)
        assert scheduled_weekly_hours(db,employee_id=ids['inactive'],shift_date=WEEK)==Decimal('8.25')
        assert scheduled_weekly_shift_count(db,employee_id=ids['inactive'],shift_date=WEEK)==1
        assert weekly_work_pattern(db,employee_id=ids['inactive'],shift_date=WEEK).worked_shifts==1
        assert scheduled_weekly_hours(db,employee_id=ids['inactive'],shift_date=TODAY)==0
        assert effective_assignment_rows(db,employee_id=ids['inactive'],start_date=TODAY)==[]


def test_cleared_schedule_intent_is_not_seeded_as_historical_work(history):
    Session,actor,ids,_=history
    with Session() as db:
        old=create_draft_period(db,principal=actor,week_start=WEEK);add(db,actor,ids,old)
        old.status=m.SchedulePeriodStatus.ARCHIVED;old.published_at=datetime(2026,9,20,tzinfo=timezone.utc)
        old.cleared_at=datetime(2026,9,25,tzinfo=timezone.utc);db.flush()
        p=open_week(db,actor)
        assert p.source_schedule_period_id is None
        assert db.scalar(select(func.count()).select_from(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==p.id))==0
        with pytest.raises(SchedulingValidationError):lock(db,actor,p)
        assert longview_rotation_fairness(db,employee_id=ids['alex'],store_id=ids['north'],before_date=TODAY).historical_assignment_count==0


@pytest.mark.parametrize('locked',[False,True])
@pytest.mark.parametrize('operation',['copy','template','generation_source'])
def test_history_cannot_seed_future_copy_template_or_generation_reservations(history,locked,operation):
    from app.services.v2_scheduling_template_service import copy_schedule_periods,save_schedule_template
    from app.services.v2_scheduling_policy_service import _create_generated_period
    Session,actor,ids,_=history
    with Session() as db:
        p=open_week(db,actor);add(db,actor,ids,p)
        if locked:lock(db,actor,p)
        before=db.scalar(select(func.count()).select_from(m.SchedulePeriod))
        with pytest.raises((SchedulingValidationError,SchedulingConflict),match='Historical evidence'):
            if operation=='copy':copy_schedule_periods(db,principal=actor,source_period_ids=(p.id,),target_week_start=date(2026,10,18),allowed_store_ids=(ids['north'],ids['south']),mode='MERGE')
            elif operation=='template':save_schedule_template(db,principal=actor,name='Must not reserve',source_period_ids=(p.id,),allowed_store_ids=(ids['north'],ids['south']))
            else:_create_generated_period(db,principal=actor,week_start=date(2026,10,18),source=p,source_week_offset=3,publication_at=None,generated_at=datetime.now(timezone.utc),note='Blocked source')
        assert db.scalar(select(func.count()).select_from(m.SchedulePeriod))==before
        assert db.scalar(select(func.count()).select_from(m.ScheduleTemplate))==0
        assert db.scalar(select(func.count()).select_from(m.ScheduleGenerationRun))==0
