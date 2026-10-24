"""Scheduling-derived inventory scope; session selection is never a grant."""
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
from zoneinfo import ZoneInfo
from sqlalchemy import select
from app.models import Employee, Store, SchedulePeriod, SchedulePeriodStatus, ScheduleShift
from app.services.v2_scheduling_lifecycle_service import within_employment_dates
from app.services.v2_scheduling_policy_service import organization_policy


class WorkingStoreConflict(PermissionError):
    pass


def lock_employee(db, employee_id):
    return db.scalar(select(Employee).where(Employee.id == employee_id).with_for_update()
                     .execution_options(populate_existing=True))


def _scheduled_context(db, employee, *, now=None, selected_store_id=None):
    """Resolve live work from the same effective intervals used by Scheduling.

    Ordinary, unambiguous daily assignments retain their existing pre/post shift
    access. Coverage and attendance-adjusted intervals only justify access while
    that interval is active. A saved emergency replacement takes precedence over
    an overlapping ordinary assignment without changing either assignment.
    """
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo(organization_policy(db).timezone_name))
    day = local.date()
    periods = list(db.scalars(select(SchedulePeriod).where(
        SchedulePeriod.status == SchedulePeriodStatus.PUBLISHED,
        SchedulePeriod.is_historical.is_(False), SchedulePeriod.cleared_at.is_(None),
        SchedulePeriod.week_start_date <= day, SchedulePeriod.week_end_date >= day)
        .order_by(SchedulePeriod.revision_number, SchedulePeriod.id)))
    current = {p.week_start_date:p.id for p in periods}
    shifts = list(db.scalars(select(ScheduleShift).where(
        ScheduleShift.schedule_period_id.in_(tuple(current.values())), ScheduleShift.shift_date == day)))
    from app.services.v2_scheduling_exception_service import effective_shift_segments
    segments, fallback = [], []
    for shift in shifts:
        store = db.get(Store, shift.store_id)
        if store is None or not store.active:
            continue
        for segment in effective_shift_segments(db, shift):
            if segment.employee_id != employee.id:
                continue
            segments.append(segment)
            if (not segment.is_coverage and segment.start_time == shift.start_time
                    and segment.end_time == shift.end_time):
                fallback.append(segment)
    clock = local.time().replace(tzinfo=None)
    active = [s for s in segments if s.start_time <= clock < s.end_time]
    coverage = [s for s in active if s.is_coverage]
    eligible = active or fallback
    stores = {s.store_id for s in eligible}
    if coverage:
        coverage_stores = {s.store_id for s in coverage}
        store_id = (selected_store_id if selected_store_id in coverage_stores else
                    max(coverage, key=lambda s: (s.coverage_assignment_id or 0, s.id)).store_id)
    else:
        # Preserve the existing fail-closed behavior for ambiguous ordinary
        # assignments: an arbitrary session store selection is never a grant.
        store_id = next(iter(stores)) if len(stores) == 1 else None
    fingerprint = '|'.join(sorted(
        f'{s.id}:{s.employee_id}:{s.store_id}:{s.start_time}:{s.end_time}:'
        f'{s.coverage_assignment_id}:{int(s in active)}' for s in segments))
    return {'store_id': store_id, 'store_ids': sorted(stores),
            'assignment_version': hashlib.sha256(fingerprint.encode()).hexdigest()[:16]}


def scheduled_store(db, employee, *, now=None):
    return _scheduled_context(db, employee, now=now)['store_id']



def working_context(db, *, principal, selected_store_id=None, now=None, lock=False):
    now = now or datetime.now(timezone.utc)
    employee = db.scalar(select(Employee).where(Employee.principal_id == principal.id))
    if employee is None:
        # Preserve legacy shared-store accounts; they have no Scheduling identity.
        from app.auth import Role
        if principal.role != Role.STORE:
            return {'store_id': None, 'version': 'unlinked', 'emergency_options': []}
        return {'store_id': principal.store_id, 'store_name': (db.get(Store, principal.store_id).name if principal.store_id and db.get(Store, principal.store_id) else 'Unassigned'), 'version': 'legacy', 'emergency_options': []}
    if lock:
        employee = lock_employee(db, employee.id)
    if not employee.active:
        return {'store_id': None, 'version': str(employee.working_context_version), 'emergency_options': []}
    local_date = now.astimezone(ZoneInfo(organization_policy(db).timezone_name)).date()
    schedule = (_scheduled_context(db, employee, now=now, selected_store_id=selected_store_id)
                if ((employee.scheduling_active or employee.eligibility_periods)
                    and within_employment_dates(employee, local_date)) else
                {'store_id': None, 'store_ids': [], 'assignment_version': 'inactive'})
    normal = schedule['store_id']
    # Historical grants remain evidence only. Session selection never grants access.
    version = f'{employee.working_context_version}:{local_date}:{normal}:scheduled:{schedule["assignment_version"]}'
    return {'store_id': normal, 'store_name': db.get(Store, normal).name if normal else 'Unassigned',
            'scheduled_store_id': normal, 'version': version, 'emergency_options': [],
            'scheduled_store_ids': schedule['store_ids']}


def inventory_principal(db, *, principal, selected_store_id=None, now=None):
    context = working_context(db, principal=principal, selected_store_id=selected_store_id, now=now, lock=True)
    if context['store_id'] is None:
        raise WorkingStoreConflict('No unambiguous published working store. Ask a Lead for assistance.')
    return replace(principal, store_id=context['store_id']), context


def assert_employee_inventory_access(db, *, principal):
    """Count entry stays scoped when an employee also has supervisory access."""
    from app.auth import Role
    from app.services.access_control_service import employee_workflow_identity, principal_has_permission
    if (not principal.active or principal.store_id is None
            or principal.role not in (Role.STORE, Role.LEAD)
            or (principal.role == Role.LEAD and not employee_workflow_identity(db, principal=principal))
            or not principal_has_permission(db, principal=principal,
                permission_key='store.access', fallback_allowed=principal.role == Role.STORE)):
        raise PermissionError('An authorized employee working store is required.')
    assert_inventory_store(db, principal=principal, store_id=principal.store_id)


def assert_inventory_store(db, *, principal, store_id, now=None):
    # Existing management endpoints pass an unscoped management principal.
    # Employee workflow dependencies always pass a resolved store instead.
    from app.auth import Role, assert_store_scope
    from app.services.access_control_service import principal_has_permission
    if principal.role in (Role.ADMIN, Role.MANAGER, Role.LEAD) and principal.store_id is None and principal_has_permission(
            db, principal=principal, permission_key='management.access', fallback_allowed=True):
        return {'store_id': store_id, 'version': 'management'}
    # Recheck inside the write transaction, before the count/store locks.
    context = working_context(db, principal=principal,
        selected_store_id=db.info.get('inventory_selected_store_id'), now=now, lock=True)
    if context['version'] == 'legacy':
        assert_store_scope(principal, store_id)
    if context['store_id'] != store_id:
        raise WorkingStoreConflict('Working store changed. Pending count entries remain at their original store.')
    return context
