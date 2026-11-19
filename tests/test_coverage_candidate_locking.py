"""Slow candidate reads cannot hold the scheduling mutation lock."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from sqlalchemy import func, select

from app import models as m
from app.services import v2_scheduling_exception_service as exceptions
from app.services.v2_scheduling_policy_service import SCHEDULE_AUTOMATION_LOCK_KEY
from test_callout_coverage_http import coverage_case, save
from test_v2_primary import primary, login
from test_v2_scheduling_foundation import scheduling_db


def can_acquire(engine):
    with engine.connect() as c:
        return bool(c.scalar(select(func.pg_try_advisory_xact_lock(SCHEDULE_AUTOMATION_LOCK_KEY))))


def test_slow_http_candidate_get_does_not_hold_scheduler_lock(primary, scheduling_db, monkeypatch):
    _, _, _, engine = scheduling_db
    primary.account('ADMIN')
    sid, _, _ = coverage_case(primary)
    login(primary)
    entered, release = Event(), Event()
    original = exceptions.commitment_eligibility

    def paused(*args, **kwargs):
        entered.set()
        assert release.wait(10), 'Candidate evaluation was not released'
        return original(*args, **kwargs)

    monkeypatch.setattr(exceptions, 'commitment_eligibility', paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(primary.client.get, f'/v2/scheduling/api/shifts/{sid}/coverage-candidates')
        try:
            assert entered.wait(5), 'HTTP candidate request never reached evaluation'
            # The request has passed both the dependency and handler authorization
            # and remains open while another scheduling transaction takes the lock.
            assert can_acquire(engine), 'Read-only candidate lookup holds the scheduling mutation lock'
        finally:
            release.set()
        response = pending.result(timeout=15)
    assert response.status_code == 200, response.text
    assert response.json()['candidates']
    with primary.Session() as db:
        assert db.get(m.ScheduleShift, sid).employee_id == primary.ids['alex']
        assert db.scalar(select(m.ScheduleAttendanceEvent)) is None
        assert db.scalar(select(m.ScheduleCoverageCommitment)) is None


@pytest.mark.parametrize('denial', ['store_scope', 'permission'])
def test_candidate_get_preserves_scope_and_permission_denials(primary, denial):
    actor = primary.account('LEAD')
    sid, _, _ = coverage_case(primary)
    login(primary)
    path = f'/v2/scheduling/api/shifts/{sid}/coverage-candidates'
    if denial == 'store_scope':
        path += '?store_id=' + str(primary.ids['south'])
    else:
        with primary.Session() as db:
            db.add(m.PrincipalPermissionOverride(principal_id=actor,
                permission_key='scheduling.attendance.record', allowed=False))
            db.commit()
    assert primary.client.get(path).status_code == 403
    with primary.Session() as db:
        assert db.scalar(select(m.ScheduleAttendanceEvent)) is None
        assert db.scalar(select(m.ScheduleCoverageCommitment)) is None


def test_candidate_get_preserves_temporary_shift_lead_permission_and_revocation(primary):
    primary.account('STORE')
    sid, _, _ = coverage_case(primary, today=True)
    with primary.Session() as db:
        db.get(m.ScheduleShift, sid).is_shift_lead = True
        db.commit()
    login(primary)
    path = f'/v2/scheduling/api/shifts/{sid}/coverage-candidates'
    assert primary.client.get(path).status_code == 200
    with primary.Session() as db:
        db.get(m.ScheduleShift, sid).is_shift_lead = False
        db.commit()
    assert primary.client.get(path).status_code == 403


def test_mutation_authorization_still_locks_and_save_revalidates_changed_candidate(primary, scheduling_db, monkeypatch):
    _, _, _, engine = scheduling_db
    primary.account('ADMIN')
    sid, _, _ = coverage_case(primary)
    login(primary)
    response = primary.client.get(f'/v2/scheduling/api/shifts/{sid}/coverage-candidates')
    assert response.status_code == 200
    assert primary.ids['blair'] in {row['id'] for row in response.json()['candidates']}
    with primary.Session() as db:
        exceptions._authorize(db, primary.manager, db.get(m.ScheduleShift, sid))
        assert not can_acquire(engine), 'Mutation authorization must acquire the scheduling lock by default'
        db.rollback()
        db.get(m.Employee, primary.ids['blair']).active = False
        db.commit()
    held = []
    original = exceptions.commitment_eligibility

    def checked(*args, **kwargs):
        held.append(not can_acquire(engine))
        return original(*args, **kwargs)

    monkeypatch.setattr(exceptions, 'commitment_eligibility', checked)
    saved = save(primary, sid, commitment_employee_id=primary.ids['blair'])
    assert saved.status_code == 422, saved.text
    assert held and all(held), 'Coverage mutations must retain their scheduling lock'
    with primary.Session() as db:
        assert db.get(m.ScheduleShift, sid).employee_id == primary.ids['alex']
        assert db.scalar(select(m.ScheduleAttendanceEvent)) is None
        assert db.scalar(select(m.ScheduleCoverageCommitment)) is None
