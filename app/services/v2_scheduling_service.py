from __future__ import annotations

import uuid
from functools import wraps
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.services.v2_scheduling_pattern_service import clear_base_pattern_annotation
from app.auth import Principal
from app.models import (
    Employee,
    EmployeeSchedulingWindow,
    ScheduleLifecycleStage,
    SchedulePeriod,
    SchedulePeriodStatus,
    ScheduleShift,
    ScheduleShiftType,
    SchedulingWindowKind,
    Store,
)
from app.v2.audit import V2AuditEvent, write_v2_audit_event
from app.services.v2_scheduling_pattern_service import alternating_week_for_date


FEATURE_KEY = 'staff_scheduling_v2'
MAX_NOTE_LENGTH = 2000


class SchedulingValidationError(ValueError):
    def __init__(self, message: str, field_errors: dict[str, str] | None = None):
        super().__init__(message)
        self.field_errors = field_errors or {}


class SchedulingConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class ShiftInput:
    employee_id: int | None
    store_id: int
    shift_date: date
    start_time: time
    end_time: time
    unpaid_break_minutes: int = 0
    shift_type_id: int | None = None
    is_opener: bool = False
    is_closer: bool = False
    employee_note: str = ''
    source_shift_id: int | None = None
    source_store_shift_id: int | None = None


@dataclass(frozen=True)
class ShiftMutationOutcome:
    schedule_period_id: int
    shift_id: int
    version: int
    warning_count: int


def _sunday(value: date) -> bool:
    return value.weekday() == 6


def _minutes(start: time, end: time) -> int:
    return (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)


def scheduled_paid_minutes(shift: ScheduleShift | ShiftInput) -> int:
    return _minutes(shift.start_time, shift.end_time) - int(shift.unpaid_break_minutes)


def lock_scheduling(db: Session) -> None:
    from app.services.v2_scheduling_policy_service import SCHEDULE_AUTOMATION_LOCK_KEY
    db.scalar(select(func.pg_advisory_xact_lock(SCHEDULE_AUTOMATION_LOCK_KEY)))


def scheduling_mutation(function):
    @wraps(function)
    def locked(db, *args, **kwargs):
        lock_scheduling(db)
        return function(db, *args, **kwargs)
    return locked


def atomic_schedule_change(function):
    @wraps(function)
    def atomic(db, *args, **kwargs):
        lock_scheduling(db)
        with db.begin_nested():
            return function(db, *args, **kwargs)
    return atomic


def _lock_period(db: Session, period_id: int) -> SchedulePeriod:
    lock_scheduling(db)
    period = db.execute(
        select(SchedulePeriod).where(SchedulePeriod.id == period_id).with_for_update().execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if period is None:
        raise SchedulingValidationError('Schedule period not found.')
    return period


def _require_draft(period: SchedulePeriod, *, expected_version: int | None = None) -> None:
    if period.status != SchedulePeriodStatus.DRAFT:
        raise SchedulingConflict('Published and archived schedule revisions are immutable.')
    if expected_version is not None and period.version != expected_version:
        raise SchedulingConflict('The schedule changed after it was loaded. Refresh before trying again.')


def _next_revision(db: Session, week_start: date) -> int:
    current = db.execute(
        select(func.max(SchedulePeriod.revision_number)).where(
            SchedulePeriod.week_start_date == week_start
        )
    ).scalar_one()
    return int(current or 0) + 1


def validate_week(week_start: date) -> None:
    if not _sunday(week_start):
        raise SchedulingValidationError('A scheduling week must begin on Sunday.', {'week_start_date': 'Choose a Sunday.'})


def create_draft_period(
    db: Session,
    *,
    principal: Principal,
    week_start: date,
    notes: str = '',
    source_schedule_period_id: int | None = None,
    source_schedule_template_id: int | None = None,
    supersedes_schedule_period_id: int | None = None,
    ip: str | None = None,
    historical: bool = False,
) -> SchedulePeriod:
    lock_scheduling(db)
    validate_week(week_start)
    from app.services.v2_scheduling_history_service import require_admin, require_past_week
    if historical:
        require_admin(principal); require_past_week(db, week_start)
    elif db.scalar(select(SchedulePeriod.id).where(SchedulePeriod.week_start_date == week_start, SchedulePeriod.is_historical.is_(True)).limit(1)):
        raise SchedulingConflict('Historical weeks require the Admin correction workflow.')
    existing = db.execute(
        select(SchedulePeriod.id).where(
            SchedulePeriod.week_start_date == week_start,
            SchedulePeriod.status == SchedulePeriodStatus.DRAFT,
        ).with_for_update()
    ).scalar_one_or_none()
    if existing is not None:
        raise SchedulingConflict('An editable draft already exists for this week.')
    now = datetime.now(tz=timezone.utc)
    row = SchedulePeriod(
        is_historical=historical,
        week_start_date=week_start,
        week_end_date=week_start + timedelta(days=6),
        status=SchedulePeriodStatus.DRAFT,
        revision_number=_next_revision(db, week_start),
        supersedes_schedule_period_id=supersedes_schedule_period_id,
        source_schedule_period_id=source_schedule_period_id,
        source_schedule_template_id=source_schedule_template_id,
        notes=(notes or '').strip() or None,
        version=1,
        created_by_principal_id=principal.id,
        updated_by_principal_id=principal.id,
        created_at=now,
        updated_at=now,
        alternating_week=alternating_week_for_date(week_start),
    )
    db.add(row)
    db.flush()
    write_v2_audit_event(
        db,
        event=V2AuditEvent(
            actor_principal_id=principal.id,
            action='DRAFT_CREATED',
            domain='SCHEDULING',
            entity_type='schedule_period',
            entity_id=row.id,
            timestamp=now,
            correlation_id=str(uuid.uuid4()),
            metadata={
                'schedule_period_id': row.id,
                'week_start_date': week_start.isoformat(),
                'revision_number': row.revision_number,
                'source_schedule_period_id': source_schedule_period_id,
                'schedule_template_id': source_schedule_template_id,
            },
        ),
        ip=ip,
    )
    return row


def _validate_shift_input(
    db: Session,
    *,
    period: SchedulePeriod,
    values: ShiftInput,
    allowed_store_ids: tuple[int, ...],
    allow_hard_unavailability_override: bool,
    exclude_shift_id: int | None = None,
    principal=None, operational_override: bool=False, override_pto: bool=False,
    override_reason: str="", exception_details=None,
) -> tuple[Employee | None, Store]:
    errors: dict[str, str] = {}
    if values.shift_date < period.week_start_date or values.shift_date > period.week_end_date:
        errors['shift_date'] = 'Shift date must fall within the schedule week.'
    span = _minutes(values.start_time, values.end_time)
    if span <= 0:
        errors['end_time'] = 'End time must be later than start time; overnight shifts are not supported.'
    if values.unpaid_break_minutes < 0:
        errors['unpaid_break_minutes'] = 'Break minutes cannot be negative.'
    elif span > 0 and values.unpaid_break_minutes >= span:
        errors['unpaid_break_minutes'] = 'Break minutes must be shorter than the shift.'
    note = (values.employee_note or '').strip()
    if len(note) > MAX_NOTE_LENGTH:
        errors['employee_note'] = f'Keep the employee-visible note to {MAX_NOTE_LENGTH:,} characters or fewer.'
    if values.store_id not in set(allowed_store_ids):
        raise PermissionError('The selected store is outside the authorized store scope.')
    store = db.get(Store, values.store_id)
    if store is None or (not store.active and not period.is_historical):
        errors['store_id'] = 'Choose an active store.'
    employee = None
    if values.employee_id is not None:
        employee = db.get(Employee, values.employee_id)
        if employee is None or (not employee.active and not employee.eligibility_periods and not period.is_historical):
            errors['employee_id'] = 'Choose an active employee.'
    if values.shift_type_id is not None:
        shift_type = db.get(ScheduleShiftType, values.shift_type_id)
        if shift_type is None or (not shift_type.active and not period.is_historical):
            errors['shift_type_id'] = 'Choose an active scheduling shift type.'
    if period.is_historical:
        if employee is None:
            errors['employee_id'] = 'Choose the employee who actually worked.'
        duplicate = db.scalar(select(ScheduleShift.id).where(
            ScheduleShift.schedule_period_id == period.id,
            ScheduleShift.employee_id == values.employee_id,
            ScheduleShift.shift_date == values.shift_date,
            ScheduleShift.start_time < values.end_time,
            ScheduleShift.end_time > values.start_time,
            ScheduleShift.id != (exclude_shift_id or -1)).limit(1))
        if duplicate:
            errors['employee_id'] = 'Historical work cannot overlap another entry for this employee.'
        if errors:
            raise SchedulingValidationError('Check the historical assignment.', errors)
        return employee, store
    from app.services.v2_scheduling_opening_service import special_hours_violation
    hours_violation = special_hours_violation(db,store_id=values.store_id,day=values.shift_date,
        start_time=values.start_time,end_time=values.end_time)
    existing = db.get(ScheduleShift, exclude_shift_id) if exclude_shift_id is not None else None
    empty_existing_position = (values.employee_id is None and existing is not None
        and all(getattr(existing, field) == getattr(values, field)
                for field in ('store_id', 'shift_date', 'start_time', 'end_time', 'unpaid_break_minutes')))
    if hours_violation and not empty_existing_position:
        raise SchedulingValidationError(hours_violation[1])
    if employee is not None and not (allow_hard_unavailability_override or operational_override):
        weekday = (values.shift_date.weekday() + 1) % 7
        hard = db.execute(
            select(EmployeeSchedulingWindow.id).where(
                EmployeeSchedulingWindow.employee_id == employee.id,
                EmployeeSchedulingWindow.day_of_week == weekday,
                EmployeeSchedulingWindow.kind == SchedulingWindowKind.HARD_UNAVAILABLE,
                EmployeeSchedulingWindow.active.is_(True),
                EmployeeSchedulingWindow.start_time < values.end_time,
                EmployeeSchedulingWindow.end_time > values.start_time,
            )
        ).scalar_one_or_none()
        if hard is not None:
            raise PermissionError('This shift conflicts with hard unavailability and requires override permission.')
    if employee is not None and store is not None:
        validate_employee_store_day(
            db,
            schedule_period_id=period.id,
            employee_id=employee.id,
            shift_date=values.shift_date,
            store_id=store.id,
            exclude_shift_id=exclude_shift_id,
        )
        # One authoritative policy evaluator is shared with generation and transfers. The legacy
        # override capability is intentionally narrow: it waives only recurring hard availability.
        from app.services.v2_scheduling_policy_service import evaluate_assignment
        eligibility = evaluate_assignment(
            db, employee_id=employee.id, store_id=store.id, shift_date=values.shift_date,
            schedule_period_id=period.id,
            start_time=values.start_time, end_time=values.end_time,
            unpaid_break_minutes=values.unpaid_break_minutes, exclude_shift_id=exclude_shift_id,
        )
        blocking = [reason for reason in eligibility.reasons
                    if not (allow_hard_unavailability_override and reason.code == 'HARD_WEEKDAY_LOCKOUT')]
        if operational_override:
            from app.services.scheduling_operational_exception_service import validate_reasons
            details=validate_reasons(db,principal,blocking,confirmed=True,reason=override_reason,override_pto=override_pto)
            if exception_details is not None:exception_details.extend(details)
        elif blocking:
            message = '; '.join(reason.message for reason in blocking)
            from app.services.scheduling_operational_exception_service import OPERATIONAL_CODES,PTO_CODES
            if all(r.code in OPERATIONAL_CODES | PTO_CODES for r in blocking):
                message += ' Request an Owner operational exception for this assignment.'
            raise SchedulingValidationError(message, {'employee_id': message})
    if errors:
        raise SchedulingValidationError('Check the shift fields.', errors)
    return employee, store  # type: ignore[return-value]


def validate_employee_store_day(
    db: Session,
    *,
    schedule_period_id: int,
    employee_id: int | None,
    shift_date: date,
    store_id: int,
    exclude_shift_id: int | None = None,
) -> None:
    """Enforce the hard one-store-per-employee-per-calendar-date rule."""
    if employee_id is None:
        return
    statement = select(ScheduleShift).where(
        ScheduleShift.schedule_period_id == schedule_period_id,
        ScheduleShift.employee_id == employee_id,
        ScheduleShift.shift_date == shift_date,
        ScheduleShift.store_id != store_id,
    )
    if exclude_shift_id is not None:
        statement = statement.where(ScheduleShift.id != exclude_shift_id)
    conflict = db.execute(statement.order_by(ScheduleShift.id).limit(1)).scalar_one_or_none()
    if conflict is None:
        return
    conflicting_store = db.get(Store, conflict.store_id)
    store_name = conflicting_store.name if conflicting_store else f'Store {conflict.store_id}'
    weekday = shift_date.strftime('%A')
    message = (
        f'This employee is already scheduled at {store_name} on {weekday}. '
        'Employees may work at only one store per day.'
    )
    raise SchedulingValidationError(message, {'employee_id': message, 'store_id': message})


def _warning_count(db: Session, period_id: int) -> int:
    from app.models import ScheduleWarning

    return int(db.execute(select(func.count()).select_from(ScheduleWarning).where(ScheduleWarning.schedule_period_id == period_id)).scalar_one())


def create_shift(
    db: Session,
    *,
    principal: Principal,
    schedule_period_id: int,
    expected_version: int,
    values: ShiftInput,
    allowed_store_ids: tuple[int, ...],
    allow_hard_unavailability_override: bool = False,
    override_reason: str = '',
    operational_override: bool = False, override_pto: bool = False,
    ip: str | None = None,
) -> ShiftMutationOutcome:
    period = _lock_period(db, schedule_period_id)
    _require_draft(period, expected_version=expected_version)
    from app.services.v2_scheduling_history_service import require_historical_editor
    require_historical_editor(db, period, principal)
    exception_details=[]
    if operational_override:
        from app.services.scheduling_operational_exception_service import authorize
        authorize(db,principal,confirmed=True,reason=override_reason)
    _validate_shift_input(
        db,
        period=period,
        values=values,
        allowed_store_ids=allowed_store_ids,
        allow_hard_unavailability_override=allow_hard_unavailability_override,
        principal=principal,operational_override=operational_override,override_pto=override_pto,
        override_reason=override_reason,exception_details=exception_details,
    )
    if allow_hard_unavailability_override and not override_reason.strip():
        raise SchedulingValidationError('An override reason is required.', {'override_reason': 'Enter an override reason.'})
    now = datetime.now(tz=timezone.utc)
    shift = ScheduleShift(
        schedule_period_id=period.id,
        employee_id=values.employee_id,
        store_id=values.store_id,
        shift_date=values.shift_date,
        start_time=values.start_time,
        end_time=values.end_time,
        unpaid_break_minutes=values.unpaid_break_minutes,
        shift_type_id=values.shift_type_id,
        is_opener=values.is_opener,
        is_closer=values.is_closer,
        employee_note=(values.employee_note or '').strip() or None,
        source_shift_id=values.source_shift_id,
        source_store_shift_id=values.source_store_shift_id,
        created_by_principal_id=principal.id,
        updated_by_principal_id=principal.id,
        created_at=now,
        updated_at=now,
        manually_locked=values.employee_id is not None,
        locked_by_principal_id=principal.id if values.employee_id is not None else None,
        locked_at=now if values.employee_id is not None else None,
        lock_reason='Manual assignment creation.' if values.employee_id is not None else None,
    )
    db.add(shift)
    period.version += 1
    period.updated_by_principal_id = principal.id
    period.updated_at = now
    db.flush()
    from app.services.v2_scheduling_assignments_service import reconcile_lead_designations
    if not period.is_historical:
        from app.services.scheduling_operational_exception_service import reconcile_authorized_assignment
        reconcile_authorized_assignment(db,schedule_period_id=period.id,shift_id=shift.id,exceptions=exception_details)
    from app.services.v2_scheduling_coverage_service import rebuild_schedule_warnings

    if not period.is_historical:
        rebuild_schedule_warnings(db, schedule_period_id=period.id)
    write_v2_audit_event(
        db,
        event=V2AuditEvent(
            actor_principal_id=principal.id,
            action='SHIFT_CREATED',
            domain='SCHEDULING',
            entity_type='schedule_shift',
            entity_id=shift.id,
            store_ids=(shift.store_id,),
            timestamp=now,
            correlation_id=str(uuid.uuid4()),
            after=_shift_values(shift),
            reason=override_reason.strip() or None,
            metadata={'schedule_period_id': period.id, 'employee_id': shift.employee_id, 'shift_id': shift.id,
                      'source_shift_id': shift.source_shift_id, 'operational_exceptions': exception_details, 'operational_override': operational_override,
                      'hard_unavailability_override': allow_hard_unavailability_override},
        ),
        ip=ip,
    )
    return ShiftMutationOutcome(period.id, shift.id, period.version, _warning_count(db, period.id))


def _shift_values(shift: ScheduleShift) -> dict[str, Any]:
    return {
        'employee_id': shift.employee_id,
        'store_id': shift.store_id,
        'shift_date': shift.shift_date.isoformat(),
        'start_time': shift.start_time.isoformat(),
        'end_time': shift.end_time.isoformat(),
        'unpaid_break_minutes': shift.unpaid_break_minutes,
        'shift_type_id': shift.shift_type_id,
        'is_opener': shift.is_opener,
        'is_closer': shift.is_closer,
        'is_lead_of_day': shift.is_lead_of_day,
        'lead_of_day_manually_assigned': shift.lead_of_day_manually_assigned,
        'is_double_coverage': shift.is_double_coverage,
        'double_coverage_manually_assigned': shift.double_coverage_manually_assigned,
        'employee_note': shift.employee_note,
        'source_store_shift_id': shift.source_store_shift_id,
    }


def update_shift(
    db: Session,
    *,
    principal: Principal,
    schedule_period_id: int,
    shift_id: int,
    expected_version: int,
    values: ShiftInput,
    allowed_store_ids: tuple[int, ...],
    allow_hard_unavailability_override: bool = False,
    override_reason: str = '',
    operational_override: bool = False, override_pto: bool = False,
    ip: str | None = None,
) -> ShiftMutationOutcome:
    period = _lock_period(db, schedule_period_id)
    _require_draft(period, expected_version=expected_version)
    from app.services.v2_scheduling_history_service import require_historical_editor
    require_historical_editor(db, period, principal)
    shift = db.execute(
        select(ScheduleShift).where(
            ScheduleShift.id == shift_id,
            ScheduleShift.schedule_period_id == period.id,
        ).with_for_update()
    ).scalar_one_or_none()
    if shift is None:
        raise SchedulingValidationError('Shift not found in this schedule.')
    before = _shift_values(shift)
    if shift.employee_id is not None and values.employee_id is None and not period.is_historical:
        if shift.manually_locked:
            raise SchedulingConflict('Unlock this assignment before removing its employee.')
        from app.auth import Role
        if not principal.active or principal.role not in (Role.ADMIN, Role.MANAGER):
            raise PermissionError('Removing an assignment requires administrator or manager access.')
        if shift.store_id not in allowed_store_ids:
            raise PermissionError('The assignment is outside your store scope.')
    if period.is_historical and (shift.employee_id != values.employee_id or shift.shift_date != values.shift_date):
        shift.is_lead_of_day = False
        shift.lead_of_day_manually_assigned = False
    exception_details=[]
    if operational_override:
        from app.services.scheduling_operational_exception_service import authorize
        authorize(db,principal,confirmed=True,reason=override_reason)
    _validate_shift_input(
        db,
        period=period,
        values=values,
        allowed_store_ids=allowed_store_ids,
        allow_hard_unavailability_override=allow_hard_unavailability_override,
        principal=principal,operational_override=operational_override,override_pto=override_pto,
        override_reason=override_reason,exception_details=exception_details,
        exclude_shift_id=shift.id,
    )
    if allow_hard_unavailability_override and not override_reason.strip():
        raise SchedulingValidationError('An override reason is required.', {'override_reason': 'Enter an override reason.'})
    if values.employee_id is None:
        # Legacy supplemental rows point at an automatic position in this draft.
        # The vacancy becomes an ordinary position; preserve published lineage.
        source = db.get(ScheduleShift, shift.source_shift_id) if shift.is_double_coverage and shift.source_shift_id else None
        if source is not None and source.schedule_period_id == period.id:
            shift.source_shift_id = None
        shift.is_lead_of_day = False
        shift.lead_of_day_manually_assigned = False
        shift.is_double_coverage = False
        shift.double_coverage_manually_assigned = False
    now = datetime.now(tz=timezone.utc)
    for field in (
        'employee_id', 'store_id', 'shift_date', 'start_time', 'end_time',
        'unpaid_break_minutes', 'shift_type_id', 'is_opener', 'is_closer',
    ):
        setattr(shift, field, getattr(values, field))
    clear_base_pattern_annotation(shift)
    shift.employee_note = (values.employee_note or '').strip() or None
    shift.updated_by_principal_id = principal.id
    shift.updated_at = now
    shift.manually_locked = True
    shift.locked_by_principal_id = principal.id
    shift.locked_at = now
    shift.lock_reason = override_reason.strip() or ('Manual assignment removal; leave unfilled until explicitly assigned or unlocked.'
                                                   if before['employee_id'] is not None and shift.employee_id is None else 'Manual schedule edit.')
    period.version += 1
    period.updated_by_principal_id = principal.id
    period.updated_at = now
    db.flush()
    from app.services.v2_scheduling_assignments_service import reconcile_lead_designations
    if not period.is_historical:
        from app.services.scheduling_operational_exception_service import reconcile_authorized_assignment
        reconcile_authorized_assignment(db,schedule_period_id=period.id,shift_id=shift.id,exceptions=exception_details)
    from app.services.v2_scheduling_coverage_service import rebuild_schedule_warnings

    if not period.is_historical:
        rebuild_schedule_warnings(db, schedule_period_id=period.id)
    write_v2_audit_event(
        db,
        event=V2AuditEvent(
            actor_principal_id=principal.id,
            action='SHIFT_CHANGED',
            domain='SCHEDULING',
            entity_type='schedule_shift',
            entity_id=shift.id,
            store_ids=tuple(sorted({before['store_id'], shift.store_id})),
            timestamp=now,
            correlation_id=str(uuid.uuid4()),
            before=before,
            after=_shift_values(shift),
            reason=override_reason.strip() or None,
            metadata={'schedule_period_id': period.id, 'employee_id': shift.employee_id, 'shift_id': shift.id,
                      'operational_exceptions': exception_details, 'operational_override': operational_override,
                      'hard_unavailability_override': allow_hard_unavailability_override,
                      'assignment_removed': before['employee_id'] is not None and shift.employee_id is None},
        ),
        ip=ip,
    )
    return ShiftMutationOutcome(period.id, shift.id, period.version, _warning_count(db, period.id))


def delete_shift(
    db: Session,
    *,
    principal: Principal,
    schedule_period_id: int,
    shift_id: int,
    expected_version: int,
    allowed_store_ids: tuple[int, ...],
    ip: str | None = None,
) -> ShiftMutationOutcome:
    period = _lock_period(db, schedule_period_id)
    _require_draft(period, expected_version=expected_version)
    from app.services.v2_scheduling_history_service import require_historical_editor
    require_historical_editor(db, period, principal)
    shift = db.execute(
        select(ScheduleShift).where(
            ScheduleShift.id == shift_id,
            ScheduleShift.schedule_period_id == period.id,
        ).with_for_update()
    ).scalar_one_or_none()
    if shift is None:
        raise SchedulingValidationError('Shift not found in this schedule.')
    if shift.store_id not in set(allowed_store_ids):
        raise PermissionError('The shift is outside the authorized store scope.')
    before = _shift_values(shift)
    now = datetime.now(tz=timezone.utc)
    db.delete(shift)
    period.version += 1
    period.updated_by_principal_id = principal.id
    period.updated_at = now
    db.flush()
    from app.services.v2_scheduling_assignments_service import reconcile_lead_designations
    if not period.is_historical:
        reconcile_lead_designations(db, schedule_period_id=period.id)
    from app.services.v2_scheduling_coverage_service import rebuild_schedule_warnings

    if not period.is_historical:
        rebuild_schedule_warnings(db, schedule_period_id=period.id)
    write_v2_audit_event(
        db,
        event=V2AuditEvent(
            actor_principal_id=principal.id,
            action='SHIFT_DELETED',
            domain='SCHEDULING',
            entity_type='schedule_shift',
            entity_id=shift_id,
            store_ids=(before['store_id'],),
            timestamp=now,
            correlation_id=str(uuid.uuid4()),
            before=before,
            metadata={'schedule_period_id': period.id, 'employee_id': before['employee_id'], 'shift_id': shift_id},
        ),
        ip=ip,
    )
    return ShiftMutationOutcome(period.id, shift_id, period.version, _warning_count(db, period.id))


def clone_published_revision(
    db: Session,
    *,
    principal: Principal,
    published_period_id: int,
    allowed_store_ids: tuple[int, ...],
    ip: str | None = None,
) -> SchedulePeriod:
    source = _lock_period(db, published_period_id)
    if source.status != SchedulePeriodStatus.PUBLISHED:
        raise SchedulingConflict('Only the current published revision can be cloned for modification.')
    draft = create_draft_period(
        db,
        principal=principal,
        week_start=source.week_start_date,
        notes=source.notes or '',
        source_schedule_period_id=source.id,
        supersedes_schedule_period_id=source.id,
        ip=ip,
    )
    shifts = db.execute(select(ScheduleShift).where(ScheduleShift.schedule_period_id == source.id)).scalars().all()
    disallowed = sorted({row.store_id for row in shifts} - set(allowed_store_ids))
    if disallowed:
        raise PermissionError('The published schedule contains stores outside the authorized scope.')
    now = datetime.now(tz=timezone.utc)
    for source_shift in shifts:
        validate_employee_store_day(
            db,
            schedule_period_id=draft.id,
            employee_id=source_shift.employee_id,
            shift_date=source_shift.shift_date,
            store_id=source_shift.store_id,
        )
        db.add(
            ScheduleShift(
                schedule_period_id=draft.id,
                employee_id=source_shift.employee_id,
                store_id=source_shift.store_id,
                shift_date=source_shift.shift_date,
                start_time=source_shift.start_time,
                end_time=source_shift.end_time,
                unpaid_break_minutes=source_shift.unpaid_break_minutes,
                shift_type_id=source_shift.shift_type_id,
                is_opener=source_shift.is_opener,
                is_closer=source_shift.is_closer,
                is_lead_of_day=source_shift.is_lead_of_day,
                lead_of_day_manually_assigned=source_shift.lead_of_day_manually_assigned,
                is_double_coverage=source_shift.is_double_coverage,
                double_coverage_manually_assigned=source_shift.double_coverage_manually_assigned,
                employee_note=source_shift.employee_note,
                manually_locked=source_shift.manually_locked,
                locked_by_principal_id=source_shift.locked_by_principal_id,
                locked_at=source_shift.locked_at,
                lock_reason=source_shift.lock_reason,
                source_shift_id=source_shift.id,
                source_store_shift_id=source_shift.source_store_shift_id,
                created_by_principal_id=principal.id,
                updated_by_principal_id=principal.id,
                created_at=now,
                updated_at=now,
            )
        )
    db.flush()
    from app.services.v2_scheduling_coverage_service import rebuild_schedule_warnings

    rebuild_schedule_warnings(db, schedule_period_id=draft.id)
    write_v2_audit_event(
        db,
        event=V2AuditEvent(
            actor_principal_id=principal.id,
            action='PUBLISHED_REVISION_CLONED',
            domain='SCHEDULING',
            entity_type='schedule_period',
            entity_id=draft.id,
            timestamp=now,
            correlation_id=str(uuid.uuid4()),
            metadata={'schedule_period_id': draft.id, 'source_schedule_period_id': source.id, 'shift_count': len(shifts)},
        ),
        ip=ip,
    )
    return draft


@atomic_schedule_change
def publish_schedule(
    db: Session,
    *,
    principal: Principal,
    schedule_period_id: int,
    expected_version: int,
    allowed_store_ids: tuple[int, ...],
    allow_serious_warnings: bool = False,
    confirmed: bool = False,
    override_reason: str = '',
    override_pto: bool = False,
    ip: str | None = None,
) -> SchedulePeriod:
    from app.models import ScheduleWarning, ScheduleWarningSeverity
    from app.services.v2_scheduling_coverage_service import rebuild_schedule_warnings

    from app.services.v2_scheduling_policy_service import SCHEDULE_AUTOMATION_LOCK_KEY
    db.scalar(select(func.pg_advisory_xact_lock(SCHEDULE_AUTOMATION_LOCK_KEY)))
    period = _lock_period(db, schedule_period_id)
    if period.is_historical:
        raise SchedulingConflict('Historical entries are attested and locked, never approved or published.')
    _require_draft(period, expected_version=expected_version)
    schedule_store_ids = set(db.execute(
        select(ScheduleShift.store_id).where(ScheduleShift.schedule_period_id == period.id).distinct()
    ).scalars())
    if schedule_store_ids - set(allowed_store_ids):
        raise PermissionError('The schedule contains stores outside the authorized store scope.')
    from app.services.access_control_service import principal_has_permission
    if not principal.active or not principal_has_permission(db, principal=principal,
            permission_key='scheduling.publish', fallback_allowed=principal.role.value in ('ADMIN','MANAGER','LEAD')):
        raise PermissionError('Schedule publication permission is required.')
    from app.services.scheduling_approval_validation import validate_complete_schedule
    serious = validate_complete_schedule(db, period=period, allowed_store_ids=allowed_store_ids,
        principal=principal, allow_serious_warnings=allow_serious_warnings,
        confirmed=confirmed, override_reason=override_reason, override_pto=override_pto)
    from app.models import ScheduleApproval
    approved = db.scalar(select(ScheduleApproval).where(
        ScheduleApproval.schedule_period_id == period.id,
        ScheduleApproval.version == period.version))
    if approved is None:
        raise SchedulingValidationError('Approve the current draft version before publishing.')
    from app.services.v2_scheduling_snapshot_service import verify_approved_content
    approval_snapshot = verify_approved_content(db, period=period, approval=approved)
    current = db.execute(
        select(SchedulePeriod).where(
            SchedulePeriod.week_start_date == period.week_start_date,
            SchedulePeriod.status == SchedulePeriodStatus.PUBLISHED,
        ).with_for_update()
    ).scalar_one_or_none()
    now = datetime.now(tz=timezone.utc)
    if current is not None:
        current.status = SchedulePeriodStatus.ARCHIVED
        current.lifecycle_stage = ScheduleLifecycleStage.CLOSED
        current.updated_by_principal_id = principal.id
        current.updated_at = now
    new_people = set(db.scalars(select(ScheduleShift.employee_id).where(
        ScheduleShift.schedule_period_id==period.id, ScheduleShift.employee_id.is_not(None))))
    previous_people = set(db.scalars(select(ScheduleShift.employee_id).where(
        ScheduleShift.schedule_period_id == current.id, ScheduleShift.employee_id.is_not(None)))) if current else set()
    for employee in db.scalars(select(Employee).where(Employee.id.in_(previous_people | new_people)).order_by(Employee.id).with_for_update()):
        employee.working_context_version += 1
    period.status = SchedulePeriodStatus.PUBLISHED
    period.lifecycle_stage = ScheduleLifecycleStage.PUBLISHED
    period.published_by_principal_id = principal.id
    period.published_at = now
    period.updated_by_principal_id = principal.id
    period.updated_at = now
    period.version += 1
    db.flush()
    from app.services.v2_scheduling_snapshot_service import lifecycle_snapshot
    publication_snapshot = lifecycle_snapshot(db, period=period, principal=principal,
        kind='PUBLICATION', approval_id=approved.id)
    correlation_id = str(uuid.uuid4())
    warning_ids = [row.id for row in serious if row.id is not None]
    write_v2_audit_event(
        db,
        event=V2AuditEvent(
            actor_principal_id=principal.id,
            action='SCHEDULE_PUBLISHED_WITH_WARNINGS' if serious else 'SCHEDULE_PUBLISHED',
            domain='SCHEDULING',
            entity_type='schedule_period',
            entity_id=period.id,
            timestamp=now,
            correlation_id=correlation_id,
            reason=override_reason.strip() or None,
            before={'status': 'DRAFT'},
            after={'status': 'PUBLISHED', 'version': period.version},
            metadata={
                'schedule_period_id': period.id,
                'warning_ids': warning_ids,
                'warning_types': sorted({row.warning_type for row in serious}),
                'overridden_warnings': [{'type': row.warning_type, 'message': row.message} for row in serious],
                'owner_override': bool(serious),
                'superseded_schedule_period_id': current.id if current else None,
                'approved_version': approved.version, 'approval_id': approved.id,
                'approval_snapshot_id': approval_snapshot.id, 'publication_snapshot_id': publication_snapshot.id,
            },
        ),
        ip=ip,
    )
    if current is not None:
        write_v2_audit_event(
            db,
            event=V2AuditEvent(
                actor_principal_id=principal.id,
                action='PUBLISHED_SCHEDULE_SUPERSEDED',
                domain='SCHEDULING',
                entity_type='schedule_period',
                entity_id=current.id,
                timestamp=now,
                correlation_id=correlation_id,
                before={'status': 'PUBLISHED'},
                after={'status': 'ARCHIVED'},
                metadata={'replacement_schedule_period_id': period.id},
            ),
            ip=ip,
        )
    return period


def validate_publication_assignments(db: Session, *, period: SchedulePeriod,
                                    allowed_store_ids: tuple[int, ...],
                                    allow_policy_override: bool = False, override_pto: bool = False) -> list[str]:
    """Revalidate the draft; explicit Owner policy waivers retain structural checks."""
    from app.services.v2_scheduling_coverage_service import validate_coverage_headcount
    from app.models import AuditLog
    from app.services.v2_scheduling_policy_service import evaluate_assignment
    policy_exceptions = []
    def policy_violation(message, code=None):
        from app.services.scheduling_operational_exception_service import OPERATIONAL_CODES
        if not allow_policy_override or (code not in OPERATIONAL_CODES and not (code=="APPROVED_TIME_OFF" and override_pto)):
            raise SchedulingValidationError(message)
        policy_exceptions.append(message)

    rows = list(db.scalars(select(ScheduleShift).where(
        ScheduleShift.schedule_period_id == period.id).order_by(ScheduleShift.id)))
    from app.services.v2_scheduling_opening_service import (
        load_opening_calendar, preparation_start, OpeningConfigurationError,
    )
    calendar = load_opening_calendar(db, store_ids={s.store_id for s in rows},
        start_date=period.week_start_date, end_date=period.week_end_date)
    # Serialize eligibility changes and adjacent-period publication for these people.
    list(db.scalars(select(Employee).where(Employee.id.in_(
        sorted({s.employee_id for s in rows if s.employee_id}))).order_by(Employee.id)
        .with_for_update().execution_options(populate_existing=True)))
    for shift in rows:
        if shift.store_id not in allowed_store_ids:
            raise PermissionError('The schedule contains stores outside the authorized store scope.')
        store = db.get(Store, shift.store_id)
        if store is None or not store.active:
            raise SchedulingValidationError('Every assignment must use an active store.')
        if not period.week_start_date <= shift.shift_date <= period.week_end_date:
            raise SchedulingValidationError('Assignment falls outside its schedule week.')
        if scheduled_paid_minutes(shift) <= 0 or shift.unpaid_break_minutes < 0:
            raise SchedulingValidationError('Assignment has invalid shift times or break duration.')
        if shift.employee_id is None and shift.manually_locked and calendar.closed(shift.store_id, shift.shift_date):
            continue
        if shift.employee_id is None:
            policy_violation(f'Unfilled assignments: #{shift.id} before publication.')
            continue
        try:
            validate_employee_store_day(db, schedule_period_id=period.id,
                employee_id=shift.employee_id, shift_date=shift.shift_date,
                store_id=shift.store_id, exclude_shift_id=shift.id)
        except SchedulingValidationError as exc:
            policy_violation(f'Shift #{shift.id}: {exc}')
        result = evaluate_assignment(db, employee_id=shift.employee_id,
            store_id=shift.store_id, shift_date=shift.shift_date,
            start_time=shift.start_time, end_time=shift.end_time,
            unpaid_break_minutes=shift.unpaid_break_minutes,
            exclude_shift_id=shift.id, schedule_period_id=period.id)
        blocking = list(result.reasons)
        if any(r.code == 'HARD_WEEKDAY_LOCKOUT' for r in blocking):
            from app.services.v2_scheduling_exception_service import shift_lineage
            lineage_ids = [str(row.id) for row in shift_lineage(db, shift)]
            audit = db.scalar(select(AuditLog).where(
                AuditLog.action.in_(('V2:SCHEDULING:SHIFT_CREATED', 'V2:SCHEDULING:SHIFT_CHANGED')),
                AuditLog.meta['entity_type'].as_string() == 'schedule_shift',
                AuditLog.meta['entity_id'].as_string().in_(lineage_ids)).order_by(AuditLog.id.desc()).limit(1))
            evidence = audit.meta if audit else {}
            after = evidence.get('after') or {}
            fields = ('employee_id', 'store_id', 'shift_date', 'start_time', 'end_time', 'unpaid_break_minutes')
            current = _shift_values(shift)
            if (evidence.get('reason') and evidence.get('metadata', {}).get('hard_unavailability_override')
                    and all(after.get(k) == current[k] for k in fields)):
                blocking = [r for r in blocking if r.code != 'HARD_WEEKDAY_LOCKOUT']
        from app.services.scheduling_operational_exception_service import authorized_assignment_codes
        permitted=authorized_assignment_codes(db,shift)
        from app.services.scheduling_operational_exception_service import OPERATIONAL_CODES,PTO_CODES
        blocking=[r for r in blocking if not (r.code in permitted and r.code in OPERATIONAL_CODES | PTO_CODES)]
        for reason in blocking:
            policy_violation(f'Hard assignment restriction on shift #{shift.id}: '+reason.message,reason.code)
        try:
            interval = calendar.interval(shift.store_id, shift.shift_date)
        except OpeningConfigurationError as exc:
            raise SchedulingValidationError(str(exc)) from exc
        if interval is None:
            policy_violation(f'Shift #{shift.id}: assignment on a closed store date.')
            continue
        required_start = preparation_start(shift.shift_date, interval[0])
        if shift.start_time != required_start:
            policy_violation(
                f'Every shift must begin exactly 15 minutes before opening '
                f'(store #{shift.store_id}, {shift.shift_date}: {required_start:%H:%M}).')

    try:
        validate_coverage_headcount(db, period=period)
    except SchedulingValidationError as exc:
        policy_violation(str(exc))
    return policy_exceptions

@atomic_schedule_change
def approve_schedule(db: Session, *, principal: Principal, schedule_period_id: int,
                     expected_version: int, allowed_store_ids: tuple[int, ...],
                     allow_serious_warnings: bool = False, confirmed: bool = False,
                     override_reason: str = '', override_pto: bool = False) -> SchedulePeriod:
    from app.models import ScheduleApproval, ScheduleWarning, ScheduleWarningSeverity
    from app.services.access_control_service import principal_has_permission
    from app.services.v2_scheduling_assignments_service import reconcile_lead_designations
    from app.services.v2_scheduling_coverage_service import rebuild_schedule_warnings
    if not principal.active or not principal_has_permission(db, principal=principal,
            permission_key='scheduling.approve', fallback_allowed=principal.role.value in ('ADMIN','MANAGER','LEAD')):
        raise PermissionError('Schedule approval permission is required.')
    from app.services.v2_scheduling_policy_service import SCHEDULE_AUTOMATION_LOCK_KEY
    db.scalar(select(func.pg_advisory_xact_lock(SCHEDULE_AUTOMATION_LOCK_KEY)))
    period = _lock_period(db, schedule_period_id)
    if period.is_historical:
        raise SchedulingConflict('Historical entries are attested and locked, never approved or published.')
    _require_draft(period, expected_version=expected_version)
    from app.services.scheduling_approval_validation import validate_complete_schedule
    from app.services.v2_scheduling_snapshot_service import (
        verify_approved_content, lifecycle_snapshot)
    serious = validate_complete_schedule(db, period=period, allowed_store_ids=allowed_store_ids,
        principal=principal, allow_serious_warnings=allow_serious_warnings,
        confirmed=confirmed, override_reason=override_reason, override_pto=override_pto)
    approval = db.scalar(select(ScheduleApproval).where(ScheduleApproval.schedule_period_id == period.id,
                                                       ScheduleApproval.version == period.version))
    if approval is not None:
        try:
            verify_approved_content(db, period=period, approval=approval)
            return period
        except SchedulingValidationError:
            # Preserve legacy/stale approval evidence and give the new attestation
            # its own version and identity. Never overwrite an earlier approval.
            period.version += 1
            db.flush()
    approval = ScheduleApproval(schedule_period_id=period.id, version=period.version, actor_principal_id=principal.id)
    db.add(approval); db.flush()
    snapshot = lifecycle_snapshot(db, period=period, principal=principal,
        kind='APPROVAL', approval_id=approval.id)
    write_v2_audit_event(db, event=V2AuditEvent(actor_principal_id=principal.id,
        action='SCHEDULE_APPROVED', domain='SCHEDULING', entity_type='schedule_period',
        reason=override_reason.strip() or None,
        entity_id=period.id, timestamp=datetime.now(timezone.utc),
        metadata={'version': period.version, 'approval_id': approval.id,
                  'owner_override': bool(serious),
                  'overridden_warnings': [{'type': row.warning_type, 'message': row.message} for row in serious],
                  'approval_snapshot_id': snapshot.id, 'checksum': snapshot.checksum}), ip=None)
    return period
