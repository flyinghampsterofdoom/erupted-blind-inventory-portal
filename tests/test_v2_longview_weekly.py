"""Weekly travel obligations, evidence provenance, and deterministic reservations."""
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from app import models as m
from app.services import v2_scheduling_policy_service as p
from app.services.v2_scheduling_attendance_service import resolve_attendance_outcome
from app.services.v2_scheduling_service import create_draft_period
from app.services.v2_scheduling_readiness_service import scheduling_readiness
from test_v2_scheduling_foundation import scheduling_db, _coverage


def setup_pool(db, actor, ids):
    db.get(m.Store, ids['north']).name = 'HWY 99'
    db.get(m.Store, ids['south']).name = 'Longview'
    p.configure_special_store(db, principal=actor, store_id=ids['south'],
                             primary_employee_ids=(), rotation_employee_ids=(ids['alex'], ids['blair']))
    for eid in (ids['alex'], ids['blair']):
        profile = db.scalar(select(m.EmployeeSchedulingProfile).where(m.EmployeeSchedulingProfile.employee_id == eid))
        profile.home_store_id = ids['north']
        profile.target_shifts_per_week = 1
    db.flush()


def shift(db, actor, period, ids, employee=None, day=None, locked=False):
    row = m.ScheduleShift(schedule_period_id=period.id, store_id=ids['south'],
        employee_id=employee, shift_date=day or period.week_start_date,
        start_time=time(9), end_time=time(17), unpaid_break_minutes=0,
        manually_locked=locked, created_by_principal_id=actor.id, updated_by_principal_id=actor.id)
    db.add(row); db.flush()
    return row


def event(kind, original=1, replacement=None, voided=False):
    return SimpleNamespace(event_type=m.AttendanceEventType(kind), original_employee_id=original,
                           replacement_employee_id=replacement, voided_at=datetime.now(timezone.utc) if voided else None)


@pytest.mark.parametrize('events,workers,evidence,outcome', [
    ([], (1,), 'PRESUMPTIVE_PUBLISHED', 'PRESUMPTIVE_SCHEDULED'),
    ([event('WORKED_AS_SCHEDULED')], (1,), 'CONFIRMED_EVENT', 'CONFIRMED_SCHEDULED'),
    ([event('CALLED_OUT'), event('COVERED_SHIFT', replacement=2)], (2,), 'CONFIRMED_EVENT', 'CONFIRMED_REPLACEMENT'),
    ([event('CALLED_OUT')], (), 'CONFIRMED_EVENT', 'CONFIRMED_ABSENCE'),
    ([event('COVERED_SHIFT', replacement=2), event('COVERED_SHIFT', replacement=3)], (), 'UNRESOLVED', 'UNRESOLVED'),
    ([event('LATE'), event('COVERED_SHIFT', replacement=2)], (), 'UNRESOLVED', 'UNRESOLVED'),
    ([event('LATE')], (), 'UNRESOLVED', 'UNRESOLVED'),
    ([event('CALLED_OUT', voided=True)], (1,), 'PRESUMPTIVE_PUBLISHED', 'PRESUMPTIVE_SCHEDULED'),
    ([event('CALLED_OUT'), event('COVERED_SHIFT', replacement=2, voided=True)], (), 'CONFIRMED_EVENT', 'CONFIRMED_ABSENCE'),
])
def test_parker_lexi_shared_outcome(events, workers, evidence, outcome):
    result = resolve_attendance_outcome(SimpleNamespace(employee_id=1), events, presumptive=True)
    assert result['credited_employee_ids'] == workers
    assert result['evidence_class'] == evidence
    assert result['outcome'] == outcome


def test_weekly_dedup_recency_and_two_workers(scheduling_db):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        setup_pool(db, actor, ids)
        for start, employees in [(date(2026, 7, 5), [ids['alex']] * 3),
                                 (date(2026, 8, 2), [ids['blair']]),
                                 (date(2026, 6, 7), [ids['alex'], ids['blair']])]:
            period = create_draft_period(db, principal=actor, week_start=start)
            period.status = m.SchedulePeriodStatus.PUBLISHED
            for offset, employee in enumerate(employees):
                shift(db, actor, period, ids, employee, start + timedelta(days=offset))
        alex = p.longview_rotation_fairness(db, employee_id=ids['alex'], store_id=ids['south'], before_date=date(2026, 9, 1))
        blair = p.longview_rotation_fairness(db, employee_id=ids['blair'], store_id=ids['south'], before_date=date(2026, 9, 1))
        assert alex.historical_assignment_count == blair.historical_assignment_count == 2
        assert len(alex.credit_details) == 4
        assert p.longview_fairness_rank(alex) < p.longview_fairness_rank(blair)
        assert all(row['evidence_class'] == 'PRESUMPTIVE_PUBLISHED' for row in alex.credit_details)
        future = create_draft_period(db, principal=actor, week_start=date(2026, 11, 1))
        open_shift = shift(db, actor, future, ids)
        diagnostics = []
        chosen, _ = p.choose_employee_for_shift(db, shift=open_shift, longview_diagnostics=diagnostics)
        assert chosen.id == ids['alex']
        # Weekly deduplication is not an attendance adjustment.
        assert all(not item['attendance_adjusted'] for item in diagnostics[0]['candidate_burdens'])
        db.get(m.Employee, ids['alex']).active = False
        db.flush()
        assert ids['alex'] not in [e.id for e in p.longview_rotation_pool(db, store_id=ids['south'], on_date=date(2026, 9, 1))[0]]
        assert p.longview_rotation_fairness(db, employee_id=ids['alex'], store_id=ids['south'], before_date=date(2026, 9, 1)).historical_assignment_count == 2


def test_future_reservations_manual_edit_and_fixed_commitment(scheduling_db):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        setup_pool(db, actor, ids)
        first = create_draft_period(db, principal=actor, week_start=date(2026, 11, 1))
        row = shift(db, actor, first, ids, ids['alex'], locked=True)
        second = create_draft_period(db, principal=actor, week_start=date(2026, 11, 8))
        later = shift(db, actor, second, ids)
        result = p.weekly_longview_travelers(db, period=second, assign=True, principal=actor, planning_date=date(2026, 10, 1))
        assert result[0]['employee_ids'] == [ids['blair']]
        fairness = p.longview_rotation_fairness(db, employee_id=ids['alex'], store_id=ids['south'], before_date=later.shift_date, as_of_date=date(2026, 10, 1))
        assert fairness.historical_assignment_count == 0 and fairness.planned_future_assignment_count == 1
        row.employee_id = ids['blair']; later.employee_id = None; db.flush()
        p.weekly_longview_travelers(db, period=second, assign=True, principal=actor, planning_date=date(2026, 10, 1))
        assert later.employee_id == ids['alex']
        first.status = m.SchedulePeriodStatus.PUBLISHED; db.flush()
        assert p.weekly_longview_travelers(db, period=first)[0]['employee_ids'] == [ids['blair']]


def test_primary_does_not_consume_weekly_traveler_position_and_regeneration(scheduling_db):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        setup_pool(db, actor, ids)
        p.set_special_store_employee_participation(db, principal=actor, employee_id=ids['alex'], store_id=ids['south'], participation=m.SpecialStoreParticipation.PRIMARY)
        # Isolate the traveler rule from the existing supplemental target pass.
        for profile in db.scalars(select(m.EmployeeSchedulingProfile)):
            profile.target_shifts_per_week = 0
        _coverage(db, actor, ids, weekday=1, store_id=ids['south'])
        period = create_draft_period(db, principal=actor, week_start=date(2026, 11, 1))
        for _ in range(2):
            result = p.regenerate_period(db, principal=actor, schedule_period_id=period.id)
            assert result['longview_weekly_travelers'][0]['employee_ids'] == [ids['blair']]
            rows = list(db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id == period.id)))
            assert len(rows) == 1  # no extra labor for traveler
        readiness = scheduling_readiness(db, today=date(2026, 11, 1))
        assert any('Longview traveler scheduled' in row.title for row in readiness.info)


def test_membership_missing_home_never_and_temporary_unavailability(scheduling_db):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        setup_pool(db, actor, ids)
        profile = db.scalar(select(m.EmployeeSchedulingProfile).where(m.EmployeeSchedulingProfile.employee_id == ids['alex']))
        profile.home_store_id = None; db.flush()
        pool, excluded = p.longview_rotation_pool(db, store_id=ids['south'], on_date=date(2026, 11, 1))
        assert [e.id for e in pool] == [ids['blair']]
        assert excluded[0]['reasons'] == ['VANCOUVER_HOME_STORE_REQUIRED']
        period = create_draft_period(db, principal=actor, week_start=date(2026, 11, 1))
        row = shift(db, actor, period, ids)
        db.add(m.EmployeeSchedulingWindow(employee_id=ids['blair'],day_of_week=0,start_time=time.min,end_time=time.max,
            kind=m.SchedulingWindowKind.HARD_UNAVAILABLE,active=True,created_by_principal_id=actor.id,updated_by_principal_id=actor.id));db.flush()
        result = p.weekly_longview_travelers(db, period=period, assign=True, principal=actor)
        assert not result[0]['satisfied'] and result[0]['candidate_failures']
        assert row.employee_id is None and len(p.longview_rotation_pool(db, store_id=ids['south'], on_date=period.week_start_date)[0]) == 1
        readiness = scheduling_readiness(db, today=period.week_start_date)
        assert any('traveler unsatisfied' in item.title for item in readiness.warnings)
        assert any('Vancouver home store required' in item.title for item in readiness.warnings)


def test_repair_cannot_remove_only_lead_to_force_traveler(scheduling_db):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        setup_pool(db, actor, ids)
        p.set_special_store_employee_participation(db, principal=actor, employee_id=ids['alex'], store_id=ids['south'], participation=m.SpecialStoreParticipation.PRIMARY)
        db.get(m.Employee, ids['blair']).scheduling_lead_capable = False
        period = create_draft_period(db, principal=actor, week_start=date(2026, 11, 1))
        row = shift(db, actor, period, ids, ids['alex'])
        result = p.weekly_longview_travelers(db, period=period, assign=True, principal=actor)
        assert not result[0]['satisfied'] and 'WOULD_REMOVE_ONLY_LEAD' in result[0]['reason_codes']
        assert row.employee_id == ids['alex']


def test_six_person_pool_cycles_and_rebuilds_without_phantom_credit(scheduling_db):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        setup_pool(db, actor, ids)
        members = [ids['alex'], ids['blair']]
        for number in range(4):
            employee = m.Employee(full_name=f'Traveler {number}', normalized_name=f'traveler {number}',
                                  active=True, scheduling_active=True, scheduling_lead_capable=True)
            db.add(employee); db.flush(); members.append(employee.id)
        p.configure_special_store(db, principal=actor, store_id=ids['south'], primary_employee_ids=(), rotation_employee_ids=tuple(members))
        for profile in db.scalars(select(m.EmployeeSchedulingProfile)):
            profile.home_store_id = ids['north']; profile.target_shifts_per_week = 0
        _coverage(db, actor, ids, weekday=1, store_id=ids['south'])
        periods = [create_draft_period(db, principal=actor, week_start=date(2027, 1, 3) + timedelta(weeks=n)) for n in range(7)]
        for _ in range(2):
            selected = []
            for period in periods:
                result = p.regenerate_period(db, principal=actor, schedule_period_id=period.id)
                selected.append(result['longview_weekly_travelers'][0]['employee_ids'][0])
            assert selected == members + [members[0]]
        for employee in members:
            fairness = p.longview_rotation_fairness(db, employee_id=employee, store_id=ids['south'], before_date=date(2027, 3, 1))
            assert fairness.historical_assignment_count == 0
        assert list(db.scalars(select(m.ScheduleAttendanceEvent))) == []


def test_recency_beats_lifetime_count_and_never_covered_wins(scheduling_db):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        setup_pool(db, actor, ids)
        for start, employee in [(date(2026, 6, 7) + timedelta(weeks=n), ids['alex']) for n in range(4)] + [(date(2026, 8, 2), ids['blair'])]:
            historical = create_draft_period(db, principal=actor, week_start=start)
            historical.status = m.SchedulePeriodStatus.PUBLISHED
            shift(db, actor, historical, ids, employee)
        period = create_draft_period(db, principal=actor, week_start=date(2026, 11, 1))
        row = shift(db, actor, period, ids)
        p.weekly_longview_travelers(db, period=period, assign=True, principal=actor)
        assert row.employee_id == ids['alex']
        # Remove Blair's one historical assignment: known never now precedes Alex.
        for old in db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.employee_id == ids['blair'])):
            db.delete(old)
        row.employee_id = None; db.flush()
        p.weekly_longview_travelers(db, period=period, assign=True, principal=actor)
        assert row.employee_id == ids['blair']


def test_primary_target_cannot_preempt_weekly_reservation(scheduling_db):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        setup_pool(db, actor, ids)
        p.set_special_store_employee_participation(db, principal=actor, employee_id=ids['alex'], store_id=ids['south'], participation=m.SpecialStoreParticipation.PRIMARY)
        profile = db.scalar(select(m.EmployeeSchedulingProfile).where(m.EmployeeSchedulingProfile.employee_id == ids['alex']))
        profile.target_shifts_per_week = 3
        period = create_draft_period(db, principal=actor, week_start=date(2026, 11, 1))
        row = shift(db, actor, period, ids)
        assert p.choose_employee_for_shift(db, shift=row)[0].id == ids['alex']
        result = p.weekly_longview_travelers(db, period=period, assign=True, principal=actor)
        assert result[0]['employee_ids'] == [ids['blair']]
        assert len(list(db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id == period.id)))) == 1


def test_never_and_nonrotation_are_not_members_and_pto_is_temporary(scheduling_db):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        setup_pool(db, actor, ids)
        db.add(m.EmployeeSchedulingStorePreference(employee_id=ids['alex'], store_id=ids['south'], active=True,
            preference_level=m.StorePreferenceLevel.NEVER, created_by_principal_id=actor.id, updated_by_principal_id=actor.id))
        period = create_draft_period(db, principal=actor, week_start=date(2026, 11, 1))
        shift(db, actor, period, ids)
        db.add(m.TimeOffRequest(employee_id=ids['blair'], start_date=period.week_start_date, end_date=period.week_end_date,
            full_day=True, status=m.TimeOffRequestStatus.APPROVED, reason_category_id=ids['vacation'],
            created_by_principal_id=actor.id, updated_by_principal_id=actor.id));db.flush()
        pool, excluded = p.longview_rotation_pool(db, store_id=ids['south'], on_date=period.week_start_date)
        assert [e.id for e in pool] == [ids['blair']] and excluded[0]['reasons'] == ['STORE_NEVER']
        result = p.weekly_longview_travelers(db, period=period, assign=True, principal=actor)
        assert not result[0]['satisfied'] and 'APPROVED_TIME_OFF' in result[0]['reason_codes']
        p.set_special_store_employee_participation(db, principal=actor, employee_id=ids['blair'], store_id=ids['south'], participation=m.SpecialStoreParticipation.NONE)
        assert not p.longview_rotation_pool(db, store_id=ids['south'], on_date=period.week_start_date)[0]


def test_attendance_facts_share_presumptive_resolution_without_claiming_actual(scheduling_db):
    from app.services.v2_scheduling_attendance_service import attendance_facts_for_shift
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        setup_pool(db, actor, ids)
        period = create_draft_period(db, principal=actor, week_start=date(2026, 8, 2))
        period.status = m.SchedulePeriodStatus.PUBLISHED
        row = shift(db, actor, period, ids, ids['alex'])
        facts = attendance_facts_for_shift(db, shift_id=row.id, as_of_date=date(2026, 8, 10))
        assert facts['actual_worker_ids'] == []
        assert facts['coverage_outcome']['credited_employee_ids'] == (ids['alex'],)
        assert facts['coverage_outcome']['evidence_class'] == 'PRESUMPTIVE_PUBLISHED'
        fairness = p.longview_rotation_fairness(db, employee_id=ids['alex'], store_id=ids['south'], before_date=date(2026, 8, 10))
        assert fairness.credit_details[0]['outcome'] == facts['coverage_outcome']['outcome']


def test_same_day_credit_waits_until_shift_end(scheduling_db, monkeypatch):
    from app.services import v2_scheduling_attendance_service as attendance
    class Clock(datetime):
        hour = 16
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 2, cls.hour, tzinfo=tz)
    monkeypatch.setattr(p, 'datetime', Clock)
    monkeypatch.setattr(attendance, 'datetime', Clock)
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        setup_pool(db, actor, ids)
        period = create_draft_period(db, principal=actor, week_start=date(2026, 8, 2))
        period.status = m.SchedulePeriodStatus.PUBLISHED
        row = shift(db, actor, period, ids, ids['alex'])
        before = p.longview_rotation_fairness(db, employee_id=ids['alex'], store_id=ids['south'], before_date=date(2026, 8, 9))
        assert before.historical_assignment_count == 0 and before.planned_future_assignment_count == 1
        Clock.hour = 18
        after = p.longview_rotation_fairness(db, employee_id=ids['alex'], store_id=ids['south'], before_date=date(2026, 8, 9))
        assert after.historical_assignment_count == 1 and after.planned_future_assignment_count == 0
        assert attendance.attendance_facts_for_shift(db, shift_id=row.id)['coverage_outcome']['evidence_class'] == 'PRESUMPTIVE_PUBLISHED'
