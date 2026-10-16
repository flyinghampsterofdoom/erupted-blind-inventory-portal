"""One weekly allocation resolver; profile values are undated legacy baselines."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import select
from app.models import Employee, EmployeeSchedulingProfile, SchedulingAllocationChange
from app.services.v2_scheduling_service import SchedulingValidationError, lock_scheduling


def week_start(day):
    return day - timedelta(days=(day.weekday() + 1) % 7)


def allocation_history(db, employee_id):
    return list(db.scalars(select(SchedulingAllocationChange).where(
        SchedulingAllocationChange.employee_id == employee_id).order_by(
            SchedulingAllocationChange.effective_date.asc().nullsfirst(), SchedulingAllocationChange.id).execution_options(populate_existing=True)))


def allocation_on(db, employee_id, day, *, profile=None):
    rows = allocation_history(db, employee_id)
    dated = [r for r in rows if r.active and r.effective_date is not None and r.effective_date <= day]
    if dated:
        return dated[-1].shifts_per_week
    baseline = next((r for r in rows if r.baseline), None)
    if baseline:
        return baseline.shifts_per_week
    if profile is None:
        profile = db.scalar(select(EmployeeSchedulingProfile).where(EmployeeSchedulingProfile.employee_id == employee_id))
    return profile.target_shifts_per_week if profile else None


def weekly_allocation(db, employee_id, day, *, profile=None):
    start = week_start(day)
    boundaries = [r for r in allocation_history(db, employee_id)
                  if r.active and r.effective_date and start < r.effective_date <= start + timedelta(days=6)]
    if boundaries:
        raise SchedulingValidationError(
            f'Employee #{employee_id} has an allocation change during the week of {start}. '
            'Choose a Sunday effective date before generating this week; weekly allocations are not prorated.')
    return allocation_on(db, employee_id, start, profile=profile)


def change_allocation(db, *, principal, employee_id, effective_date=None, shifts_per_week=None,
                      change_id=None, expected_revision=None, cancel=False, reason='', today=None):
    from app.services.access_control_service import principal_has_permission
    from app.v2.audit import V2AuditEvent, write_v2_audit_event
    if not principal.active or not principal_has_permission(db, principal=principal,
            permission_key='scheduling.manage_preferences',
            fallback_allowed=str(getattr(principal.role, 'value', principal.role)) in {'ADMIN', 'MANAGER'}):
        raise PermissionError('Employee scheduling management permission is required.')
    lock_scheduling(db)
    today = today or datetime.now(ZoneInfo('America/Los_Angeles')).date()
    employee = db.scalar(select(Employee).where(Employee.id == employee_id).with_for_update().execution_options(populate_existing=True))
    profile = db.scalar(select(EmployeeSchedulingProfile).where(EmployeeSchedulingProfile.employee_id == employee_id).with_for_update().execution_options(populate_existing=True))
    if not employee or not profile:
        raise SchedulingValidationError('Save a scheduling profile before scheduling an allocation change.')
    rows = allocation_history(db, employee_id)
    old = next((r for r in rows if r.id == change_id and r.active and not r.baseline), None) if change_id else None
    if change_id and (old is None or expected_revision != old.revision):
        raise SchedulingValidationError('This change was updated by another user. Reload the profile before saving.')
    if old and old.effective_date <= today:
        raise SchedulingValidationError('This allocation is already in effect. Schedule a new change to preserve its history.')
    if not cancel:
        if effective_date is None or effective_date <= today:
            raise SchedulingValidationError('Choose a future Sunday for the allocation change.')
        if effective_date.weekday() != 6:
            raise SchedulingValidationError('Choose Sunday, the first day of the scheduling week. Midweek allocations are not prorated.')
        if shifts_per_week is None or not 0 <= shifts_per_week <= 7:
            raise SchedulingValidationError('Weekly allocation must be between zero and seven shifts.')
        if any(r.active and r.effective_date == effective_date and r.id != change_id for r in rows):
            raise SchedulingValidationError('An allocation change already exists for this date. Edit that change instead.')
    elif old is None:
        raise SchedulingValidationError('Choose an upcoming change to cancel.')
    if not any(r.baseline for r in rows):
        db.add(SchedulingAllocationChange(employee_id=employee_id, shifts_per_week=profile.target_shifts_per_week,
            baseline=True, created_by_principal_id=principal.id))
    before = {'id':old.id,'effective_date':str(old.effective_date),'shifts_per_week':old.shifts_per_week,'revision':old.revision} if old else None
    if old:
        old.active = False
        db.flush()  # Release the active-date key, retaining the original revision.
    row = None
    if not cancel:
        row = SchedulingAllocationChange(employee_id=employee_id,effective_date=effective_date,
            shifts_per_week=shifts_per_week,created_by_principal_id=principal.id,
            revision=old.revision + 1 if old else 1,replaces_id=old.id if old else None)
        db.add(row)
    db.flush()
    write_v2_audit_event(db, event=V2AuditEvent(action='ALLOCATION_CHANGED', domain='scheduling',
        actor_principal_id=principal.id,
        entity_type='scheduling_allocation_change',entity_id=(row or old).id,
        before=before,after={'employee_id':employee_id,'effective_date':str(effective_date) if not cancel else None,
            'shifts_per_week':shifts_per_week if not cancel else None,'cancelled':cancel},
        metadata={'reason':reason.strip(),'assignments_unchanged':True}), ip=None)
    return row or old
