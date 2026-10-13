from datetime import date, time, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from app import models as m
from app.services.v2_scheduling_service import (
    create_draft_period, create_shift, publish_schedule, ShiftInput, SchedulingValidationError,
)
from test_v2_scheduling_foundation import scheduling_db
from test_v2_primary import primary, login


@pytest.mark.parametrize('change', ['inactive', 'scheduling_inactive', 'square_inactive',
    'never', 'overlap', 'consecutive', 'hours', 'pto'])
def test_publication_revalidates_changed_assignment(scheduling_db, change):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        period = create_draft_period(db, principal=actor, week_start=date(2026, 10, 18))
        person = db.get(m.Employee, ids['alex'])
        person.scheduling_lead_capable = True
        result = create_shift(db, principal=actor, schedule_period_id=period.id,
            expected_version=period.version, allowed_store_ids=(ids['north'], ids['south']),
            values=ShiftInput(person.id, ids['north'], date(2026, 10, 20), time(9), time(17)))
        if change == 'inactive': person.active = False
        elif change == 'scheduling_inactive': person.scheduling_active = False
        elif change == 'square_inactive': person.square_status = 'INACTIVE'
        elif change == 'never':
            db.add(m.EmployeeSchedulingStorePreference(employee_id=person.id, store_id=ids['north'],
                preference_level=m.StorePreferenceLevel.NEVER,
                created_by_principal_id=actor.id, updated_by_principal_id=actor.id))
        elif change == 'hours':
            db.add(m.EmployeeSchedulingProfile(employee_id=person.id, maximum_weekly_hours=Decimal('1'),
                created_by_principal_id=actor.id, updated_by_principal_id=actor.id))
        elif change == 'pto':
            db.add(m.TimeOffRequest(employee_id=person.id, start_date=date(2026,10,20),
                end_date=date(2026,10,20), full_day=True, status=m.TimeOffRequestStatus.APPROVED,
                reason_category_id=ids['vacation'], submitted_by_principal_id=actor.id,
                created_by_principal_id=actor.id, updated_by_principal_id=actor.id))
        else:
            days = [date(2026,10,20)] if change == 'overlap' else [date(2026,10,18),date(2026,10,19),date(2026,10,21)]
            for day in days:
                db.add(m.ScheduleShift(schedule_period_id=period.id, employee_id=person.id,
                    store_id=ids['north'], shift_date=day, start_time=time(9), end_time=time(17),
                    created_by_principal_id=actor.id, updated_by_principal_id=actor.id))
        db.flush()
        with pytest.raises(SchedulingValidationError):
            publish_schedule(db, principal=actor, schedule_period_id=period.id,
                expected_version=period.version, allowed_store_ids=(ids['north'], ids['south']),
                allow_serious_warnings=True, confirmed=True, override_reason='Cannot waive a hard rule')
        assert period.status == m.SchedulePeriodStatus.DRAFT


def ready_period(db, actor, ids, *, day=date(2026, 10, 20), two=False):
    period = create_draft_period(db, principal=actor, week_start=day-timedelta(days=(day.weekday()+1)%7))
    rows = []
    for person, store in [('alex','north')] + ([('blair','south')] if two else []):
        result = create_shift(db, principal=actor, schedule_period_id=period.id,
            expected_version=period.version, allowed_store_ids=(ids['north'], ids['south']),
            values=ShiftInput(ids[person], ids[store], day, time(9), time(17)))
        rows.append(db.get(m.ScheduleShift, result.shift_id))
    return period, rows


def test_approval_is_version_bound_and_revalidates(scheduling_db):
    from app.services.v2_scheduling_service import approve_schedule, update_shift
    from app.auth import Principal, Role
    Session, actor, ids, _ = scheduling_db
    lead = Principal(actor.id, actor.username, Role.LEAD, None, True)
    with Session() as db:
        period, rows = ready_period(db, actor, ids)
        values = dict(principal=lead, schedule_period_id=period.id, expected_version=period.version,
                      allowed_store_ids=(ids['north'], ids['south']))
        with pytest.raises(SchedulingValidationError, match='Approve'):
            publish_schedule(db, **values)
        approve_schedule(db, **values)
        old_version = period.version
        update_shift(db, principal=lead, schedule_period_id=period.id, shift_id=rows[0].id,
            expected_version=period.version, allowed_store_ids=(ids['north'], ids['south']),
            values=ShiftInput(ids['alex'], ids['north'], rows[0].shift_date, time(10), time(17)))
        with pytest.raises(SchedulingValidationError, match='Approve'):
            publish_schedule(db, **{**values, 'expected_version': period.version})
        approve_schedule(db, **{**values, 'expected_version': period.version})
        publish_schedule(db, **{**values, 'expected_version': period.version})
        assert len(list(db.scalars(select(m.ScheduleApproval)))) == 2
        assert period.status == m.SchedulePeriodStatus.PUBLISHED


def test_nine_future_weeks_catchup_and_no_auto_publication(scheduling_db, monkeypatch):
    from datetime import datetime, timezone
    from app.services.v2_scheduling_policy_service import manual_generate_draft_schedule, run_schedule_automation
    from app.services.v2_scheduling_prebeta_service import publication_status
    from test_v2_scheduling_foundation import _coverage
    Session, actor, ids, _ = scheduling_db
    monkeypatch.setenv('SCHEDULE_AUTOMATION_EXECUTION_ENABLED','true')
    with Session() as db:
        _coverage(db, actor, ids)
        first = manual_generate_draft_schedule(db, principal=actor, now=datetime(2026,10,13,18,tzinfo=timezone.utc))
        assert first['horizon_weeks'] == 9 and first['week_start_date'] == date(2026,10,18)
        assert len(first['created_period_ids']) == 9
        assert publication_status(db, today=date(2026,10,13))['color'] == 'red'
        again = run_schedule_automation(db, principal=actor, now=datetime(2026,10,13,19,tzinfo=timezone.utc))
        assert again['generated_period_ids'] == again['published_period_ids'] == []
        catchup = run_schedule_automation(db, principal=actor, now=datetime(2026,11,3,18,tzinfo=timezone.utc))
        assert len(catchup['generated_period_ids']) == 3 and catchup['published_period_ids'] == []
        assert all(p.status == m.SchedulePeriodStatus.DRAFT for p in db.scalars(select(m.SchedulePeriod)))


def test_store_swap_inventory_context_expiry_and_reversal(scheduling_db):
    from datetime import datetime, timezone
    from app.auth import Principal, Role
    from app.services.v2_scheduling_service import approve_schedule
    from app.services.v2_scheduling_swap_service import change_assignments
    from app.services.inventory_working_store_service import working_context, authorize_store, assert_inventory_store, WorkingStoreConflict
    Session, actor, ids, _ = scheduling_db
    now = datetime(2026,10,20,17,tzinfo=timezone.utc)
    with Session() as db:
        user = m.Principal(username='linked-inventory', password_hash='unused', role=m.PrincipalRole.STORE, store_id=ids['north'], active=True)
        db.add(user); db.flush()
        person = db.get(m.Employee, ids['alex']); person.principal_id = user.id
        employee = Principal(user.id, user.username, Role.STORE, ids['north'], True)
        period, rows = ready_period(db, actor, ids, two=True)
        args = dict(principal=actor, schedule_period_id=period.id, expected_version=period.version, allowed_store_ids=(ids['north'],ids['south']))
        approve_schedule(db, **args); publish_schedule(db, **args)
        before = working_context(db, principal=employee, now=now)
        assert before['store_id'] == ids['north']
        grant = authorize_store(db, principal=actor, employee_id=person.id, store_id=ids['south'],
            reason='Actual working location confirmed after checking schedule', expires_at=now+timedelta(hours=1),
            expected_version=person.working_context_version, now=now)
        assert working_context(db, principal=employee, selected_store_id=ids['south'], now=now)['store_id'] == ids['south']
        assert working_context(db, principal=employee, selected_store_id=ids['south'], now=now+timedelta(hours=2))['store_id'] == ids['north']
        result = change_assignments(db, principal=actor, shift_id=rows[0].id, other_shift_id=rows[1].id,
            kind='STORE', reason='Same-day store exchange', expected_versions={str(period.id):period.version},
            allowed_store_ids=(ids['north'],ids['south']), now=now)
        after = working_context(db, principal=employee, now=now)
        assert after['store_id'] == ids['south'] and not after['emergency_options']
        assert period.status == m.SchedulePeriodStatus.ARCHIVED and rows[0].store_id == ids['north']
        latest = db.get(m.SchedulePeriod, result['period_ids'][0])
        new_rows = list(db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==latest.id).order_by(m.ScheduleShift.id)))
        change_assignments(db, principal=actor, shift_id=new_rows[0].id, other_shift_id=new_rows[1].id,
            kind='STORE', reason='Reverse exchange', expected_versions={str(latest.id):latest.version},
            allowed_store_ids=(ids['north'],ids['south']), now=now)
        reverted = working_context(db, principal=employee, now=now)
        assert reverted['store_id'] == ids['north'] and reverted['version'] != before['version']
        assert user.store_id == ids['north']


def test_preference_request_pending_approval_expiration_and_baseline(scheduling_db):
    from app.auth import Principal, Role
    from app.services.v2_scheduling_preference_request_service import submit_request, decide_request, approved_preferences
    from app.services.v2_scheduling_policy_service import assignment_score
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        db.get(m.Employee, ids['alex']).principal_id = actor.id
        row = submit_request(db, principal=actor, start_date=date(2026,10,18), end_date=date(2026,10,24),
            preferences={'preferred_stores':[ids['south']], 'off_days':[2]})
        assert approved_preferences(db, employee_id=ids['alex'], on_date=date(2026,10,20)) == {}
        with pytest.raises(PermissionError):
            decide_request(db, principal=actor, request_id=row.id, decision='APPROVED', reason='Manager cannot decide')
        admin = Principal(actor.id, actor.username, Role.ADMIN, None, True)
        decide_request(db, principal=admin, request_id=row.id, decision='APPROVED', reason='Approved dates and soft preferences')
        assert assignment_score(db, employee_id=ids['alex'], store_id=ids['south'], shift_date=date(2026,10,20))[0] == 300
        assert approved_preferences(db, employee_id=ids['alex'], on_date=date(2026,10,25)) == {}
        assert db.scalar(select(m.EmployeeSchedulingStorePreference)) is None


def test_generation_failure_evidence_survives_caller_rollback(scheduling_db, monkeypatch):
    from datetime import datetime, timezone
    import app.services.v2_scheduling_policy_service as policy
    from test_v2_scheduling_foundation import _coverage
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        _coverage(db, actor, ids); db.commit()
        def fail(*args, **kwargs):
            raise RuntimeError('Do not persist sensitive error details')
        monkeypatch.setattr(policy, '_create_generated_period', fail)
        with pytest.raises(RuntimeError):
            policy.manual_generate_draft_schedule(db, principal=actor, now=datetime(2026,10,13,18,tzinfo=timezone.utc))
        db.rollback()
        run = db.scalar(select(m.ScheduleGenerationRun))
        assert run.status == 'FAILED' and run.result == {'error_type':'RuntimeError'}
        assert db.scalar(select(m.SchedulePeriod)) is None


def test_publication_status_colors(scheduling_db):
    from datetime import datetime, timezone
    from app.services.v2_scheduling_prebeta_service import publication_status
    from app.services.v2_scheduling_service import approve_schedule
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        assert publication_status(db, today=date(2026,10,13))['color'] == 'red'
        period, _ = ready_period(db, actor, ids)
        args=dict(principal=actor,schedule_period_id=period.id,expected_version=period.version,allowed_store_ids=(ids['north'],ids['south']))
        approve_schedule(db, **args); publish_schedule(db, **args)
        assert publication_status(db, today=date(2026,10,13))['color'] == 'green'
        upcoming = create_draft_period(db, principal=actor, week_start=date(2026,10,25))
        upcoming.generated_at = datetime.now(timezone.utc); db.flush()
        assert publication_status(db, today=date(2026,10,13))['color'] == 'yellow'


def test_callout_reservation_then_presumptive_replacement(scheduling_db):
    from datetime import datetime, timezone
    from app.services.v2_scheduling_service import approve_schedule
    from app.services.v2_scheduling_exception_service import record_exception, expected_employee_id
    from app.services.v2_scheduling_policy_service import effective_assignment_rows
    from app.services.v2_scheduling_attendance_service import resolve_attendance_outcome
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        period, rows = ready_period(db, actor, ids)
        args=dict(principal=actor,schedule_period_id=period.id,expected_version=period.version,allowed_store_ids=(ids['north'],ids['south']))
        approve_schedule(db, **args); publish_schedule(db, **args)
        record_exception(db, principal=actor, shift_id=rows[0].id, event_type=m.AttendanceEventType.CALLED_OUT,
            event_at=datetime(2026,10,19,18,tzinfo=timezone.utc), commitment_employee_id=ids['blair'], today=date(2026,10,19))
        assert rows[0].employee_id == ids['alex'] and expected_employee_id(db, rows[0]) == ids['blair']
        assert not effective_assignment_rows(db, employee_id=ids['alex'])
        assert effective_assignment_rows(db, employee_id=ids['blair'])[0].id == rows[0].id
        events = list(db.scalars(select(m.ScheduleAttendanceEvent)))
        future = resolve_attendance_outcome(rows[0],events,db=db,presumptive=False)
        completed = resolve_attendance_outcome(rows[0],events,db=db,presumptive=True)
        assert future['credited_employee_ids'] == ()
        assert completed['credited_employee_ids'] == (ids['blair'],)
        assert completed['evidence_class'] == 'PRESUMPTIVE_PUBLISHED'


def test_swap_invalid_second_side_rolls_back_both(scheduling_db):
    from datetime import datetime, timezone
    from app.services.v2_scheduling_service import approve_schedule
    from app.services.v2_scheduling_swap_service import change_assignments
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        period, rows = ready_period(db, actor, ids, two=True)
        args=dict(principal=actor,schedule_period_id=period.id,expected_version=period.version,allowed_store_ids=(ids['north'],ids['south']))
        approve_schedule(db, **args); publish_schedule(db, **args)
        db.add(m.EmployeeSchedulingStorePreference(employee_id=ids['blair'],store_id=ids['north'],
            preference_level=m.StorePreferenceLevel.NEVER,created_by_principal_id=actor.id,updated_by_principal_id=actor.id));db.flush()
        with pytest.raises(SchedulingValidationError):
            change_assignments(db,principal=actor,shift_id=rows[0].id,other_shift_id=rows[1].id,
                kind='STORE',reason='Invalid second side',expected_versions={str(period.id):period.version},
                allowed_store_ids=(ids['north'],ids['south']),now=datetime(2026,10,20,17,tzinfo=timezone.utc))
        assert period.status == m.SchedulePeriodStatus.PUBLISHED
        assert [r.store_id for r in rows] == [ids['north'],ids['south']]
        assert len(list(db.scalars(select(m.SchedulePeriod)))) == 1


def test_inventory_submission_rejects_old_store_and_preserves_count(scheduling_db):
    from datetime import datetime, timezone
    from unittest.mock import Mock
    from app.auth import Principal, Role
    from app.services.inventory_working_store_service import working_context, WorkingStoreConflict
    from app.services.v2_scheduling_service import approve_schedule
    from app.services.v2_scheduling_swap_service import change_assignments
    from app.services.blind_count_service import save_draft, submit_round
    Session, actor, ids, _ = scheduling_db
    day=datetime.now(timezone.utc).astimezone(__import__('zoneinfo').ZoneInfo('America/Los_Angeles')).date()
    with Session() as db:
        user=m.Principal(username='count-worker',password_hash='unused',role=m.PrincipalRole.STORE,store_id=ids['north'],active=True)
        campaign=m.Campaign(label='Preserved inventory');db.add_all([user,campaign]);db.flush()
        db.get(m.Employee,ids['alex']).principal_id=user.id
        employee=Principal(user.id,user.username,Role.STORE,ids['north'],True)
        period, rows=ready_period(db,actor,ids,day=day,two=True)
        args=dict(principal=actor,schedule_period_id=period.id,expected_version=period.version,allowed_store_ids=(ids['north'],ids['south']))
        approve_schedule(db,**args);publish_schedule(db,**args)
        count=m.CountSession(store_id=ids['north'],campaign_id=campaign.id,employee_name='Count Worker',created_by_principal_id=user.id)
        db.add(count);db.flush()
        db.add(m.SnapshotLine(session_id=count.id,variation_id='V1',item_name='Product',variation_name='Test'));db.flush()
        saved=save_draft(db,principal=employee,session_id=count.id,revision=0,changes={'V1':{'front_qty':'2','back_qty':'3'}},operation_id='before-swap')
        # Explicit historical test instant keeps the shift uncompleted regardless of wall-clock hour.
        now=datetime.combine(day,time(16),timezone.utc)
        change_assignments(db,principal=actor,shift_id=rows[0].id,other_shift_id=rows[1].id,kind='STORE',reason='Actual store changed',
            expected_versions={str(period.id):period.version},allowed_store_ids=(ids['north'],ids['south']),now=now)
        provider=Mock()
        with pytest.raises(WorkingStoreConflict):
            submit_round(db,principal=employee,session_id=count.id,revision=saved['revision'],provider=provider)
        provider.fetch_current_on_hand.assert_not_called()
        assert count.store_id == ids['north'] and not count.observation_closed
        assert db.get(m.Entry,(count.id,'V1')).counted_qty == Decimal('5')


def test_lead_http_review_approve_publish_without_automation_administration(primary):
    from datetime import datetime, timezone
    from app.services.v2_scheduling_lead_duty_service import business_today
    primary.account('LEAD')
    with primary.Session() as db:
        day = business_today(db)
        period, rows = ready_period(db, primary.manager, primary.ids, day=day)
        period.generated_at = datetime.now(timezone.utc)
        period_id, version = period.id, period.version
        db.commit()
    assert login(primary).status_code == 303
    client = primary.client
    assert client.get('/v2/scheduling/automation').status_code == 200
    assert 'Save automation settings' not in client.get('/v2/scheduling/automation').text
    assert client.get(f'/v2/scheduling/periods/{period_id}/review').status_code == 303
    headers = {'X-CSRF-Token':client.cookies.get('csrf_token')}
    assert client.post(f'/v2/scheduling/api/periods/{period_id}/approve',json={'expected_version':version}).status_code == 403
    assert client.post('/v2/scheduling/api/automation/run',headers=headers).status_code == 403
    assert client.post(f'/v2/scheduling/api/periods/{period_id}/publish',json={'expected_version':version},headers=headers).status_code == 422
    approved = client.post(f'/v2/scheduling/api/periods/{period_id}/approve',json={'expected_version':version},headers=headers)
    assert approved.status_code == 200, approved.text
    published = client.post(f'/v2/scheduling/api/periods/{period_id}/publish',json={'expected_version':version},headers=headers)
    assert published.status_code == 200, published.text
    assert client.get('/v2/scheduling/my-schedule').status_code == 200
    assert '9:00 AM' in client.get('/v2/scheduling/my-schedule').text
    history = client.get(f'/v2/scheduling/periods/{period_id}/history')
    assert history.status_code == 200 and 'SCHEDULE_APPROVED' in history.text and 'SCHEDULE_PUBLISHED' in history.text


def test_logged_in_employee_stale_inventory_context_and_hidden_emergency_selection(primary):
    from datetime import datetime, timezone
    from app.services.v2_scheduling_lead_duty_service import business_today
    from app.services.v2_scheduling_service import approve_schedule
    from app.services.v2_scheduling_swap_service import change_assignments
    primary.account()
    with primary.Session() as db:
        day=business_today(db)
        period,rows=ready_period(db,primary.manager,primary.ids,day=day,two=True)
        for shift in rows: shift.start_time=time(0);shift.end_time=time(23,59)
        db.flush()
        args=dict(principal=primary.manager,schedule_period_id=period.id,expected_version=period.version,
                  allowed_store_ids=(primary.ids['north'],primary.ids['south']))
        approve_schedule(db,**args);publish_schedule(db,**args)
        count=m.CountSession(store_id=primary.ids['north'],campaign_id=db.scalar(select(m.Campaign.id)),
            employee_name='Preserved Draft',created_by_principal_id=primary.ids['manager'])
        db.add(count);db.flush();count_id=count.id;pid=period.id;shift_ids=[s.id for s in rows];version=period.version
        db.commit()
    login(primary)
    client=primary.client
    initial=client.get('/session-status').json()
    assert initial['store_id']==primary.ids['north']
    assert client.get('/store/emergency-store').status_code==404
    assert client.get(f'/store/sessions/{count_id}').status_code==200
    with primary.Session() as db:
        change_assignments(db,principal=primary.manager,shift_id=shift_ids[0],other_shift_id=shift_ids[1],kind='STORE',
            reason='Same-day authorized swap',expected_versions={str(pid):version},
            allowed_store_ids=(primary.ids['north'],primary.ids['south']))
        db.commit()
    after=client.get('/session-status').json()
    assert after['store_id']==primary.ids['south'] and after['working_context']!=initial['working_context']
    headers={'X-CSRF-Token':client.cookies.get('csrf_token'),'X-Working-Context':initial['working_context']}
    assert client.post(f'/store/sessions/{count_id}/submit',json={'revision':0},headers=headers).status_code==409
    assert client.get(f'/store/sessions/{count_id}').status_code==403
    assert client.get('/store/daily-count').status_code==200
    with primary.Session() as db:
        assert db.get(m.CountSession,count_id).store_id==primary.ids['north']
        assert not db.get(m.CountSession,count_id).observation_closed


def test_employee_preference_request_http_and_admin_only_decision(primary):
    from app.services.v2_scheduling_lead_duty_service import business_today
    primary.account()
    login(primary)
    client=primary.client
    response=client.get('/v2/scheduling/preference-requests')
    assert response.status_code==200 and 'Request and decision history' in response.text
    with primary.Session() as db: today=business_today(db)
    response=client.post('/v2/scheduling/preference-requests',data={
        'csrf_token':client.cookies.get('csrf_token'),'start_date':str(today),'work_days':'1'})
    assert response.status_code==303
    with primary.Session() as db:
        row=db.scalar(select(m.SchedulingPreferenceRequest));rid=row.id
        assert row.status=='PENDING'
    assert client.post(f'/v2/scheduling/preference-requests/{rid}/decision',data={
        'csrf_token':client.cookies.get('csrf_token'),'decision':'APPROVED','reason':'Not permitted'}).status_code==403


def test_concurrent_store_swaps_cannot_apply_stale_versions(scheduling_db):
    from datetime import datetime, timezone
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from app.services.v2_scheduling_service import approve_schedule, SchedulingConflict
    from app.services.v2_scheduling_swap_service import change_assignments
    Session, actor, ids, _=scheduling_db
    with Session() as db:
        period,rows=ready_period(db,actor,ids,two=True)
        args=dict(principal=actor,schedule_period_id=period.id,expected_version=period.version,allowed_store_ids=(ids['north'],ids['south']))
        approve_schedule(db,**args);publish_schedule(db,**args)
        pid,version=period.id,period.version;first,second=[s.id for s in rows];db.commit()
    barrier=Barrier(2)
    def swap():
        with Session() as db:
            barrier.wait()
            try:
                change_assignments(db,principal=actor,shift_id=first,other_shift_id=second,kind='STORE',reason='Concurrent swap',
                    expected_versions={str(pid):version},allowed_store_ids=(ids['north'],ids['south']),now=datetime(2026,10,20,17,tzinfo=timezone.utc))
                db.commit();return 'applied'
            except SchedulingConflict:
                db.rollback();return 'stale'
    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes=list(workers.map(lambda _:swap(),range(2)))
    assert sorted(outcomes)==['applied','stale']
    with Session() as db:
        assert len(list(db.scalars(select(m.SchedulePeriod))))==2
        assert len(list(db.scalars(select(m.SchedulePeriod).where(m.SchedulePeriod.status==m.SchedulePeriodStatus.PUBLISHED))))==1


def test_inventory_submit_racing_store_swap_has_one_valid_order(scheduling_db):
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from unittest.mock import Mock
    from app.auth import Principal,Role
    from app.services.v2_scheduling_service import approve_schedule
    from app.services.v2_scheduling_swap_service import change_assignments
    from app.services.inventory_working_store_service import WorkingStoreConflict
    from app.services.blind_count_service import save_draft,submit_round
    Session,actor,ids,_=scheduling_db
    today=datetime.now(ZoneInfo('America/Los_Angeles')).date()
    with Session() as db:
        user=m.Principal(username='race-worker',password_hash='unused',role=m.PrincipalRole.STORE,store_id=ids['north'],active=True)
        campaign=m.Campaign(label='Race fixture');db.add_all([user,campaign]);db.flush()
        db.get(m.Employee,ids['alex']).principal_id=user.id
        employee=Principal(user.id,user.username,Role.STORE,ids['north'],True)
        period,rows=ready_period(db,actor,ids,day=today,two=True)
        for s in rows:s.start_time=time(0);s.end_time=time(23,59)
        db.flush();args=dict(principal=actor,schedule_period_id=period.id,expected_version=period.version,allowed_store_ids=(ids['north'],ids['south']))
        approve_schedule(db,**args);publish_schedule(db,**args)
        count=m.CountSession(store_id=ids['north'],campaign_id=campaign.id,employee_name='Race worker',created_by_principal_id=user.id)
        db.add(count);db.flush();db.add(m.SnapshotLine(session_id=count.id,variation_id='RACE',item_name='Synthetic',variation_name='Test'));db.flush()
        revision=save_draft(db,principal=employee,session_id=count.id,revision=0,changes={'RACE':{'front_qty':'2','back_qty':'3'}},operation_id='seed')['revision']
        cid=count.id;pid=period.id;version=period.version;first,second=[s.id for s in rows];db.commit()
    barrier=Barrier(2);provider=Mock();provider.fetch_current_on_hand.return_value={'RACE':Decimal('5')}
    def submit():
        with Session() as db:
            barrier.wait()
            try:
                submit_round(db,principal=employee,session_id=cid,revision=revision,provider=provider)
                db.commit();return 'submitted_before_swap'
            except WorkingStoreConflict:
                db.rollback();return 'rejected_after_swap'
    def swap():
        with Session() as db:
            barrier.wait()
            change_assignments(db,principal=actor,shift_id=first,other_shift_id=second,kind='STORE',reason='Concurrent store change',
                expected_versions={str(pid):version},allowed_store_ids=(ids['north'],ids['south']))
            db.commit()
    with ThreadPoolExecutor(max_workers=2) as workers:
        future=workers.submit(submit);swapped=workers.submit(swap)
        outcome=future.result(timeout=30);swapped.result(timeout=30)
    with Session() as db:
        count=db.get(m.CountSession,cid)
        assert count.store_id==ids['north']
        assert count.observation_closed == (outcome=='submitted_before_swap')
        assert provider.fetch_current_on_hand.call_count == int(outcome=='submitted_before_swap')


def test_stale_login_cookie_does_not_replace_fresh_authenticated_session(primary):
    from app.config import settings
    primary.account()
    primary.client.cookies.set(settings.session_cookie_name, 'expired-preview-session')
    response=login(primary)
    assert response.status_code==303
    assert primary.client.get('/v2/scheduling/my-schedule').status_code==200
    assert primary.client.get('/session-status').json()['authenticated'] is True


@pytest.mark.parametrize('exchange', [False, True])
def test_shift_changes_preserve_published_source_and_joint_assignment(scheduling_db, exchange):
    from datetime import datetime, timezone
    from app.services.v2_scheduling_service import approve_schedule
    from app.services.v2_scheduling_swap_service import change_assignments
    from app.services.v2_scheduling_policy_service import effective_assignment_rows
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        period, rows = ready_period(db, actor, ids, two=exchange)
        args = dict(principal=actor, schedule_period_id=period.id, expected_version=period.version,
                    allowed_store_ids=(ids['north'], ids['south']))
        approve_schedule(db, **args); publish_schedule(db, **args)
        result = change_assignments(db, principal=actor, shift_id=rows[0].id,
            other_shift_id=rows[1].id if exchange else None,
            replacement_employee_id=None if exchange else ids['blair'],
            kind='SHIFT', reason='Lead attested assignment change',
            expected_versions={str(period.id): period.version},
            allowed_store_ids=(ids['north'], ids['south']),
            now=datetime(2026,10,20,17,tzinfo=timezone.utc))
        assert rows[0].employee_id == ids['alex'] and period.status == m.SchedulePeriodStatus.ARCHIVED
        current = list(db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id.in_(result['period_ids'])).order_by(m.ScheduleShift.id)))
        by_source={s.source_shift_id:s for s in current}
        assert by_source[rows[0].id].employee_id==ids['blair']
        if exchange: assert by_source[rows[1].id].employee_id==ids['alex']
        effective = effective_assignment_rows(db, employee_id=ids['blair'])
        assert len(effective) == 1 and effective[0].id == by_source[rows[0].id].id
        assert len(list(db.scalars(select(m.ScheduleApproval)))) == 2


def test_previous_week_template_source_keeps_both_historical_fairness_facts(scheduling_db):
    from app.services.v2_scheduling_service import approve_schedule
    from app.services.v2_scheduling_policy_service import _longview_historical_credit_rows
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        previous = None
        for day in (date(2026,9,1),date(2026,9,8)):
            period, rows = ready_period(db, actor, ids, day=day)
            if previous: rows[0].source_shift_id = previous.id
            args=dict(principal=actor,schedule_period_id=period.id,expected_version=period.version,
                      allowed_store_ids=(ids['north'],ids['south']))
            approve_schedule(db,**args);publish_schedule(db,**args)
            previous=rows[0]
        facts=_longview_historical_credit_rows(db,store_id=ids['north'],before_date=date(2026,10,1))
        assert len(facts)==2
        assert all(f['credited_employee_ids']==(ids['alex'],) for f in facts)


def test_temporary_preference_restores_permanent_and_revocation_preserves_history(scheduling_db):
    from app.services.v2_scheduling_preference_request_service import submit_request,decide_request,approved_preferences
    from app.auth import Principal,Role
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        user=m.Principal(username='preference-overlay',password_hash='unused',role=m.PrincipalRole.STORE,store_id=ids['north'],active=True)
        db.add(user);db.flush();db.get(m.Employee,ids['alex']).principal_id=user.id
        person=Principal(user.id,user.username,Role.STORE,ids['north'],True)
        admin=Principal(actor.id,actor.username,Role.ADMIN,None,True)
        for end,stores in [(None,[ids['north']]),(date(2026,10,25),[ids['south']])]:
            row=submit_request(db,principal=person,start_date=date(2026,10,18),end_date=end,preferences={'preferred_stores':stores})
            decide_request(db,principal=admin,request_id=row.id,decision='APPROVED',reason='Approved dates')
        assert approved_preferences(db,employee_id=ids['alex'],on_date=date(2026,10,20))['preferred_stores']==[ids['south']]
        assert approved_preferences(db,employee_id=ids['alex'],on_date=date(2026,10,26))['preferred_stores']==[ids['north']]
        decide_request(db,principal=admin,request_id=row.id,decision='REVOKED',reason='Early end',today=date(2026,10,22))
        assert approved_preferences(db,employee_id=ids['alex'],on_date=date(2026,10,20))['preferred_stores']==[ids['south']]
        assert approved_preferences(db,employee_id=ids['alex'],on_date=date(2026,10,22))['preferred_stores']==[ids['north']]
        assert row.end_date==date(2026,10,25)


def test_prebeta_migration_roundtrip_and_refusal_to_discard_evidence(scheduling_db):
    from alembic import command
    from app.schema_contract import _alembic_config,current_revision
    Session,actor,ids,engine=scheduling_db
    config=_alembic_config(engine.url.render_as_string(hide_password=False))
    with Session() as db:
        from app.services.v2_scheduling_policy_service import organization_policy
        policy=organization_policy(db,principal_id=actor.id);policy.schedule_length_weeks=8;policy.replenish_at_weeks=7;db.commit()
    command.downgrade(config,'20260930_0031')
    assert current_revision(engine)=='20260930_0031'
    command.upgrade(config,'head')
    with Session() as db:
        policy=db.scalar(select(m.SchedulingOrganizationPolicy))
        assert policy.schedule_length_weeks==8 and policy.replenish_at_weeks==7
        db.add(m.ScheduleGenerationRun(actor_principal_id=actor.id,origin='REHEARSAL',status='FAILED',result={'error_type':'TestFailure'}));db.commit()
    with pytest.raises(RuntimeError,match='evidence must be preserved'):
        command.downgrade(config,'20260930_0031')
    assert current_revision(engine)=='20261013_0032'


def test_direct_replacement_does_not_inherit_original_callout(scheduling_db):
    from datetime import datetime, timezone
    from app.services.v2_scheduling_service import approve_schedule
    from app.services.v2_scheduling_exception_service import record_exception,expected_employee_id
    from app.services.v2_scheduling_swap_service import change_assignments
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        period,rows=ready_period(db,actor,ids)
        args=dict(principal=actor,schedule_period_id=period.id,expected_version=period.version,allowed_store_ids=(ids['north'],ids['south']))
        approve_schedule(db,**args);publish_schedule(db,**args)
        now=datetime(2026,10,19,18,tzinfo=timezone.utc)
        record_exception(db,principal=actor,shift_id=rows[0].id,event_type=m.AttendanceEventType.CALLED_OUT,event_at=now,today=date(2026,10,19))
        assert expected_employee_id(db,rows[0]) is None
        result=change_assignments(db,principal=actor,shift_id=rows[0].id,replacement_employee_id=ids['blair'],kind='SHIFT',reason='Assign confirmed replacement',expected_versions={str(period.id):period.version},allowed_store_ids=(ids['north'],ids['south']),now=now)
        replacement=db.scalar(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id.in_(result['period_ids'])))
        assert expected_employee_id(db,replacement)==ids['blair']
        assert expected_employee_id(db,rows[0]) is None
        assert len(list(db.scalars(select(m.ScheduleAttendanceEvent))))==1


def test_publication_keeps_only_documented_assignment_specific_availability_override(scheduling_db):
    from app.services.v2_scheduling_rules_service import create_scheduling_window
    from app.services.v2_scheduling_service import approve_schedule
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        period=create_draft_period(db,principal=actor,week_start=date(2026,10,18))
        create_scheduling_window(db,principal=actor,employee_id=ids['alex'],day_of_week=2,start_time=time(8),end_time=time(18),kind=m.SchedulingWindowKind.HARD_UNAVAILABLE)
        result=create_shift(db,principal=actor,schedule_period_id=period.id,expected_version=period.version,allowed_store_ids=(ids['north'],ids['south']),values=ShiftInput(ids['alex'],ids['north'],date(2026,10,20),time(9),time(17)),allow_hard_unavailability_override=True,override_reason='Explicit manager-approved availability exception')
        create_shift(db,principal=actor,schedule_period_id=period.id,expected_version=period.version,allowed_store_ids=(ids['north'],ids['south']),values=ShiftInput(ids['blair'],ids['south'],date(2026,10,20),time(9),time(17)))
        args=dict(principal=actor,schedule_period_id=period.id,expected_version=period.version,allowed_store_ids=(ids['north'],ids['south']))
        approve_schedule(db,**args);publish_schedule(db,**args)
        assert period.status==m.SchedulePeriodStatus.PUBLISHED
        from app.services.v2_scheduling_service import clone_published_revision
        draft=clone_published_revision(db,principal=actor,published_period_id=period.id,allowed_store_ids=(ids['north'],ids['south']))
        approve_schedule(db,principal=actor,schedule_period_id=draft.id,expected_version=draft.version,allowed_store_ids=(ids['north'],ids['south']))
        copied=db.scalar(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==draft.id,m.ScheduleShift.employee_id==ids['alex']))
        copied.start_time=time(10);db.flush()
        with pytest.raises(SchedulingValidationError,match='Hard assignment'):
            approve_schedule(db,principal=actor,schedule_period_id=draft.id,expected_version=draft.version,allowed_store_ids=(ids['north'],ids['south']))
