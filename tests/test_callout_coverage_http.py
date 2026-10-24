"""Coverage acceptance through authenticated application routes and PostgreSQL."""
from datetime import datetime, time, timedelta, timezone

import pytest
from sqlalchemy import select

from app import models as m
from app.services.v2_scheduling_lead_duty_service import business_today
from test_v2_primary import primary, login
from test_v2_scheduling_foundation import scheduling_db


def coverage_case(primary, *, today=False):
    with primary.Session() as db:
        day = business_today(db) + timedelta(days=0 if today else 1)
        week = day - timedelta(days=(day.weekday() + 1) % 7)
        db.get(m.Employee, primary.ids['alex']).full_name = 'Micah'
        db.get(m.Employee, primary.ids['blair']).full_name = 'Cameron'
        db.get(m.Store, primary.ids['north']).name = 'Longview'
        db.get(m.Store, primary.ids['south']).name = 'Andresen'
        period = m.SchedulePeriod(
            week_start_date=week, week_end_date=week + timedelta(days=6),
            status=m.SchedulePeriodStatus.PUBLISHED, revision_number=1,
            published_at=datetime.now(timezone.utc),
            created_by_principal_id=primary.manager.id, updated_by_principal_id=primary.manager.id)
        db.add(period); db.flush()
        shift = m.ScheduleShift(
            schedule_period_id=period.id, employee_id=primary.ids['alex'],
            store_id=primary.ids['north'], shift_date=day,
            start_time=time(0) if today else time(8, 45),
            end_time=time(23, 59) if today else time(22, 15), unpaid_break_minutes=0,
            created_by_principal_id=primary.manager.id, updated_by_principal_id=primary.manager.id)
        db.add(shift); db.commit()
        return shift.id, day, week


def save(primary, sid, **values):
    return primary.client.post(
        f'/v2/scheduling/api/shifts/{sid}/attendance',
        json={'event_type': 'CALLED_OUT', **values},
        headers={'X-CSRF-Token': primary.client.cookies.get('csrf_token')})


@pytest.mark.parametrize('role', ['ADMIN', 'MANAGER', 'LEAD', 'STORE'])
def test_authenticated_role_save_without_approval_or_note(primary, role):
    actor_id = primary.account(role)
    sid, day, week = coverage_case(primary)
    assert login(primary).status_code == 303
    response = save(primary, sid, commitment_employee_id=primary.ids['blair'])
    if role == 'STORE':
        assert response.status_code == 403
        with primary.Session() as db:
            assert db.scalar(select(m.ScheduleAttendanceEvent)) is None
            assert db.scalar(select(m.ScheduleCoverageCommitment)) is None
        return
    assert response.status_code == 201, response.text
    rows = response.json()['board']['shifts']
    original = next(row for row in rows if row['id'] == sid)
    coverage = next(row for row in rows if row.get('is_coverage'))
    assert original['employee_id'] == primary.ids['alex']
    assert coverage['employee_id'] == primary.ids['blair']
    assert coverage['source_shift_id'] == sid
    page = primary.client.get('/v2/scheduling/week', params={'start': str(week)})
    assert page.status_code == 200 and 'COVERAGE' in page.text and 'Micah' in page.text
    with primary.Session() as db:
        event = db.scalar(select(m.ScheduleAttendanceEvent))
        commitment = db.scalar(select(m.ScheduleCoverageCommitment))
        assert event.recorded_by_principal_id == commitment.recorded_by_principal_id == actor_id
        assert event.original_employee_id == primary.ids['alex']
        assert commitment.employee_id == primary.ids['blair'] and commitment.note == ''
        assert db.get(m.ScheduleShift, sid).employee_id == primary.ids['alex']
        assert db.scalar(select(m.AuditLog).where(
            m.AuditLog.action == 'V2:SCHEDULING:COVERAGE_COMMITTED')) is not None
    company = primary.client.get('/v2/scheduling/my-schedule', params={'start': str(week)})
    assert company.status_code == 200 and 'COVERAGE' in company.text and 'Cameron' in company.text
    own_api = primary.client.get('/v2/scheduling/api/own-schedule')
    assert own_api.status_code == 200, own_api.text
    retained = next(row for row in own_api.json()['assignments'] if row['id'] == sid)
    assert retained['original_scheduled_hours'] == 13.5
    assert retained['effective_scheduled_hours'] == 0 and not retained['transfer_eligible']


def test_neutral_reassignment_preserves_genuine_callout_after_cancellation(primary):
    from test_cascading_coverage_acceptance import _ordinary_coverage_payload

    primary.account('LEAD')
    sid, _, _ = coverage_case(primary)
    login(primary)
    assert save(primary, sid).status_code == 201
    with primary.Session() as db:
        event_id = db.scalar(select(m.ScheduleAttendanceEvent.id))
    path = f'/v2/scheduling/api/shifts/{sid}/attendance'
    headers = {'X-CSRF-Token': primary.client.cookies.get('csrf_token')}
    response = primary.client.post(path,
        json=_ordinary_coverage_payload(primary.ids['blair']), headers=headers)
    assert response.status_code == 201, response.text
    with primary.Session() as db:
        commitment_id = db.scalar(select(m.ScheduleCoverageCommitment.id))
    cancelled = primary.client.post(
        f'/v2/scheduling/api/coverage-commitments/{commitment_id}/void',
        json={}, headers=headers)
    assert cancelled.status_code == 200, cancelled.text
    with primary.Session() as db:
        from app.services.v2_scheduling_exception_service import effective_shift_segments
        events = list(db.scalars(select(m.ScheduleAttendanceEvent)))
        assert len(events) == 1 and events[0].id == event_id
        assert events[0].event_type == m.AttendanceEventType.CALLED_OUT
        assert events[0].voided_at is None
        assert effective_shift_segments(db, db.get(m.ScheduleShift, sid)) == []


def test_lead_scope_and_explicit_permission_denial(primary):
    actor = primary.account('LEAD')
    sid, _, _ = coverage_case(primary)
    login(primary)
    payload = {'event_type': 'CALLED_OUT', 'commitment_employee_id': primary.ids['blair']}
    headers = {'X-CSRF-Token': primary.client.cookies.get('csrf_token')}
    path = f'/v2/scheduling/api/shifts/{sid}/attendance'
    assert primary.client.post(path + '?store_id=' + str(primary.ids['south']),
        json=payload, headers=headers).status_code == 403
    with primary.Session() as db:
        db.add(m.PrincipalPermissionOverride(principal_id=actor,
            permission_key='scheduling.attendance.record', allowed=False)); db.commit()
    assert primary.client.post(path, json=payload, headers=headers).status_code == 403
    assert primary.client.get('/management/users').status_code == 403
    with primary.Session() as db:
        assert db.scalar(select(m.ScheduleCoverageCommitment)) is None


def test_partial_coverage_change_and_cancel_keep_history(primary):
    primary.account('LEAD')
    sid, _, _ = coverage_case(primary)
    login(primary)
    result = save(primary, sid, commitment_employee_id=primary.ids['blair'],
        coverage_start_time='12:00', coverage_end_time='17:00', coverage_break_minutes=15)
    assert result.status_code == 201, result.text
    first = next(row for row in result.json()['board']['shifts'] if row.get('is_coverage'))
    assert first['start_time'].startswith('12:00') and first['end_time'].startswith('17:00')
    with primary.Session() as db:
        replacement = m.Employee(full_name='Another replacement', normalized_name='another replacement', active=True)
        db.add(replacement); db.commit(); eid = replacement.id
    changed = save(primary, sid, commitment_employee_id=eid)
    assert changed.status_code == 201, changed.text
    assert next(row for row in changed.json()['board']['shifts'] if row.get('is_coverage'))['employee_id'] == eid
    cancelled = save(primary, sid, cancel_coverage=True)
    assert cancelled.status_code == 201, cancelled.text
    assert not any(row.get('is_coverage') for row in cancelled.json()['board']['shifts'])
    with primary.Session() as db:
        commitments = list(db.scalars(select(m.ScheduleCoverageCommitment).order_by(m.ScheduleCoverageCommitment.id)))
        assert len(commitments) == 2 and all(row.voided_at is not None for row in commitments)
        assert db.get(m.ScheduleShift, sid).employee_id == primary.ids['alex']
        assert len(list(db.scalars(select(m.ScheduleAttendanceEvent).where(
            m.ScheduleAttendanceEvent.voided_at.is_(None))))) == 1


@pytest.mark.parametrize('status', ['CALLED_OUT', 'LATE', 'LEFT_EARLY', 'NO_CALL_NO_SHOW'])
def test_four_statuses_save_and_correct_automatically(primary, status):
    primary.account('LEAD')
    sid, _, _ = coverage_case(primary, today=True)
    login(primary)
    initial = save(primary, sid)
    assert initial.status_code == 201, initial.text
    result = save(primary, sid, event_type=status)
    assert result.status_code == 201, result.text
    with primary.Session() as db:
        active = list(db.scalars(select(m.ScheduleAttendanceEvent).where(
            m.ScheduleAttendanceEvent.voided_at.is_(None))))
        assert len(active) == 1 and active[0].event_type.value == status
        assert db.get(m.ScheduleShift, sid).employee_id == primary.ids['alex']


def test_csrf_and_optional_cancellation_note(primary):
    primary.account('LEAD')
    sid, _, _ = coverage_case(primary)
    login(primary)
    path = f'/v2/scheduling/api/shifts/{sid}/attendance'
    assert primary.client.post(path, json={'event_type': 'CALLED_OUT'}).status_code == 403
    saved = save(primary, sid, commitment_employee_id=primary.ids['blair'])
    assert saved.status_code == 201, saved.text
    with primary.Session() as db:
        cid = db.scalar(select(m.ScheduleCoverageCommitment.id))
    result = primary.client.post(f'/v2/scheduling/api/coverage-commitments/{cid}/void',
        json={}, headers={'X-CSRF-Token': primary.client.cookies.get('csrf_token')})
    assert result.status_code == 200, result.text
    with primary.Session() as db:
        event_id = db.scalar(select(m.ScheduleAttendanceEvent.id))
    reversal = primary.client.post(f'/v2/scheduling/api/attendance/{event_id}/void',
        json={}, headers={'X-CSRF-Token': primary.client.cookies.get('csrf_token')})
    assert reversal.status_code == 200, reversal.text


def test_conflicts_remain_visible_and_save_preserves_pto_and_existing_shift(primary):
    primary.account('LEAD')
    sid, day, _ = coverage_case(primary)
    with primary.Session() as db:
        source = db.get(m.ScheduleShift, sid)
        existing = m.ScheduleShift(
            schedule_period_id=source.schedule_period_id, employee_id=primary.ids['blair'],
            store_id=primary.ids['south'], shift_date=day, start_time=time(9), end_time=time(17),
            created_by_principal_id=primary.manager.id, updated_by_principal_id=primary.manager.id)
        pto = m.TimeOffRequest(
            employee_id=primary.ids['blair'], start_date=day, end_date=day, full_day=True,
            status=m.TimeOffRequestStatus.APPROVED, reason_category_id=primary.ids['vacation'],
            submitted_by_principal_id=primary.manager.id, created_by_principal_id=primary.manager.id,
            updated_by_principal_id=primary.manager.id)
        profile = m.EmployeeSchedulingProfile(
            employee_id=primary.ids['blair'], maximum_weekly_hours=1, approval_weekly_hours=1,
            target_weekly_hours=1, created_by_principal_id=primary.manager.id,
            updated_by_principal_id=primary.manager.id)
        db.add_all([existing, pto, profile]); db.commit()
        existing_id, pto_id = existing.id, pto.id
    login(primary)
    response = primary.client.get(f'/v2/scheduling/api/shifts/{sid}/coverage-candidates')
    assert response.status_code == 200, response.text
    candidate = next(row for row in response.json()['candidates'] if row['id'] == primary.ids['blair'])
    codes = {row['code'] for row in candidate['warnings']}
    assert {'APPROVED_TIME_OFF', 'OVERLAPPING_SHIFT', 'MAX_WEEKLY_HOURS', 'COVERAGE_OVERTIME'} <= codes
    result = save(primary, sid, commitment_employee_id=primary.ids['blair'])
    assert result.status_code == 201, result.text
    assert {'APPROVED_TIME_OFF', 'OVERLAPPING_SHIFT'} <= {
        row['code'] for row in result.json()['coverage_warnings']}
    with primary.Session() as db:
        assert db.get(m.TimeOffRequest, pto_id).status == m.TimeOffRequestStatus.APPROVED
        assert db.get(m.ScheduleShift, existing_id).employee_id == primary.ids['blair']
        assert db.get(m.ScheduleShift, existing_id).store_id == primary.ids['south']


def test_edit_removing_last_coverage_card_returns_full_board_and_preserves_source(primary):
    """A saved draft edit must replace the prior negative card without a reload."""
    primary.account('ADMIN')
    sid, day, week = coverage_case(primary)
    assert login(primary).status_code == 303
    saved = save(primary, sid, commitment_employee_id=primary.ids['blair'])
    assert saved.status_code == 201, saved.text
    prior_coverage = next(row for row in saved.json()['board']['shifts'] if row.get('is_coverage'))
    with primary.Session() as db:
        source = db.get(m.ScheduleShift, sid)
        published_id = source.schedule_period_id
        another = m.Employee(full_name='Different scheduled employee', normalized_name='different scheduled employee', active=True)
        db.add(another); db.commit(); other_id = another.id
    headers = {'X-CSRF-Token': primary.client.cookies.get('csrf_token')}
    cloned = primary.client.post(f'/v2/scheduling/api/periods/{published_id}/clone-published?start={week}', headers=headers)
    assert cloned.status_code == 201, cloned.text
    with primary.Session() as db:
        draft = db.scalar(select(m.SchedulePeriod).where(m.SchedulePeriod.status == m.SchedulePeriodStatus.DRAFT))
        child = db.scalar(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id == draft.id))
        draft_id, child_id, version = draft.id, child.id, draft.version
    current = primary.client.get(f'/v2/scheduling/api/board?start={week}&schedule_period_id={draft_id}')
    assert current.status_code == 200
    assert any(row.get('is_coverage') for row in current.json()['shifts'])
    changed = primary.client.patch(
        f'/v2/scheduling/api/periods/{draft_id}/shifts/{child_id}?start={week}&schedule_period_id={draft_id}',
        json={'employee_id': other_id, 'store_id': primary.ids['north'], 'shift_date': str(day),
              'start_time': '08:45', 'end_time': '22:15', 'unpaid_break_minutes': 0,
              'expected_version': version},
        headers={**headers, 'X-Scheduling-Response': 'delta'})
    assert changed.status_code == 200, changed.text
    response = changed.json()
    assert response['save_outcome'] == 'local_saved' and 'board' in response and 'delta' not in response
    assert response['board']['period']['id'] == draft_id
    assert response['board']['period']['version'] == version + 1
    assert not any(row.get('is_coverage') for row in response['board']['shifts'])
    assert next(row for row in response['board']['shifts'] if row['id'] == child_id)['employee_id'] == other_id
    with primary.Session() as db:
        assert db.get(m.ScheduleShift, sid).employee_id == primary.ids['alex']
        commitment = db.get(m.ScheduleCoverageCommitment, prior_coverage['coverage_assignment_id'])
        assert commitment.schedule_shift_id == sid and commitment.voided_at is None
        assert db.scalar(select(m.ScheduleAttendanceEvent).where(
            m.ScheduleAttendanceEvent.schedule_shift_id == sid)).voided_at is None
