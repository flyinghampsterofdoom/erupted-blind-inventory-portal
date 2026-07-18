from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import Principal
from app.models import SchedulePeriod, ScheduleShift, Store, StoreShift
from app.services.v2_scheduling_service import (
    ShiftInput,
    SchedulingValidationError,
    create_shift,
)
from app.v2.audit import V2AuditEvent, write_v2_audit_event


WEEKDAY_NAMES = ('Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday')
SHORT_WEEKDAY_NAMES = ('Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat')
MAX_LABEL_LENGTH = 120
MAX_MANAGER_NOTE_LENGTH = 2000


@dataclass(frozen=True)
class StoreShiftInput:
    label: str
    store_id: int
    start_time: time
    end_time: time
    active_weekdays: tuple[int, ...]
    active: bool = True
    display_order: int = 0
    manager_note: str = ''


def weekday_mask(days: tuple[int, ...]) -> int:
    clean = sorted(set(days))
    if not clean or any(day < 0 or day > 6 for day in clean):
        raise SchedulingValidationError(
            'Choose at least one active weekday.',
            {'active_weekdays': 'Choose one or more days from Sunday through Saturday.'},
        )
    return sum(1 << day for day in clean)


def weekdays_from_mask(mask: int) -> tuple[int, ...]:
    return tuple(day for day in range(7) if mask & (1 << day))


def weekday_active(row: StoreShift, day: date) -> bool:
    scheduling_day = (day.weekday() + 1) % 7
    return bool(row.active_weekdays & (1 << scheduling_day))


def weekday_summary(mask: int) -> str:
    days = weekdays_from_mask(mask)
    if days == tuple(range(7)):
        return 'Sun–Sat'
    return ', '.join(SHORT_WEEKDAY_NAMES[day] for day in days)


def _clock(value: time) -> str:
    return value.strftime('%-I:%M %p')


def _audit(
    db: Session,
    *,
    principal: Principal,
    action: str,
    row: StoreShift,
    before: dict | None = None,
    after: dict | None = None,
    metadata: dict | None = None,
    ip: str | None = None,
) -> None:
    write_v2_audit_event(
        db,
        event=V2AuditEvent(
            actor_principal_id=principal.id,
            action=action,
            domain='SCHEDULING',
            entity_type='store_shift',
            entity_id=row.id,
            store_ids=(row.store_id,),
            timestamp=datetime.now(tz=timezone.utc),
            correlation_id=str(uuid.uuid4()),
            before=before,
            after=after,
            metadata=metadata or {},
        ),
        ip=ip,
    )


def _values(row: StoreShift, *, include_manager_note: bool = True) -> dict:
    result = {
        'id': row.id,
        'label': row.label,
        'store_id': row.store_id,
        'start_time': row.start_time.isoformat(timespec='minutes'),
        'end_time': row.end_time.isoformat(timespec='minutes'),
        'active_weekdays': list(weekdays_from_mask(row.active_weekdays)),
        'active_day_summary': weekday_summary(row.active_weekdays),
        'active': row.active,
        'display_order': row.display_order,
    }
    if include_manager_note:
        result['manager_note'] = row.manager_note
    return result


def _authorized_store(db: Session, store_id: int, allowed_store_ids: tuple[int, ...]) -> Store:
    if store_id not in set(allowed_store_ids):
        raise PermissionError('The selected store is outside the authorized store scope.')
    store = db.get(Store, store_id)
    if store is None or not store.active:
        raise SchedulingValidationError('Choose an active store.', {'store_id': 'Choose an active store.'})
    return store


def _validate_input(
    db: Session,
    *,
    values: StoreShiftInput,
    allowed_store_ids: tuple[int, ...],
    exclude_id: int | None = None,
) -> tuple[Store, int]:
    store = _authorized_store(db, values.store_id, allowed_store_ids)
    label = values.label.strip()
    errors: dict[str, str] = {}
    if not label:
        errors['label'] = 'Enter a Store Shift label.'
    elif len(label) > MAX_LABEL_LENGTH:
        errors['label'] = f'Keep the label to {MAX_LABEL_LENGTH} characters or fewer.'
    if values.end_time <= values.start_time:
        errors['end_time'] = 'End time must be later than start time; overnight shifts are not supported.'
    if values.display_order < 0:
        errors['display_order'] = 'Display order cannot be negative.'
    if len(values.manager_note.strip()) > MAX_MANAGER_NOTE_LENGTH:
        errors['manager_note'] = f'Keep the manager note to {MAX_MANAGER_NOTE_LENGTH:,} characters or fewer.'
    mask = weekday_mask(values.active_weekdays)
    duplicate_query = select(StoreShift.id).where(
        StoreShift.store_id == values.store_id,
        StoreShift.label == label,
    )
    if exclude_id is not None:
        duplicate_query = duplicate_query.where(StoreShift.id != exclude_id)
    if db.execute(duplicate_query.limit(1)).scalar_one_or_none() is not None:
        errors['label'] = 'This store already has a Store Shift with that label.'
    if errors:
        raise SchedulingValidationError('Check the Store Shift fields.', errors)
    return store, mask


def create_store_shift(
    db: Session,
    *,
    principal: Principal,
    values: StoreShiftInput,
    allowed_store_ids: tuple[int, ...],
    ip: str | None = None,
) -> StoreShift:
    _store, mask = _validate_input(db, values=values, allowed_store_ids=allowed_store_ids)
    now = datetime.now(tz=timezone.utc)
    row = StoreShift(
        label=values.label.strip(),
        store_id=values.store_id,
        start_time=values.start_time,
        end_time=values.end_time,
        active_weekdays=mask,
        active=values.active,
        display_order=values.display_order,
        manager_note=values.manager_note.strip() or None,
        created_by_principal_id=principal.id,
        updated_by_principal_id=principal.id,
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.flush()
    _audit(db, principal=principal, action='STORE_SHIFT_CREATED', row=row, after=_values(row), ip=ip)
    return row


def update_store_shift(
    db: Session,
    *,
    principal: Principal,
    store_shift_id: int,
    values: StoreShiftInput,
    allowed_store_ids: tuple[int, ...],
    ip: str | None = None,
) -> StoreShift:
    row = db.execute(select(StoreShift).where(StoreShift.id == store_shift_id).with_for_update()).scalar_one_or_none()
    if row is None:
        raise SchedulingValidationError('Store Shift not found.')
    if row.store_id not in set(allowed_store_ids):
        raise PermissionError('The Store Shift is outside the authorized store scope.')
    _store, mask = _validate_input(
        db,
        values=values,
        allowed_store_ids=allowed_store_ids,
        exclude_id=row.id,
    )
    before = _values(row)
    row.label = values.label.strip()
    row.store_id = values.store_id
    row.start_time = values.start_time
    row.end_time = values.end_time
    row.active_weekdays = mask
    row.active = values.active
    row.display_order = values.display_order
    row.manager_note = values.manager_note.strip() or None
    row.updated_by_principal_id = principal.id
    row.updated_at = datetime.now(tz=timezone.utc)
    db.flush()
    _audit(db, principal=principal, action='STORE_SHIFT_CHANGED', row=row, before=before, after=_values(row), ip=ip)
    return row


def copy_store_shift(
    db: Session,
    *,
    principal: Principal,
    store_shift_id: int,
    destination_store_id: int,
    label: str | None,
    allowed_store_ids: tuple[int, ...],
    ip: str | None = None,
) -> StoreShift:
    source = db.get(StoreShift, store_shift_id)
    if source is None:
        raise SchedulingValidationError('Store Shift not found.')
    if source.store_id not in set(allowed_store_ids):
        raise PermissionError('The source Store Shift is outside the authorized store scope.')
    copied = create_store_shift(
        db,
        principal=principal,
        values=StoreShiftInput(
            label=(label or source.label).strip(),
            store_id=destination_store_id,
            start_time=source.start_time,
            end_time=source.end_time,
            active_weekdays=weekdays_from_mask(source.active_weekdays),
            active=source.active,
            display_order=source.display_order,
            manager_note=source.manager_note or '',
        ),
        allowed_store_ids=allowed_store_ids,
        ip=ip,
    )
    _audit(
        db,
        principal=principal,
        action='STORE_SHIFT_COPIED',
        row=copied,
        after=_values(copied),
        metadata={'source_store_shift_id': source.id, 'source_store_id': source.store_id},
        ip=ip,
    )
    return copied


def reorder_store_shifts(
    db: Session,
    *,
    principal: Principal,
    ordered_ids: tuple[int, ...],
    allowed_store_ids: tuple[int, ...],
    ip: str | None = None,
) -> list[StoreShift]:
    if not ordered_ids or len(set(ordered_ids)) != len(ordered_ids):
        raise SchedulingValidationError('Provide each Store Shift exactly once in the requested order.')
    rows = db.execute(select(StoreShift).where(StoreShift.id.in_(ordered_ids)).with_for_update()).scalars().all()
    if len(rows) != len(ordered_ids):
        raise SchedulingValidationError('One or more Store Shifts were not found.')
    if {row.store_id for row in rows} - set(allowed_store_ids):
        raise PermissionError('One or more Store Shifts are outside the authorized store scope.')
    by_id = {row.id: row for row in rows}
    now = datetime.now(tz=timezone.utc)
    for index, store_shift_id in enumerate(ordered_ids):
        row = by_id[store_shift_id]
        row.display_order = index * 10
        row.updated_by_principal_id = principal.id
        row.updated_at = now
    db.flush()
    anchor = by_id[ordered_ids[0]]
    _audit(
        db,
        principal=principal,
        action='STORE_SHIFTS_REORDERED',
        row=anchor,
        metadata={'ordered_store_shift_ids': list(ordered_ids)},
        ip=ip,
    )
    return [by_id[row_id] for row_id in ordered_ids]


def list_store_shifts(
    db: Session,
    *,
    allowed_store_ids: tuple[int, ...],
    include_inactive: bool,
    include_manager_note: bool,
    period: SchedulePeriod | None = None,
) -> list[dict]:
    statement = select(StoreShift, Store).join(Store, Store.id == StoreShift.store_id).where(
        StoreShift.store_id.in_(allowed_store_ids)
    )
    if not include_inactive:
        statement = statement.where(StoreShift.active.is_(True))
    rows = db.execute(statement.order_by(Store.name, StoreShift.display_order, StoreShift.label, StoreShift.id)).all()
    scheduled: list[ScheduleShift] = []
    if period is not None:
        scheduled = db.execute(
            select(ScheduleShift).where(
                ScheduleShift.schedule_period_id == period.id,
                ScheduleShift.store_id.in_(allowed_store_ids),
            )
        ).scalars().all()
    result: list[dict] = []
    for row, store in rows:
        data = _values(row, include_manager_note=include_manager_note)
        data.update({
            'store_name': store.name,
            'time_label': f'{_clock(row.start_time)}–{_clock(row.end_time)}',
            'fill_states': {},
        })
        if period is not None:
            current = period.week_start_date
            while current <= period.week_end_date:
                if weekday_active(row, current):
                    matches = [
                        shift for shift in scheduled
                        if shift.store_id == row.store_id
                        and shift.shift_date == current
                        and shift.start_time == row.start_time
                        and shift.end_time == row.end_time
                    ]
                    state = 'assigned' if any(shift.employee_id is not None for shift in matches) else 'open' if matches else 'not_placed'
                    data['fill_states'][current.isoformat()] = state
                current = date.fromordinal(current.toordinal() + 1)
        result.append(data)
    return result


def place_store_shift(
    db: Session,
    *,
    principal: Principal,
    schedule_period_id: int,
    store_shift_id: int,
    expected_version: int,
    shift_date: date,
    employee_id: int | None,
    destination_store_id: int,
    allowed_store_ids: tuple[int, ...],
    eligible_employee_ids: tuple[int, ...],
    ip: str | None = None,
):
    row = db.get(StoreShift, store_shift_id)
    if row is None or not row.active:
        raise SchedulingValidationError('Choose an active Store Shift.')
    if row.store_id not in set(allowed_store_ids):
        raise PermissionError('The Store Shift is outside the authorized store scope.')
    if destination_store_id != row.store_id:
        raise SchedulingValidationError(
            'A Store Shift can only be placed at its configured store.',
            {'destination_store_id': 'Choose the Store Shift’s configured store.'},
        )
    if employee_id is not None and employee_id not in set(eligible_employee_ids):
        raise SchedulingValidationError(
            'Choose an employee who is eligible for the current board scope.',
            {'employee_id': 'Choose an employee shown on this schedule board.'},
        )
    if not weekday_active(row, shift_date):
        raise SchedulingValidationError(
            f'{row.label} is not active on {shift_date.strftime("%A")}.',
            {'shift_date': 'Choose an active weekday for this Store Shift.'},
        )
    outcome = create_shift(
        db,
        principal=principal,
        schedule_period_id=schedule_period_id,
        expected_version=expected_version,
        values=ShiftInput(
            employee_id=employee_id,
            store_id=row.store_id,
            shift_date=shift_date,
            start_time=row.start_time,
            end_time=row.end_time,
            unpaid_break_minutes=0,
            source_store_shift_id=row.id,
        ),
        allowed_store_ids=allowed_store_ids,
        ip=ip,
    )
    _audit(
        db,
        principal=principal,
        action='STORE_SHIFT_PLACED',
        row=row,
        metadata={
            'schedule_period_id': schedule_period_id,
            'scheduled_shift_id': outcome.shift_id,
            'source_store_shift_id': row.id,
            'destination_employee_id': employee_id,
            'destination_store_id': destination_store_id,
            'shift_date': shift_date.isoformat(),
            'before_period_version': expected_version,
            'after_period_version': outcome.version,
        },
        ip=ip,
    )
    return outcome
