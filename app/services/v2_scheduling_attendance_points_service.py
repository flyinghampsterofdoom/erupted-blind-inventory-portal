from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import Principal
from app.models import (
    AttendanceEventType,
    AttendancePointEntry,
    AttendancePointReason,
    Employee,
    Principal as PrincipalModel,
    ScheduleAttendanceEvent,
    ScheduleShift,
    Store,
)
from app.services.v2_scheduling_service import SchedulingValidationError
from app.v2.audit import V2AuditEvent, write_v2_audit_event


PORTAL_TIMEZONE = ZoneInfo('America/Los_Angeles')


@dataclass(frozen=True)
class AttendancePointSummary:
    current_points: Decimal
    active_entry_count: int
    history: tuple[dict, ...]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _amount(value: Decimal | str | int | float) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise SchedulingValidationError('Enter a valid attendance point amount.') from exc
    if not amount.is_finite() or amount == 0:
        raise SchedulingValidationError('Attendance point amount must be non-zero.')
    if abs(amount) > Decimal('999999.99'):
        raise SchedulingValidationError('Attendance point amount is outside the supported range.')
    if amount.as_tuple().exponent < -2:
        raise SchedulingValidationError('Attendance point amount may have at most two decimal places.')
    return amount


def _event_context(event: ScheduleAttendanceEvent, shift: ScheduleShift, store: Store | None) -> dict:
    scheduled_start = datetime.combine(
        shift.shift_date, shift.start_time, tzinfo=PORTAL_TIMEZONE)
    event_at = event.event_at.astimezone(PORTAL_TIMEZONE)
    notice_minutes = None
    minutes_late = None
    if event.event_type == AttendanceEventType.CALLED_OUT:
        notice_minutes = int((scheduled_start - event_at).total_seconds() // 60)
    if event.event_type in {AttendanceEventType.LATE, AttendanceEventType.OPENED_STORE_LATE}:
        minutes_late = max(0, int((event_at - scheduled_start).total_seconds() // 60))
    return {
        'attendance_event_id': event.id,
        'event_type': event.event_type.value,
        'event_label': event.event_type.value.replace('_', ' ').title(),
        'event_at': event.event_at,
        'event_at_local': event_at,
        'event_voided': event.voided_at is not None,
        'shift_id': shift.id,
        'shift_date': shift.shift_date,
        'scheduled_start': scheduled_start,
        'scheduled_start_label': scheduled_start.strftime('%b %-d, %Y %-I:%M %p'),
        'store_id': shift.store_id,
        'store_name': store.name if store else f'Store {shift.store_id}',
        'notice_minutes': notice_minutes,
        'minutes_late': minutes_late,
    }


def attendance_incidents_for_employee(db: Session, *, employee_id: int) -> tuple[dict, ...]:
    rows = list(db.execute(select(
        ScheduleAttendanceEvent, ScheduleShift, Store,
    ).join(
        ScheduleShift, ScheduleShift.id == ScheduleAttendanceEvent.schedule_shift_id,
    ).join(
        Store, Store.id == ScheduleShift.store_id,
    ).where(
        (ScheduleAttendanceEvent.original_employee_id == employee_id)
        | (ScheduleAttendanceEvent.replacement_employee_id == employee_id),
    ).order_by(
        ScheduleAttendanceEvent.event_at.desc(), ScheduleAttendanceEvent.id.desc(),
    )).all())
    return tuple({
        **_event_context(event, shift, store),
        'employee_role': (
            'replacement' if event.replacement_employee_id == employee_id else 'scheduled'),
    } for event, shift, store in rows)


def list_attendance_point_reasons(db: Session, *, active_only: bool = False) -> tuple[AttendancePointReason, ...]:
    statement = select(AttendancePointReason)
    if active_only:
        statement = statement.where(AttendancePointReason.active.is_(True))
    return tuple(db.execute(statement.order_by(AttendancePointReason.code)).scalars())


def create_attendance_point_reason(
    db: Session, *, principal: Principal, code: str, label: str,
    point_value: Decimal | str, description: str = '',
    attendance_event_type: AttendanceEventType | None = None, active: bool = True,
    ip: str | None = None,
) -> AttendancePointReason:
    clean_code = code.strip().upper()
    if not clean_code or len(clean_code) > 100 or not clean_code[0].isalpha() or any(
            not (char.isupper() or char.isdigit() or char == '_') for char in clean_code):
        raise SchedulingValidationError('Reason code must use uppercase letters, numbers, and underscores.')
    if db.execute(select(AttendancePointReason.id).where(
            AttendancePointReason.code == clean_code)).scalar_one_or_none() is not None:
        raise SchedulingValidationError('Attendance point reason code already exists.')
    row = AttendancePointReason(
        code=clean_code, label=label.strip(), point_value=_amount(point_value),
        description=description.strip(), attendance_event_type=attendance_event_type,
        active=active, created_by_principal_id=principal.id,
        updated_by_principal_id=principal.id)
    if not row.label or len(row.label) > 200 or len(row.description) > 2000:
        raise SchedulingValidationError('Reason label is required and policy text is too long.')
    db.add(row); db.flush()
    write_v2_audit_event(db, event=V2AuditEvent(
        actor_principal_id=principal.id, action='ATTENDANCE_POINT_REASON_CREATED',
        domain='SCHEDULING_ATTENDANCE_POINTS', entity_type='attendance_point_reason',
        entity_id=row.id, timestamp=_now(), correlation_id=str(uuid.uuid4()),
        after={'code': row.code, 'label': row.label, 'point_value': str(row.point_value),
               'description': row.description, 'attendance_event_type': (
                   row.attendance_event_type.value if row.attendance_event_type else None),
               'active': row.active}), ip=ip)
    return row


def update_attendance_point_reason(
    db: Session, *, principal: Principal, reason_id: int, label: str,
    point_value: Decimal | str, description: str = '',
    attendance_event_type: AttendanceEventType | None = None, active: bool = True,
    ip: str | None = None,
) -> AttendancePointReason:
    row = db.execute(select(AttendancePointReason).where(
        AttendancePointReason.id == reason_id).with_for_update()).scalar_one_or_none()
    if row is None:
        raise SchedulingValidationError('Attendance point reason not found.')
    before = {'code': row.code, 'label': row.label, 'point_value': str(row.point_value),
              'description': row.description, 'attendance_event_type': (
                  row.attendance_event_type.value if row.attendance_event_type else None),
              'active': row.active}
    clean_label, clean_description = label.strip(), description.strip()
    if not clean_label or len(clean_label) > 200 or len(clean_description) > 2000:
        raise SchedulingValidationError('Reason label is required and policy text is too long.')
    row.label = clean_label; row.point_value = _amount(point_value)
    row.description = clean_description; row.attendance_event_type = attendance_event_type
    row.active = active; row.updated_by_principal_id = principal.id; row.updated_at = _now()
    db.flush()
    write_v2_audit_event(db, event=V2AuditEvent(
        actor_principal_id=principal.id, action='ATTENDANCE_POINT_REASON_UPDATED',
        domain='SCHEDULING_ATTENDANCE_POINTS', entity_type='attendance_point_reason',
        entity_id=row.id, timestamp=_now(), correlation_id=str(uuid.uuid4()), before=before,
        after={'code': row.code, 'label': row.label, 'point_value': str(row.point_value),
               'description': row.description, 'attendance_event_type': (
                   row.attendance_event_type.value if row.attendance_event_type else None),
               'active': row.active}), ip=ip)
    return row


def assign_attendance_points(
    db: Session, *, principal: Principal, employee_id: int,
    amount: Decimal | str | int | float, category: str, effective_date: date,
    management_note: str = '', attendance_event_id: int | None = None,
    schedule_shift_id: int | None = None, replaces_point_entry_id: int | None = None,
    ip: str | None = None, _point_reason: AttendancePointReason | None = None,
) -> AttendancePointEntry:
    employee = db.get(Employee, employee_id)
    if employee is None:
        raise SchedulingValidationError('Employee not found.')
    clean_amount = _amount(amount)
    clean_category = category.strip()
    if not clean_category or len(clean_category) > 100:
        raise SchedulingValidationError('Attendance point category is required and must be 100 characters or fewer.')
    clean_note = management_note.strip()
    if len(clean_note) > 2000:
        raise SchedulingValidationError('Management note must be 2,000 characters or fewer.')
    if _point_reason is None and attendance_event_id is None and not clean_note:
        raise SchedulingValidationError('A management note is required for a manual point entry without an attendance event.')

    event = db.get(ScheduleAttendanceEvent, attendance_event_id) if attendance_event_id else None
    if attendance_event_id is not None and event is None:
        raise SchedulingValidationError('Attendance event not found.')
    if event is not None:
        if event.voided_at is not None:
            raise SchedulingValidationError(
                'Cannot assign new points to a voided attendance event. Use a deliberate manual entry if needed.')
        involved_ids = {event.original_employee_id, event.replacement_employee_id}
        if employee_id not in involved_ids:
            raise SchedulingValidationError('Employee is not involved in the selected attendance event.')
        if schedule_shift_id is not None and schedule_shift_id != event.schedule_shift_id:
            raise SchedulingValidationError('Attendance event and scheduled shift do not match.')
        schedule_shift_id = event.schedule_shift_id

    shift = db.get(ScheduleShift, schedule_shift_id) if schedule_shift_id else None
    if schedule_shift_id is not None and shift is None:
        raise SchedulingValidationError('Scheduled shift not found.')
    if shift is not None and event is None and shift.employee_id != employee_id:
        raise SchedulingValidationError('Employee is not assigned to the selected scheduled shift.')

    replacement = None
    if replaces_point_entry_id is not None:
        replacement = db.get(AttendancePointEntry, replaces_point_entry_id)
        if replacement is None or replacement.employee_id != employee_id:
            raise SchedulingValidationError('Corrected point entry must replace an entry for the same employee.')
        if replacement.reversed_at is None:
            raise SchedulingValidationError('Reverse the incorrect point entry before creating its correction.')

    row = AttendancePointEntry(
        employee_id=employee_id,
        attendance_event_id=attendance_event_id,
        schedule_shift_id=schedule_shift_id,
        amount=clean_amount,
        entry_kind='POLICY' if _point_reason else 'MANUAL',
        point_reason_id=_point_reason.id if _point_reason else None,
        reason_code_snapshot=_point_reason.code if _point_reason else None,
        reason_label_snapshot=_point_reason.label if _point_reason else None,
        category=clean_category,
        effective_date=effective_date,
        management_note=clean_note,
        assigned_by_principal_id=principal.id,
        replaces_point_entry_id=replacement.id if replacement else None,
    )
    db.add(row)
    db.flush()
    write_v2_audit_event(db, event=V2AuditEvent(
        actor_principal_id=principal.id,
        action='ATTENDANCE_POINTS_ASSIGNED',
        domain='SCHEDULING_ATTENDANCE_POINTS',
        entity_type='attendance_point_entry',
        entity_id=row.id,
        store_ids=(shift.store_id,) if shift else (),
        timestamp=_now(),
        correlation_id=str(uuid.uuid4()),
        after={
            'employee_id': employee_id,
            'attendance_event_id': attendance_event_id,
            'schedule_shift_id': schedule_shift_id,
            'amount': str(clean_amount),
            'entry_kind': row.entry_kind,
            'point_reason_id': row.point_reason_id,
            'reason_code_snapshot': row.reason_code_snapshot,
            'reason_label_snapshot': row.reason_label_snapshot,
            'category': clean_category,
            'effective_date': effective_date.isoformat(),
            'management_note': clean_note,
            'assigned_by_principal_id': principal.id,
            'replaces_point_entry_id': row.replaces_point_entry_id,
        },
    ), ip=ip)
    return row


def assign_configured_attendance_points(
    db: Session, *, principal: Principal, employee_id: int, reason_id: int,
    effective_date: date, management_note: str = '',
    attendance_event_id: int | None = None, schedule_shift_id: int | None = None,
    replaces_point_entry_id: int | None = None, ip: str | None = None,
) -> AttendancePointEntry:
    reason = db.get(AttendancePointReason, reason_id)
    if reason is None or not reason.active:
        raise SchedulingValidationError('Choose an active configured attendance point reason.')
    if reason.attendance_event_type is not None and attendance_event_id is not None:
        event = db.get(ScheduleAttendanceEvent, attendance_event_id)
        if event is None or event.event_type != reason.attendance_event_type:
            raise SchedulingValidationError('Configured reason does not apply to this attendance event type.')
    return assign_attendance_points(
        db, principal=principal, employee_id=employee_id,
        amount=reason.point_value, category=reason.label,
        effective_date=effective_date, management_note=management_note,
        attendance_event_id=attendance_event_id, schedule_shift_id=schedule_shift_id,
        replaces_point_entry_id=replaces_point_entry_id, ip=ip, _point_reason=reason)


def reverse_attendance_points(
    db: Session, *, principal: Principal, point_entry_id: int, reason: str,
    ip: str | None = None,
) -> AttendancePointEntry:
    clean_reason = reason.strip()
    if not clean_reason:
        raise SchedulingValidationError('A reversal reason is required.')
    if len(clean_reason) > 2000:
        raise SchedulingValidationError('Reversal reason must be 2,000 characters or fewer.')
    row = db.execute(select(AttendancePointEntry).where(
        AttendancePointEntry.id == point_entry_id).with_for_update()).scalar_one_or_none()
    if row is None:
        raise SchedulingValidationError('Attendance point entry not found.')
    if row.reversed_at is not None:
        raise SchedulingValidationError('Attendance point entry is already reversed.')
    before = {
        'amount': str(row.amount),
        'category': row.category,
        'active': True,
    }
    row.reversed_at = _now()
    row.reversed_by_principal_id = principal.id
    row.reversal_reason = clean_reason
    db.flush()
    shift = db.get(ScheduleShift, row.schedule_shift_id) if row.schedule_shift_id else None
    write_v2_audit_event(db, event=V2AuditEvent(
        actor_principal_id=principal.id,
        action='ATTENDANCE_POINTS_REVERSED',
        domain='SCHEDULING_ATTENDANCE_POINTS',
        entity_type='attendance_point_entry',
        entity_id=row.id,
        store_ids=(shift.store_id,) if shift else (),
        timestamp=row.reversed_at,
        correlation_id=str(uuid.uuid4()),
        before=before,
        after={
            'amount': str(row.amount),
            'category': row.category,
            'active': False,
            'reversed_by_principal_id': principal.id,
            'reversal_reason': clean_reason,
        },
        reason=clean_reason,
    ), ip=ip)
    return row


def attendance_point_summary(db: Session, *, employee_id: int) -> AttendancePointSummary:
    rows = list(db.execute(select(AttendancePointEntry).where(
        AttendancePointEntry.employee_id == employee_id,
    ).order_by(
        AttendancePointEntry.effective_date.desc(),
        AttendancePointEntry.created_at.desc(),
        AttendancePointEntry.id.desc(),
    )).scalars())
    event_ids = {row.attendance_event_id for row in rows if row.attendance_event_id is not None}
    events = {row.id: row for row in db.execute(select(ScheduleAttendanceEvent).where(
        ScheduleAttendanceEvent.id.in_(event_ids or (-1,)))).scalars()}
    shift_ids = {row.schedule_shift_id for row in rows if row.schedule_shift_id is not None}
    shifts = {row.id: row for row in db.execute(select(ScheduleShift).where(
        ScheduleShift.id.in_(shift_ids or (-1,)))).scalars()}
    store_ids = {shift.store_id for shift in shifts.values()}
    stores = {row.id: row for row in db.execute(select(Store).where(
        Store.id.in_(store_ids or (-1,)))).scalars()}
    principal_ids = {
        value for row in rows
        for value in (row.assigned_by_principal_id, row.reversed_by_principal_id)
        if value is not None
    }
    principals = {row.id: row for row in db.execute(select(PrincipalModel).where(
        PrincipalModel.id.in_(principal_ids or (-1,)))).scalars()}
    history = []
    for row in rows:
        event = events.get(row.attendance_event_id)
        shift = shifts.get(row.schedule_shift_id)
        context = (
            _event_context(event, shift, stores.get(shift.store_id))
            if event is not None and shift is not None else None)
        history.append({
            'entry': row,
            'assigned_by': (
                principals.get(row.assigned_by_principal_id).username
                if row.assigned_by_principal_id in principals else f'Principal {row.assigned_by_principal_id}'),
            'reversed_by': (
                principals.get(row.reversed_by_principal_id).username
                if row.reversed_by_principal_id in principals else None),
            'active': row.reversed_at is None,
            'event': context,
            'event_voided': bool(event and event.voided_at is not None),
            'reconciliation_required': bool(event and event.voided_at is not None and row.reversed_at is None),
            'shift_date': shift.shift_date if shift else None,
            'store_name': stores.get(shift.store_id).name if shift and shift.store_id in stores else None,
        })
    active = [row for row in rows if row.reversed_at is None]
    return AttendancePointSummary(
        current_points=sum((row.amount for row in active), Decimal('0.00')),
        active_entry_count=len(active),
        history=tuple(history),
    )
