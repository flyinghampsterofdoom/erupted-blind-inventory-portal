"""Operational exceptions preserve intent, evidence, and company-wide responsibility."""
from datetime import date, datetime, time, timedelta, timezone
import pytest
from sqlalchemy import select, func
from app import models as m
from app.auth import Principal, Role
from app.services.v2_scheduling_attendance_service import record_attendance_event, void_attendance_event, attendance_facts_for_shift
from app.services.v2_scheduling_exception_service import record_exception, record_commitment, cancel_commitment, exception_facts, published_provenance
from app.services.v2_scheduling_lead_duty_service import record_lead_duty, lead_duty_facts
from app.services.v2_scheduling_assignments_service import lead_fairness
from app.services.v2_scheduling_policy_service import (create_transfer_request, respond_to_transfer, review_transfer,
    configure_special_store, longview_rotation_fairness)
from app.services.v2_scheduling_service import SchedulingValidationError, SchedulingConflict
from test_v2_scheduling_foundation import scheduling_db
from test_v2_lead_duty import period, shift, attendance

TODAY = date(2026, 9, 30)

def at(day, hour=17):
    return datetime.combine(day, time(hour), timezone.utc)


def setup_shift(db, actor, ids, day, lead=False):
    return shift(db, actor, period(db, actor, day), ids['alex'], ids['north'], day, lead=lead)


@pytest.mark.parametrize('kind', list(m.AttendanceEventType))
def test_advance_only_callout(scheduling_db, kind):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        row = setup_shift(db, actor, ids, TODAY + timedelta(days=1))
        if kind == m.AttendanceEventType.CALLED_OUT:
            event = record_attendance_event(db, principal=actor, shift_id=row.id, event_type=kind,
                event_at=at(TODAY), today=TODAY).event
            assert exception_facts(db, row, TODAY)['status'] == 'Coverage needed'
            assert row.employee_id == ids['alex']
            void_attendance_event(db, principal=actor, event_id=event.id, reason='Wrong date')
            assert exception_facts(db, row, TODAY)['status'] is None
        else:
            with pytest.raises(SchedulingValidationError, match='before the scheduled date'):
                record_attendance_event(db, principal=actor, shift_id=row.id, event_type=kind,
                    event_at=at(TODAY), replacement_employee_id=ids['blair'], today=TODAY)


@pytest.mark.parametrize('role', [Role.MANAGER, Role.LEAD])
def test_combined_commitment_history_and_no_work(scheduling_db, role):
    Session, actor, ids, _ = scheduling_db
    actor = Principal(id=actor.id, username=actor.username, role=role, store_id=None, active=True)
    with Session() as db:
        day = TODAY + timedelta(days=1)
        row = setup_shift(db, actor, ids, day, lead=True)
        record_exception(db, principal=actor, shift_id=row.id, event_type=m.AttendanceEventType.CALLED_OUT,
            event_at=at(TODAY), commitment_employee_id=ids['blair'], today=TODAY)
        facts = exception_facts(db, row, TODAY)
        assert facts['status'] == 'Replacement committed' and facts['actual_worker_names'] == []
        assert row.employee_id == ids['alex'] and row.is_lead_of_day
        assert db.scalar(select(func.count()).select_from(m.ScheduleAttendanceEvent)) == 1
        assert db.scalar(select(func.count()).select_from(m.AttendancePointEntry)) == 0
        assert lead_fairness(db, employee_id=ids['blair'], before_date=day+timedelta(days=2), planning_date=TODAY).confirmed_assignment_count == 0
        third = m.Employee(full_name='Lexi', normalized_name='lexi', active=True)
        db.add(third); db.flush()
        changed = record_commitment(db, principal=actor, shift_id=row.id, employee_id=third.id, today=TODAY)
        history = exception_facts(db, row, TODAY)['commitment_history']
        assert len(history) == 2 and history[0]['voided'] and not history[1]['voided']
        cancel_commitment(db, principal=actor, commitment_id=changed.id, reason='Unavailable')
        assert exception_facts(db, row, TODAY)['status'] == 'Coverage needed'


def test_combined_atomicity(scheduling_db):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        row = setup_shift(db, actor, ids, TODAY + timedelta(days=1))
        with pytest.raises(SchedulingValidationError):
            record_exception(db, principal=actor, shift_id=row.id, event_type=m.AttendanceEventType.CALLED_OUT,
                event_at=at(TODAY), commitment_employee_id=ids['inactive'], today=TODAY)
        assert db.scalar(select(func.count()).select_from(m.ScheduleAttendanceEvent)) == 0
        assert db.scalar(select(func.count()).select_from(m.AuditLog).where(m.AuditLog.action.like('%ATTENDANCE_EVENT_RECORDED'))) == 0


@pytest.mark.parametrize('restriction', ['inactive', 'scheduling_inactive', 'planned_overlap', 'actual_conflict'])
def test_historical_actual_correction(scheduling_db, restriction):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        day = TODAY - timedelta(days=10)
        row = setup_shift(db, actor, ids, day)
        person = db.get(m.Employee, ids['blair'])
        if restriction == 'inactive': person.active = False
        if restriction == 'scheduling_inactive': person.scheduling_active = False
        if restriction in ('planned_overlap', 'actual_conflict'):
            other = shift(db, actor, db.get(m.SchedulePeriod, row.schedule_period_id), person.id, ids['south'], day, lead=False)
            if restriction == 'actual_conflict': attendance(db, actor, other, 'WORKED_AS_SCHEDULED')
        db.flush()
        results = record_exception(db, principal=actor, shift_id=row.id, event_type=m.AttendanceEventType.CALLED_OUT,
            event_at=at(day, 8), actual_employee_id=person.id, actual_event_at=at(day), today=TODAY)
        assert len(results) == 2 and row.employee_id == ids['alex']
        result = attendance_facts_for_shift(db, shift_id=row.id, as_of_date=TODAY)
        fact = exception_facts(db, row, TODAY)
        assert fact['status'] == ('Coverage unresolved/conflicting' if restriction == 'actual_conflict' else 'Actual coverage confirmed')
        if restriction == 'actual_conflict':
            assert exception_facts(db, other, TODAY)['actual_worker_names'] == []
        assert db.scalar(select(func.count()).select_from(m.AttendancePointEntry)) == 0


def test_longview_commitment_actual_difference_and_void(scheduling_db):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        day = TODAY - timedelta(days=9)
        row = setup_shift(db, actor, ids, day)
        third = m.Employee(full_name='Lexi', normalized_name='lexi', active=True)
        db.add(third); db.flush()
        configure_special_store(db, principal=actor, store_id=ids['north'],
            primary_employee_ids=(), rotation_employee_ids=(ids['alex'], ids['blair'], third.id))
        record_exception(db, principal=actor, shift_id=row.id, event_type=m.AttendanceEventType.CALLED_OUT,
            event_at=at(day-timedelta(days=1)), commitment_employee_id=ids['blair'], today=day-timedelta(days=1))
        fair = lambda eid: longview_rotation_fairness(db, employee_id=eid, store_id=ids['north'], before_date=TODAY, as_of_date=TODAY)
        assert fair(ids['blair']).historical_assignment_count == 0
        coverage = record_attendance_event(db, principal=actor, shift_id=row.id, event_type=m.AttendanceEventType.COVERED_SHIFT,
            replacement_employee_id=third.id, event_at=at(day), today=TODAY).event
        assert fair(third.id).historical_assignment_count == 1
        assert fair(ids['alex']).historical_assignment_count == fair(ids['blair']).historical_assignment_count == 0
        assert exception_facts(db, row, TODAY)['commitment']['employee_id'] == ids['blair']
        void_attendance_event(db, principal=actor, event_id=coverage.id, reason='Wrong employee')
        assert fair(third.id).historical_assignment_count == 0


def test_same_day_lead_handoff_no_premature_credit(scheduling_db):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        row = setup_shift(db, actor, ids, TODAY, lead=True)
        get = lambda cutoff: lead_duty_facts(db, start_date=TODAY, end_date=TODAY, as_of_date=cutoff)[0]
        assert get(TODAY)['employee_id'] == ids['alex']
        record_attendance_event(db, principal=actor, shift_id=row.id, event_type=m.AttendanceEventType.CALLED_OUT,
            event_at=at(TODAY, 8), today=TODAY)
        assert get(TODAY)['outcome'] == 'UNRESOLVED' and get(TODAY)['employee_id'] is None
        record_lead_duty(db, principal=actor, shift_id=row.id, outcome='PERFORMED', employee_id=ids['blair'],
            reason='Assumed responsibility across all stores', today=TODAY)
        assert get(TODAY)['outcome'] == 'CURRENT' and get(TODAY)['employee_id'] == ids['blair']
        fair = lead_fairness(db, employee_id=ids['blair'], before_date=TODAY+timedelta(days=2), planning_date=TODAY)
        assert fair.confirmed_assignment_count == fair.historical_assignment_count == 0
        assert get(TODAY+timedelta(days=1))['outcome'] == 'CONFIRMED'
        fair = lead_fairness(db, employee_id=ids['blair'], before_date=TODAY+timedelta(days=2), planning_date=TODAY+timedelta(days=1))
        assert fair.confirmed_assignment_count == 1
        assert row.employee_id == ids['alex'] and row.is_lead_of_day
        assert db.scalar(select(func.count()).select_from(m.LeadDutyOutcome).where(m.LeadDutyOutcome.voided_at.is_(None))) == 1


def transfer_fixture(db, actor, ids):
    giver = m.Principal(username='giver-exception', password_hash='unused', role=m.PrincipalRole.LEAD, active=True)
    receiver = m.Principal(username='receiver-exception', password_hash='unused', role=m.PrincipalRole.LEAD, active=True)
    db.add_all([giver, receiver]); db.flush()
    db.get(m.Employee, ids['alex']).principal_id = giver.id
    db.get(m.Employee, ids['blair']).principal_id = receiver.id
    db.flush()
    principal = lambda p: Principal(id=p.id, username=p.username, role=Role.LEAD, store_id=None, active=True)
    row = setup_shift(db, actor, ids, TODAY+timedelta(days=1))
    request = create_transfer_request(db, principal=principal(giver), shift_id=row.id, to_employee_id=ids['blair'], today=TODAY)
    return row, request, principal(receiver)


@pytest.mark.parametrize('stale', ['passed', 'archived', 'assignment', 'attendance', 'inactive'])
@pytest.mark.parametrize('stage', ['accept', 'approve'])
def test_transfer_revalidates(scheduling_db, stale, stage):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        row, request, receiver = transfer_fixture(db, actor, ids)
        day = TODAY
        if stale == 'passed': day = row.shift_date+timedelta(days=1)
        if stale == 'archived': db.get(m.SchedulePeriod, row.schedule_period_id).status = m.SchedulePeriodStatus.ARCHIVED
        if stale == 'assignment': row.employee_id = ids['inactive']
        if stale == 'attendance': attendance(db, actor, row, 'CALLED_OUT')
        if stale == 'inactive': db.get(m.Employee, ids['blair']).active = False
        if stage == 'approve': request.status = m.ShiftTransferStatus.PENDING_MANAGER
        db.flush()
        before = row.employee_id
        with pytest.raises((SchedulingConflict, SchedulingValidationError, PermissionError)):
            if stage == 'accept': respond_to_transfer(db, principal=receiver, request_id=request.id, accept=True, today=day)
            else: review_transfer(db, principal=actor, request_id=request.id, approve=True, today=day)
        assert row.employee_id == before


def test_transfer_provenance_and_revision_ambiguity(scheduling_db):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        row, request, receiver = transfer_fixture(db, actor, ids)
        respond_to_transfer(db, principal=receiver, request_id=request.id, accept=True, today=TODAY)
        assert row.employee_id == ids['blair']
        assert published_provenance(db, row)['employee_id'] == ids['alex']
        db.get(m.SchedulePeriod, row.schedule_period_id).status = m.SchedulePeriodStatus.ARCHIVED
        db.flush()
        revision = period(db, actor, row.shift_date, revision=2)
        copy = shift(db, actor, revision, ids['blair'], ids['north'], row.shift_date, lead=False)
        copy.source_shift_id = row.id; db.flush()
        assert published_provenance(db, copy)['employee_id'] == ids['alex']
        assert db.scalar(select(func.count()).select_from(m.ScheduleAttendanceEvent)) == 0
        request.to_employee_id = ids['inactive']; db.flush()
        assert published_provenance(db, copy)['state'] == 'AMBIGUOUS'


@pytest.mark.parametrize('kind,hour', [('WORKED_AS_SCHEDULED', 8), ('COVERED_SHIFT', 12), ('NO_CALL_NO_SHOW', 8), ('LATE', 8)])
def test_same_day_temporal_boundaries(scheduling_db, monkeypatch, kind, hour):
    from app.services import v2_scheduling_attendance_service as service
    from zoneinfo import ZoneInfo
    Session, actor, ids, _ = scheduling_db
    monkeypatch.setattr(service, '_now', lambda: datetime.combine(TODAY, time(hour), ZoneInfo('America/Los_Angeles')))
    with Session() as db:
        row = setup_shift(db, actor, ids, TODAY)
        with pytest.raises(SchedulingValidationError, match='before the shift has occurred'):
            record_attendance_event(db, principal=actor, shift_id=row.id, event_type=m.AttendanceEventType(kind),
                replacement_employee_id=ids['blair'], event_at=at(TODAY), today=TODAY)


def test_previous_week_copy_starts_new_publication(scheduling_db):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        old = setup_shift(db, actor, ids, TODAY-timedelta(days=7))
        row = setup_shift(db, actor, ids, TODAY)
        row.employee_id = ids['blair']; row.source_shift_id = old.id; db.flush()
        assert published_provenance(db, row)['employee_id'] == ids['blair']


def test_commitment_constraints_reuse_planning_and_permission(scheduling_db):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        row = setup_shift(db, actor, ids, TODAY+timedelta(days=1))
        denied = Principal(id=actor.id, username='staff', role=Role.STORE, store_id=ids['north'], active=True)
        with pytest.raises(PermissionError):
            record_commitment(db, principal=denied, shift_id=row.id, employee_id=ids['blair'], today=TODAY)
        with pytest.raises(SchedulingValidationError):
            record_commitment(db, principal=actor, shift_id=row.id, employee_id=ids['inactive'], today=TODAY)
        record_commitment(db, principal=actor, shift_id=row.id, employee_id=ids['blair'], today=TODAY)
        other = shift(db, actor, db.get(m.SchedulePeriod,row.schedule_period_id), ids['inactive'], ids['south'], row.shift_date, lead=False)
        with pytest.raises(SchedulingValidationError, match='overlapping'):
            record_commitment(db, principal=actor, shift_id=other.id, employee_id=ids['blair'], today=TODAY)


def test_exception_http_authority_csrf_atomicity_and_candidates(scheduling_db, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.auth import get_current_principal
    from app.config import settings
    from app.db import get_db
    from app.routers.v2_scheduling import FEATURE_KEY, router
    from app.security.csrf import install_csrf_cookie_middleware
    from app.services.v2_scheduling_lead_duty_service import business_today
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        day = business_today(db) + timedelta(days=1)
        row = setup_shift(db, actor, ids, day); shift_id = row.id; db.commit()
    app = FastAPI(); install_csrf_cookie_middleware(app); app.include_router(router)
    current = {'principal': actor}
    def session():
        with Session() as db: yield db
    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_current_principal] = lambda: current['principal']
    monkeypatch.setattr(settings, 'v2_enabled_features', FEATURE_KEY)
    monkeypatch.setattr(settings, 'v2_principal_features', '')
    monkeypatch.setattr(settings, 'session_cookie_secure', False)
    with TestClient(app, client=('127.0.0.1',50000)) as client:
        client.get('/missing'); headers = {'X-CSRF-Token': client.cookies.get('csrf_token')}
        path = f'/v2/scheduling/api/shifts/{shift_id}/attendance'
        payload = {'event_type':'CALLED_OUT', 'event_at': datetime.now(timezone.utc).isoformat(),
                   'commitment_employee_id':ids['blair']}
        assert client.post(path, json=payload).status_code == 403
        current['principal'] = Principal(id=actor.id, username='staff', role=Role.STORE, store_id=ids['north'], active=True)
        assert client.post(path, json=payload, headers=headers).status_code == 403
        current['principal'] = actor
        candidates = client.get(f'/v2/scheduling/api/shifts/{shift_id}/coverage-candidates').json()['candidates']
        assert ids['blair'] in [p['id'] for p in candidates] and ids['inactive'] not in [p['id'] for p in candidates]
        bad = client.post(path, json={**payload, 'commitment_employee_id':ids['inactive']}, headers=headers)
        assert bad.status_code == 422
        with Session() as db: assert db.scalar(select(func.count()).select_from(m.ScheduleAttendanceEvent)) == 0
        response = client.post(path, json=payload, headers=headers)
        assert response.status_code == 201, response.text
        fact = next(s for s in response.json()['board']['shifts'] if s['id'] == shift_id)['exception']
        assert fact['status'] == 'Replacement committed' and fact['actual_worker_names'] == []
        cid = fact['commitment']['id']
        assert client.post(f'/v2/scheduling/api/coverage-commitments/{cid}/void', json={'reason':'Changed plan'}, headers=headers).status_code == 200
        with Session() as db:
            assert db.get(m.ScheduleCoverageCommitment,cid).voided_at is not None
            assert db.scalar(select(func.count()).select_from(m.AttendancePointEntry)) == 0


@pytest.mark.parametrize('restriction', ['inactive', 'not_lead', 'future'])
def test_current_lead_retains_dynamic_eligibility_and_future_boundary(scheduling_db, restriction):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        day = TODAY + timedelta(days=1) if restriction == 'future' else TODAY
        row = setup_shift(db, actor, ids, day, lead=True)
        person = db.get(m.Employee, ids['blair'])
        if restriction == 'inactive': person.active = False
        if restriction == 'not_lead': person.scheduling_lead_capable = False
        db.flush()
        with pytest.raises(SchedulingValidationError):
            record_lead_duty(db, principal=actor, shift_id=row.id, outcome='PERFORMED', employee_id=person.id,
                             reason='Current handoff', today=TODAY)


def test_provenance_broken_cycle_is_unknown(scheduling_db):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        row = setup_shift(db, actor, ids, TODAY)
        row.source_shift_id = row.id; db.flush()
        assert published_provenance(db, row)['state'] == 'AMBIGUOUS'


def test_finished_today_is_retrospective_evidence(scheduling_db, monkeypatch):
    from app.services import v2_scheduling_attendance_service as service
    from zoneinfo import ZoneInfo
    Session, actor, ids, _ = scheduling_db
    monkeypatch.setattr(service, '_now', lambda: datetime.combine(TODAY, time(18), ZoneInfo('America/Los_Angeles')))
    with Session() as db:
        row = setup_shift(db, actor, ids, TODAY)
        result = record_attendance_event(db, principal=actor, shift_id=row.id, event_type=m.AttendanceEventType.COVERED_SHIFT,
            replacement_employee_id=ids['inactive'], event_at=at(TODAY), today=TODAY)
        assert result.event.replacement_employee_id == ids['inactive']
        assert 'HISTORICAL_EMPLOYEE_ELIGIBILITY' in result.warnings


def test_publication_audit_fallback_never_guesses_transfer_timing(scheduling_db):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        row, request, receiver = transfer_fixture(db, actor, ids)
        respond_to_transfer(db, principal=receiver, request_id=request.id, accept=True, today=TODAY)
        per = db.get(m.SchedulePeriod, row.schedule_period_id)
        per.published_at = None; db.flush()
        assert published_provenance(db, row)['state'] == 'AMBIGUOUS'
        db.add(m.AuditLog(actor_principal_id=actor.id, action='V2:SCHEDULING:SCHEDULE_PUBLISHED',
            meta={'entity_type':'schedule_period', 'entity_id':str(per.id),
                  'occurred_at':datetime(2026,1,1,tzinfo=timezone.utc).isoformat()}))
        db.flush()
        assert published_provenance(db, row)['employee_id'] == ids['alex']


@pytest.mark.parametrize('kind', ['CALLED_OUT', 'COVERED_SHIFT'])
def test_future_occurrence_cannot_be_hidden_on_past_shift(scheduling_db, kind):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        row = setup_shift(db, actor, ids, TODAY-timedelta(days=1))
        with pytest.raises(SchedulingValidationError, match='time cannot be in the future'):
            record_attendance_event(db, principal=actor, shift_id=row.id, event_type=m.AttendanceEventType(kind),
                replacement_employee_id=ids['blair'] if kind == 'COVERED_SHIFT' else None,
                event_at=at(TODAY+timedelta(days=1)), today=TODAY)
