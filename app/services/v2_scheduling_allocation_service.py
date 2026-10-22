"""One weekly allocation resolver; profile values are undated legacy baselines."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import select
from app.models import Employee, EmployeeSchedulingProfile, SchedulingAllocationChange
from app.services.v2_scheduling_service import SchedulingValidationError, lock_scheduling


def week_start(day):
    return day - timedelta(days=(day.weekday() + 1) % 7)


def allocation_history(db, employee_id):
    from app.services.read_scope import has_read_scope, read_value
    if has_read_scope(db):
        rows = read_value(db, ('allocation_history',), lambda: list(db.scalars(
            select(SchedulingAllocationChange).order_by(SchedulingAllocationChange.effective_date.asc().nullsfirst(),
                SchedulingAllocationChange.id).execution_options(populate_existing=True))))
        return [row for row in rows if row.employee_id == employee_id]
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
    exception = leave_exception(db, employee_id, start)
    return exception.work_shifts if exception is not None else allocation_on(db, employee_id, start, profile=profile)


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


def leave_exception(db, employee_id, day):
    from app.models import SchedulingLeaveAllocationException as ExceptionRow, TimeOffRequest, TimeOffRequestStatus
    start = week_start(day)
    statement = select(ExceptionRow).join(TimeOffRequest, TimeOffRequest.id == ExceptionRow.request_id).where(
        ExceptionRow.week_start_date == start, ExceptionRow.active.is_(True),
        TimeOffRequest.employee_id == ExceptionRow.employee_id,
        TimeOffRequest.status == TimeOffRequestStatus.APPROVED, TimeOffRequest.full_day.is_(True),
        TimeOffRequest.start_date <= start, TimeOffRequest.end_date >= start + timedelta(days=6))
    from app.services.read_scope import has_read_scope, read_value
    if has_read_scope(db):
        rows = read_value(db, ('weekly_leave_exceptions', start),
                          lambda: list(db.scalars(statement)))
        return next((row for row in rows if row.employee_id == employee_id), None)
    return db.scalar(statement.where(ExceptionRow.employee_id == employee_id))


def set_leave_exception(db, *, principal, request_id, week, reason, revoke_id=None):
    from app.models import SchedulingLeaveAllocationException as ExceptionRow, TimeOffRequest, TimeOffRequestStatus
    from app.services.access_control_service import principal_has_permission
    from app.services.v2_scheduling_rules_service import _audit
    if not principal.active or not principal_has_permission(db, principal=principal,
            permission_key='scheduling.manage_preferences',
            fallback_allowed=str(getattr(principal.role,'value',principal.role)) in {'ADMIN','MANAGER'}):
        raise PermissionError('Weekly leave allocation changes require employee scheduling management permission.')
    if not reason.strip() or len(reason) > 2000:
        raise SchedulingValidationError('Provide a reason of up to 2,000 characters.')
    lock_scheduling(db)
    request = db.scalar(select(TimeOffRequest).where(TimeOffRequest.id == request_id).with_for_update().execution_options(populate_existing=True))
    if request is None:
        raise SchedulingValidationError('Time-off request not found.')
    if revoke_id is not None:
        row = db.scalar(select(ExceptionRow).where(ExceptionRow.id == revoke_id,
            ExceptionRow.request_id == request_id, ExceptionRow.active.is_(True)).with_for_update().execution_options(populate_existing=True))
        if row is None:
            raise SchedulingValidationError('Active leave exception not found.')
        row.active = False
        row.revoked_at = datetime.now(ZoneInfo('America/Los_Angeles'))
        row.revoked_by_principal_id = principal.id
        row.revocation_reason = reason.strip()
        action = 'LEAVE_ALLOCATION_REVOKED'
    else:
        from app.services.v2_scheduling_lead_duty_service import business_today
        today = business_today(db)
        if week != week_start(week) or week + timedelta(days=6) < today:
            raise SchedulingValidationError('Choose a current or future Sunday scheduling week.')
        if request.status != TimeOffRequestStatus.APPROVED or not request.full_day or request.start_date > week or request.end_date < week + timedelta(days=6):
            raise SchedulingValidationError('A zero-work leave exception requires approved full-day PTO covering the entire scheduling week.')
        normal = allocation_on(db, request.employee_id, week)
        if normal is None:
            raise SchedulingValidationError('Configure the normal allocation first.')
        if db.scalar(select(ExceptionRow.id).where(ExceptionRow.employee_id == request.employee_id,
                ExceptionRow.week_start_date == week, ExceptionRow.active.is_(True))):
            raise SchedulingValidationError('This employee already has an active leave exception for that week.')
        row = ExceptionRow(employee_id=request.employee_id, request_id=request.id,
            week_start_date=week, normal_shifts=normal, work_shifts=0, reason=reason.strip(),
            active=True, created_by_principal_id=principal.id)
        db.add(row)
        action = 'LEAVE_ALLOCATION_AUTHORIZED'
    db.flush()
    _audit(db, principal=principal, action=action, entity_type='scheduling_leave_allocation_exception', entity_id=row.id,
        after={'employee_id':row.employee_id,'request_id':row.request_id,'week':str(row.week_start_date),
               'normal_shifts':row.normal_shifts,'work_shifts':row.work_shifts,'active':row.active}, reason=reason.strip())
    return row


def revoke_request_leave_exceptions(db, *, principal, request_id):
    from app.models import SchedulingLeaveAllocationException as ExceptionRow
    for row in list(db.scalars(select(ExceptionRow).where(ExceptionRow.request_id == request_id, ExceptionRow.active.is_(True)))):
        # PTO review authority can retire derived authorizations when their source
        # approval is removed; it does not grant authority to create allocations.
        row.active = False
        row.revoked_by_principal_id = principal.id
        row.revoked_at = datetime.now(ZoneInfo('America/Los_Angeles'))
        row.revocation_reason = 'Source PTO approval changed.'
        from app.services.v2_scheduling_rules_service import _audit
        _audit(db, principal=principal, action='LEAVE_ALLOCATION_REVOKED',
            entity_type='scheduling_leave_allocation_exception', entity_id=row.id,
            after={'active':False,'request_id':request_id}, reason=row.revocation_reason)


def allocation_report(db, period):
    """Normal, authorized, and actual counts remain distinct in review/evidence."""
    from collections import Counter
    from app.models import ScheduleShift
    from app.services.v2_scheduling_roster_service import list_scheduling_candidates
    from app.services.v2_scheduling_lifecycle_service import eligible_during
    counts = Counter(db.scalars(select(ScheduleShift.employee_id).where(
        ScheduleShift.schedule_period_id == period.id, ScheduleShift.employee_id.is_not(None))))
    rows = []
    for employee in list_scheduling_candidates(db):
        if not eligible_during(employee, period.week_start_date, period.week_end_date):
            continue
        exception = leave_exception(db, employee.id, period.week_start_date)
        rows.append({'employee_id':employee.id,'normal_shifts':allocation_on(db,employee.id,period.week_start_date),
            'effective_shifts':weekly_allocation(db,employee.id,period.week_start_date),'actual_shifts':counts[employee.id],
            'leave_exception_id':exception.id if exception else None,
            'requires_leave_authorization':False})
    targets = {r['employee_id']: r['effective_shifts'] for r in rows if r['effective_shifts'] is not None}
    allowed = closure_shortfall_context(db, period, targets, counts)
    for row in rows:
        row['allocation_shortfall'] = max(0, (row['effective_shifts'] or 0)-row['actual_shifts'])
        row['closure_shortfall'] = (max(0, (row['effective_shifts'] or 0)-row['actual_shifts']) if allowed else 0)
    return rows


def weekly_capacity_reductions(db, period):
    """Report dated closure capacity and intentional vacancies without changing targets."""
    from app.models import CoverageRequirement, ScheduleShift, StoreSpecialHour
    from app.services.v2_scheduling_coverage_service import configured_headcounts, scheduling_weekday
    counts = configured_headcounts(db.scalars(select(CoverageRequirement).where(CoverageRequirement.active.is_(True))))
    closures = {(r.store_id, r.calendar_date) for r in db.scalars(select(StoreSpecialHour).where(
        StoreSpecialHour.active.is_(True), StoreSpecialHour.closed_all_day.is_(True),
        StoreSpecialHour.calendar_date.between(period.week_start_date, period.week_end_date)))}
    closed_positions = sum(counts.get((sid, scheduling_weekday(day)), 0) for sid, day in closures)
    vacancies = list(db.scalars(select(ScheduleShift).where(ScheduleShift.schedule_period_id == period.id,
        ScheduleShift.employee_id.is_(None), ScheduleShift.manually_locked.is_(True))))
    intentional = sum((r.store_id, r.shift_date) not in closures for r in vacancies)
    return {'closure_positions': closed_positions, 'manual_vacancies': intentional,
            'total': closed_positions + intentional}


def closure_shortfall_context(db, period, targets, counts):
    """Label shortages reconciled with closures; this is not an approval gate."""
    reductions = weekly_capacity_reductions(db, period)
    shortage = sum(max(0, target-counts.get(eid, 0)) for eid, target in targets.items())
    return (0 < shortage <= reductions['closure_positions']
            and all(counts.get(eid, 0) <= target for eid, target in targets.items()))
