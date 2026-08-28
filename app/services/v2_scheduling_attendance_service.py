from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import Principal
from app.models import (
    AttendanceEventType,
    Employee,
    EmployeeSchedulingProfile,
    EmployeeSchedulingStorePreference,
    Principal as PrincipalModel,
    ScheduleAttendanceEvent,
    SchedulePeriod,
    SchedulePeriodStatus,
    ScheduleShift,
    StorePreferenceLevel,
)
from app.services.v2_scheduling_policy_service import organization_policy, scheduled_weekly_hours
from app.services.v2_scheduling_roster_service import is_scheduling_candidate
from app.services.v2_scheduling_service import SchedulingValidationError, scheduled_paid_minutes
from app.v2.audit import V2AuditEvent, write_v2_audit_event


@dataclass(frozen=True)
class AttendanceRecordResult:
    event: ScheduleAttendanceEvent
    warnings: tuple[str, ...]
    resulting_hours: Decimal | None = None
    approval_threshold_hours: Decimal | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _audit(
    db: Session, *, principal: Principal, action: str,
    event: ScheduleAttendanceEvent, metadata: dict, ip: str | None,
) -> None:
    write_v2_audit_event(db, event=V2AuditEvent(
        actor_principal_id=principal.id,
        action=action,
        domain='SCHEDULING_ATTENDANCE',
        entity_type='schedule_attendance_event',
        entity_id=event.id,
        store_ids=(db.get(ScheduleShift, event.schedule_shift_id).store_id,),
        timestamp=_now(),
        correlation_id=str(uuid.uuid4()),
        metadata=metadata,
    ), ip=ip)


def _published_shift(db: Session, shift_id: int) -> tuple[ScheduleShift, SchedulePeriod]:
    row = db.execute(select(ScheduleShift, SchedulePeriod).join(SchedulePeriod).where(
        ScheduleShift.id == shift_id).with_for_update()).one_or_none()
    if row is None:
        raise SchedulingValidationError('Scheduled shift not found.')
    shift, period = row
    if shift.employee_id is None:
        raise SchedulingValidationError('Attendance can only be recorded for an assigned shift.')
    # Superseded published revisions become ARCHIVED. published_at is the durable
    # evidence that the exact shift was once authoritative.
    if period.status != SchedulePeriodStatus.PUBLISHED and period.published_at is None:
        raise SchedulingValidationError('Attendance can only be recorded against a published shift.')
    return shift, period


def _active_events(db: Session, shift_id: int) -> list[ScheduleAttendanceEvent]:
    return list(db.execute(select(ScheduleAttendanceEvent).where(
        ScheduleAttendanceEvent.schedule_shift_id == shift_id,
        ScheduleAttendanceEvent.voided_at.is_(None),
    ).order_by(ScheduleAttendanceEvent.created_at, ScheduleAttendanceEvent.id)).scalars())


def _coverage_hours_before(
    db: Session, *, employee_id: int, shift_date: date, excluding_shift_id: int,
) -> Decimal:
    week_start = shift_date - timedelta(days=(shift_date.weekday() + 1) % 7)
    rows = db.execute(select(ScheduleAttendanceEvent, ScheduleShift).join(
        ScheduleShift, ScheduleShift.id == ScheduleAttendanceEvent.schedule_shift_id).where(
        ScheduleAttendanceEvent.event_type == AttendanceEventType.COVERED_SHIFT,
        ScheduleAttendanceEvent.replacement_employee_id == employee_id,
        ScheduleAttendanceEvent.voided_at.is_(None),
        ScheduleAttendanceEvent.schedule_shift_id != excluding_shift_id,
        ScheduleShift.shift_date.between(week_start, week_start + timedelta(days=6)),
    )).all()
    minutes = sum(scheduled_paid_minutes(shift) for _event, shift in rows)
    return (Decimal(minutes) / Decimal(60)).quantize(Decimal('0.01'))


def record_attendance_event(
    db: Session, *, principal: Principal, shift_id: int,
    event_type: AttendanceEventType, event_at: datetime,
    replacement_employee_id: int | None = None, note: str = '',
    override_store_restriction: bool = False, override_reason: str = '',
    today: date | None = None, ip: str | None = None,
) -> AttendanceRecordResult:
    shift, period = _published_shift(db, shift_id)
    if shift.shift_date > (today or _now().date()):
        raise SchedulingValidationError('Attendance cannot be recorded before the scheduled date.')
    if event_at.tzinfo is None or event_at.utcoffset() is None:
        raise SchedulingValidationError('Attendance event timestamp must include a timezone.')
    note = note.strip()
    if len(note) > 2000:
        raise SchedulingValidationError('Attendance note must be 2,000 characters or fewer.')

    active = _active_events(db, shift.id)
    if any(row.event_type == event_type for row in active):
        raise SchedulingValidationError(f'An active {event_type.value} event already exists for this shift.')
    active_types = {row.event_type for row in active}
    if event_type == AttendanceEventType.WORKED_AS_SCHEDULED and active_types & {
        AttendanceEventType.CALLED_OUT, AttendanceEventType.COVERED_SHIFT,
        AttendanceEventType.NO_CALL_NO_SHOW,
    }:
        raise SchedulingValidationError('Worked-as-scheduled conflicts with the recorded absence outcome.')
    if event_type in {AttendanceEventType.CALLED_OUT, AttendanceEventType.NO_CALL_NO_SHOW} and (
        AttendanceEventType.WORKED_AS_SCHEDULED in active_types
        or ({AttendanceEventType.CALLED_OUT, AttendanceEventType.NO_CALL_NO_SHOW} - {event_type}) & active_types
    ):
        raise SchedulingValidationError('This absence outcome conflicts with an existing active event.')

    replacement = None
    warnings: list[str] = []
    resulting_hours = None
    threshold = None
    if event_type == AttendanceEventType.COVERED_SHIFT:
        if AttendanceEventType.WORKED_AS_SCHEDULED in active_types:
            raise SchedulingValidationError(
                'Covered-shift conflicts with a worked-as-scheduled outcome.')
        replacement = db.get(Employee, replacement_employee_id) if replacement_employee_id else None
        if replacement is None:
            raise SchedulingValidationError('Choose an existing replacement employee.')
        if replacement.id == shift.employee_id:
            raise SchedulingValidationError('Replacement employee must differ from the originally scheduled employee.')
        if not is_scheduling_candidate(replacement):
            raise SchedulingValidationError('Replacement employee must be active in Scheduling.')
        overlapping = db.execute(select(ScheduleShift).join(SchedulePeriod).where(
            ScheduleShift.id != shift.id,
            ScheduleShift.employee_id == replacement.id,
            ScheduleShift.shift_date == shift.shift_date,
            ScheduleShift.start_time < shift.end_time,
            ScheduleShift.end_time > shift.start_time,
            SchedulePeriod.status == SchedulePeriodStatus.PUBLISHED,
        )).scalars().first()
        if overlapping is not None:
            raise SchedulingValidationError(
                'Replacement employee has an overlapping published shift and cannot be recorded as coverage.')
        preference = db.execute(select(EmployeeSchedulingStorePreference).where(
            EmployeeSchedulingStorePreference.employee_id == replacement.id,
            EmployeeSchedulingStorePreference.store_id == shift.store_id,
            EmployeeSchedulingStorePreference.active.is_(True),
            EmployeeSchedulingStorePreference.preference_level == StorePreferenceLevel.NEVER,
        )).scalar_one_or_none()
        if preference is not None:
            if not override_store_restriction:
                raise SchedulingValidationError(
                    'Replacement employee is marked Never for this store. Use a deliberate override if they actually worked.')
            if not override_reason.strip():
                raise SchedulingValidationError('A reason is required to override a Never-store restriction.')
            warnings.append('STORE_NEVER_OVERRIDDEN')

        scheduled = scheduled_weekly_hours(
            db, employee_id=replacement.id, shift_date=shift.shift_date)
        prior_coverage = _coverage_hours_before(
            db, employee_id=replacement.id, shift_date=shift.shift_date,
            excluding_shift_id=shift.id)
        shift_hours = (Decimal(scheduled_paid_minutes(shift)) / Decimal(60)).quantize(Decimal('0.01'))
        resulting_hours = scheduled + prior_coverage + shift_hours
        profile = db.execute(select(EmployeeSchedulingProfile).where(
            EmployeeSchedulingProfile.employee_id == replacement.id)).scalar_one_or_none()
        policy = organization_policy(db)
        threshold = (
            profile.approval_weekly_hours
            if profile is not None and profile.approval_weekly_hours is not None
            else policy.weekly_approval_hours)
        if resulting_hours > threshold:
            warnings.append('ACTUAL_COVERAGE_OVER_APPROVAL_THRESHOLD')
    elif replacement_employee_id is not None:
        raise SchedulingValidationError('Replacement employee is only valid for a covered-shift event.')

    event = ScheduleAttendanceEvent(
        schedule_shift_id=shift.id,
        original_employee_id=shift.employee_id,
        replacement_employee_id=replacement.id if replacement else None,
        event_type=event_type,
        event_at=event_at,
        note=note,
        recorded_by_principal_id=principal.id,
    )
    db.add(event)
    db.flush()
    _audit(db, principal=principal, action='ATTENDANCE_EVENT_RECORDED', event=event, ip=ip,
           metadata={
               'schedule_shift_id': shift.id,
               'schedule_period_id': period.id,
               'event_type': event_type.value,
               'original_employee_id': shift.employee_id,
               'replacement_employee_id': event.replacement_employee_id,
               'event_at': event_at.isoformat(),
               'warnings': warnings,
               'override_store_restriction': override_store_restriction,
               'override_reason': override_reason.strip() or None,
           })
    return AttendanceRecordResult(
        event=event, warnings=tuple(warnings), resulting_hours=resulting_hours,
        approval_threshold_hours=threshold)


def void_attendance_event(
    db: Session, *, principal: Principal, event_id: int, reason: str,
    ip: str | None = None,
) -> ScheduleAttendanceEvent:
    reason = reason.strip()
    if not reason:
        raise SchedulingValidationError('A correction reason is required.')
    event = db.execute(select(ScheduleAttendanceEvent).where(
        ScheduleAttendanceEvent.id == event_id).with_for_update()).scalar_one_or_none()
    if event is None:
        raise SchedulingValidationError('Attendance event not found.')
    if event.voided_at is not None:
        raise SchedulingValidationError('Attendance event is already voided.')
    event.voided_at = _now()
    event.voided_by_principal_id = principal.id
    event.void_reason = reason
    db.flush()
    _audit(db, principal=principal, action='ATTENDANCE_EVENT_VOIDED', event=event, ip=ip,
           metadata={'event_type': event.event_type.value, 'reason': reason})
    return event


def attendance_facts_for_shift(db: Session, *, shift_id: int) -> dict:
    shift = db.get(ScheduleShift, shift_id)
    if shift is None:
        raise SchedulingValidationError('Scheduled shift not found.')
    events = list(db.execute(select(ScheduleAttendanceEvent).where(
        ScheduleAttendanceEvent.schedule_shift_id == shift_id).order_by(
        ScheduleAttendanceEvent.created_at, ScheduleAttendanceEvent.id)).scalars())
    active = [row for row in events if row.voided_at is None]
    absent = any(row.event_type in {
        AttendanceEventType.CALLED_OUT, AttendanceEventType.NO_CALL_NO_SHOW,
    } for row in active)
    replacement_ids = [row.replacement_employee_id for row in active
                       if row.event_type == AttendanceEventType.COVERED_SHIFT]
    worked_as_scheduled = any(
        row.event_type == AttendanceEventType.WORKED_AS_SCHEDULED for row in active)
    original_worked = any(row.event_type in {
        AttendanceEventType.WORKED_AS_SCHEDULED,
        AttendanceEventType.LATE,
        AttendanceEventType.OPENED_STORE_LATE,
    } for row in active)
    return {
        'schedule_shift_id': shift.id,
        'scheduled_employee_id': shift.employee_id,
        'store_id': shift.store_id,
        'shift_date': shift.shift_date,
        'is_longview_compatible': True,
        'is_weekend': shift.shift_date.weekday() in (5, 6),
        'scheduled_lead_of_day': shift.is_lead_of_day,
        'worked_as_scheduled': worked_as_scheduled,
        'scheduled_employee_absent': absent,
        'replacement_employee_ids': replacement_ids,
        'actual_worker_ids': (
            ([] if absent or not original_worked else [shift.employee_id]) + replacement_ids),
        'events': events,
    }


def serialize_attendance_event(
    event: ScheduleAttendanceEvent, *, employees: dict[int, Employee],
    principals: dict[int, PrincipalModel],
) -> dict:
    recorder = principals.get(event.recorded_by_principal_id)
    voider = principals.get(event.voided_by_principal_id) if event.voided_by_principal_id else None
    return {
        'id': event.id,
        'event_type': event.event_type.value,
        'event_label': event.event_type.value.replace('_', ' ').title(),
        'original_employee_id': event.original_employee_id,
        'original_employee_name': employees.get(event.original_employee_id).full_name,
        'replacement_employee_id': event.replacement_employee_id,
        'replacement_employee_name': (
            employees.get(event.replacement_employee_id).full_name
            if event.replacement_employee_id in employees else None),
        'event_at': event.event_at.isoformat(),
        'note': event.note,
        'recorded_by': recorder.username if recorder else f'Principal {event.recorded_by_principal_id}',
        'recorded_at': event.created_at.isoformat(),
        'voided': event.voided_at is not None,
        'voided_at': event.voided_at.isoformat() if event.voided_at else None,
        'voided_by': voider.username if voider else None,
        'void_reason': event.void_reason,
    }
