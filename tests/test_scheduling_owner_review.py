"""Owner confirmation is explicit, transactional, and separate from generation."""
from datetime import datetime, timedelta
import re

from sqlalchemy import select

from app import models as m
from app.services import scheduling_training_service as training
from app.services.v2_scheduling_coverage_service import evaluate_schedule_warnings
from app.services.v2_scheduling_lifecycle_service import eligible_on
from app.services.v2_scheduling_policy_service import evaluate_assignment
from test_scheduling_training import NOW, START, rows, setup, shape
from test_v2_employee_profiles import _admin, _post, employee_client
from test_v2_scheduling_foundation import scheduling_db
from test_v2_scheduling_repair import shift


def _profile_token(response):
    assert response.status_code == 422
    assert 'Make Changes Anyway' in response.text and 'View affected dates' in response.text
    return re.search(r'name="owner_confirmation" value="([a-f0-9]{64})"', response.text)[1]


def _owner_schedule(employee_client, monkeypatch, *, qualified=True, existing_gap=False, published=False):
    client, current, Session, ids = employee_client
    monkeypatch.setattr(training, 'local_now', lambda db, now=None: NOW)
    from app.services import v2_scheduling_lifecycle_service as lifecycle

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)

    monkeypatch.setattr(lifecycle, 'datetime', FixedDateTime)
    with Session() as db:
        owner = _admin(db)
        current['principal'] = owner
        outgoing, incoming, _, periods = setup(db, owner, ids)
        incoming.scheduling_lead_capable = qualified
        source = periods[1]
        if published:
            source.status = m.SchedulePeriodStatus.PUBLISHED
            source.published_at = NOW
        if existing_gap:
            next(r for r in rows(db, source) if r.employee_id == outgoing.id).is_lead_of_day = False
        db.commit()
        return client, current, Session, ids, outgoing.id, incoming.id, source.id


def _audit(db):
    return db.scalars(select(m.AuditLog).where(m.AuditLog.action == 'V2:SCHEDULING:OWNER_SCHEDULING_OVERRIDE')).all()


def test_replacement_does_not_require_fixing_a_preexisting_lead_gap(employee_client, monkeypatch):
    client, _, Session, _, outgoing, incoming, period_id = _owner_schedule(
        employee_client, monkeypatch, existing_gap=True, published=True)
    with Session() as db:
        source = db.get(m.SchedulePeriod, period_id)
        before = shape(rows(db, source))
    response = _post(client, f'/v2/scheduling/employees/{incoming}/training',
        action='transition', first_schedule_date=str(START + timedelta(days=3)), replacing_employee_id=outgoing)
    assert response.status_code == 303 and 'error=' not in response.headers['location']
    with Session() as db:
        assert shape(rows(db, db.get(m.SchedulePeriod, period_id))) == before
        draft = db.scalar(select(m.SchedulePeriod).where(m.SchedulePeriod.supersedes_schedule_period_id == period_id))
        assert draft.status == m.SchedulePeriodStatus.DRAFT
        for row in rows(db, draft):
            original = before[row.source_shift_id]
            assert row.employee_id == (incoming if original[0] == outgoing else original[0])
        assert not _audit(db)


def test_replacement_confirmation_keeps_published_source_and_warns_in_draft(employee_client, monkeypatch):
    client, _, Session, _, outgoing, incoming, period_id = _owner_schedule(
        employee_client, monkeypatch, qualified=False, published=True)
    url = f'/v2/scheduling/employees/{incoming}/training'
    fields = dict(action='transition', first_schedule_date=str(START + timedelta(days=3)), replacing_employee_id=outgoing)
    preview = _post(client, url, **fields)
    token = _profile_token(preview)
    assert '3 dates without assigned lead coverage' in preview.text
    assert str(START + timedelta(days=7)) in preview.text and 'assignment #' not in preview.text
    with Session() as db:
        source = db.get(m.SchedulePeriod, period_id)
        before = shape(rows(db, source))
        assert not db.scalar(select(m.SchedulePeriod.id).where(m.SchedulePeriod.supersedes_schedule_period_id == period_id))
        assert db.get(m.Employee, incoming).first_schedule_date is None and not _audit(db)
    assert client.get(f'/v2/hr/employees/{incoming}#training').status_code == 200
    # Approval for a different submitted date cannot authorize the same warnings.
    changed = _post(client, url, **{**fields, 'first_schedule_date': str(START + timedelta(days=4))}, owner_confirmation=token)
    assert _profile_token(changed) != token
    saved = _post(client, url, **fields, owner_confirmation=token)
    assert saved.status_code == 303 and 'error=' not in saved.headers['location']
    with Session() as db:
        assert db.get(m.Employee, outgoing).active
        assert shape(rows(db, db.get(m.SchedulePeriod, period_id))) == before
        draft = db.scalar(select(m.SchedulePeriod).where(m.SchedulePeriod.supersedes_schedule_period_id == period_id))
        assert draft.status == m.SchedulePeriodStatus.DRAFT and draft.published_at is None
        for row in rows(db, draft):
            original = before[row.source_shift_id]
            assert shape([row])[row.id][1:7] == original[1:7]
            if original[0] == outgoing:
                assert row.employee_id == incoming and not row.is_lead_of_day
            else:
                assert row.employee_id == original[0] and row.is_lead_of_day == original[7]
        assert len([w for w in evaluate_schedule_warnings(db, schedule_period_id=draft.id)
                    if w.warning_type == 'NO_LEAD_OF_DAY']) == 3
        assert {w['code'] for w in _audit(db)[0].meta['metadata']['warnings']} == {'NO_LEAD_OF_DAY'}


def test_replacement_overlap_cannot_be_overridden_even_after_review(employee_client, monkeypatch):
    client, current, Session, ids, outgoing, incoming, period_id = _owner_schedule(
        employee_client, monkeypatch, qualified=False)
    url = f'/v2/scheduling/employees/{incoming}/training'
    fields = dict(action='transition', first_schedule_date=str(START + timedelta(days=3)), replacing_employee_id=outgoing)
    token = _profile_token(_post(client, url, **fields))
    with Session() as db:
        shift(db, current['principal'], db.get(m.SchedulePeriod, period_id), db.get(m.Employee, incoming),
              START + timedelta(days=7), ids['north'])
        db.commit()
    response = _post(client, url, **fields, owner_confirmation=token)
    assert response.status_code == 303 and 'overlapping' in response.headers['location']
    with Session() as db:
        assert db.get(m.Employee, incoming).first_schedule_date is None and not _audit(db)
        assert len([r for r in rows(db, db.get(m.SchedulePeriod, period_id)) if r.employee_id == outgoing]) == 3


def test_owner_eligibility_review_reports_lead_and_staffing_without_rewriting_assignments(employee_client, monkeypatch):
    client, current, Session, _, outgoing, _, period_id = _owner_schedule(
        employee_client, monkeypatch, published=True)
    owner = current['principal']
    url = f'/v2/scheduling/employees/{outgoing}/eligibility'
    fields = dict(action='end', eligible_date=str(START + timedelta(days=2)))
    with Session() as db:
        source = db.get(m.SchedulePeriod, period_id)
        before = shape(rows(db, source))
        published_version = source.version
    review = _post(client, url, **fields)
    token = _profile_token(review)
    assert '3 dates without assigned lead coverage' in review.text
    assert '3 dates below minimum staffing' in review.text
    with Session() as db:
        assert eligible_on(db.get(m.Employee, outgoing), START + timedelta(days=7)) and not _audit(db)
    saved = _post(client, url, **fields, owner_confirmation=token)
    assert saved.status_code == 303 and 'error=' not in saved.headers['location']
    with Session() as db:
        source = db.get(m.SchedulePeriod, period_id)
        assert shape(rows(db, source)) == before and source.version == published_version
        assert db.get(m.Employee, outgoing).active
        assert not eligible_on(db.get(m.Employee, outgoing), START + timedelta(days=7))
        warnings = evaluate_schedule_warnings(db, schedule_period_id=period_id)
        assert sum(w.warning_type == 'NO_LEAD_OF_DAY' for w in warnings) == 3
        assert any(w.warning_type == 'NO_ASSIGNED_EMPLOYEE' for w in warnings)
        assert _audit(db)[0].actor_principal_id == owner.id


def test_manual_removal_requires_owner_confirmation_and_keeps_other_assignments(employee_client, monkeypatch):
    from app.routers import v2_scheduling
    monkeypatch.setattr(v2_scheduling, 'get_client_ip', lambda request: '127.0.0.1')
    client, _, Session, _, outgoing, _, period_id = _owner_schedule(employee_client, monkeypatch)
    with Session() as db:
        source = db.get(m.SchedulePeriod, period_id)
        before = shape(rows(db, source))
        row = next(r for r in rows(db, source) if r.employee_id == outgoing)
        shift_id = row.id
        values = dict(expected_version=source.version, employee_id=None, store_id=row.store_id,
                      shift_date=str(row.shift_date), start_time=str(row.start_time), end_time=str(row.end_time))
    path = f'/v2/scheduling/api/periods/{period_id}/shifts/{shift_id}'
    headers = {'X-CSRF-Token':client.cookies.get('csrf_token')}
    preview = client.patch(path, json=values, headers=headers)
    assert preview.status_code == 422, preview.text
    review = preview.json()['owner_review']
    assert [g['summary'] for g in review['groups']] == ['1 date without assigned lead coverage', '1 date below minimum staffing']
    with Session() as db:
        assert shape(rows(db, db.get(m.SchedulePeriod, period_id))) == before and not _audit(db)
    saved = client.patch(path, json={**values, 'owner_confirmation':review['token']}, headers=headers)
    assert saved.status_code == 200, saved.text
    with Session() as db:
        after = shape(rows(db, db.get(m.SchedulePeriod, period_id)))
        assert after[shift_id][0] is None
        assert {k:v for k,v in after.items() if k != shift_id} == {k:v for k,v in before.items() if k != shift_id}
        assert db.scalar(select(m.ScheduleWarning.id).where(
            m.ScheduleWarning.schedule_period_id == period_id, m.ScheduleWarning.warning_type == 'NO_LEAD_OF_DAY'))
        assert len(_audit(db)) == 1


def test_owner_assignment_change_prepares_draft_without_republishing(employee_client, monkeypatch):
    from app.services import v2_scheduling_swap_service

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)

    monkeypatch.setattr(v2_scheduling_swap_service, 'datetime', FixedDateTime)
    client, _, Session, _, outgoing, incoming, period_id = _owner_schedule(
        employee_client, monkeypatch, qualified=False, published=True)
    with Session() as db:
        source = db.get(m.SchedulePeriod, period_id)
        before = shape(rows(db, source))
        row = next(r for r in rows(db, source) if r.employee_id == outgoing)
        fields = dict(shift_id=row.id, replacement_employee_id=incoming, kind='SHIFT',
                      reason='Owner requested assignment change', expected_versions={str(period_id):source.version})
    headers = {'X-CSRF-Token':client.cookies.get('csrf_token')}
    url = '/v2/scheduling/api/assignment-change'
    preview = client.post(url, json=fields, headers=headers)
    assert preview.status_code == 422, preview.text
    review = preview.json()['owner_review']
    assert review['groups'][0]['summary'] == '1 date without assigned lead coverage'
    with Session() as db:
        assert not db.scalar(select(m.SchedulePeriod.id).where(m.SchedulePeriod.supersedes_schedule_period_id == period_id))
    result = client.post(url, json={**fields, 'owner_confirmation':review['token']}, headers=headers)
    assert result.status_code == 200, result.text
    with Session() as db:
        source = db.get(m.SchedulePeriod, period_id)
        draft = db.get(m.SchedulePeriod, result.json()['period_ids'][0])
        assert source.status == m.SchedulePeriodStatus.PUBLISHED and shape(rows(db, source)) == before
        assert draft.status == m.SchedulePeriodStatus.DRAFT and draft.published_at is None
        assert any(r.source_shift_id == fields['shift_id'] and r.employee_id == incoming for r in rows(db, draft))
        assert len(_audit(db)) == 1


def test_owner_assignment_policy_override_does_not_relax_automatic_validation(employee_client, monkeypatch):
    from datetime import time
    from app.routers import v2_scheduling
    monkeypatch.setattr(v2_scheduling, 'get_client_ip', lambda request: '127.0.0.1')
    client, current, Session, _, outgoing, incoming, period_id = _owner_schedule(employee_client, monkeypatch)
    with Session() as db:
        source = db.get(m.SchedulePeriod, period_id)
        row = next(r for r in rows(db, source) if r.employee_id == outgoing)
        weekday = (row.shift_date.weekday() + 1) % 7
        db.add(m.EmployeeSchedulingWindow(employee_id=incoming, day_of_week=weekday,
            kind=m.SchedulingWindowKind.HARD_UNAVAILABLE, start_time=time.min, end_time=time.max,
            active=True, created_by_principal_id=current['principal'].id, updated_by_principal_id=current['principal'].id))
        db.commit()
        shift_id = row.id
        values = dict(expected_version=source.version, employee_id=incoming, store_id=row.store_id,
                      shift_date=str(row.shift_date), start_time=str(row.start_time), end_time=str(row.end_time))
    path = f'/v2/scheduling/api/periods/{period_id}/shifts/{shift_id}'
    headers = {'X-CSRF-Token':client.cookies.get('csrf_token')}
    preview = client.patch(path, json=values, headers=headers)
    assert preview.status_code == 422, preview.text
    review = preview.json()['owner_review']
    assert any('scheduling policy exceptions' in g['summary'] for g in review['groups'])
    saved = client.patch(path, json={**values, 'owner_confirmation':review['token']}, headers=headers)
    assert saved.status_code == 200, saved.text
    with Session() as db:
        row = db.get(m.ScheduleShift, shift_id)
        assert row.employee_id == incoming
        result = evaluate_assignment(db, employee_id=incoming, store_id=row.store_id,
            shift_date=row.shift_date, start_time=row.start_time, end_time=row.end_time,
            schedule_period_id=period_id, exclude_shift_id=shift_id)
        assert not result.eligible and 'HARD_WEEKDAY_LOCKOUT' in {r.code for r in result.reasons}
        assert any(w.get('policy_code') == 'HARD_WEEKDAY_LOCKOUT'
                   for w in _audit(db)[0].meta['metadata']['warnings'])


def test_owner_confirmation_cannot_be_submitted_by_manager(employee_client, monkeypatch):
    manager = employee_client[1]['principal']
    client, current, Session, _, outgoing, incoming, period_id = _owner_schedule(
        employee_client, monkeypatch, qualified=False)
    url = f'/v2/scheduling/employees/{incoming}/training'
    fields = dict(action='transition', first_schedule_date=str(START + timedelta(days=3)), replacing_employee_id=outgoing)
    token = _profile_token(_post(client, url, **fields))
    current['principal'] = manager
    assert _post(client, url, **fields, owner_confirmation=token).status_code == 403
    with Session() as db:
        assert db.get(m.Employee, incoming).first_schedule_date is None
        assert len([r for r in rows(db, db.get(m.SchedulePeriod, period_id)) if r.employee_id == outgoing]) == 3
        assert not _audit(db)
