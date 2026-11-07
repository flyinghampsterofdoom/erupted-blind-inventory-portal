"""Real, authenticated Board mutations against disposable PostgreSQL schedules."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta
from threading import Barrier
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app import models as m
from app.auth import Principal, Role
from app.routers import v2_scheduling as routes
from app.services.scheduling_operational_exception_service import authorized_assignment_codes
from app.services.v2_scheduling_assignments_service import update_store_defaults
from app.services.v2_scheduling_policy_service import regenerate_period
from app.services.v2_scheduling_service import lock_scheduling
from test_v2_employee_profiles import _admin, employee_client
from test_v2_scheduling_foundation import opening_hours, scheduling_db
from test_v2_scheduling_repair import period, person, shift


@pytest.fixture
def board(employee_client):
    client, current, Session, ids = employee_client
    today = datetime.now(ZoneInfo('America/Los_Angeles')).date()
    day = today + timedelta(days=7 - (today.weekday() + 1) % 7)
    with Session() as db:
        owner = current['principal'] = _admin(db)
        lead_account = m.Principal(username='board-lead', password_hash='unused',
            role=m.PrincipalRole.LEAD, active=True)
        db.add(lead_account)
        first = person(db, owner, 'First board employee', lead=True, target=2)
        second = person(db, owner, 'Second board employee', lead=True, target=2)
        lead = Principal(lead_account.id, lead_account.username, Role.LEAD, None, True)
        db.get(m.Store, ids['north']).name = 'Andresen'
        for weekday in range(7):
            opening_hours(db, owner, ids['north'], weekday)
        update_store_defaults(db, principal=owner, store_id=None, standard_shift_start=time(8, 45),
            standard_shift_end=time(22), training_hours=(time(10), time(14), 30, ids['north']))
        draft = period(db, owner, day)
        ids.update(first=first.id, second=second.id, period=draft.id)
        db.commit()
    csrf = {'X-CSRF-Token': client.cookies.get('csrf_token'), 'X-Forwarded-For': '127.0.0.1'}

    def edit(action='create', *, when=day, employee=ids['first'], store=ids['north'],
             version=1, shift_id=None, training=False, period_id=ids['period'], undo_audit_id=None):
        return client.post(f'/v2/scheduling/api/periods/{period_id}/board-assignment', headers=csrf,
            json=dict(action=action, employee_id=employee, store_id=store, shift_date=str(when),
                shift_id=shift_id, training=training, expected_version=version, undo_audit_id=undo_audit_id))

    return edit, client, current, Session, ids, day, owner, lead


def test_palette_create_move_both_axes_and_remove(board):
    edit, client, _, Session, ids, day, _, _ = board
    first = edit()
    assert first.status_code == 200, first.json()
    state = first.json()
    assignment = state['shift']
    assert (assignment['start_time'], assignment['end_time'], assignment['paid_hours']) == ('08:45', '22:00', 13.25)
    assert assignment['manually_locked']
    pk = assignment['id']
    version = state['period_version']
    for employee, when in [(ids['second'], day), (ids['second'], day+timedelta(days=1)),
                           (ids['first'], day+timedelta(days=2))]:
        moved = edit('move', employee=employee, when=when, version=version, shift_id=pk)
        assert moved.status_code == 200, moved.json()
        result = moved.json()
        assert (result['shift']['id'], result['shift']['employee_id'], result['shift']['shift_date']) == (pk, employee, str(when))
        assert result['summary']['assigned_hours'] == 13.25
        assert len(result['board']['shifts']) == 1
        version = result['period_version']
    fresh = client.get(f'/v2/scheduling/api/board?start={day}').json()
    assert fresh['shifts'][0]['shift_date'] == str(day+timedelta(days=2))
    removed = edit('remove', version=version, shift_id=pk).json()
    assert removed['summary']['assigned_hours'] == 0
    with Session() as db:
        vacancy = db.get(m.ScheduleShift, pk)
        assert vacancy.employee_id is None and vacancy.manually_locked
        assert db.get(m.SchedulePeriod, ids['period']).version == version+1
    undo = removed['undo']['audit_id']
    restored = edit('undo', version=version+1, undo_audit_id=undo)
    assert restored.status_code == 200, restored.json()
    assert restored.json()['shift']['id'] == pk
    assert restored.json()['summary']['assigned_hours'] == 13.25
    assert edit('undo', version=version+1, undo_audit_id=undo).status_code == 409
    assert edit('undo', version=version+2, undo_audit_id=undo).status_code == 422


def test_training_is_paid_without_counting_toward_staffing_and_moves(board):
    edit, _, _, Session, ids, day, _, _ = board
    response = edit(training=True, store=None)
    assert response.status_code == 200, response.json()
    result = response.json()
    card = result['shift']
    assert card['is_training'] and not card['is_lead_of_day']
    assert card['store_id'] == ids['north'] and card['paid_hours'] == 3.5
    assert result['summary']['assigned_hours'] == 3.5
    assert not any(row['assigned_shift_count'] for row in result['summary']['stores'])
    pk = card['id']
    moved = edit('move', when=day+timedelta(days=1), employee=ids['second'], version=2, shift_id=pk)
    assert moved.status_code == 200, moved.json()
    assert moved.json()['shift']['is_training'] and moved.json()['shift']['paid_hours'] == 3.5
    removed = edit('remove', shift_id=pk, version=3)
    assert removed.status_code == 200
    with Session() as db:
        assert db.get(m.ScheduleShift, pk) is None


def test_administrator_hard_availability_and_pto_are_audited_but_lead_is_restricted(board):
    edit, _, identity, Session, ids, day, owner, lead = board
    with Session() as db:
        db.add(m.EmployeeSchedulingWindow(employee_id=ids['first'], day_of_week=0,
            kind=m.SchedulingWindowKind.HARD_UNAVAILABLE, start_time=time(9), end_time=time(12),
            active=True, created_by_principal_id=owner.id, updated_by_principal_id=owner.id))
        pto = m.TimeOffRequest(employee_id=ids['first'], start_date=day, end_date=day, full_day=True,
            status=m.TimeOffRequestStatus.APPROVED, reason_category_id=ids['vacation'],
            created_by_principal_id=owner.id, updated_by_principal_id=owner.id)
        db.add(pto)
        db.commit()
        pto_id = pto.id
    identity['principal'] = lead
    denied = edit()
    assert denied.status_code == 403 and 'hard unavailability' in denied.json()['message']
    identity['principal'] = owner
    allowed = edit()
    assert allowed.status_code == 200, allowed.json()
    result = allowed.json()
    assert {'HARD_UNAVAILABLE', 'APPROVED_TIME_OFF'} <= {w['type'] for w in result['warnings']}
    with Session() as db:
        card = db.get(m.ScheduleShift, result['shift']['id'])
        assert authorized_assignment_codes(db, card) == {'HARD_WEEKDAY_LOCKOUT', 'APPROVED_TIME_OFF'}
        assert db.get(m.TimeOffRequest, pto_id).status == m.TimeOffRequestStatus.APPROVED


def test_occupied_stale_locked_and_rollback_leave_source_unchanged(board, monkeypatch):
    edit, _, _, Session, ids, day, _, _ = board
    first = edit().json()['shift']
    second = edit(employee=ids['second'], version=2).json()['shift']
    occupied = edit('move', employee=ids['second'], shift_id=first['id'], version=3)
    assert occupied.status_code == 422 and 'already has' in occupied.json()['message']
    assert edit('remove', shift_id=second['id'], version=2).status_code == 409
    with Session() as locked:
        lock_scheduling(locked)
        assert edit('remove', shift_id=first['id'], version=3).status_code == 423
    def fail(*args, **kwargs):
        raise SQLAlchemyError('Injected serialization failure')
    with monkeypatch.context() as patch:
        patch.setattr(routes, '_success_response', fail)
        assert edit('move', shift_id=first['id'], when=day+timedelta(days=1), version=3).status_code == 500
    with Session() as db:
        assert db.get(m.ScheduleShift, first['id']).shift_date == day
        assert db.get(m.ScheduleShift, second['id']).employee_id == ids['second']
        assert db.get(m.SchedulePeriod, ids['period']).version == 3
    with ThreadPoolExecutor(max_workers=2) as pool:
        start = Barrier(2)
        def remove(pk):
            start.wait()
            return edit('remove', shift_id=pk, version=3).status_code
        statuses = set(pool.map(remove, [first['id'], second['id']]))
    assert statuses in ({200, 409}, {200, 423})
    with Session() as db:
        assert db.get(m.SchedulePeriod, ids['period']).version == 4


def test_published_move_clones_and_does_not_notify_or_republish(board):
    edit, _, _, Session, ids, day, owner, _ = board
    next_week = day + timedelta(days=7)
    with Session() as db:
        published = period(db, owner, next_week, status='PUBLISHED')
        original = shift(db, owner, published, db.get(m.Employee, ids['first']), next_week, ids['north'])
        original.end_time, original.unpaid_break_minutes, original.employee_note = time(16), 20, 'Original custom shift'
        original.is_shift_lead = original.is_lead_of_day = True
        other = shift(db, owner, published, db.get(m.Employee, ids['second']), next_week+timedelta(days=1), ids['north'])
        other.is_lead_of_day = True
        published_id, original_id, other_id = published.id, original.id, other.id
        db.commit()
    response = edit('move', shift_id=original_id, employee=ids['first'], when=next_week+timedelta(days=1), period_id=published_id)
    assert response.status_code == 200, response.json()
    result = response.json()
    assert result['board']['period']['status'] == 'DRAFT'
    assert result['board']['period']['supersedes_schedule_period_id'] == published_id
    assert result['shift']['is_shift_lead'] and not result['shift']['is_lead_of_day']
    assert (result['shift']['end_time'], result['shift']['unpaid_break_minutes'], result['shift']['employee_note']) == ('16:00', 20, 'Original custom shift')
    with Session() as db:
        assert db.get(m.SchedulePeriod, published_id).status == m.SchedulePeriodStatus.PUBLISHED
        assert db.get(m.ScheduleShift, original_id).shift_date == next_week
        assert db.get(m.ScheduleShift, other_id).is_lead_of_day
        assert not db.scalar(select(func.count(m.SchedulingNotification.id)))
    assert edit('remove', shift_id=original_id, period_id=published_id).status_code == 409


def test_regeneration_retains_the_manual_assignment_and_training(board):
    edit, _, _, Session, ids, day, owner, _ = board
    first = edit().json()['shift']
    with Session() as db:
        from app.services.scheduling_training_service import start_training
        for name in ('alex', 'blair'):
            db.get(m.Employee, ids[name]).scheduling_active = False
        first_profile = db.scalar(select(m.EmployeeSchedulingProfile).where(
            m.EmployeeSchedulingProfile.employee_id == ids['first']))
        first_profile.target_shifts_per_week = 1
        start_training(db, principal=owner, employee_id=ids['second'], start_date=day)
        db.add(m.CoverageRequirement(store_id=ids['north'], day_of_week=0, start_time=time(9),
            end_time=time(22), minimum_employee_count=1, active=True,
            created_by_principal_id=owner.id, updated_by_principal_id=owner.id))
        db.commit()
    trained = edit(training=True, store=None, employee=ids['second'], version=2).json()['shift']
    with Session() as db:
        regenerate_period(db, principal=owner, schedule_period_id=ids['period'])
        assert db.get(m.ScheduleShift, first['id']).employee_id == ids['first']
        assert db.get(m.ScheduleShift, trained['id']).is_training
        assert set(db.scalars(select(m.ScheduleShift.id).where(
            m.ScheduleShift.schedule_period_id == ids['period']))) == {first['id'], trained['id']}


def test_training_hours_can_be_configured_without_changing_standard_shifts(board):
    edit, client, _, _, ids, day, _, _ = board
    path = '/v2/scheduling/store-defaults'
    fields = dict(csrf_token=client.cookies.get('csrf_token'), standard_shift_start='08:45',
                  standard_shift_end='22:00', training_shift_start='', training_shift_end='',
                  training_unpaid_break_minutes=0, training_default_store_id='')
    response = client.post(path, data=fields)
    assert response.status_code == 303 and 'saved' in response.headers['location']
    unconfigured = edit(training=True, store=None)
    assert unconfigured.status_code == 422
    assert 'Standard Training Shift' in unconfigured.json()['message']
    fields.update(training_shift_start='12:00', training_shift_end='16:00',
                  training_unpaid_break_minutes=15, training_default_store_id=ids['north'])
    response = client.post(path, data=fields)
    assert response.status_code == 303 and 'saved' in response.headers['location']
    training = edit(training=True, store=None)
    assert training.status_code == 200, training.json()
    assert training.json()['shift']['paid_hours'] == 3.75
    standard = edit(when=day+timedelta(days=1), version=2)
    assert standard.status_code == 200, standard.json()
    assert standard.json()['shift']['paid_hours'] == 13.25
