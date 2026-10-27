"""Owner-designated local scheduling duty; never a permanent role or global grant."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from sqlalchemy import select
from app.auth import Role
from app.models import Employee, ScheduleShift, SchedulePeriod, SchedulePeriodStatus, Store
from app.services.access_control_service import principal_has_permission
from app.services.v2_scheduling_service import SchedulingValidationError

CHOICES = frozenset({'KEEP', 'ASSIGN', 'REMOVE'})
# Resource-aware operational actions only. No global permission flags are changed.
SCOPED_PERMISSIONS = frozenset({'scheduling.attendance.record', 'scheduling.approve_transfer_hours'})


def require_owner(db, principal):
    if (not principal.active or principal.role != Role.ADMIN or
            not principal_has_permission(db, principal=principal,
                permission_key='scheduling.edit_draft_shifts', fallback_allowed=True)):
        raise PermissionError('Only the Owner may change shift Lead designations.')


def validate_choice(db, principal, choice):
    if choice not in CHOICES:
        raise SchedulingValidationError('Choose Keep, Assign, or Remove Lead designation.')
    if choice != 'KEEP':
        require_owner(db, principal)


def apply_designation(db, *, principal, shift, choice='KEEP', before=None,
                      allowed_store_ids=(), reassign_shift_id=None):
    """Called inside the assignment transaction, after final employee/location changes."""
    validate_choice(db, principal, choice)
    before = before or {}
    previous = bool(before.get('is_shift_lead', shift.is_shift_lead))
    from app.services.v2_scheduling_service import _shift_values
    current = _shift_values(shift)
    changed = any(before.get(k, current[k]) != current[k]
                  for k in ('employee_id', 'store_id', 'shift_date', 'start_time', 'end_time'))
    if previous and changed and principal.role == Role.ADMIN:
        require_owner(db, principal)
    desired = choice == 'ASSIGN' or (choice == 'KEEP' and previous)
    # A non-Owner cannot transfer a temporary grant to another employee/location.
    if choice == 'KEEP' and changed and principal.role != Role.ADMIN:
        desired = False
    if shift.employee_id is None:
        if choice == 'ASSIGN':
            raise SchedulingValidationError('Assign an eligible employee before designating a Lead.')
        desired = False
    if desired:
        period = db.get(SchedulePeriod, shift.schedule_period_id)
        from app.services.v2_scheduling_roster_service import is_scheduling_candidate
        from app.services.v2_scheduling_lifecycle_service import within_employment_dates
        person = db.get(Employee, shift.employee_id)
        if (period.is_historical or not person or not is_scheduling_candidate(person)
                or not within_employment_dates(person, shift.shift_date)):
            raise SchedulingValidationError('Shift Lead requires an eligible employee on a current or future schedule.')
    if shift.store_id not in allowed_store_ids:
        raise PermissionError('The Lead assignment is outside your store scope.')
    shift.is_shift_lead = desired
    if previous != desired or (previous and changed):
        from app.services.v2_scheduling_rules_service import _audit
        _audit(db, principal=principal, action='SHIFT_LEAD_ASSIGNED' if desired else 'SHIFT_LEAD_REMOVED',
            entity_type='schedule_shift', entity_id=shift.id, before=before,
            after=_shift_values(shift), metadata={'shift_id': shift.id,
                'employee_id': shift.employee_id, 'store_id': shift.store_id,
                'choice': choice, 'scope': 'LOCATION_AND_SHIFT_ONLY'})
    if reassign_shift_id is not None:
        require_owner(db, principal)
        replacement = db.scalar(select(ScheduleShift).where(ScheduleShift.id == reassign_shift_id)
                                 .with_for_update())
        if replacement is not None and replacement.schedule_period_id != shift.schedule_period_id:
            replacement = db.scalar(select(ScheduleShift).where(
                ScheduleShift.schedule_period_id == shift.schedule_period_id,
                ScheduleShift.source_shift_id == reassign_shift_id).with_for_update())
        if (not previous or not replacement or replacement.id == shift.id or
                replacement.schedule_period_id != shift.schedule_period_id or
                replacement.store_id != before.get('store_id') or
                replacement.shift_date.isoformat() != before.get('shift_date') or
                replacement.start_time.isoformat() > before.get('start_time', '') or
                replacement.end_time.isoformat() < before.get('end_time', '')):
            raise SchedulingValidationError('Choose an assigned shift covering the original Lead location and interval.')
        apply_designation(db, principal=principal, shift=replacement, choice='ASSIGN',
                          allowed_store_ids=allowed_store_ids)
    return shift


def has_shift_lead_permission(db, *, principal, permission_key, shift, now=None):
    """Recompute on every action; revision, absence, cancellation and end time revoke immediately."""
    if permission_key not in SCOPED_PERMISSIONS or not principal.active or shift is None:
        return False
    # Explicit account/role denial remains authoritative, including for temporary Leads.
    if not principal_has_permission(db, principal=principal, permission_key=permission_key,
                                    fallback_allowed=True):
        return False
    from app.services.v2_scheduling_policy_service import organization_policy
    from app.services.v2_scheduling_lifecycle_service import within_employment_dates
    from app.services.v2_scheduling_roster_service import is_scheduling_candidate
    local = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(organization_policy(db).timezone_name))
    if shift.shift_date != local.date():
        return False
    employee = db.scalar(select(Employee).where(Employee.principal_id == principal.id))
    store = db.get(Store, shift.store_id)
    if (not employee or not is_scheduling_candidate(employee) or not store or not store.active
            or not within_employment_dates(employee, local.date())):
        return False
    periods = list(db.scalars(select(SchedulePeriod).where(
        SchedulePeriod.status == SchedulePeriodStatus.PUBLISHED,
        SchedulePeriod.is_historical.is_(False), SchedulePeriod.cleared_at.is_(None),
        SchedulePeriod.week_start_date <= local.date(), SchedulePeriod.week_end_date >= local.date())
        .order_by(SchedulePeriod.revision_number.desc(), SchedulePeriod.id.desc())))
    if not periods or shift.schedule_period_id != periods[0].id:
        return False
    from app.services.v2_scheduling_exception_service import effective_shift_segments
    clock = local.time().replace(tzinfo=None)
    # Both the actor's duty and the requested responsibility must be in its interval.
    if not shift.start_time <= clock < shift.end_time:
        return False
    for assigned in db.scalars(select(ScheduleShift).where(
            ScheduleShift.schedule_period_id == periods[0].id,
            ScheduleShift.store_id == shift.store_id, ScheduleShift.shift_date == local.date())):
        for segment in effective_shift_segments(db, assigned):
            if (segment.employee_id == employee.id and segment.is_shift_lead
                    and segment.start_time <= clock < segment.end_time
                    and segment.start_time <= shift.start_time and segment.end_time >= shift.end_time):
                return True
    return False


def active_lead_store_ids(db, principal, *, now=None):
    """Used only by the scheduler's resource-aware UI; no inventory/admin scope changes."""
    if not principal.active:
        return set()
    employee = db.scalar(select(Employee).where(Employee.principal_id == principal.id))
    if employee is None:
        return set()
    from app.services.v2_scheduling_policy_service import organization_policy
    local = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(organization_policy(db).timezone_name))
    current = db.scalar(select(SchedulePeriod).where(
        SchedulePeriod.status == SchedulePeriodStatus.PUBLISHED,
        SchedulePeriod.is_historical.is_(False), SchedulePeriod.cleared_at.is_(None),
        SchedulePeriod.week_start_date <= local.date(), SchedulePeriod.week_end_date >= local.date())
        .order_by(SchedulePeriod.revision_number.desc(), SchedulePeriod.id.desc()).limit(1))
    if current is None:
        return set()
    from app.services.v2_scheduling_exception_service import effective_shift_segments
    result = set()
    for shift in db.scalars(select(ScheduleShift).where(
            ScheduleShift.schedule_period_id == current.id, ScheduleShift.shift_date == local.date())):
        if any(segment.employee_id == employee.id and segment.is_shift_lead
                and has_shift_lead_permission(db, principal=principal,
                    permission_key='scheduling.attendance.record', shift=segment, now=now)
                for segment in effective_shift_segments(db, shift)):
            result.add(shift.store_id)
    return result
