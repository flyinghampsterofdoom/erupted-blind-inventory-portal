"""Direct edits to the current board, using the ordinary shift mutation services."""
from datetime import timedelta

from sqlalchemy import select, text

from app.auth import Role
from app.models import (
    AuditLog, ScheduleAttendanceEvent, ScheduleCoverageCommitment,
    SchedulePeriod, SchedulePeriodStatus, ScheduleShift, StoreShift,
)
from app.services.access_control_service import principal_has_permission
from app.services.scheduling_operational_exception_service import can_override
from app.services.scheduling_training_service import future_shift, local_now, training_on
from app.services.v2_scheduling_assignments_service import get_store_defaults
from app.services.v2_scheduling_exception_service import effective_shift_segments, shift_lineage
from app.services.v2_scheduling_opening_service import (
    OpeningConfigurationError, load_opening_calendar, preparation_start,
)
from app.services.v2_scheduling_service import (
    SchedulingConflict, SchedulingValidationError, ShiftInput, _lock_period,
    clone_published_revision, create_shift, delete_shift, lock_scheduling, update_shift,
)


class BoardBusy(SchedulingConflict):
    pass


def _standard_shift(db, *, employee_id, day, store_id, training):
    defaults = get_store_defaults(db)
    if training:
        if not defaults or defaults.training_shift_start is None:
            raise SchedulingValidationError('Set Standard Training Shift hours in Scheduling Store Defaults first.')
        return ShiftInput(employee_id, store_id, day, defaults.training_shift_start,
            defaults.training_shift_end, defaults.training_unpaid_break_minutes, is_training=True)

    if training_on(db, employee_id, day):
        raise SchedulingValidationError('This employee is in training. Use the Training tile.')
    calendar = load_opening_calendar(db, store_ids=(store_id,), start_date=day, end_date=day)
    try:
        opening = calendar.interval(store_id, day)
    except OpeningConfigurationError as exc:
        raise SchedulingValidationError(str(exc)) from exc
    if opening is None:
        raise SchedulingValidationError('This store is closed on the selected date.')
    weekday = (day.weekday() + 1) % 7
    source = db.scalar(select(StoreShift).where(StoreShift.store_id == store_id,
        StoreShift.active.is_(True), StoreShift.active_weekdays.bitwise_and(1 << weekday) != 0)
        .order_by(StoreShift.display_order, StoreShift.start_time, StoreShift.end_time,
                  StoreShift.label, StoreShift.id).limit(1))
    end = (opening[1] if (store_id, day) in calendar.specials else
           source.end_time if source else defaults.standard_shift_end if defaults else None)
    if end is None:
        raise SchedulingValidationError('Set Standard Shift End in Scheduling Store Defaults first.')
    return ShiftInput(employee_id, store_id, day, preparation_start(day, opening[0]), end,
        source_store_shift_id=source.id if source else None)


def _from_shift(shift, employee_id, day):
    return ShiftInput(employee_id, shift.store_id, day, shift.start_time, shift.end_time,
        shift.unpaid_break_minutes, shift.shift_type_id, shift.is_opener, shift.is_closer,
        shift.employee_note or '', is_training=shift.is_training and employee_id is not None)


def _occupied(db, period_id, *, employee_id, day, excluding):
    for row in db.scalars(select(ScheduleShift).where(
            ScheduleShift.schedule_period_id == period_id, ScheduleShift.shift_date == day,
            ScheduleShift.id != (excluding or -1))):
        if row.employee_id == employee_id or any(
                segment.employee_id == employee_id for segment in effective_shift_segments(db, row)):
            return True
    return False


def _has_operational_history(db, shift):
    ids = [row.id for row in shift_lineage(db, shift)]
    return any(db.scalar(select(model.id).where(model.schedule_shift_id.in_(ids),
        model.voided_at.is_(None)).limit(1)) for model in (ScheduleAttendanceEvent, ScheduleCoverageCommitment))


UNDO_WINDOW_SECONDS = 20


def _undo_removal(db, *, principal, period, audit_id, allowed_store_ids, now, ip):
    audit = db.get(AuditLog, audit_id)
    if (not audit or audit.actor_principal_id != principal.id
            or audit.action != 'V2:SCHEDULING:SHIFT_CHANGED'
            or now - audit.created_at > timedelta(seconds=UNDO_WINDOW_SECONDS)):
        raise SchedulingValidationError('Undo is no longer available.')
    details = audit.meta.get('metadata', {})
    before = audit.meta.get('before') or {}
    if (details.get('schedule_period_id') != period.id or not details.get('direct_board_removal')
            or not before.get('employee_id') or before.get('is_training') or before.get('is_lead_of_day')
            or before.get('is_shift_lead') or before.get('is_double_coverage')):
        raise SchedulingValidationError('Undo is no longer available.')
    shift = db.get(ScheduleShift, details['shift_id'])
    if (not shift or shift.schedule_period_id != period.id or shift.employee_id is not None
            or shift.owner_principal_id is not None or shift.shift_date < now.date()
            or shift.store_id not in allowed_store_ids or not shift.manually_locked
            or _has_operational_history(db, shift)):
        raise SchedulingValidationError('Undo is no longer available.')
    from app.services.v2_scheduling_service import _shift_values
    if _shift_values(shift) != audit.meta.get('after'):
        raise SchedulingConflict('This assignment changed elsewhere.')
    employee_id = before['employee_id']
    if _occupied(db, period.id, employee_id=employee_id, day=shift.shift_date, excluding=shift.id):
        raise SchedulingValidationError('That employee already has an assignment on this date.')
    if training_on(db, employee_id, shift.shift_date):
        raise SchedulingValidationError('This employee is in training. Use the Training tile.')
    return update_shift(db, principal=principal, schedule_period_id=period.id, shift_id=shift.id,
        expected_version=period.version, allowed_store_ids=allowed_store_ids, manual_edit=True, ip=ip,
        values=_from_shift(shift, employee_id, shift.shift_date),
        direct_admin=principal.role == Role.ADMIN and can_override(db, principal))


def edit_board_assignment(db, *, principal, period_id, expected_version, allowed_store_ids,
                          authorized_store_ids, action, employee_id=None, day=None,
                          shift_id=None, store_id=None, training=False, undo_audit_id=None, ip=None):
    if not principal.active or not principal_has_permission(db, principal=principal,
            permission_key='scheduling.edit_draft_shifts',
            fallback_allowed=principal.role in (Role.ADMIN, Role.MANAGER, Role.LEAD)):
        raise PermissionError('You do not have permission to edit schedule assignments.')
    try:
        lock_scheduling(db, wait=False)
    except SchedulingConflict as exc:
        raise BoardBusy(str(exc)) from exc

    # A separate administrative transaction may hold a row without using the
    # scheduling lock. Keep that case bounded too.
    db.execute(text("SET LOCAL lock_timeout = '3s'"))
    with db.begin_nested():
        period = _lock_period(db, period_id)
        if period.version != expected_version or period.cleared_at or period.status not in (
                SchedulePeriodStatus.DRAFT, SchedulePeriodStatus.PUBLISHED):
            raise SchedulingConflict('This schedule changed elsewhere.')
        now = local_now(db)
        if period.is_historical or period.week_end_date < now.date():
            raise SchedulingValidationError('Use the historical scheduling controls for this week.')
        published = period.status == SchedulePeriodStatus.PUBLISHED
        if action == 'undo':
            if published:
                raise SchedulingValidationError('Undo is available only in the unpublished schedule.')
            outcome = _undo_removal(db, principal=principal, period=period, audit_id=undo_audit_id,
                allowed_store_ids=allowed_store_ids, now=now, ip=ip)
            return outcome, False, None
        if published and not principal_has_permission(db, principal=principal,
                permission_key='scheduling.modify_published',
                fallback_allowed=principal.role in (Role.ADMIN, Role.MANAGER, Role.LEAD)):
            raise PermissionError('You do not have permission to revise a published schedule.')

        shift = db.get(ScheduleShift, shift_id) if shift_id else None
        if action != 'create':
            if not shift or shift.schedule_period_id != period.id or shift.employee_id is None:
                raise SchedulingValidationError('Choose an employee assignment on this board.')
            if shift.store_id not in allowed_store_ids:
                raise PermissionError('This assignment is outside your store access.')
            if shift.shift_date < now.date() or _has_operational_history(db, shift) or (
                    published and not future_shift(shift, now)):
                raise SchedulingValidationError('This shift has already started or has recorded coverage or attendance. Use its shift details.')
            if shift.is_shift_lead and principal.role != Role.ADMIN:
                raise PermissionError('Only an administrator can change an assignment with Shift Lead duty.')
            if action == 'remove' and not principal_has_permission(db, principal=principal,
                    permission_key='scheduling.delete_draft_shifts',
                    fallback_allowed=principal.role in (Role.ADMIN, Role.MANAGER, Role.LEAD)):
                raise PermissionError('You do not have permission to remove schedule assignments.')

        if action == 'remove':
            if shift.manually_locked and not shift.is_training and principal.role not in (Role.ADMIN, Role.LEAD):
                raise PermissionError('Unlock this protected assignment in shift details before removing it.')
            values = _from_shift(shift, None, shift.shift_date)
        else:
            if not period.week_start_date <= day <= period.week_end_date or day < now.date():
                raise SchedulingValidationError('Choose a current or future date in this schedule week.')
            if _occupied(db, period.id, employee_id=employee_id, day=day, excluding=shift_id):
                raise SchedulingValidationError('That employee already has an assignment on this date.')
            if action == 'move':
                if not shift.is_training and training_on(db, employee_id, day):
                    raise SchedulingValidationError('This employee is in training. Use the Training tile.')
                values = _from_shift(shift, employee_id, day)
            else:
                if store_id not in allowed_store_ids:
                    raise PermissionError('The assignment location is outside your selected stores or store access.')
                values = _standard_shift(db, employee_id=employee_id, day=day, store_id=store_id, training=training)
            if published and not future_shift(values, now):
                raise SchedulingValidationError('Choose a shift that has not started.')

        if values.store_id not in allowed_store_ids:
            raise PermissionError('The assignment location is outside your selected stores or store access.')
        if published:
            period = clone_published_revision(db, principal=principal, published_period_id=period.id,
                allowed_store_ids=authorized_store_ids, ip=ip)
            if shift:
                shift = db.scalar(select(ScheduleShift).where(
                    ScheduleShift.schedule_period_id == period.id, ScheduleShift.source_shift_id == shift.id))

        common = dict(principal=principal, schedule_period_id=period.id, expected_version=period.version,
            allowed_store_ids=allowed_store_ids, manual_edit=True, ip=ip)
        if action == 'remove' and shift.is_training:
            return delete_shift(db, shift_id=shift.id, **common), published, None

        direct_admin = principal.role == Role.ADMIN and can_override(db, principal)
        if not shift and not training:
            # Fill the existing standard position if one is already open.
            # Keep any customized open position available through its details.
            shift = db.scalar(select(ScheduleShift).where(
                ScheduleShift.schedule_period_id == period.id, ScheduleShift.store_id == values.store_id,
                ScheduleShift.shift_date == day, ScheduleShift.start_time == values.start_time,
                ScheduleShift.end_time == values.end_time, ScheduleShift.unpaid_break_minutes == 0,
                ScheduleShift.employee_id.is_(None), ScheduleShift.owner_principal_id.is_(None),
            ).order_by(ScheduleShift.id).limit(1))
            if shift and not _has_operational_history(db, shift):
                values = _from_shift(shift, employee_id, day)
            else:
                shift = None
        if not shift:
            return create_shift(db, values=values, direct_admin=direct_admin, **common), published, None

        offer_undo = (action == 'remove' and not published and not shift.is_shift_lead
                      and not shift.is_lead_of_day and not shift.is_double_coverage)
        outcome = update_shift(db, shift_id=shift.id, values=values, direct_admin=direct_admin,
            direct_removal=action == 'remove', **common)
        audit_id = None
        if offer_undo:
            audit_id = db.scalar(select(AuditLog.id).where(
                AuditLog.actor_principal_id == principal.id,
                AuditLog.action == 'V2:SCHEDULING:SHIFT_CHANGED',
                AuditLog.meta['entity_id'].as_string() == str(shift.id),
            ).order_by(AuditLog.id.desc()).limit(1))
        return outcome, published, audit_id
