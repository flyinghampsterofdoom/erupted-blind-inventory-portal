"""Audited call-outs and effective coverage without rewriting published intent."""
from app.services.read_scope import active_evidence, read_model
from datetime import datetime, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select, or_
from app.models import (Employee, Principal as PrincipalModel, ScheduleShift, SchedulePeriod,
    SchedulePeriodStatus, ShiftTransferRequest, ShiftTransferStatus, ScheduleAttendanceEvent,
    ScheduleCoverageCommitment, AuditLog)
from app.services.v2_scheduling_service import SchedulingValidationError
from app.services.v2_scheduling_policy_service import evaluate_assignment, organization_policy
from app.v2.audit import V2AuditEvent, write_v2_audit_event


def shift_lineage(db, shift):
    rows, seen = [], set()
    while shift is not None and shift.id not in seen:
        rows.append(shift); seen.add(shift.id)
        source = read_model(db, ScheduleShift, shift.source_shift_id) if shift.source_shift_id else None
        if source and read_model(db, SchedulePeriod, source.schedule_period_id).week_start_date != read_model(db, SchedulePeriod, shift.schedule_period_id).week_start_date:
            break  # Template/previous-week copies start a new publication lineage.
        shift = source
    return rows


def published_provenance(db, shift):
    """Reverse audited transfers on the first published ancestor; fail unknown on broken chains."""
    lineage = shift_lineage(db, shift)
    unknown = {'employee_id': None, 'employee_name': None, 'state': 'AMBIGUOUS',
               'source_shift_id': None}
    if not lineage:
        return unknown
    last = lineage[-1]
    if last.source_shift_id:
        source = db.get(ScheduleShift, last.source_shift_id)
        if source is None or source.id in {s.id for s in lineage}:
            return unknown
    published = [s for s in reversed(lineage) if (p := db.get(SchedulePeriod, s.schedule_period_id))
                 and (p.status == SchedulePeriodStatus.PUBLISHED or p.published_at)]
    if not published:
        return unknown
    source = published[0]
    employee_id = source.employee_id
    period = db.get(SchedulePeriod, source.schedule_period_id)
    transfers = list(db.scalars(select(ShiftTransferRequest).where(
        or_(ShiftTransferRequest.shift_id == source.id,ShiftTransferRequest.counterpart_shift_id == source.id),
        ShiftTransferRequest.status == ShiftTransferStatus.COMPLETED).order_by(
        ShiftTransferRequest.completed_at.desc(), ShiftTransferRequest.id.desc())))
    published_at = period.published_at
    if transfers and published_at is None:
        # Legacy published rows may lack a timestamp; use the publication audit
        # only when it identifies one unambiguous instant for this exact period.
        audits = list(db.scalars(select(AuditLog).where(
            AuditLog.action.in_(('V2:SCHEDULING:SCHEDULE_PUBLISHED',
                                 'V2:SCHEDULING:SCHEDULE_PUBLISHED_WITH_WARNINGS')),
            AuditLog.meta['entity_id'].as_string() == str(period.id),
            AuditLog.meta['entity_type'].as_string() == 'schedule_period')))
        try:
            instants = {datetime.fromisoformat(a.meta['occurred_at']) for a in audits}
        except (KeyError, TypeError, ValueError):
            return unknown
        if len(instants) != 1 or next(iter(instants)).utcoffset() is None:
            return unknown
        published_at = next(iter(instants))
    for transfer in transfers:
        if transfer.completed_at is None:
            return unknown
        if published_at and transfer.completed_at < published_at:
            continue  # A draft transfer predates publication and is not original published intent.
        original=transfer.from_employee_id if transfer.shift_id==source.id else transfer.to_employee_id
        recipient=transfer.to_employee_id if transfer.shift_id==source.id else transfer.from_employee_id
        if recipient != employee_id:
            return unknown
        employee_id = original
    person = db.get(Employee, employee_id) if employee_id else None
    return {'employee_id': employee_id, 'employee_name': person.full_name if person else None,
            'state': 'KNOWN' if person else 'UNASSIGNED', 'source_shift_id': source.id}


def coverage_interval(shift, commitment):
    """Null intervals preserve existing full-shift assignments without backfill."""
    start = commitment.start_time or shift.start_time
    end = commitment.end_time or shift.end_time
    unpaid = commitment.unpaid_break_minutes
    if unpaid is None:
        unpaid = shift.unpaid_break_minutes if (start, end) == (shift.start_time, shift.end_time) else 0
    return start, end, unpaid


def coverage_assignments(db, shift):
    lineage = shift_lineage(db, shift)
    positions = {row.id: index for index, row in enumerate(lineage)}
    assignments = []
    for row in active_evidence(db, ScheduleCoverageCommitment, tuple(positions)):
        source_position = positions[row.schedule_shift_id]
        source = lineage[source_position]
        start, end, unpaid = coverage_interval(source, row)
        elapsed = (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)
        if unpaid < 0 or unpaid >= elapsed:
            continue
        # Every intervening revision must preserve the agreement. A later
        # reversion must never resurrect coverage invalidated by a moved store,
        # changed employee/date, or an incompatible time interval in between.
        compatible = True
        for revision in lineage[:source_position + 1]:
            if (revision.employee_id, revision.shift_date, revision.store_id) != (
                    source.employee_id, source.shift_date, source.store_id):
                compatible = False; break
            if row.start_time is None and (
                    revision.start_time, revision.end_time, revision.unpaid_break_minutes) != (
                    source.start_time, source.end_time, source.unpaid_break_minutes):
                compatible = False; break
            if not (revision.start_time <= start < end <= revision.end_time):
                compatible = False; break
        if not compatible:
            continue
        assignments.append(row)
    return assignments


def _interval(shift, start_time=None, end_time=None, unpaid_break_minutes=None):
    start, end = start_time or shift.start_time, end_time or shift.end_time
    if not (shift.start_time <= start < end <= shift.end_time):
        raise SchedulingValidationError('Coverage hours must be within the original shift.')
    unpaid = unpaid_break_minutes
    if unpaid is None:
        unpaid = shift.unpaid_break_minutes if (start, end) == (shift.start_time, shift.end_time) else 0
    elapsed = (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)
    if unpaid < 0 or unpaid >= elapsed:
        raise SchedulingValidationError('Coverage break must be shorter than coverage hours.')
    return start, end, unpaid


def effective_shift_segments(db, shift):
    """The common effective schedule for hours, work cards and temporary access.

    Each projection is a copy; the published assignment remains untouched.
    An absence removes the original effective hours. Partial relief for a late
    arrival/early departure leaves the original employee's remaining interval.
    """
    from types import SimpleNamespace
    from app.models import AttendanceEventType
    ids = [s.id for s in shift_lineage(db, shift)
           if s.employee_id == shift.employee_id and s.shift_date == shift.shift_date]
    events = active_evidence(db, ScheduleAttendanceEvent, ids)
    commitments = coverage_assignments(db, shift)
    from app.services.v2_scheduling_attendance_service import resolve_attendance_outcome
    if resolve_attendance_outcome(shift, events)['reason'] == 'AMBIGUOUS_ACTIVE_ATTENDANCE':
        return []
    types = {e.event_type for e in events}
    absent = bool(types & {AttendanceEventType.CALLED_OUT, AttendanceEventType.NO_CALL_NO_SHOW})
    actual = {e.replacement_employee_id for e in events if e.event_type == AttendanceEventType.COVERED_SHIFT}
    if len(actual) > 1:
        return []
    def segment(employee_id, start, end, unpaid, commitment=None):
        values = {c.key: getattr(shift, c.key) for c in ScheduleShift.__table__.columns}
        values.update(employee_id=employee_id, start_time=start, end_time=end,
                      unpaid_break_minutes=unpaid, is_coverage=commitment is not None or bool(actual),
                      coverage_assignment_id=commitment.id if commitment else None,
                      original_shift_id=shift.id,
                      original_employee_id=shift.employee_id)
        if commitment is not None or actual:
            values['is_lead_of_day'] = False  # Relief never conveys another employee's Lead role.
        return SimpleNamespace(**values)
    if actual:
        return [segment(next(iter(actual)), shift.start_time, shift.end_time, shift.unpaid_break_minutes)]
    if len(commitments) > 1:
        return []  # Ambiguous legacy evidence must never grant access.
    if commitments:
        row = commitments[0]
        start, end, unpaid = coverage_interval(shift, row)
        result = [segment(row.employee_id, start, end, unpaid, row)]
        if not absent:
            # Break placement is unknown. Allocate the remaining planned break
            # once, capped to each residual interval, rather than duplicating it.
            remaining_break = max(0, shift.unpaid_break_minutes - unpaid)
            for left, right in ((shift.start_time, start), (end, shift.end_time)):
                if left < right:
                    elapsed = (right.hour * 60 + right.minute) - (left.hour * 60 + left.minute)
                    pause = min(remaining_break, elapsed)
                    remaining_break -= pause
                    result.append(segment(shift.employee_id, left, right, pause))
        return result
    if absent or shift.employee_id is None:
        return []
    return [segment(shift.employee_id, shift.start_time, shift.end_time, shift.unpaid_break_minutes)]


def _audit(db, principal, row, action, reason='', warnings=()):
    shift = db.get(ScheduleShift, row.schedule_shift_id)
    start, end, unpaid = coverage_interval(shift, row)
    write_v2_audit_event(db, event=V2AuditEvent(actor_principal_id=principal.id,
        action=action, domain='SCHEDULING', entity_type='schedule_coverage_commitment',
        entity_id=row.id, store_ids=(shift.store_id,), timestamp=datetime.now(timezone.utc),
        correlation_id=str(uuid4()), reason=reason or None,
        metadata={'shift_id': row.schedule_shift_id, 'store_id': shift.store_id,
            'shift_date': shift.shift_date.isoformat(), 'original_employee_id': shift.employee_id,
            'employee_id': row.employee_id, 'replacement_employee_id': row.employee_id,
            'start_time': start.isoformat(), 'end_time': end.isoformat(),
            'unpaid_break_minutes': unpaid, 'note': row.note,
            'warnings': [{'code': r.code, 'message': r.message} for r in warnings]}), ip=None)


def commitment_eligibility(db, shift, employee_id, *, today=None, start_time=None,
                           end_time=None, unpaid_break_minutes=None, coverage_start_time=None,
                           coverage_end_time=None, coverage_break_minutes=None):
    """Employee validity remains binding; scheduling conflicts are warnings."""
    from dataclasses import replace
    from app.services.v2_scheduling_policy_service import ConstraintReason
    start, end, unpaid = _interval(shift, coverage_start_time or start_time,
        coverage_end_time or end_time, coverage_break_minutes if coverage_break_minutes is not None else unpaid_break_minutes)
    result = evaluate_assignment(db, employee_id=employee_id, store_id=shift.store_id,
        shift_date=shift.shift_date, start_time=start, end_time=end,
        unpaid_break_minutes=unpaid, exclude_shift_id=shift.id)
    binding = {'INACTIVE_EMPLOYEE', 'SCHEDULING_INACTIVE', 'SQUARE_INACTIVE', 'AFTER_LAST_EFFECTIVE_DATE'}
    reasons = [replace(reason, hard=reason.code in binding) for reason in result.reasons]
    if result.requires_hour_approval:
        reasons.append(ConstraintReason('COVERAGE_OVERTIME',
            f'Coverage results in {result.resulting_hours} scheduled hours; '
            f'the usual threshold is {result.approval_threshold_hours}.', hard=False))
    from app.models import (EmployeeSchedulingProfile, EmployeeSchedulingStorePreference,
                            EmployeeSchedulingWindow, SchedulingWindowKind, StorePreferenceLevel)
    from app.services.v2_scheduling_lifecycle_service import profile_in_use_clause
    from app.services.v2_scheduling_pattern_service import is_base_workday, scheduling_weekday
    from app.services.v2_scheduling_preference_request_service import approved_preferences
    profile = db.scalar(select(EmployeeSchedulingProfile).where(
        EmployeeSchedulingProfile.employee_id == employee_id, profile_in_use_clause()))
    if profile and profile.target_weekly_hours > 0 and result.resulting_hours > profile.target_weekly_hours:
        reasons.append(ConstraintReason('COVERAGE_ABOVE_HOUR_TARGET',
            f'Coverage results in {result.resulting_hours} scheduled hours; '
            f'the employee target is {profile.target_weekly_hours}.', hard=False))
    approved = approved_preferences(db, employee_id=employee_id, on_date=shift.shift_date)
    weekday = scheduling_weekday(shift.shift_date)
    prefers_off = (weekday in approved.get('off_days', []) or
                  (weekday not in approved.get('work_days', [])
                   and profile is not None and is_base_workday(profile, shift.shift_date) is False))
    if prefers_off:
        reasons.append(ConstraintReason('COVERAGE_DAY_PREFERENCE',
            'The employee prefers this weekday off.', hard=False))
    preference = db.scalar(select(EmployeeSchedulingStorePreference).where(
        EmployeeSchedulingStorePreference.employee_id == employee_id,
        EmployeeSchedulingStorePreference.store_id == shift.store_id,
        EmployeeSchedulingStorePreference.active.is_(True)))
    if ((preference is not None and preference.preference_level == StorePreferenceLevel.AVOID)
            or shift.store_id in approved.get('avoided_stores', [])):
        reasons.append(ConstraintReason('STORE_AVOID',
            'The employee prefers to avoid this store.', hard=False))
    windows = list(db.scalars(select(EmployeeSchedulingWindow).where(
        EmployeeSchedulingWindow.employee_id == employee_id,
        EmployeeSchedulingWindow.day_of_week == weekday,
        EmployeeSchedulingWindow.active.is_(True),
        EmployeeSchedulingWindow.kind.in_((SchedulingWindowKind.PREFERRED, SchedulingWindowKind.AVAILABLE)))))
    available_until = start
    for window in sorted(windows, key=lambda row: row.start_time):
        if window.start_time <= available_until < window.end_time:
            available_until = window.end_time
    if windows and available_until < end:
        reasons.append(ConstraintReason('COVERAGE_TIME_PREFERENCE',
            'Coverage is outside the employee\'s preferred or available time window.', hard=False))
    return replace(result, eligible=not any(r.hard for r in reasons), reasons=tuple(reasons))


def _authorize(db, principal, shift=None):
    from app.services.access_control_service import principal_has_permission
    if not principal.active or not principal_has_permission(db, principal=principal,
            permission_key='scheduling.attendance.record',
            fallback_allowed=principal.role.value in ('ADMIN', 'MANAGER', 'LEAD')):
        raise PermissionError('Attendance recording permission is required.')
    if shift is not None:
        from app.v2.store_scope import list_authorized_stores
        if shift.store_id not in {store.id for store in list_authorized_stores(db, principal)}:
            raise PermissionError('This store is outside your authorized operational scope.')
    from sqlalchemy import func
    from app.services.v2_scheduling_policy_service import SCHEDULE_AUTOMATION_LOCK_KEY
    db.scalar(select(func.pg_advisory_xact_lock(SCHEDULE_AUTOMATION_LOCK_KEY)))


def record_commitment(db, *, principal, shift_id, employee_id, note='', today=None,
                      start_time=None, end_time=None, unpaid_break_minutes=None,
                      operational_override=False, override_pto=False, override_reason=''):
    _authorize(db, principal)
    from app.services.v2_scheduling_attendance_service import _published_shift
    shift, period = _published_shift(db, shift_id)
    _authorize(db, principal, shift)
    if period.status != SchedulePeriodStatus.PUBLISHED:
        raise SchedulingValidationError('Coverage requires the current published shift.')
    if len(note.strip()) > 2000:
        raise SchedulingValidationError('Coverage note must be 2,000 characters or fewer.')
    if employee_id == shift.employee_id:
        raise SchedulingValidationError('Choose a replacement different from the scheduled employee.')
    person = db.scalar(select(Employee).where(Employee.id == employee_id).with_for_update())
    if person is None:
        raise SchedulingValidationError('Choose an existing employee.')
    start, end, unpaid = _interval(shift, start_time, end_time, unpaid_break_minutes)
    result = commitment_eligibility(db, shift, employee_id, today=today,
        start_time=start, end_time=end, unpaid_break_minutes=unpaid)
    if not result.eligible:
        raise SchedulingValidationError('; '.join(r.message for r in result.reasons if r.hard))
    lineage = shift_lineage(db, shift)
    db.scalar(select(ScheduleShift).where(ScheduleShift.id == lineage[-1].id).with_for_update())
    for old in active_evidence(db, ScheduleCoverageCommitment, [s.id for s in lineage]):
        cancel_commitment(db, principal=principal, commitment_id=old.id,
                          reason='Coverage reassigned or updated.')
    row = ScheduleCoverageCommitment(schedule_shift_id=shift.id, employee_id=employee_id,
        recorded_by_principal_id=principal.id, note=note.strip(),
        start_time=start if start_time is not None or end_time is not None else None,
        end_time=end if start_time is not None or end_time is not None else None,
        unpaid_break_minutes=unpaid if (unpaid_break_minutes is not None
            or start_time is not None or end_time is not None) else None)
    for eid in sorted({shift.employee_id, employee_id} - {None}):
        person = db.scalar(select(Employee).where(Employee.id == eid).with_for_update())
        person.working_context_version += 1
    db.add(row); db.flush()
    _audit(db, principal, row, 'COVERAGE_COMMITTED', warnings=result.reasons)
    return row


def cancel_commitment(db, *, principal, commitment_id, reason=''):
    _authorize(db, principal)
    if len(reason.strip()) > 2000:
        raise SchedulingValidationError('Cancellation note must be 2,000 characters or fewer.')
    row = db.scalar(select(ScheduleCoverageCommitment).where(
        ScheduleCoverageCommitment.id == commitment_id).with_for_update())
    if row is None or row.voided_at:
        raise SchedulingValidationError('Coverage is missing or already canceled.')
    shift = db.get(ScheduleShift, row.schedule_shift_id)
    _authorize(db, principal, shift)
    for eid in sorted({shift.employee_id, row.employee_id} - {None}):
        person = db.scalar(select(Employee).where(Employee.id == eid).with_for_update())
        person.working_context_version += 1
    row.voided_at = datetime.now(timezone.utc); row.voided_by_principal_id = principal.id
    row.void_reason = reason.strip() or 'Coverage canceled.'
    db.flush(); _audit(db, principal, row, 'COVERAGE_COMMITMENT_CANCELED', row.void_reason)
    return row


def exception_facts(db, shift, today):
    from app.services.v2_scheduling_attendance_service import resolve_attendance_outcome
    ids = [s.id for s in shift_lineage(db, shift)]
    history = list(db.scalars(select(ScheduleCoverageCommitment).where(
        ScheduleCoverageCommitment.schedule_shift_id.in_(ids)).order_by(ScheduleCoverageCommitment.id)))
    current_ids = [s.id for s in shift_lineage(db, shift)
                   if s.employee_id == shift.employee_id and s.shift_date == shift.shift_date]
    events = active_evidence(db, ScheduleAttendanceEvent, current_ids)
    outcome = resolve_attendance_outcome(shift, events, db=db, presumptive=shift.shift_date < today)
    active = coverage_assignments(db, shift)
    now = datetime.now(ZoneInfo(organization_policy(db).timezone_name))
    ended = shift.shift_date < today or (shift.shift_date == today == now.date() and shift.end_time <= now.time())
    status = None
    if events or active:
        if outcome['outcome'] == 'UNRESOLVED' or len(active) > 1:
            status = 'Coverage unresolved/conflicting'
        elif outcome['outcome'] == 'PRESUMPTIVE_REPLACEMENT':
            status = 'Coverage presumed completed'
        elif outcome['outcome'] == 'CONFIRMED_REPLACEMENT':
            status = 'Actual coverage confirmed'
        elif outcome['outcome'] == 'CONFIRMED_ABSENCE':
            status = ('Coverage unresolved/conflicting' if ended else
                      'Replacement committed' if active else 'Coverage needed')
        elif active:
            status = 'Coverage unresolved/conflicting' if ended else 'Replacement committed'
    def name(eid):
        person = db.get(Employee, eid)
        return person.full_name if person else 'Unknown employee'
    return {'status': status, 'original': published_provenance(db, shift),
        'actual_worker_names': [name(eid) for eid in outcome['credited_employee_ids']]
            if outcome['evidence_class'] == 'CONFIRMED_EVENT' else [],
        'actual_evidence': outcome['evidence_class'],
        'commitment': {'id': active[0].id, 'employee_id': active[0].employee_id,
                       'employee_name': name(active[0].employee_id),
                       'start_time': coverage_interval(shift, active[0])[0].isoformat(),
                       'end_time': coverage_interval(shift, active[0])[1].isoformat(),
                       'unpaid_break_minutes': coverage_interval(shift, active[0])[2]} if len(active) == 1 else None,
        'commitment_history': [{'id': r.id, 'employee_name': name(r.employee_id), 'note': r.note,
            'start_time': coverage_interval(shift, r)[0].isoformat(),
            'end_time': coverage_interval(shift, r)[1].isoformat(),
            'recorded_at': r.created_at.isoformat(),
            'recorded_by': db.get(PrincipalModel, r.recorded_by_principal_id).username,
            'voided': r.voided_at is not None, 'void_reason': r.void_reason} for r in history]}


def record_exception(db, *, principal, shift_id, event_type=None, event_at=None,
                     replacement_employee_id=None, commitment_employee_id=None,
                     actual_employee_id=None, actual_event_at=None, note='',
                     override_store_restriction=False, override_reason='', today=None, ip=None,
                     operational_override=False, override_pto=False,
                     coverage_start_time=None, coverage_end_time=None, coverage_break_minutes=None,
                     replace_existing=False, cancel_coverage=False):
    """One save atomically updates absence, effective coverage and audited history."""
    from app.models import AttendanceEventType
    from app.services.v2_scheduling_attendance_service import (
        record_attendance_event, void_attendance_event, _published_shift)
    ordinary = {AttendanceEventType.CALLED_OUT, AttendanceEventType.LATE,
                AttendanceEventType.LEFT_EARLY, AttendanceEventType.NO_CALL_NO_SHOW}
    if event_type in ordinary and replacement_employee_id is not None:
        if commitment_employee_id not in (None, replacement_employee_id):
            raise SchedulingValidationError('Choose one replacement employee.')
        commitment_employee_id = replacement_employee_id
        replacement_employee_id = None
    if event_type is None and commitment_employee_id is None and actual_employee_id is None and not cancel_coverage:
        raise SchedulingValidationError('Choose an exception to record.')
    if actual_employee_id and event_type == AttendanceEventType.COVERED_SHIFT:
        raise SchedulingValidationError('Record actual coverage only once.')
    if cancel_coverage and commitment_employee_id is not None:
        raise SchedulingValidationError('Choose replacement coverage or cancellation.')
    _authorize(db, principal)
    results = []
    with db.begin_nested():
        source, _period = _published_shift(db, shift_id)
        _authorize(db, principal, source)
        ids = [s.id for s in shift_lineage(db, source)]
        existing = active_evidence(db, ScheduleCoverageCommitment, ids)
        people = sorted({x for x in (source.employee_id, commitment_employee_id,
                                    actual_employee_id, replacement_employee_id,
                                    *(row.employee_id for row in existing)) if x})
        from app.services.inventory_working_store_service import lock_employee
        for eid in people:
            lock_employee(db, eid)
        if event_type is not None:
            if replace_existing:
                ids = [s.id for s in shift_lineage(db, source)
                       if s.employee_id == source.employee_id and s.shift_date == source.shift_date]
                for old in active_evidence(db, ScheduleAttendanceEvent, ids):
                    if old.event_type in ordinary or (event_type in ordinary and old.event_type == AttendanceEventType.WORKED_AS_SCHEDULED):
                        void_attendance_event(db, principal=principal, event_id=old.id,
                            reason='Attendance status updated.', ip=ip)
            if event_at is None:
                event_at = datetime.now(timezone.utc)
                if today is not None:
                    local = event_at.astimezone(ZoneInfo(organization_policy(db).timezone_name))
                    if local.date() > today:
                        event_at = datetime.combine(today, source.end_time, local.tzinfo)
            results.append(record_attendance_event(db, principal=principal, shift_id=shift_id,
                event_type=event_type, event_at=event_at, replacement_employee_id=replacement_employee_id,
                note=note, override_store_restriction=override_store_restriction,
                override_reason=override_reason, today=today, ip=ip))
        if commitment_employee_id is not None:
            record_commitment(db, principal=principal, shift_id=shift_id,
                employee_id=commitment_employee_id, note=note, today=today,
                start_time=coverage_start_time, end_time=coverage_end_time,
                unpaid_break_minutes=coverage_break_minutes)
        elif cancel_coverage:
            for row in existing:
                cancel_commitment(db, principal=principal, commitment_id=row.id)
        if actual_employee_id:
            results.append(record_attendance_event(db, principal=principal, shift_id=shift_id,
                event_type=AttendanceEventType.COVERED_SHIFT,
                event_at=actual_event_at or datetime.now(timezone.utc),
                replacement_employee_id=actual_employee_id, note=note,
                override_store_restriction=override_store_restriction, override_reason=override_reason,
                today=today, ip=ip))
        for eid in people:
            db.get(Employee, eid).working_context_version += 1
        db.flush()
    return results


def expected_employee_id(db, shift):
    """Effective prospective coverage; a commitment is not a completion assertion."""
    # An assignment edit starts new prospective responsibility. Older employee
    # exceptions remain historical evidence, not authority over the new assignee.
    ids = [s.id for s in shift_lineage(db, shift)
           if s.employee_id == shift.employee_id and s.shift_date == shift.shift_date]
    events = active_evidence(db, ScheduleAttendanceEvent, ids)
    from app.models import AttendanceEventType
    from app.services.v2_scheduling_attendance_service import resolve_attendance_outcome
    outcome = resolve_attendance_outcome(shift, events)
    if outcome['reason'] == 'AMBIGUOUS_ACTIVE_ATTENDANCE':
        return None
    actual = {e.replacement_employee_id for e in events if e.event_type == AttendanceEventType.COVERED_SHIFT}
    if len(actual) == 1:
        return next(iter(actual))
    if len(actual) > 1:
        return None
    commitments = coverage_assignments(db, shift)
    if len(commitments) == 1:
        return commitments[0].employee_id
    if len(commitments) > 1 or any(e.event_type in (AttendanceEventType.CALLED_OUT, AttendanceEventType.NO_CALL_NO_SHOW) for e in events):
        return None
    return shift.employee_id
