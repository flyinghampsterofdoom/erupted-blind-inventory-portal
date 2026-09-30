"""Company-wide responsibility: evidence, bounded burden and exception correction."""
from datetime import date, datetime, time, timedelta, timezone

import pytest
from sqlalchemy import select
from app import models as m
from app.auth import Principal, Role
from app.services.v2_scheduling_assignments_service import lead_fairness, reconcile_lead_designations, set_lead_of_day
from app.services.v2_scheduling_lead_duty_service import lead_duty_facts, record_lead_duty, void_lead_duty
from app.services.v2_scheduling_coverage_service import rebuild_schedule_warnings
from app.services.v2_scheduling_board_service import serialize_week_board
from app.services.v2_scheduling_service import SchedulingValidationError
from test_v2_scheduling_foundation import scheduling_db

ASOF = date(2026, 10, 1)


def period(db, actor, day, status='PUBLISHED', revision=1):
    start = day - timedelta(days=(day.weekday() + 1) % 7)
    existing = db.scalar(select(m.SchedulePeriod).where(m.SchedulePeriod.week_start_date == start, m.SchedulePeriod.revision_number == revision))
    if existing is not None:
        return existing
    row = m.SchedulePeriod(week_start_date=start, week_end_date=start + timedelta(days=6),
        status=m.SchedulePeriodStatus(status), revision_number=revision,
        published_at=datetime(2026, 1, 1, tzinfo=timezone.utc) if status != 'DRAFT' else None,
        created_by_principal_id=actor.id, updated_by_principal_id=actor.id)
    db.add(row); db.flush(); return row


def shift(db, actor, per, employee, store, day, lead=True):
    row = m.ScheduleShift(schedule_period_id=per.id, employee_id=employee, store_id=store,
        shift_date=day, start_time=time(9), end_time=time(17), unpaid_break_minutes=0,
        is_lead_of_day=lead, created_by_principal_id=actor.id, updated_by_principal_id=actor.id)
    db.add(row); db.flush(); return row


def attendance(db, actor, row, kind, replacement=None):
    event = m.ScheduleAttendanceEvent(schedule_shift_id=row.id, original_employee_id=row.employee_id,
        replacement_employee_id=replacement, event_type=m.AttendanceEventType(kind),
        event_at=datetime.combine(row.shift_date, time(9), timezone.utc),
        recorded_by_principal_id=actor.id, note='Synthetic evidence')
    db.add(event); db.flush(); return event


def facts(db, day):
    return lead_duty_facts(db, start_date=day, end_date=day, as_of_date=ASOF)


@pytest.mark.parametrize('events,expected,credited', [
    ([], 'PRESUMPTIVE', True), (['WORKED_AS_SCHEDULED'], 'PRESUMPTIVE', True),
    (['CALLED_OUT'], 'UNRESOLVED', False), (['NO_CALL_NO_SHOW'], 'UNRESOLVED', False),
    (['COVERED_SHIFT'], 'UNRESOLVED', False),
    (['CALLED_OUT', 'COVERED_SHIFT'], 'UNRESOLVED', False),
    (['WORKED_AS_SCHEDULED', 'CALLED_OUT'], 'UNRESOLVED', False),
    (['LATE'], 'UNRESOLVED', False),
])
def test_work_is_not_lead_responsibility(scheduling_db, events, expected, credited):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        day = date(2026, 9, 22); per = period(db, actor, day)
        row = shift(db, actor, per, ids['alex'], ids['north'], day)
        for kind in events:
            attendance(db, actor, row, kind, ids['blair'] if kind == 'COVERED_SHIFT' else None)
        fact, = facts(db, day)
        assert fact['outcome'] == expected
        assert fact['employee_id'] == (ids['alex'] if credited else None)
        fair = lead_fairness(db, employee_id=ids['alex'], before_date=ASOF, planning_date=ASOF)
        assert fair.historical_assignment_count == int(credited)
        assert fair.confirmed_assignment_count == 0
        assert lead_fairness(db, employee_id=ids['blair'], before_date=ASOF, planning_date=ASOF).historical_assignment_count == 0


def test_actual_lead_distinct_from_replacement_corrections_and_void(scheduling_db):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        day = date(2026, 9, 22); per = period(db, actor, day)
        row = shift(db, actor, per, ids['alex'], ids['north'], day)
        attendance(db, actor, row, 'CALLED_OUT')
        attendance(db, actor, row, 'COVERED_SHIFT', ids['blair'])
        # Historical fact can name an inactive/non-capable employee.
        resolved = record_lead_duty(db, principal=actor, shift_id=row.id, outcome='PERFORMED',
            employee_id=ids['inactive'], reason='Mikey assumed company-wide responsibility', today=ASOF)
        fact, = facts(db, day)
        assert (fact['outcome'], fact['employee_id']) == ('CONFIRMED', ids['inactive'])
        assert row.employee_id == ids['alex'] and row.is_lead_of_day
        assert len(list(db.scalars(select(m.ScheduleAttendanceEvent)))) == 2
        assert lead_fairness(db, employee_id=ids['inactive'], before_date=ASOF, planning_date=ASOF).confirmed_assignment_count == 1
        corrected = record_lead_duty(db, principal=actor, shift_id=row.id, outcome='UNCOVERED', employee_id=None,
            reason='Correction: no one assumed responsibility', today=ASOF)
        assert resolved.voided_at and facts(db, day)[0]['outcome'] == 'UNCOVERED'
        void_lead_duty(db, principal=actor, outcome_id=corrected.id, reason='Retracted assertion')
        assert facts(db, day)[0]['outcome'] == 'UNRESOLVED'
        assert facts(db, day)[0]['employee_id'] is None
        assert len(list(db.scalars(select(m.LeadDutyOutcome)))) == 2
        assert len(list(db.scalars(select(m.AuditLog).where(m.AuditLog.action.like('V2:SCHEDULING:LEAD_DUTY_%'))))) == 4


@pytest.mark.parametrize('outcome,employee,reason', [('BAD', None, 'reason'), ('PERFORMED', None, 'reason'),
    ('UNCOVERED', 'alex', 'reason'), ('PERFORMED', 'alex', ' '), ('UNRESOLVED', None, 'x'*2001)])
def test_resolution_validates_input(scheduling_db, outcome, employee, reason):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        per = period(db, actor, date(2026, 9, 22)); row = shift(db, actor, per, ids['alex'], ids['north'], date(2026, 9, 22))
        with pytest.raises(SchedulingValidationError):
            record_lead_duty(db, principal=actor, shift_id=row.id, outcome=outcome,
                employee_id=ids.get(employee), reason=reason, today=ASOF)


def test_separate_permission_future_boundary_and_explicit_unknown(scheduling_db):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        per = period(db, actor, ASOF); row = shift(db, actor, per, ids['alex'], ids['north'], ASOF)
        with pytest.raises(SchedulingValidationError, match='day has passed'):
            record_lead_duty(db, principal=actor, shift_id=row.id, outcome='UNRESOLVED', employee_id=None, reason='Unknown', today=ASOF-timedelta(days=1))
        lead = Principal(id=actor.id, username='lead', role=Role.LEAD, store_id=None, active=True)
        with pytest.raises(PermissionError):
            record_lead_duty(db, principal=lead, shift_id=row.id, outcome='UNRESOLVED', employee_id=None, reason='Unknown', today=ASOF)
        row.shift_date -= timedelta(days=1); db.flush()
        record_lead_duty(db, principal=actor, shift_id=row.id, outcome='UNRESOLVED', employee_id=None, reason='Unknown', today=ASOF)
        assert facts(db, row.shift_date)[0]['outcome'] == 'UNRESOLVED'


@pytest.mark.parametrize('count', [2, 3, 4])
def test_dynamic_pool_and_exactly_one_company_wide_lead(scheduling_db, count):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        employees = [db.get(m.Employee, ids['alex']), db.get(m.Employee, ids['blair'])]
        for n in range(count - 2):
            e = m.Employee(full_name=f'Lead {n}', normalized_name=f'lead {n}', active=True,
                           scheduling_active=True, scheduling_lead_capable=True)
            db.add(e); db.flush(); employees.append(e)
        day = date(2026, 11, 1); per = period(db, actor, day, 'DRAFT')
        for offset in range(count):
            for i, e in enumerate(employees):
                shift(db, actor, per, e.id, ids['north'] if i % 2 else ids['south'], day + timedelta(days=offset * 2), False)
        for _ in range(2):
            assert reconcile_lead_designations(db, schedule_period_id=per.id, planning_date=ASOF) == []
            selected = list(db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id == per.id, m.ScheduleShift.is_lead_of_day)))
            assert len(selected) == count
            assert {s.employee_id for s in selected} == {e.id for e in employees}
            assert len({s.shift_date for s in selected}) == count


@pytest.mark.parametrize('lead_store', ['Longview', 'Andresen'])
def test_four_stores_one_lead_location_irrelevant(scheduling_db, lead_store):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        db.get(m.Store, ids['north']).name = 'Andresen'; db.get(m.Store, ids['south']).name = 'Longview'
        for name in ('HWY 99', 'SR 503'):
            db.add(m.Store(name=name, active=True))
        db.flush(); stores = list(db.scalars(select(m.Store)))
        day = date(2026, 11, 1); per = period(db, actor, day, 'DRAFT')
        for store in stores:
            employee = m.Employee(full_name=store.name + ' Lead', normalized_name=store.name.lower() + ' lead', active=True, scheduling_lead_capable=True)
            db.add(employee); db.flush()
            row = shift(db, actor, per, employee.id, store.id, day, store.name == lead_store)
            row.lead_of_day_manually_assigned = row.is_lead_of_day
        db.flush()
        assert reconcile_lead_designations(db, schedule_period_id=per.id, planning_date=ASOF) == []
        leads = list(db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.is_lead_of_day)))
        assert len(leads) == 1 and db.get(m.Store, leads[0].store_id).name == lead_store
        rebuild_schedule_warnings(db, schedule_period_id=per.id)
        assert not list(db.scalars(select(m.ScheduleWarning).where(m.ScheduleWarning.warning_type == 'NO_LEAD_OF_DAY')))
        board = serialize_week_board(db, week_start=per.week_start_date, selected_store_ids=tuple(s.id for s in stores),
            all_authorized_store_ids=tuple(s.id for s in stores), permission_flags={}, schedule_period_id=per.id)
        assert sum(s['is_lead_of_day'] for s in board['shifts']) == 1


def test_combined_burden_recency_and_window(scheduling_db):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        # Historical counts 2 versus 3, then one reservation ties and recency wins.
        history = period(db, actor, date(2026, 9, 20))
        for offset, employee in enumerate([ids['alex']]*2 + [ids['blair']]*3):
            shift(db, actor, history, employee, ids['north'], date(2026, 9, 20)+timedelta(days=offset))
        future = period(db, actor, date(2026, 10, 4), 'DRAFT')
        shift(db, actor, future, ids['alex'], ids['north'], date(2026, 10, 4))
        target = date(2026, 10, 5)
        a = lead_fairness(db, employee_id=ids['alex'], before_date=target, planning_date=ASOF)
        b = lead_fairness(db, employee_id=ids['blair'], before_date=target, planning_date=ASOF)
        assert a.total_assignment_count == b.total_assignment_count == 3
        assert b.rank < a.rank
        boundary = target - timedelta(days=84)
        for day in [boundary-timedelta(days=1), boundary]:
            per = period(db, actor, day)
            shift(db, actor, per, ids['alex'], ids['north'], day)
        assert lead_fairness(db, employee_id=ids['alex'], before_date=target, planning_date=ASOF).total_assignment_count == 4
        assert lead_fairness(db, employee_id=ids['alex'], before_date=target+timedelta(days=1), planning_date=ASOF).total_assignment_count == 3


def test_global_revision_selection_archived_evidence_and_empty_draft(scheduling_db):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        day = date(2026, 9, 22); old = period(db, actor, day, 'ARCHIVED')
        source = shift(db, actor, old, ids['alex'], ids['north'], day)
        new = period(db, actor, day, 'PUBLISHED', 2)
        copied = shift(db, actor, new, ids['blair'], ids['south'], day); copied.source_shift_id = source.id; db.flush()
        assert facts(db, day)[0]['employee_id'] == ids['blair']
        new.status = m.SchedulePeriodStatus.ARCHIVED; db.flush()
        assert facts(db, day)[0]['employee_id'] == ids['blair']
        # Explicit fact survives schedule revision and archival independently.
        record_lead_duty(db, principal=actor, shift_id=source.id, outcome='PERFORMED', employee_id=ids['alex'], reason='Actual duty', today=ASOF)
        assert len(facts(db, day)) == 1 and facts(db, day)[0]['employee_id'] == ids['alex']
        future = period(db, actor, date(2026, 11, 1)); shift(db, actor, future, ids['alex'], ids['north'], date(2026, 11, 1))
        draft = period(db, actor, date(2026, 11, 1), 'DRAFT', 2)
        fair = lead_fairness(db, employee_id=ids['alex'], before_date=date(2026, 11, 2), planning_date=ASOF, current_period_id=draft.id)
        assert fair.planned_future_assignment_count == fair.current_week_assignment_count == 0


def test_old_revision_attendance_and_multiple_shifts_do_not_duplicate(scheduling_db):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        day = date(2026, 9, 22); old = period(db, actor, day, 'ARCHIVED')
        source = shift(db, actor, old, ids['alex'], ids['north'], day)
        callout = attendance(db, actor, source, 'CALLED_OUT')
        new = period(db, actor, day, 'PUBLISHED', 2)
        copied = shift(db, actor, new, ids['alex'], ids['south'], day); copied.source_shift_id = source.id
        shift(db, actor, new, ids['alex'], ids['north'], day, False); db.flush()
        assert facts(db, day)[0]['outcome'] == 'UNRESOLVED'
        callout.voided_at = datetime.now(timezone.utc); db.flush()
        assert len(facts(db, day)) == 1
        assert lead_fairness(db, employee_id=ids['alex'], before_date=ASOF, planning_date=ASOF).historical_assignment_count == 1


def test_capability_lifecycle_does_not_change_history(scheduling_db):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        day = date(2026, 9, 22); past = period(db, actor, day)
        shift(db, actor, past, ids['alex'], ids['north'], day)
        future = period(db, actor, date(2026, 11, 1), 'DRAFT')
        for eid in (ids['alex'], ids['blair']): shift(db, actor, future, eid, ids['north'], date(2026, 11, 1), False)
        alex = db.get(m.Employee, ids['alex']); blair = db.get(m.Employee, ids['blair'])
        for attr in ('scheduling_lead_capable', 'active', 'scheduling_active'):
            setattr(blair, attr, False); db.flush()
            reconcile_lead_designations(db, schedule_period_id=future.id, planning_date=ASOF)
            assert db.scalar(select(m.ScheduleShift.employee_id).where(m.ScheduleShift.schedule_period_id == future.id, m.ScheduleShift.is_lead_of_day)) == alex.id
            setattr(blair, attr, True); db.flush()
            reconcile_lead_designations(db, schedule_period_id=future.id, planning_date=ASOF)
            assert db.scalar(select(m.ScheduleShift.employee_id).where(m.ScheduleShift.schedule_period_id == future.id, m.ScheduleShift.is_lead_of_day)) == blair.id
        alex.active = False; alex.scheduling_lead_capable = False; db.flush()
        assert lead_fairness(db, employee_id=alex.id, before_date=ASOF, planning_date=ASOF).historical_assignment_count == 1


def test_future_manual_reservation_moves_once_and_historical_edit_rejected(scheduling_db):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        day = date(2027, 1, 3); per = period(db, actor, day, 'DRAFT')
        a = shift(db, actor, per, ids['alex'], ids['north'], day, False)
        b = shift(db, actor, per, ids['blair'], ids['south'], day, False)
        for row in (a, b, a):
            set_lead_of_day(db, principal=actor, shift_id=row.id)
            fair = lead_fairness(db, employee_id=row.employee_id, before_date=day+timedelta(days=1), planning_date=ASOF)
            assert fair.total_assignment_count == fair.planned_future_assignment_count == 1
            other = b if row is a else a
            assert lead_fairness(db, employee_id=other.employee_id, before_date=day+timedelta(days=1), planning_date=ASOF).total_assignment_count == 0
        per.status = m.SchedulePeriodStatus.PUBLISHED; a.shift_date = date(2020, 1, 1); db.flush()
        with pytest.raises(SchedulingValidationError, match='Who was Lead'):
            set_lead_of_day(db, principal=actor, shift_id=a.id)


@pytest.mark.parametrize('traveler_is_lead', [True, False])
def test_longview_and_company_wide_lead_coexist(scheduling_db, traveler_is_lead):
    from test_v2_longview_weekly import setup_pool
    from app.services import v2_scheduling_policy_service as policy
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        setup_pool(db, actor, ids)
        day = date(2026, 11, 1); per = period(db, actor, day, 'DRAFT')
        traveler = shift(db, actor, per, ids['blair'], ids['south'], day, False)
        local = shift(db, actor, per, ids['alex'], ids['north'], day, False)
        db.get(m.Employee, ids['blair']).scheduling_lead_capable = traveler_is_lead
        (traveler if traveler_is_lead else local).is_lead_of_day = True
        (traveler if traveler_is_lead else local).lead_of_day_manually_assigned = True
        db.flush()
        for _ in range(2):
            assert policy.weekly_longview_travelers(db, period=per, assign=True, principal=actor)[0]['satisfied']
            assert reconcile_lead_designations(db, schedule_period_id=per.id, planning_date=ASOF) == []
            assert (traveler if traveler_is_lead else local).is_lead_of_day
            assert sum(s.is_lead_of_day for s in (traveler, local)) == 1


def test_lead_repair_revalidates_traveler_and_reports_impossible_combination(scheduling_db):
    from test_v2_longview_weekly import setup_pool
    from app.services import v2_scheduling_policy_service as policy
    from app.services.v2_scheduling_assignments_service import ensure_daily_lead_staffing
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        setup_pool(db, actor, ids)
        policy.set_special_store_employee_participation(db, principal=actor, employee_id=ids['alex'],
            store_id=ids['south'], participation=m.SpecialStoreParticipation.PRIMARY)
        db.get(m.Employee, ids['blair']).scheduling_lead_capable = False
        day = date(2026, 11, 1); per = period(db, actor, day, 'DRAFT')
        row = shift(db, actor, per, ids['blair'], ids['south'], day, False)
        decisions = []
        assert ensure_daily_lead_staffing(db, principal=actor, schedule_period_id=per.id, planning_date=ASOF, diagnostics=decisions) == []
        result, = policy.weekly_longview_travelers(db, period=per, assign=True, principal=actor, planning_date=ASOF)
        assert not result['satisfied'] and 'WOULD_REMOVE_ONLY_LEAD' in result['reason_codes']
        assert row.employee_id == ids['alex']
        rebuild_schedule_warnings(db, schedule_period_id=per.id)
        assert list(db.scalars(select(m.ScheduleWarning).where(m.ScheduleWarning.warning_type == 'LONGVIEW_WEEKLY_TRAVELER_UNSATISFIED')))


def test_resolution_readiness_and_board_provenance(scheduling_db):
    from app.services.v2_scheduling_readiness_service import scheduling_readiness
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        day = date(2026, 9, 22); per = period(db, actor, day)
        row = shift(db, actor, per, ids['alex'], ids['north'], day)
        attendance(db, actor, row, 'CALLED_OUT')
        assert any(w.code == 'LEAD_DUTY_EXCEPTIONS' for w in scheduling_readiness(db, today=ASOF).warnings)
        record_lead_duty(db, principal=actor, shift_id=row.id, outcome='PERFORMED', employee_id=ids['blair'], reason='Assumed responsibility', today=ASOF)
        assert not any(w.code == 'LEAD_DUTY_EXCEPTIONS' for w in scheduling_readiness(db, today=ASOF).warnings)
        board = serialize_week_board(db, week_start=per.week_start_date, selected_store_ids=(ids['north'], ids['south']),
            all_authorized_store_ids=(ids['north'], ids['south']), permission_flags={'scheduling.lead_duty.resolve': True}, schedule_period_id=per.id)
        item, = board['shifts']
        assert item['lead_duty']['scheduled_employee_id'] == ids['alex']
        assert item['lead_duty']['employee_id'] == ids['blair']
        assert item['lead_duty']['evidence_class'] == 'CONFIRMED_LEAD'
        assert len(item['lead_duty_history']) == 1
        assert not item['can_change_lead_designation']
        assert item['can_resolve_lead_duty']


def test_resolution_routes_require_distinct_permission_csrf_and_preserve_history(scheduling_db, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.auth import get_current_principal
    from app.config import settings
    from app.db import get_db
    from app.routers.v2_scheduling import FEATURE_KEY, router
    from app.security.csrf import install_csrf_cookie_middleware
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        day = date(2026, 9, 20); per = period(db, actor, day)
        row = shift(db, actor, per, ids['alex'], ids['north'], day); shift_id = row.id; db.commit()
    app = FastAPI(); install_csrf_cookie_middleware(app); app.include_router(router)
    current = {'principal': actor}
    def session():
        with Session() as db: yield db
    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_current_principal] = lambda: current['principal']
    monkeypatch.setattr(settings, 'v2_enabled_features', FEATURE_KEY)
    monkeypatch.setattr(settings, 'v2_principal_features', '')
    monkeypatch.setattr(settings, 'session_cookie_secure', False)
    with TestClient(app, client=('127.0.0.1', 50000), follow_redirects=False) as client:
        client.get('/missing'); csrf = client.cookies.get('csrf_token')
        payload = {'outcome': 'PERFORMED', 'employee_id': ids['blair'], 'reason': 'Actual company-wide Lead'}
        path = f'/v2/scheduling/api/shifts/{shift_id}/lead-duty'
        assert client.post(path, json=payload).status_code == 403
        current['principal'] = Principal(id=actor.id, username='lead', role=Role.LEAD, store_id=None, active=True)
        assert client.post(path, json=payload, headers={'X-CSRF-Token': csrf}).status_code == 403
        current['principal'] = actor
        response = client.post(path, json=payload, headers={'X-CSRF-Token': csrf})
        assert response.status_code == 200, response.text
        with Session() as db:
            outcome = db.scalar(select(m.LeadDutyOutcome)); outcome_id = outcome.id
            assert outcome.employee_id == ids['blair']
        assert client.post(f'/v2/scheduling/api/lead-duty/{outcome_id}/void', json={'reason': 'Correction'}, headers={'X-CSRF-Token': csrf}).status_code == 200
        with Session() as db:
            assert db.get(m.LeadDutyOutcome, outcome_id).voided_at is not None
            assert db.get(m.ScheduleShift, shift_id).employee_id == ids['alex']


def test_database_enforces_one_active_company_wide_resolution(scheduling_db):
    from sqlalchemy.exc import IntegrityError
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        day = date(2026, 9, 20); per = period(db, actor, day)
        row = shift(db, actor, per, ids['alex'], ids['north'], day)
        record_lead_duty(db, principal=actor, shift_id=row.id, outcome='UNCOVERED', employee_id=None, reason='No coverage', today=ASOF)
        with pytest.raises(IntegrityError), db.begin_nested():
            db.add(m.LeadDutyOutcome(business_date=day, scheduled_shift_id=row.id, outcome='PERFORMED', employee_id=ids['blair'], reason='Duplicate forbidden', recorded_by_principal_id=actor.id)); db.flush()
        assert len(list(db.scalars(select(m.LeadDutyOutcome)))) == 1


def test_default_future_reconciliation_counts_earlier_draft_reservations(scheduling_db, monkeypatch):
    from app.services import v2_scheduling_lead_duty_service as duty
    Session, actor, ids, _ = scheduling_db
    monkeypatch.setattr(duty, 'business_today', lambda db: ASOF)
    with Session() as db:
        early = period(db, actor, date(2026, 11, 1), 'DRAFT')
        shift(db, actor, early, ids['alex'], ids['north'], date(2026, 11, 1))
        later = period(db, actor, date(2026, 11, 8), 'DRAFT')
        for eid in (ids['alex'], ids['blair']): shift(db, actor, later, eid, ids['north'], date(2026, 11, 8), False)
        diagnostics = []
        assert reconcile_lead_designations(db, schedule_period_id=later.id, diagnostics=diagnostics) == []
        assert db.scalar(select(m.ScheduleShift.employee_id).where(m.ScheduleShift.schedule_period_id == later.id, m.ScheduleShift.is_lead_of_day)) == ids['blair']
        burdens = {r['employee_id']: r for r in diagnostics[-1]['candidate_burdens']}
        assert burdens[ids['alex']]['historical_count'] == 0
        assert burdens[ids['alex']]['planned_future_count'] == 1


def test_longview_repair_cannot_count_an_ineligible_other_lead(scheduling_db):
    from test_v2_longview_weekly import setup_pool
    from app.services import v2_scheduling_policy_service as policy
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        setup_pool(db, actor, ids)
        policy.set_special_store_employee_participation(db, principal=actor, employee_id=ids['alex'],
            store_id=ids['south'], participation=m.SpecialStoreParticipation.PRIMARY)
        db.get(m.Employee, ids['blair']).scheduling_lead_capable = False
        db.get(m.Employee, ids['inactive']).scheduling_lead_capable = True
        day = date(2026, 11, 1); per = period(db, actor, day, 'DRAFT')
        only = shift(db, actor, per, ids['alex'], ids['south'], day, False)
        shift(db, actor, per, ids['inactive'], ids['north'], day, False)
        result, = policy.weekly_longview_travelers(db, period=per, assign=True, principal=actor)
        assert not result['satisfied'] and 'WOULD_REMOVE_ONLY_LEAD' in result['reason_codes']
        assert only.employee_id == ids['alex']


def test_missing_historical_designation_is_unknown_not_confirmed_uncovered(scheduling_db):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        day = date(2026, 9, 20); per = period(db, actor, day)
        shift(db, actor, per, ids['alex'], ids['north'], day, False)
        fact, = facts(db, day)
        assert fact['outcome'] == 'UNRESOLVED' and fact['employee_id'] is None
        assert fact['evidence_class'] == 'UNRESOLVED'
