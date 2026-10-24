"""Coverage authorizes live store work through the normal application resolver."""
from datetime import date, datetime, time, timedelta, timezone

import pytest
from sqlalchemy import select

from app import models as m
from app.auth import Principal, Role
from app.services.inventory_working_store_service import (
    WorkingStoreConflict, assert_inventory_store, inventory_principal, working_context,
)
from app.services.v2_scheduling_exception_service import (
    cancel_commitment, record_commitment, record_exception,
)
from test_v2_scheduling_foundation import scheduling_db
from test_v2_primary import primary


DAY = date(2026, 10, 20)


def _now(hour):
    return datetime(2026, 10, 20, hour + 7, tzinfo=timezone.utc)


def _scenario(db, actor, ids, *, existing=True):
    period = m.SchedulePeriod(week_start_date=date(2026, 10, 18),
        week_end_date=date(2026, 10, 24), status=m.SchedulePeriodStatus.PUBLISHED,
        published_at=_now(8), revision_number=1,
        created_by_principal_id=actor.id, updated_by_principal_id=actor.id)
    db.add(period); db.flush()
    shifts = []
    for employee, store in [('alex', 'north')] + ([('blair', 'south')] if existing else []):
        row = m.ScheduleShift(schedule_period_id=period.id, employee_id=ids[employee],
            store_id=ids[store], shift_date=DAY, start_time=time(9), end_time=time(17),
            created_by_principal_id=actor.id, updated_by_principal_id=actor.id)
        db.add(row); shifts.append(row)
    principals = {}
    for employee, store in [('alex', 'north'), ('blair', 'south')]:
        account = m.Principal(username=employee+'-coverage', role=m.PrincipalRole.STORE,
            store_id=ids[store], active=True)
        db.add(account); db.flush()
        db.get(m.Employee, ids[employee]).principal_id = account.id
        principals[employee] = Principal(account.id, account.username, Role.STORE, account.store_id, True)
    db.flush()
    record_exception(db, principal=actor, shift_id=shifts[0].id,
        event_type=m.AttendanceEventType.CALLED_OUT, event_at=_now(8), today=DAY)
    db.flush()
    return period, shifts, principals


def test_partial_coverage_uses_exact_interval_preserves_existing_work_and_role(scheduling_db):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        _, shifts, people = _scenario(db, actor, ids)
        replacement = people['blair']
        before = working_context(db, principal=replacement, now=_now(10))
        row = record_commitment(db, principal=actor, shift_id=shifts[0].id,
            employee_id=ids['blair'], start_time=time(12), end_time=time(15))
        assert working_context(db, principal=people['alex'], now=_now(13))['store_id'] is None
        outside = working_context(db, principal=replacement, now=_now(10))
        during = working_context(db, principal=replacement, now=_now(13))
        after = working_context(db, principal=replacement, now=_now(16))
        assert before['store_id'] == outside['store_id'] == after['store_id'] == ids['south']
        assert during['store_id'] == ids['north']
        assert set(during['scheduled_store_ids']) == {ids['north'], ids['south']}
        assert during['version'] != outside['version'] and during['version'] != after['version']
        # A stale/unrelated session choice does not override the new work assignment.
        assert working_context(db, principal=replacement, selected_store_id=ids['south'], now=_now(13))['store_id'] == ids['north']
        scoped, _ = inventory_principal(db, principal=replacement, now=_now(13))
        assert scoped.store_id == ids['north'] and scoped.role == Role.STORE
        assert_inventory_store(db, principal=scoped, store_id=ids['north'], now=_now(13))
        assert shifts[0].employee_id == ids['alex'] and shifts[1].employee_id == ids['blair']
        assert shifts[1].store_id == ids['south']
        assert db.get(m.Principal, replacement.id).store_id == ids['south']
        assert db.get(m.Principal, replacement.id).role == m.PrincipalRole.STORE
        assert row.schedule_shift_id == shifts[0].id


def test_cancel_and_reassign_revoke_only_the_temporary_assignment(scheduling_db):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        _, shifts, people = _scenario(db, actor, ids)
        row = record_commitment(db, principal=actor, shift_id=shifts[0].id, employee_id=ids['blair'])
        scoped, context = inventory_principal(db, principal=people['blair'], now=_now(10))
        cancel_commitment(db, principal=actor, commitment_id=row.id)
        restored = working_context(db, principal=people['blair'], now=_now(10))
        assert restored['store_id'] == ids['south'] and restored['version'] != context['version']
        with pytest.raises(WorkingStoreConflict):
            assert_inventory_store(db, principal=scoped, store_id=ids['north'], now=_now(10))
        assert working_context(db, principal=people['alex'], now=_now(10))['store_id'] is None
        first = record_commitment(db, principal=actor, shift_id=shifts[0].id, employee_id=ids['blair'])
        other = m.Employee(full_name='Casey Replacement', normalized_name='casey replacement',
            active=True, visible_to_leads=True)
        account = m.Principal(username='casey-coverage', role=m.PrincipalRole.STORE,
            store_id=ids['south'], active=True)
        db.add_all([other, account]); db.flush(); other.principal_id = account.id
        next_row = record_commitment(db, principal=actor, shift_id=shifts[0].id, employee_id=other.id)
        assert first.voided_at is not None and next_row.voided_at is None
        assert working_context(db, principal=people['blair'], now=_now(10))['store_id'] == ids['south']
        casey = Principal(account.id, account.username, Role.STORE, account.store_id, True)
        assert working_context(db, principal=casey, now=_now(10))['store_id'] == ids['north']
        assert account.store_id == ids['south']


def test_coverage_never_grants_before_or_after_its_interval(scheduling_db):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        _, shifts, people = _scenario(db, actor, ids, existing=False)
        record_commitment(db, principal=actor, shift_id=shifts[0].id, employee_id=ids['blair'],
            start_time=time(12), end_time=time(15))
        for hour in (10, 16):
            assert working_context(db, principal=people['blair'], selected_store_id=ids['north'], now=_now(hour))['store_id'] is None
            with pytest.raises(WorkingStoreConflict):
                inventory_principal(db, principal=people['blair'], now=_now(hour))
        assert working_context(db, principal=people['blair'], now=_now(12))['store_id'] == ids['north']
        assert working_context(db, principal=people['blair'], now=_now(15))['store_id'] is None


def test_superseded_published_revision_and_inactive_store_cannot_grant_coverage(scheduling_db):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        period, shifts, people = _scenario(db, actor, ids)
        record_commitment(db, principal=actor, shift_id=shifts[0].id, employee_id=ids['blair'])
        assert working_context(db, principal=people['blair'], now=_now(10))['store_id'] == ids['north']
        db.get(m.Store, ids['north']).active = False; db.flush()
        assert working_context(db, principal=people['blair'], now=_now(10))['store_id'] == ids['south']
        db.get(m.Store, ids['north']).active = True
        period.status = m.SchedulePeriodStatus.ARCHIVED
        successor = m.SchedulePeriod(week_start_date=period.week_start_date,
            week_end_date=period.week_end_date, status=m.SchedulePeriodStatus.PUBLISHED,
            revision_number=2, published_at=_now(9),
            created_by_principal_id=actor.id, updated_by_principal_id=actor.id)
        db.add(successor); db.flush()
        db.add(m.ScheduleShift(schedule_period_id=successor.id, employee_id=ids['blair'],
            store_id=ids['south'], shift_date=DAY, start_time=time(9), end_time=time(17),
            created_by_principal_id=actor.id, updated_by_principal_id=actor.id)); db.flush()
        assert working_context(db, principal=people['blair'], now=_now(10))['store_id'] == ids['south']
        assert db.scalar(select(m.ScheduleCoverageCommitment)).voided_at is None


def test_lead_save_refreshes_logged_in_covering_employee_and_revokes_stale_work(primary):
    """One ordinary Save drives cards, hours and actual store-work authorization."""
    import re
    from fastapi.testclient import TestClient
    from app.config import settings
    from app.security.passwords import hash_password
    from test_callout_coverage_http import coverage_case, save
    from test_v2_primary import PASSWORD, login, post

    lead_id = primary.account('LEAD')
    sid, day, week = coverage_case(primary, today=True)
    with primary.Session() as db:
        source = db.get(m.ScheduleShift, sid)
        existing = m.ScheduleShift(schedule_period_id=source.schedule_period_id,
            employee_id=primary.ids['blair'], store_id=primary.ids['south'], shift_date=day,
            start_time=time(0), end_time=time(23, 59), unpaid_break_minutes=0,
            created_by_principal_id=primary.manager.id, updated_by_principal_id=primary.manager.id)
        account = m.Principal(username='cameron@example.test', role=m.PrincipalRole.STORE,
            store_id=primary.ids['south'], active=True, password_hash=hash_password(PASSWORD))
        other = m.Employee(full_name='Casey Relief', normalized_name='casey relief', active=True)
        db.add_all([existing, account, other]); db.flush()
        db.get(m.Employee, primary.ids['blair']).principal_id = account.id
        cameron_id, existing_id, casey_id = account.id, existing.id, other.id
        db.commit()
    assert login(primary).status_code == 303
    with TestClient(primary.app, follow_redirects=False, client=('127.0.0.1', 50001)) as cameron:
        cameron.get('/login')
        assert post(cameron, '/login', username='cameron@example.test', password=PASSWORD).status_code == 303
        session_token = cameron.cookies.get(settings.session_cookie_name)
        before = cameron.get('/session-status').json()
        assert before['store_id'] == primary.ids['south']
        assert 'Location: Andresen' in cameron.get('/store/daily-chore-sheet').text

        result = save(primary, sid, commitment_employee_id=primary.ids['blair'])
        assert result.status_code == 201, result.text
        rows = result.json()['board']['shifts']
        original = next(row for row in rows if row['id'] == sid)
        relief = next(row for row in rows if row.get('is_coverage'))
        assert original['employee_id'] == primary.ids['alex']
        assert relief['employee_id'] == primary.ids['blair'] and relief['source_shift_id'] == sid
        assert 'OVERLAPPING_SHIFT' in {warning['code'] for warning in result.json()['coverage_warnings']}
        current = cameron.get('/session-status').json()
        assert current['store_id'] == primary.ids['north'] and current['working_context'] != before['working_context']
        assert cameron.cookies.get(settings.session_cookie_name) == session_token
        schedule = cameron.get('/v2/scheduling/my-schedule', params={'start': str(week)})
        assert schedule.status_code == 200
        assert f'id="company-shift-{sid}"' in schedule.text and 'shift-card-exception' in schedule.text
        assert 'company-schedule__shift--coverage' in schedule.text and 'Covering: Micah' in schedule.text
        assert 'Your effective scheduled hours: 47.97' in schedule.text
        page = cameron.get('/store/daily-chore-sheet')
        assert page.status_code == 200 and 'Location: Longview' in page.text
        sheet_id = int(re.search(r'/daily-chore-sheet/(\d+)/save', page.text).group(1))
        path = f'/store/daily-chore-sheet/{sheet_id}/save'
        assert post(cameron, path, working_context=current['working_context'], employee_name='Cameron').status_code == 303
        assert cameron.get('/store/daily-count').status_code == 200
        assert cameron.get('/management/sessions').status_code == 403
        assert cameron.get('/management/users').status_code == 403
        with primary.Session() as db:
            assert db.get(m.DailyChoreSheet, sheet_id).store_id == primary.ids['north']
            audit = db.scalar(select(m.AuditLog).where(m.AuditLog.action == 'DAILY_CHORE_SHEET_SAVED'))
            assert audit.actor_principal_id == cameron_id
            assert db.get(m.Principal, cameron_id).store_id == primary.ids['south']
            assert db.get(m.Principal, cameron_id).role == m.PrincipalRole.STORE
            assert db.get(m.ScheduleShift, existing_id).store_id == primary.ids['south']

        reassigned = save(primary, sid, commitment_employee_id=casey_id)
        assert reassigned.status_code == 201, reassigned.text
        restored = cameron.get('/session-status').json()
        assert restored['store_id'] == primary.ids['south'] and restored['working_context'] != current['working_context']
        assert post(cameron, path, working_context=current['working_context'], employee_name='Stale').status_code == 409
        assert post(cameron, path, working_context=restored['working_context'], employee_name='Wrong store').status_code == 403
        assert f'id="company-shift-{relief["id"]}"' not in cameron.get('/v2/scheduling/my-schedule', params={'start': str(week)}).text
        assert 'Location: Andresen' in cameron.get('/store/daily-chore-sheet').text

        # The same logged-in session also loses access on a direct cancellation.
        reassigned_back = save(primary, sid, commitment_employee_id=primary.ids['blair'])
        assert reassigned_back.status_code == 201, reassigned_back.text
        covered_again = cameron.get('/session-status').json()
        assert covered_again['store_id'] == primary.ids['north']
        with primary.Session() as db:
            commitment_id = db.scalar(select(m.ScheduleCoverageCommitment.id).where(m.ScheduleCoverageCommitment.voided_at.is_(None)))
        canceled = primary.client.post(f'/v2/scheduling/api/coverage-commitments/{commitment_id}/void',
            json={}, headers={'X-CSRF-Token': primary.client.cookies.get('csrf_token')})
        assert canceled.status_code == 200, canceled.text
        final = cameron.get('/session-status').json()
        assert final['store_id'] == primary.ids['south'] and final['working_context'] != covered_again['working_context']
        assert post(cameron, path, working_context=covered_again['working_context'], employee_name='Stale again').status_code == 409
        final_schedule = cameron.get('/v2/scheduling/my-schedule', params={'start': str(week)})
        assert 'company-schedule__shift--coverage' not in final_schedule.text
        assert 'Your effective scheduled hours: 23.98' in final_schedule.text
        assert cameron.cookies.get(settings.session_cookie_name) == session_token
        with primary.Session() as db:
            assert db.get(m.ScheduleShift, sid).employee_id == primary.ids['alex']
            assert db.get(m.ScheduleShift, existing_id).employee_id == primary.ids['blair']
            assert db.get(m.DailyChoreSheet, sheet_id).employee_name == 'Cameron'
            commitments = list(db.scalars(select(m.ScheduleCoverageCommitment)))
            assert len(commitments) == 3 and all(row.voided_at is not None for row in commitments)
            assert all(row.recorded_by_principal_id == lead_id and row.note == '' for row in commitments)


@pytest.mark.parametrize('lifecycle', ['eligibility_period', 'legacy_cutoff'])
def test_employment_end_revokes_existing_coverage_without_deactivating_account(scheduling_db, lifecycle):
    from app.services.v2_scheduling_lifecycle_service import change_eligibility
    from app.services.v2_scheduling_roster_service import set_last_effective_date
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        _, shifts, people = _scenario(db, actor, ids, existing=False)
        record_commitment(db, principal=actor, shift_id=shifts[0].id, employee_id=ids['blair'])
        scoped, before = inventory_principal(db, principal=people['blair'], now=_now(10))
        assert before['store_id'] == ids['north']
        last_day = DAY - timedelta(days=1)
        if lifecycle == 'eligibility_period':
            change_eligibility(db, principal=actor, employee_id=ids['blair'], action='end', day=last_day, today=DAY)
        else:
            set_last_effective_date(db, principal=actor, employee_id=ids['blair'], last_effective_date=last_day)
        db.flush()
        after = working_context(db, principal=people['blair'], selected_store_id=ids['north'], now=_now(10))
        assert after['store_id'] is None and after['version'] != before['version']
        with pytest.raises(WorkingStoreConflict):
            inventory_principal(db, principal=people['blair'], selected_store_id=ids['north'], now=_now(10))
        with pytest.raises(WorkingStoreConflict):
            assert_inventory_store(db, principal=scoped, store_id=ids['north'], now=_now(10))
        assert db.get(m.Employee, ids['blair']).active
        assert db.get(m.Principal, people['blair'].id).active
        assert db.get(m.Principal, people['blair'].id).store_id == ids['south']
        assert db.scalar(select(m.ScheduleCoverageCommitment)).voided_at is None
        assert db.get(m.ScheduleShift, shifts[0].id).employee_id == ids['alex']
