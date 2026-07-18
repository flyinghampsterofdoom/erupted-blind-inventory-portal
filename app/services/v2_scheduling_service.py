from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import Principal
from app.models import (
    Employee,
    EmployeeSchedulingWindow,
    SchedulePeriod,
    SchedulePeriodStatus,
    ScheduleShift,
    ScheduleShiftType,
    SchedulingWindowKind,
    Store,
)
from app.v2.audit import V2AuditEvent, write_v2_audit_event


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


def _lock_period(db: Session, period_id: int) -> SchedulePeriod:
    period = db.execute(
        select(SchedulePeriod).where(SchedulePeriod.id == period_id).with_for_update()
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
) -> SchedulePeriod:
    validate_week(week_start)
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
    if store is None or not store.active:
        errors['store_id'] = 'Choose an active store.'
    employee = None
    if values.employee_id is not None:
        employee = db.get(Employee, values.employee_id)
        if employee is None or not employee.active:
            errors['employee_id'] = 'Choose an active employee.'
    if values.shift_type_id is not None:
        shift_type = db.get(ScheduleShiftType, values.shift_type_id)
        if shift_type is None or not shift_type.active:
            errors['shift_type_id'] = 'Choose an active scheduling shift type.'
    if employee is not None and not allow_hard_unavailability_override:
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
    if errors:
        raise SchedulingValidationError('Check the shift fields.', errors)
    return employee, store  # type: ignore[return-value]


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
    ip: str | None = None,
) -> ShiftMutationOutcome:
    period = _lock_period(db, schedule_period_id)
    _require_draft(period, expected_version=expected_version)
    _validate_shift_input(
        db,
        period=period,
        values=values,
        allowed_store_ids=allowed_store_ids,
        allow_hard_unavailability_override=allow_hard_unavailability_override,
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
        created_by_principal_id=principal.id,
        updated_by_principal_id=principal.id,
        created_at=now,
        updated_at=now,
    )
    db.add(shift)
    period.version += 1
    period.updated_by_principal_id = principal.id
    period.updated_at = now
    db.flush()
    from app.services.v2_scheduling_coverage_service import rebuild_schedule_warnings

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
                      'source_shift_id': shift.source_shift_id},
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
        'employee_note': shift.employee_note,
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
    ip: str | None = None,
) -> ShiftMutationOutcome:
    period = _lock_period(db, schedule_period_id)
    _require_draft(period, expected_version=expected_version)
    shift = db.execute(
        select(ScheduleShift).where(
            ScheduleShift.id == shift_id,
            ScheduleShift.schedule_period_id == period.id,
        ).with_for_update()
    ).scalar_one_or_none()
    if shift is None:
        raise SchedulingValidationError('Shift not found in this schedule.')
    before = _shift_values(shift)
    _validate_shift_input(
        db,
        period=period,
        values=values,
        allowed_store_ids=allowed_store_ids,
        allow_hard_unavailability_override=allow_hard_unavailability_override,
    )
    if allow_hard_unavailability_override and not override_reason.strip():
        raise SchedulingValidationError('An override reason is required.', {'override_reason': 'Enter an override reason.'})
    now = datetime.now(tz=timezone.utc)
    for field in (
        'employee_id', 'store_id', 'shift_date', 'start_time', 'end_time',
        'unpaid_break_minutes', 'shift_type_id', 'is_opener', 'is_closer',
    ):
        setattr(shift, field, getattr(values, field))
    shift.employee_note = (values.employee_note or '').strip() or None
    shift.updated_by_principal_id = principal.id
    shift.updated_at = now
    period.version += 1
    period.updated_by_principal_id = principal.id
    period.updated_at = now
    db.flush()
    from app.services.v2_scheduling_coverage_service import rebuild_schedule_warnings

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
            metadata={'schedule_period_id': period.id, 'employee_id': shift.employee_id, 'shift_id': shift.id},
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
    from app.services.v2_scheduling_coverage_service import rebuild_schedule_warnings

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
                employee_note=source_shift.employee_note,
                source_shift_id=source_shift.id,
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
    ip: str | None = None,
) -> SchedulePeriod:
    from app.models import ScheduleWarning, ScheduleWarningSeverity
    from app.services.v2_scheduling_coverage_service import rebuild_schedule_warnings

    period = _lock_period(db, schedule_period_id)
    _require_draft(period, expected_version=expected_version)
    schedule_store_ids = set(db.execute(
        select(ScheduleShift.store_id).where(ScheduleShift.schedule_period_id == period.id).distinct()
    ).scalars())
    if schedule_store_ids - set(allowed_store_ids):
        raise PermissionError('The schedule contains stores outside the authorized store scope.')
    rebuild_schedule_warnings(db, schedule_period_id=period.id)
    serious = db.execute(
        select(ScheduleWarning).where(
            ScheduleWarning.schedule_period_id == period.id,
            ScheduleWarning.severity == ScheduleWarningSeverity.SERIOUS,
        ).order_by(ScheduleWarning.id)
    ).scalars().all()
    if serious and not allow_serious_warnings:
        raise PermissionError('Publishing this schedule requires permission to publish with serious warnings.')
    if serious and (not confirmed or not override_reason.strip()):
        raise SchedulingValidationError(
            'Publishing with serious warnings requires confirmation and an override reason.',
            {'override_reason': 'Confirm publication and enter an override reason.'},
        )
    current = db.execute(
        select(SchedulePeriod).where(
            SchedulePeriod.week_start_date == period.week_start_date,
            SchedulePeriod.status == SchedulePeriodStatus.PUBLISHED,
        ).with_for_update()
    ).scalar_one_or_none()
    now = datetime.now(tz=timezone.utc)
    if current is not None:
        current.status = SchedulePeriodStatus.ARCHIVED
        current.updated_by_principal_id = principal.id
        current.updated_at = now
    period.status = SchedulePeriodStatus.PUBLISHED
    period.published_by_principal_id = principal.id
    period.published_at = now
    period.updated_by_principal_id = principal.id
    period.updated_at = now
    period.version += 1
    db.flush()
    correlation_id = str(uuid.uuid4())
    warning_ids = [row.id for row in serious]
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
                'superseded_schedule_period_id': current.id if current else None,
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
