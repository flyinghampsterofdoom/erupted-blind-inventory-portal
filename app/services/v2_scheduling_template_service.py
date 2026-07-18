from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.auth import Principal
from app.models import (
    Employee,
    SchedulePeriod,
    SchedulePeriodStatus,
    ScheduleShift,
    ScheduleShiftType,
    ScheduleTemplate,
    ScheduleTemplateShift,
    ShiftTemplate,
    Store,
    TimeOffReasonCategory,
)
from app.services.v2_scheduling_coverage_service import rebuild_schedule_warnings
from app.services.v2_scheduling_service import SchedulingConflict, SchedulingValidationError, create_draft_period, validate_week
from app.v2.audit import V2AuditEvent, write_v2_audit_event


@dataclass(frozen=True)
class CopySelection:
    employee_id: int | None = None
    store_id: int | None = None
    shift_ids: tuple[int, ...] = ()
    source_start_date: date | None = None
    source_end_date: date | None = None


@dataclass(frozen=True)
class CopyOutcome:
    schedule_period_ids: tuple[int, ...]
    shift_count: int
    correlation_id: str


def _audit(
    db: Session,
    *,
    principal: Principal,
    action: str,
    entity_type: str,
    entity_id: int,
    store_ids: tuple[int, ...] = (),
    metadata: dict | None = None,
    ip: str | None = None,
    correlation_id: str | None = None,
) -> None:
    write_v2_audit_event(
        db,
        event=V2AuditEvent(
            actor_principal_id=principal.id,
            action=action,
            domain='SCHEDULING',
            entity_type=entity_type,
            entity_id=entity_id,
            store_ids=store_ids,
            timestamp=datetime.now(tz=timezone.utc),
            correlation_id=correlation_id or str(uuid.uuid4()),
            metadata=metadata or {},
        ),
        ip=ip,
    )


def create_shift_type(
    db: Session,
    *,
    principal: Principal,
    name: str,
    description: str = '',
    display_order: int = 0,
    ip: str | None = None,
) -> ScheduleShiftType:
    if not name.strip():
        raise SchedulingValidationError('Shift type name is required.')
    now = datetime.now(tz=timezone.utc)
    row = ScheduleShiftType(
        name=name.strip(), description=description.strip() or None, display_order=display_order, active=True,
        created_by_principal_id=principal.id, updated_by_principal_id=principal.id,
        created_at=now, updated_at=now,
    )
    db.add(row)
    db.flush()
    _audit(db, principal=principal, action='SHIFT_TYPE_CHANGED', entity_type='schedule_shift_type',
           entity_id=row.id, metadata={'name': row.name}, ip=ip)
    return row


def create_time_off_reason_category(
    db: Session,
    *,
    principal: Principal,
    name: str,
    display_order: int = 0,
    ip: str | None = None,
) -> TimeOffReasonCategory:
    if not name.strip():
        raise SchedulingValidationError('Time-off reason category name is required.')
    now = datetime.now(tz=timezone.utc)
    row = TimeOffReasonCategory(
        name=name.strip(), display_order=display_order, active=True,
        created_by_principal_id=principal.id, updated_by_principal_id=principal.id,
        created_at=now, updated_at=now,
    )
    db.add(row)
    db.flush()
    _audit(db, principal=principal, action='TIME_OFF_REASON_CHANGED', entity_type='time_off_reason_category',
           entity_id=row.id, metadata={'name': row.name}, ip=ip)
    return row


def create_shift_template(
    db: Session,
    *,
    principal: Principal,
    store_id: int,
    day_of_week: int,
    start_time: time,
    end_time: time,
    unpaid_break_minutes: int,
    allowed_store_ids: tuple[int, ...],
    shift_type_id: int | None = None,
    is_opener: bool = False,
    is_closer: bool = False,
    note: str = '',
    ip: str | None = None,
) -> ShiftTemplate:
    if store_id not in set(allowed_store_ids):
        raise PermissionError('The selected store is outside the authorized store scope.')
    store = db.get(Store, store_id)
    if store is None or not store.active:
        raise SchedulingValidationError('Choose an active store.')
    span = (end_time.hour * 60 + end_time.minute) - (start_time.hour * 60 + start_time.minute)
    if not 0 <= day_of_week <= 6 or span <= 0 or unpaid_break_minutes < 0 or unpaid_break_minutes >= span:
        raise SchedulingValidationError('Enter a valid shift-template day, time, and break.')
    if shift_type_id is not None:
        shift_type = db.get(ScheduleShiftType, shift_type_id)
        if shift_type is None or not shift_type.active:
            raise SchedulingValidationError('Choose an active shift type.')
    now = datetime.now(tz=timezone.utc)
    row = ShiftTemplate(
        store_id=store_id, day_of_week=day_of_week, start_time=start_time, end_time=end_time,
        unpaid_break_minutes=unpaid_break_minutes, shift_type_id=shift_type_id,
        is_opener=is_opener, is_closer=is_closer, note=note.strip() or None, active=True,
        created_by_principal_id=principal.id, updated_by_principal_id=principal.id,
        created_at=now, updated_at=now,
    )
    db.add(row)
    db.flush()
    _audit(db, principal=principal, action='SHIFT_TEMPLATE_CHANGED', entity_type='shift_template',
           entity_id=row.id, store_ids=(store_id,), metadata={'store_id': store_id}, ip=ip)
    return row


def save_schedule_template(
    db: Session,
    *,
    principal: Principal,
    name: str,
    source_period_ids: tuple[int, ...],
    allowed_store_ids: tuple[int, ...],
    description: str = '',
    ip: str | None = None,
) -> ScheduleTemplate:
    if not name.strip() or not source_period_ids:
        raise SchedulingValidationError('Template name and at least one source week are required.')
    periods = db.execute(
        select(SchedulePeriod).where(SchedulePeriod.id.in_(source_period_ids)).order_by(SchedulePeriod.week_start_date)
    ).scalars().all()
    if len(periods) != len(set(source_period_ids)):
        raise SchedulingValidationError('One or more source schedule periods were not found.')
    first_start = periods[0].week_start_date
    expected = [first_start + timedelta(days=7 * index) for index in range(len(periods))]
    if [row.week_start_date for row in periods] != expected:
        raise SchedulingValidationError('Schedule-template source weeks must be consecutive.')
    shifts = db.execute(
        select(ScheduleShift).where(ScheduleShift.schedule_period_id.in_([row.id for row in periods]))
    ).scalars().all()
    store_ids = {row.store_id for row in shifts}
    if store_ids - set(allowed_store_ids):
        raise PermissionError('The template source contains stores outside the authorized scope.')
    now = datetime.now(tz=timezone.utc)
    template = ScheduleTemplate(
        name=name.strip(), description=description.strip() or None, week_count=len(periods), active=True,
        created_by_principal_id=principal.id, updated_by_principal_id=principal.id,
        created_at=now, updated_at=now,
    )
    db.add(template)
    db.flush()
    for shift in shifts:
        db.add(ScheduleTemplateShift(
            schedule_template_id=template.id,
            day_offset=(shift.shift_date - first_start).days,
            employee_id=shift.employee_id,
            store_id=shift.store_id,
            start_time=shift.start_time,
            end_time=shift.end_time,
            unpaid_break_minutes=shift.unpaid_break_minutes,
            shift_type_id=shift.shift_type_id,
            is_opener=shift.is_opener,
            is_closer=shift.is_closer,
            note=shift.employee_note,
            source_shift_id=shift.id,
            created_at=now,
        ))
    db.flush()
    _audit(db, principal=principal, action='SCHEDULE_TEMPLATE_CHANGED', entity_type='schedule_template',
           entity_id=template.id, store_ids=tuple(sorted(store_ids)), metadata={'schedule_template_id': template.id,
           'source_schedule_period_ids': [row.id for row in periods], 'week_count': template.week_count,
           'shift_count': len(shifts)}, ip=ip)
    return template


def _target_draft(
    db: Session,
    *,
    principal: Principal,
    week_start: date,
    mode: str,
    source_period_id: int | None,
    source_template_id: int | None,
    ip: str | None,
) -> SchedulePeriod:
    clean_mode = mode.strip().upper()
    if clean_mode not in {'MERGE', 'REPLACE'}:
        raise SchedulingValidationError('Copy mode must explicitly be MERGE or REPLACE.')
    draft = db.execute(
        select(SchedulePeriod).where(
            SchedulePeriod.week_start_date == week_start,
            SchedulePeriod.status == SchedulePeriodStatus.DRAFT,
        ).with_for_update()
    ).scalar_one_or_none()
    if draft is None:
        return create_draft_period(
            db, principal=principal, week_start=week_start,
            source_schedule_period_id=source_period_id,
            source_schedule_template_id=source_template_id,
            ip=ip,
        )
    if clean_mode == 'REPLACE':
        db.execute(delete(ScheduleShift).where(ScheduleShift.schedule_period_id == draft.id))
        draft.version += 1
        draft.updated_by_principal_id = principal.id
        draft.updated_at = datetime.now(tz=timezone.utc)
        db.flush()
    return draft


def copy_schedule_periods(
    db: Session,
    *,
    principal: Principal,
    source_period_ids: tuple[int, ...],
    target_week_start: date,
    allowed_store_ids: tuple[int, ...],
    mode: str,
    selection: CopySelection = CopySelection(),
    ip: str | None = None,
) -> CopyOutcome:
    validate_week(target_week_start)
    if not source_period_ids:
        raise SchedulingValidationError('Choose at least one source schedule period.')
    periods = db.execute(
        select(SchedulePeriod).where(SchedulePeriod.id.in_(source_period_ids)).order_by(SchedulePeriod.week_start_date)
    ).scalars().all()
    if len(periods) != len(set(source_period_ids)):
        raise SchedulingValidationError('One or more source schedule periods were not found.')
    first_start = periods[0].week_start_date
    expected = [first_start + timedelta(days=7 * index) for index in range(len(periods))]
    if [row.week_start_date for row in periods] != expected:
        raise SchedulingValidationError('Copied schedule weeks must be consecutive.')
    shifts = db.execute(
        select(ScheduleShift).where(ScheduleShift.schedule_period_id.in_([row.id for row in periods]))
    ).scalars().all()
    if selection.employee_id is not None:
        shifts = [row for row in shifts if row.employee_id == selection.employee_id]
    if selection.store_id is not None:
        shifts = [row for row in shifts if row.store_id == selection.store_id]
    if selection.shift_ids:
        selected = set(selection.shift_ids)
        shifts = [row for row in shifts if row.id in selected]
    if selection.source_start_date is not None:
        shifts = [row for row in shifts if row.shift_date >= selection.source_start_date]
    if selection.source_end_date is not None:
        shifts = [row for row in shifts if row.shift_date <= selection.source_end_date]
    store_ids = {row.store_id for row in shifts}
    if store_ids - set(allowed_store_ids):
        raise PermissionError('The selected source shifts include stores outside the authorized scope.')
    correlation_id = str(uuid.uuid4())
    targets: dict[int, SchedulePeriod] = {}
    for index, source in enumerate(periods):
        targets[source.id] = _target_draft(
            db, principal=principal, week_start=target_week_start + timedelta(days=7 * index), mode=mode,
            source_period_id=source.id, source_template_id=None, ip=ip,
        )
    now = datetime.now(tz=timezone.utc)
    for source_shift in shifts:
        source_period = next(row for row in periods if row.id == source_shift.schedule_period_id)
        target = targets[source_period.id]
        target_date = target.week_start_date + (source_shift.shift_date - source_period.week_start_date)
        db.add(ScheduleShift(
            schedule_period_id=target.id, employee_id=source_shift.employee_id, store_id=source_shift.store_id,
            shift_date=target_date, start_time=source_shift.start_time, end_time=source_shift.end_time,
            unpaid_break_minutes=source_shift.unpaid_break_minutes, shift_type_id=source_shift.shift_type_id,
            is_opener=source_shift.is_opener, is_closer=source_shift.is_closer,
            employee_note=source_shift.employee_note, source_shift_id=source_shift.id,
            created_by_principal_id=principal.id, updated_by_principal_id=principal.id,
            created_at=now, updated_at=now,
        ))
        target.version += 1
        target.updated_by_principal_id = principal.id
        target.updated_at = now
    db.flush()
    for target in targets.values():
        rebuild_schedule_warnings(db, schedule_period_id=target.id)
    first_target = next(iter(targets.values()))
    _audit(db, principal=principal, action='SCHEDULE_COPIED', entity_type='schedule_period',
           entity_id=first_target.id, store_ids=tuple(sorted(store_ids)), correlation_id=correlation_id,
           metadata={'schedule_period_ids': [row.id for row in targets.values()],
           'source_schedule_period_ids': [row.id for row in periods], 'shift_count': len(shifts),
           'mode': mode.upper(), 'selection': {'employee_id': selection.employee_id,
           'store_id': selection.store_id, 'shift_ids': list(selection.shift_ids),
           'source_start_date': selection.source_start_date.isoformat() if selection.source_start_date else None,
           'source_end_date': selection.source_end_date.isoformat() if selection.source_end_date else None}}, ip=ip)
    return CopyOutcome(tuple(row.id for row in targets.values()), len(shifts), correlation_id)


def instantiate_schedule_template(
    db: Session,
    *,
    principal: Principal,
    schedule_template_id: int,
    target_week_start: date,
    allowed_store_ids: tuple[int, ...],
    mode: str,
    ip: str | None = None,
) -> CopyOutcome:
    validate_week(target_week_start)
    template = db.execute(
        select(ScheduleTemplate).where(ScheduleTemplate.id == schedule_template_id).with_for_update()
    ).scalar_one_or_none()
    if template is None or not template.active:
        raise SchedulingValidationError('Choose an active schedule template.')
    template_shifts = db.execute(
        select(ScheduleTemplateShift).where(ScheduleTemplateShift.schedule_template_id == template.id)
    ).scalars().all()
    store_ids = {row.store_id for row in template_shifts}
    if store_ids - set(allowed_store_ids):
        raise PermissionError('The schedule template contains stores outside the authorized scope.')
    targets = [
        _target_draft(
            db, principal=principal, week_start=target_week_start + timedelta(days=7 * index), mode=mode,
            source_period_id=None, source_template_id=template.id, ip=ip,
        )
        for index in range(template.week_count)
    ]
    now = datetime.now(tz=timezone.utc)
    for source in template_shifts:
        week_index = source.day_offset // 7
        if week_index >= len(targets):
            raise SchedulingValidationError('Template shift falls outside the declared template week count.')
        target = targets[week_index]
        employee_id = source.employee_id
        # Inactive employees intentionally remain assigned and are surfaced by
        # the warning rebuild so management can repair the copied draft.
        if employee_id is not None and db.get(Employee, employee_id) is None:
            raise SchedulingValidationError('Template references an unknown employee.')
        db.add(ScheduleShift(
            schedule_period_id=target.id, employee_id=employee_id, store_id=source.store_id,
            shift_date=target_week_start + timedelta(days=source.day_offset),
            start_time=source.start_time, end_time=source.end_time,
            unpaid_break_minutes=source.unpaid_break_minutes, shift_type_id=source.shift_type_id,
            is_opener=source.is_opener, is_closer=source.is_closer, employee_note=source.note,
            source_shift_id=source.source_shift_id, created_by_principal_id=principal.id,
            updated_by_principal_id=principal.id, created_at=now, updated_at=now,
        ))
        target.version += 1
        target.updated_by_principal_id = principal.id
        target.updated_at = now
    db.flush()
    for target in targets:
        rebuild_schedule_warnings(db, schedule_period_id=target.id)
    correlation_id = str(uuid.uuid4())
    _audit(db, principal=principal, action='TEMPLATE_INSTANTIATED', entity_type='schedule_template',
           entity_id=template.id, store_ids=tuple(sorted(store_ids)), correlation_id=correlation_id,
           metadata={'schedule_template_id': template.id, 'schedule_period_ids': [row.id for row in targets],
           'shift_count': len(template_shifts), 'mode': mode.upper()}, ip=ip)
    return CopyOutcome(tuple(row.id for row in targets), len(template_shifts), correlation_id)
