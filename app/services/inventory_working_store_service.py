"""Scheduling-derived inventory scope; session selection is never a grant."""
from dataclasses import replace
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from sqlalchemy import select
from app.models import Employee, Store, SchedulePeriod, SchedulePeriodStatus, ScheduleShift
from app.services.v2_scheduling_policy_service import organization_policy


class WorkingStoreConflict(PermissionError):
    pass


def lock_employee(db, employee_id):
    return db.scalar(select(Employee).where(Employee.id == employee_id).with_for_update()
                     .execution_options(populate_existing=True))


def scheduled_store(db, employee, *, now=None):
    now = now or datetime.now(timezone.utc)
    day = now.astimezone(ZoneInfo(organization_policy(db).timezone_name)).date()
    local = now.astimezone(ZoneInfo(organization_policy(db).timezone_name))
    periods = list(db.scalars(select(SchedulePeriod).where(
        SchedulePeriod.status == SchedulePeriodStatus.PUBLISHED,
        SchedulePeriod.is_historical.is_(False), SchedulePeriod.cleared_at.is_(None),
        SchedulePeriod.week_start_date <= day, SchedulePeriod.week_end_date >= day)
        .order_by(SchedulePeriod.revision_number)))
    current = {p.week_start_date:p.id for p in periods}
    shifts = list(db.scalars(select(ScheduleShift).where(
        ScheduleShift.schedule_period_id.in_(tuple(current.values())), ScheduleShift.shift_date == day)))
    from app.services.v2_scheduling_exception_service import expected_employee_id
    shifts = [s for s in shifts if expected_employee_id(db, s) == employee.id]
    # Sequential assignments may resolve at different times. Overlapping stores
    # fail closed. Outside shift hours, only an unambiguous daily store is usable.
    active = [s for s in shifts if s.start_time <= local.time().replace(tzinfo=None) < s.end_time]
    stores = {s.store_id for s in (active or shifts)}
    return next(iter(stores)) if len(stores) == 1 else None



def working_context(db, *, principal, selected_store_id=None, now=None, lock=False):
    now = now or datetime.now(timezone.utc)
    employee = db.scalar(select(Employee).where(Employee.principal_id == principal.id))
    if employee is None:
        # Preserve legacy shared-store accounts; they have no Scheduling identity.
        return {'store_id': principal.store_id, 'store_name': (db.get(Store, principal.store_id).name if principal.store_id and db.get(Store, principal.store_id) else 'Unassigned'), 'version': 'legacy', 'emergency_options': []}
    if lock:
        employee = lock_employee(db, employee.id)
    if not employee.active:
        return {'store_id': None, 'version': str(employee.working_context_version), 'emergency_options': []}
    normal = scheduled_store(db, employee, now=now) if (employee.scheduling_active or employee.eligibility_periods) else None
    if normal is not None and not db.get(Store, normal).active:
        normal = None
    # Historical grants remain evidence only. Session selection never grants access.
    local_date = now.astimezone(ZoneInfo(organization_policy(db).timezone_name)).date()
    version = f'{employee.working_context_version}:{local_date}:{normal}:scheduled'
    return {'store_id': normal, 'store_name': db.get(Store, normal).name if normal else 'Unassigned',
            'scheduled_store_id': normal, 'version': version, 'emergency_options': []}


def inventory_principal(db, *, principal, selected_store_id=None, now=None):
    context = working_context(db, principal=principal, selected_store_id=selected_store_id, now=now, lock=True)
    if context['store_id'] is None:
        raise WorkingStoreConflict('No unambiguous published working store. Ask a Lead for assistance.')
    return replace(principal, store_id=context['store_id']), context


def assert_inventory_store(db, *, principal, store_id, now=None):
    # Existing management endpoints pass an unscoped management principal.
    # Employee workflow dependencies always pass a resolved store instead.
    from app.auth import Role
    from app.services.access_control_service import principal_has_permission
    if principal.role in (Role.ADMIN, Role.MANAGER, Role.LEAD) and principal.store_id is None and principal_has_permission(
            db, principal=principal, permission_key='management.access', fallback_allowed=True):
        return {'store_id': store_id, 'version': 'management'}
    # Recheck inside the write transaction, before the count/store locks.
    context = working_context(db, principal=principal,
        selected_store_id=db.info.get('inventory_selected_store_id'), now=now, lock=True)
    if context['version'] != 'legacy' and context['store_id'] != store_id:
        raise WorkingStoreConflict('Working store changed. Pending count entries remain at their original store.')
    return context
