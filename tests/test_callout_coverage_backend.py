"""Coverage uses real authorization, policy, evidence and effective-hour paths."""
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select
from app import models as m
from app.auth import Principal, Role
from app.services.v2_scheduling_attendance_service import attendance_facts_for_shift
from app.services.v2_scheduling_exception_service import (
    cancel_commitment, commitment_eligibility, coverage_assignments,
    effective_shift_segments, record_exception,
)
from app.services.v2_scheduling_policy_service import scheduled_weekly_hours
from app.services.v2_scheduling_service import SchedulingValidationError
from test_v2_scheduling_foundation import scheduling_db
from test_v2_lead_duty import period, shift

DAY = date(2026, 10, 25)
TODAY = DAY - timedelta(days=1)


@pytest.mark.parametrize('role', [Role.ADMIN, Role.MANAGER, Role.LEAD])
def test_ordinary_save_reassign_cancel_preserves_published_intent_and_audit(scheduling_db, role):
    Session, manager, ids, _ = scheduling_db
    actor = Principal(manager.id, manager.username, role, ids['north'], True)
    with Session() as db:
        per = period(db, manager, DAY)
        source = shift(db, manager, per, ids['alex'], ids['north'], DAY)
        existing = shift(db, manager, per, ids['blair'], ids['south'], DAY, lead=False)
        db.flush()
        original = (source.employee_id, source.store_id, source.start_time, source.end_time)
        record_exception(db, principal=actor, shift_id=source.id,
            event_type=m.AttendanceEventType.CALLED_OUT,
            replacement_employee_id=ids['blair'], replace_existing=True, today=TODAY)
        first, = coverage_assignments(db, source)
        assert first.note == ''
        assert (source.employee_id, source.store_id, source.start_time, source.end_time) == original
        assert existing.employee_id == ids['blair'] and existing.store_id == ids['south']
        assert scheduled_weekly_hours(db, employee_id=ids['alex'], shift_date=DAY) == Decimal('0.00')
        assert scheduled_weekly_hours(db, employee_id=ids['blair'], shift_date=DAY) == Decimal('16.00')
        assert attendance_facts_for_shift(db, shift_id=source.id, as_of_date=TODAY)['actual_worker_ids'] == []
        third = m.Employee(full_name='Cameron Relief', normalized_name='cameron relief', active=True)
        db.add(third); db.flush()
        record_exception(db, principal=actor, shift_id=source.id,
            event_type=m.AttendanceEventType.CALLED_OUT,
            replacement_employee_id=third.id, replace_existing=True, today=TODAY)
        second, = coverage_assignments(db, source)
        assert first.voided_at and second.employee_id == third.id
        assert scheduled_weekly_hours(db, employee_id=ids['blair'], shift_date=DAY) == Decimal('8.00')
        assert scheduled_weekly_hours(db, employee_id=third.id, shift_date=DAY) == Decimal('8.00')
        cancel_commitment(db, principal=actor, commitment_id=second.id)
        assert coverage_assignments(db, source) == [] and second.voided_at
        assert scheduled_weekly_hours(db, employee_id=third.id, shift_date=DAY) == Decimal('0.00')
        audits = list(db.scalars(select(m.AuditLog).where(m.AuditLog.action.like('%COVERAGE%'))))
        assert len(audits) == 4
        recorded = next(a for a in audits if a.meta['metadata'].get('replacement_employee_id') == third.id)
        assert recorded.actor_principal_id == actor.id
        assert recorded.meta['metadata']['original_employee_id'] == ids['alex']
        assert recorded.meta['metadata']['store_id'] == ids['north']
        assert recorded.meta['occurred_at']
        assert db.scalar(select(m.AttendancePointEntry)) is None


def test_conflicts_warn_and_preserve_pto_existing_shifts_and_preferences(scheduling_db):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        per = period(db, actor, DAY)
        source = shift(db, actor, per, ids['alex'], ids['north'], DAY)
        existing = shift(db, actor, per, ids['blair'], ids['south'], DAY, lead=False)
        for offset in range(1, 7):
            prior_day = DAY - timedelta(days=offset)
            shift(db, actor, period(db, actor, prior_day), ids['blair'], ids['south'], prior_day, lead=False)
        profile = m.EmployeeSchedulingProfile(employee_id=ids['blair'], maximum_weekly_hours=1,
            approval_weekly_hours=1, created_by_principal_id=actor.id, updated_by_principal_id=actor.id)
        pref = m.EmployeeSchedulingStorePreference(employee_id=ids['blair'], store_id=ids['north'],
            preference_level=m.StorePreferenceLevel.NEVER,
            created_by_principal_id=actor.id, updated_by_principal_id=actor.id)
        pto = m.TimeOffRequest(employee_id=ids['blair'], start_date=DAY, end_date=DAY,
            full_day=True, status=m.TimeOffRequestStatus.APPROVED,
            reason_category_id=ids['vacation'], submitted_by_principal_id=actor.id,
            created_by_principal_id=actor.id, updated_by_principal_id=actor.id)
        db.add_all([profile, pref, pto]); db.flush()
        verdict = commitment_eligibility(db, source, ids['blair'])
        assert verdict.eligible
        codes = {r.code for r in verdict.reasons}
        assert {'APPROVED_TIME_OFF', 'OVERLAPPING_SHIFT', 'STORE_NEVER', 'MAX_WEEKLY_HOURS',
                'COVERAGE_OVERTIME', 'MAX_CONSECUTIVE_DAYS'} <= codes
        assert all(not r.hard for r in verdict.reasons)
        record_exception(db, principal=actor, shift_id=source.id,
            event_type=m.AttendanceEventType.CALLED_OUT,
            replacement_employee_id=ids['blair'], today=TODAY)
        assert pto.status == m.TimeOffRequestStatus.APPROVED
        assert pref.preference_level == m.StorePreferenceLevel.NEVER
        assert profile.maximum_weekly_hours == 1 and existing.employee_id == ids['blair']
        assert db.get(m.ScheduleShift, source.id).employee_id == ids['alex']


@pytest.mark.parametrize('status', [m.AttendanceEventType.CALLED_OUT, m.AttendanceEventType.LATE,
                                    m.AttendanceEventType.LEFT_EARLY, m.AttendanceEventType.NO_CALL_NO_SHOW])
def test_partial_coverage_uses_only_its_interval_and_never_infers_actual_hours(scheduling_db, status):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        source = shift(db, actor, period(db, actor, DAY), ids['alex'], ids['north'], DAY)
        # Lateness/early departure require the scheduled day to have occurred.
        record_exception(db, principal=actor, shift_id=source.id, event_type=status,
            event_at=datetime.combine(DAY, time(17), timezone.utc),
            replacement_employee_id=ids['blair'], coverage_start_time=time(13),
            coverage_end_time=time(17), coverage_break_minutes=0, today=DAY + timedelta(days=1))
        segments = effective_shift_segments(db, source)
        replacement, = [s for s in segments if s.employee_id == ids['blair']]
        assert (replacement.start_time, replacement.end_time) == (time(13), time(17))
        assert replacement.is_coverage and not replacement.is_lead_of_day
        assert scheduled_weekly_hours(db, employee_id=ids['blair'], shift_date=DAY) == Decimal('4.00')
        expected_original = Decimal('4.00') if status in {m.AttendanceEventType.LATE, m.AttendanceEventType.LEFT_EARLY} else Decimal('0.00')
        assert scheduled_weekly_hours(db, employee_id=ids['alex'], shift_date=DAY) == expected_original
        assert attendance_facts_for_shift(db, shift_id=source.id, as_of_date=DAY + timedelta(days=1))['actual_worker_ids'] == []


def test_invalid_replacement_and_interval_rollback_entire_save(scheduling_db):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        source = shift(db, actor, period(db, actor, DAY), ids['alex'], ids['north'], DAY)
        for eid, start in ((ids['inactive'], time(9)), (ids['blair'], time(8))):
            with pytest.raises(SchedulingValidationError):
                record_exception(db, principal=actor, shift_id=source.id,
                    event_type=m.AttendanceEventType.CALLED_OUT,
                    replacement_employee_id=eid, coverage_start_time=start, today=TODAY)
            assert db.scalar(select(m.ScheduleAttendanceEvent)) is None
            assert db.scalar(select(m.ScheduleCoverageCommitment)) is None
        denied = Principal(actor.id, actor.username, Role.STORE, ids['north'], True)
        with pytest.raises(PermissionError):
            record_exception(db, principal=denied, shift_id=source.id,
                event_type=m.AttendanceEventType.CALLED_OUT,
                replacement_employee_id=ids['blair'], today=TODAY)


def test_multiple_actual_coverage_events_count_each_assignment_once(scheduling_db):
    from app.services.v2_scheduling_attendance_service import record_attendance_event
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        for offset in (0, 1):
            day = DAY + timedelta(days=offset)
            source = shift(db, actor, period(db, actor, day), ids['alex'], ids['north'], day)
            result = record_attendance_event(db, principal=actor, shift_id=source.id,
                event_type=m.AttendanceEventType.COVERED_SHIFT, replacement_employee_id=ids['blair'],
                event_at=datetime.combine(day, time(17), timezone.utc), today=DAY + timedelta(days=2))
            assert result.resulting_hours == Decimal(8 * (offset + 1))
        assert scheduled_weekly_hours(db, employee_id=ids['blair'], shift_date=DAY) == Decimal('16.00')


def test_partial_coverage_retains_schedule_clear_guard_for_original_residual(scheduling_db):
    from zoneinfo import ZoneInfo
    from app.services.v2_scheduling_clear_service import preview_clear
    Session, actor, ids, _ = scheduling_db
    admin = Principal(actor.id, actor.username, Role.ADMIN, None, True)
    with Session() as db:
        db.get(m.Employee, ids['alex']).principal_id = actor.id
        source = shift(db, actor, period(db, actor, DAY), ids['alex'], ids['north'], DAY)
        record_exception(db, principal=actor, shift_id=source.id,
            event_type=m.AttendanceEventType.LEFT_EARLY,
            event_at=datetime.combine(DAY, time(17), timezone.utc),
            replacement_employee_id=ids['blair'], coverage_start_time=time(13),
            today=DAY + timedelta(days=1))
        preview = preview_clear(db, principal=admin, kind='confirmed',
            now=datetime.combine(DAY, time(10), ZoneInfo('America/Los_Angeles')))
        assert preview['blocked_by_working_access']
        assert any(row['employee_id'] == ids['alex'] for row in preview['access'])


def test_middle_relief_counts_one_logical_shift_for_original_employee(scheduling_db):
    from app.services.v2_scheduling_policy_service import scheduled_weekly_shift_count, weekly_work_pattern
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        source = shift(db, actor, period(db, actor, DAY), ids['alex'], ids['north'], DAY)
        record_exception(db, principal=actor, shift_id=source.id,
            event_type=m.AttendanceEventType.LATE,
            event_at=datetime.combine(DAY, time(17), timezone.utc),
            replacement_employee_id=ids['blair'], coverage_start_time=time(11),
            coverage_end_time=time(13), today=DAY + timedelta(days=1))
        assert len([s for s in effective_shift_segments(db, source) if s.employee_id == ids['alex']]) == 2
        assert scheduled_weekly_hours(db, employee_id=ids['alex'], shift_date=DAY) == Decimal('6.00')
        assert scheduled_weekly_shift_count(db, employee_id=ids['alex'], shift_date=DAY) == 1
        pattern = weekly_work_pattern(db, employee_id=ids['alex'], shift_date=DAY)
        assert pattern.worked_shifts == pattern.accounted_days == 1


@pytest.mark.parametrize('change', ['store', 'shorten', 'expand_legacy_full'])
def test_coverage_does_not_follow_incompatible_published_revision(scheduling_db, change):
    from app.services.v2_scheduling_exception_service import record_commitment, exception_facts
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        parent_period = period(db, actor, DAY)
        parent = shift(db, actor, parent_period, ids['alex'], ids['north'], DAY)
        kwargs = {} if change == 'expand_legacy_full' else {'start_time': time(13), 'end_time': time(17)}
        assignment = record_commitment(db, principal=actor, shift_id=parent.id,
            employee_id=ids['blair'], today=TODAY, **kwargs)
        parent_period.status = m.SchedulePeriodStatus.ARCHIVED
        child_period = period(db, actor, DAY, revision=2)
        child = shift(db, actor, child_period, ids['alex'],
            ids['south'] if change == 'store' else ids['north'], DAY)
        child.source_shift_id = parent.id
        if change == 'shorten':
            child.start_time = time(10); child.end_time = time(14)
        elif change == 'expand_legacy_full':
            child.start_time = time(8); child.end_time = time(18)
        db.flush()
        assert coverage_assignments(db, child) == []
        assert not any(row.is_coverage for row in effective_shift_segments(db, child))
        assert scheduled_weekly_hours(db, employee_id=ids['blair'], shift_date=DAY) == Decimal('0.00')
        facts = exception_facts(db, child, TODAY)
        assert facts['commitment'] is None
        assert facts['commitment_history'][0]['id'] == assignment.id
        assert db.get(m.ScheduleCoverageCommitment, assignment.id).employee_id == ids['blair']


@pytest.mark.parametrize('partial', [False, True])
def test_coverage_survives_unchanged_published_copy(scheduling_db, partial):
    from app.services.v2_scheduling_exception_service import record_commitment
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        parent_period = period(db, actor, DAY)
        parent = shift(db, actor, parent_period, ids['alex'], ids['north'], DAY)
        assignment = record_commitment(db, principal=actor, shift_id=parent.id,
            employee_id=ids['blair'], today=TODAY,
            **({'start_time': time(13), 'end_time': time(17)} if partial else {}))
        parent_period.status = m.SchedulePeriodStatus.ARCHIVED
        child = shift(db, actor, period(db, actor, DAY, revision=2), ids['alex'], ids['north'], DAY)
        child.source_shift_id = parent.id; db.flush()
        assert coverage_assignments(db, child) == [assignment]
        replacement, = [s for s in effective_shift_segments(db, child) if s.is_coverage]
        assert replacement.start_time == (time(13) if partial else time(9))
        assert scheduled_weekly_hours(db, employee_id=ids['blair'], shift_date=DAY) == Decimal('4.00' if partial else '8.00')


@pytest.mark.parametrize('assertion', [m.AttendanceEventType.WORKED_AS_SCHEDULED,
                                       m.AttendanceEventType.COVERED_SHIFT])
def test_left_early_conflicts_with_full_shift_actual_assertions(scheduling_db, assertion):
    from app.services.v2_scheduling_attendance_service import record_attendance_event
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        source = shift(db, actor, period(db, actor, DAY), ids['alex'], ids['north'], DAY)
        record_attendance_event(db, principal=actor, shift_id=source.id,
            event_type=m.AttendanceEventType.LEFT_EARLY,
            event_at=datetime.combine(DAY, time(15), timezone.utc), today=DAY + timedelta(days=1))
        record_attendance_event(db, principal=actor, shift_id=source.id,
            event_type=assertion,
            replacement_employee_id=ids['blair'] if assertion == m.AttendanceEventType.COVERED_SHIFT else None,
            event_at=datetime.combine(DAY, time(17), timezone.utc), today=DAY + timedelta(days=1))
        fact = attendance_facts_for_shift(db, shift_id=source.id, as_of_date=DAY + timedelta(days=1))
        assert fact['actual_worker_ids'] == []
        assert fact['coverage_outcome']['outcome'] == 'UNRESOLVED'
        assert fact['coverage_outcome']['reason'] == 'AMBIGUOUS_ACTIVE_ATTENDANCE'
        assert effective_shift_segments(db, source) == []


def test_soft_preferences_and_hour_target_warn_without_blocking_coverage(scheduling_db):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        source = shift(db, actor, period(db, actor, DAY), ids['alex'], ids['north'], DAY)
        weekday = (DAY.weekday() + 1) % 7
        states = ['AVAILABLE'] * 7; states[weekday] = 'PREFER_OFF'
        profile = m.EmployeeSchedulingProfile(employee_id=ids['blair'], target_weekly_hours=1,
            weekday_preferences=states, created_by_principal_id=actor.id, updated_by_principal_id=actor.id)
        preference = m.EmployeeSchedulingStorePreference(employee_id=ids['blair'], store_id=ids['north'],
            preference_level=m.StorePreferenceLevel.AVOID,
            created_by_principal_id=actor.id, updated_by_principal_id=actor.id)
        window = m.EmployeeSchedulingWindow(employee_id=ids['blair'], day_of_week=weekday,
            kind=m.SchedulingWindowKind.PREFERRED, start_time=time(9), end_time=time(12),
            created_by_principal_id=actor.id, updated_by_principal_id=actor.id)
        db.add_all([profile, preference, window]); db.flush()
        verdict = commitment_eligibility(db, source, ids['blair'], start_time=time(13), end_time=time(17))
        assert verdict.eligible
        assert {'COVERAGE_ABOVE_HOUR_TARGET', 'COVERAGE_DAY_PREFERENCE',
                'STORE_AVOID', 'COVERAGE_TIME_PREFERENCE'} <= {r.code for r in verdict.reasons}
        assert all(not r.hard for r in verdict.reasons)
        record_exception(db, principal=actor, shift_id=source.id,
            event_type=m.AttendanceEventType.CALLED_OUT, replacement_employee_id=ids['blair'],
            coverage_start_time=time(13), coverage_end_time=time(17), today=TODAY)
        assert coverage_assignments(db, source)[0].employee_id == ids['blair']
        assert profile.weekday_preferences == states and profile.target_weekly_hours == 1
        assert preference.preference_level == m.StorePreferenceLevel.AVOID


def test_new_employee_revision_does_not_inherit_old_employee_absence_marker(scheduling_db):
    from app.services.v2_scheduling_exception_service import exception_facts
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        parent_period = period(db, actor, DAY)
        parent = shift(db, actor, parent_period, ids['alex'], ids['north'], DAY)
        record_exception(db, principal=actor, shift_id=parent.id,
            event_type=m.AttendanceEventType.CALLED_OUT, replacement_employee_id=ids['blair'], today=TODAY)
        parent_period.status = m.SchedulePeriodStatus.ARCHIVED
        child = shift(db, actor, period(db, actor, DAY, revision=2), ids['blair'], ids['north'], DAY)
        child.source_shift_id = parent.id; db.flush()
        facts = exception_facts(db, child, TODAY)
        assert facts['status'] is None and facts['commitment'] is None
        assert len(facts['commitment_history']) == 1
        segments = effective_shift_segments(db, child)
        assert len(segments) == 1 and segments[0].employee_id == ids['blair'] and not segments[0].is_coverage
        assert db.scalar(select(m.ScheduleAttendanceEvent)).original_employee_id == ids['alex']


@pytest.mark.parametrize('change', ['store', 'interval', 'employee', 'date'])
@pytest.mark.parametrize('partial', [False, True])
def test_coverage_never_revives_after_incompatible_intervening_revision(scheduling_db, change, partial):
    from app.services.v2_scheduling_exception_service import record_commitment
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        parent_period = period(db, actor, DAY)
        parent = shift(db, actor, parent_period, ids['alex'], ids['north'], DAY)
        assignment = record_commitment(db, principal=actor, shift_id=parent.id,
            employee_id=ids['blair'], today=TODAY,
            **({'start_time': time(13), 'end_time': time(17)} if partial else {}))
        parent_period.status = m.SchedulePeriodStatus.ARCHIVED
        middle_period = period(db, actor, DAY, revision=2)
        middle = shift(db, actor, middle_period,
            ids['blair'] if change == 'employee' else ids['alex'],
            ids['south'] if change == 'store' else ids['north'],
            DAY + timedelta(days=1) if change == 'date' else DAY)
        middle.source_shift_id = parent.id
        if change == 'interval':
            middle.start_time = time(10); middle.end_time = time(14)
        db.flush()
        assert coverage_assignments(db, middle) == []
        middle_period.status = m.SchedulePeriodStatus.ARCHIVED
        reverted = shift(db, actor, period(db, actor, DAY, revision=3), ids['alex'], ids['north'], DAY)
        reverted.source_shift_id = middle.id; db.flush()
        assert coverage_assignments(db, reverted) == []
        assert not any(row.is_coverage for row in effective_shift_segments(db, reverted))
        assert scheduled_weekly_hours(db, employee_id=ids['blair'], shift_date=DAY) == Decimal('0.00')
        assert db.get(m.ScheduleCoverageCommitment, assignment.id).employee_id == ids['blair']


@pytest.mark.parametrize('action', ['replace', 'cancel'])
@pytest.mark.parametrize('change', ['store', 'employee'])
def test_new_coverage_or_cancel_voids_all_stale_lineage_agreements(scheduling_db, action, change):
    from app.services.v2_scheduling_exception_service import record_commitment
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        parent_period = period(db, actor, DAY)
        parent = shift(db, actor, parent_period, ids['alex'], ids['north'], DAY)
        stale = record_commitment(db, principal=actor, shift_id=parent.id,
            employee_id=ids['blair'], start_time=time(13), end_time=time(17), today=TODAY)
        parent_period.status = m.SchedulePeriodStatus.ARCHIVED
        middle_period = period(db, actor, DAY, revision=2)
        middle = shift(db, actor, middle_period,
            ids['blair'] if change == 'employee' else ids['alex'],
            ids['south'] if change == 'store' else ids['north'], DAY)
        middle.start_time = time(10); middle.end_time = time(14)
        middle.source_shift_id = parent.id; db.flush()
        assert coverage_assignments(db, middle) == []
        if action == 'replace':
            third = m.Employee(full_name='Taylor Relief', normalized_name='taylor relief', active=True)
            db.add(third); db.flush()
            fresh = record_commitment(db, principal=actor, shift_id=middle.id,
                employee_id=third.id, today=TODAY)
            assert stale.voided_at is not None
            assert coverage_assignments(db, middle) == [fresh]
            record_exception(db, principal=actor, shift_id=middle.id, cancel_coverage=True)
            assert fresh.voided_at is not None
        else:
            record_exception(db, principal=actor, shift_id=middle.id, cancel_coverage=True)
            assert stale.voided_at is not None
        assert stale.voided_by_principal_id == actor.id
        middle_period.status = m.SchedulePeriodStatus.ARCHIVED
        reverted = shift(db, actor, period(db, actor, DAY, revision=3), ids['alex'], ids['north'], DAY)
        reverted.source_shift_id = middle.id; db.flush()
        assert coverage_assignments(db, reverted) == []
        assert not any(row.is_coverage for row in effective_shift_segments(db, reverted))
        canceled = list(db.scalars(select(m.AuditLog).where(
            m.AuditLog.action == 'V2:SCHEDULING:COVERAGE_COMMITMENT_CANCELED')))
        assert any(row.meta['entity_id'] == str(stale.id) for row in canceled)
