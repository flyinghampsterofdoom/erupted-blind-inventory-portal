"""One date resolver. Legacy dates remain unknown until management supplies them.

Account activation is intentionally not changed by scheduling lifecycle actions.
"""
from datetime import date, datetime, timedelta, timezone
from sqlalchemy import select
from app.models import Employee, SchedulingEligibilityPeriod, SchedulePeriod, SchedulePeriodStatus, ScheduleShift
from app.services.v2_scheduling_service import atomic_schedule_change

POST_CUTOFF = 'AFTER_LAST_EFFECTIVE_DATE'  # Preserve existing non-overridable validation identifier.


def profile_in_use_clause():
    """Dated rehires reuse retained policy without another activation toggle."""
    from sqlalchemy import or_
    from app.models import EmployeeSchedulingProfile
    managed = select(SchedulingEligibilityPeriod.id).where(
        SchedulingEligibilityPeriod.employee_id == EmployeeSchedulingProfile.employee_id).exists()
    return or_(EmployeeSchedulingProfile.active.is_(True), managed)


def legacy_candidate(employee):
    return bool(employee and employee.active and employee.scheduling_active
                and str(employee.square_status or '').upper() != 'INACTIVE')


def has_eligibility_periods(employee):
    return bool(getattr(employee, 'eligibility_periods', ()))


def within_employment_dates(employee: Employee | None, shift_date: date) -> bool:
    if employee is None:
        return False
    periods = getattr(employee, 'eligibility_periods', ())
    if periods:
        return any((p.start_date is None or p.start_date <= shift_date)
                   and (p.end_date is None or shift_date <= p.end_date) for p in periods)
    # Undated legacy participation remains unchanged; NULL does not mean inactive.
    return employee.last_effective_date is None or shift_date <= employee.last_effective_date


def eligible_on(employee, day):
    return bool(employee and (has_eligibility_periods(employee) or legacy_candidate(employee))
                and within_employment_dates(employee, day))


def eligible_during(employee, first, last):
    return any(eligible_on(employee, first + timedelta(days=n)) for n in range((last-first).days+1))


def scheduling_status(employee, today):
    periods = getattr(employee, 'eligibility_periods', ())
    if eligible_on(employee, today):
        current = next((p for p in periods if (p.start_date is None or p.start_date <= today)
                        and (p.end_date is None or today <= p.end_date)), None)
        end = current.end_date if current else employee.last_effective_date
        return 'Scheduled Departure' if end is not None else 'Active'
    return 'Scheduled Return' if any(p.start_date and p.start_date > today for p in periods) else 'Inactive'


def cutoff_message(employee):
    if not has_eligibility_periods(employee):
        return f'{employee.full_name} is assigned after their Last Effective Date ({employee.last_effective_date}). Correct the assignment; this cannot be overridden.'
    return f'{employee.full_name} is not eligible for scheduling on this shift date. Review their scheduling eligibility dates; this cannot be overridden.'


def eligibility_conflicts(db, employee_id, allows, today):
    # Include every operational draft/publication, even an unpublished correction
    # of a published week. Historical/retired evidence is deliberately untouched.
    return [s for s in db.scalars(select(ScheduleShift).join(SchedulePeriod).where(
        ScheduleShift.employee_id == employee_id, ScheduleShift.shift_date >= today,
        SchedulePeriod.is_historical.is_(False), SchedulePeriod.cleared_at.is_(None),
        SchedulePeriod.status.in_([SchedulePeriodStatus.DRAFT, SchedulePeriodStatus.PUBLISHED]))
        .order_by(ScheduleShift.shift_date, ScheduleShift.id)) if not allows(s.shift_date)]


def _review_eligibility_conflicts(db, conflicts, owner_review, today):
    if not conflicts:
        return {}
    if owner_review is None or any(s.shift_date < today for s in conflicts):
        dates = len({s.shift_date for s in conflicts})
        unit = 'date' if dates == 1 else 'dates'
        raise ValueError(f'Existing assignments on {dates} {unit} fall outside the proposed eligibility. Review them in the Schedule Board.')
    from app.services.scheduling_owner_review import warning_snapshot
    affected = {}
    for shift in conflicts:
        period = db.get(SchedulePeriod, shift.schedule_period_id)
        if period.id not in affected:
            affected[period.id] = (period, warning_snapshot(db, period), set())
        affected[period.id][2].add(shift.shift_date)
        owner_review.add('ELIGIBILITY', shift.shift_date, 'An existing assignment will remain outside scheduling eligibility.',
                         shift_id=shift.id, period_id=period.id)
    return affected


def _confirm_eligibility_change(db, owner_review, employee_id, affected):
    if not affected:
        return
    from app.services.v2_scheduling_coverage_service import rebuild_schedule_warnings
    from app.services.scheduling_owner_review import warning_snapshot
    for period, before, days in affected.values():
        owner_review.compare(before, warning_snapshot(db, period), days=days)
    owner_review.finish(db, entity_type='employee', entity_id=employee_id)
    for period, _, _ in affected.values():
        if period.status == SchedulePeriodStatus.DRAFT:
            rebuild_schedule_warnings(db, schedule_period_id=period.id)


@atomic_schedule_change
def edit_eligibility_period(db, *, principal, employee_id, period_id, start_date,
                            end_date, expected_updated_at=None, reason='', owner_review=None, today=None):
    """Correct a retained period in place; never create a replacement identity/history."""
    from app.services.access_control_service import principal_has_permission
    from app.v2.audit import V2AuditEvent, write_v2_audit_event
    if not principal.active or not principal_has_permission(db, principal=principal,
            permission_key='scheduling.manage_preferences',
            fallback_allowed=str(getattr(principal.role, 'value', principal.role)) in {'ADMIN', 'MANAGER'}):
        raise PermissionError('Employee scheduling management permission is required.')
    employee = db.scalar(select(Employee).where(Employee.id == employee_id)
                         .with_for_update().execution_options(populate_existing=True))
    if employee is None:
        raise ValueError('Employee not found.')
    period = next((p for p in employee.eligibility_periods if p.id == period_id), None)
    if period is None:
        raise ValueError('Scheduling eligibility period not found for this employee.')
    if expected_updated_at is not None and period.updated_at != expected_updated_at:
        raise ValueError('These dates were changed by another user. Reload the employee profile before saving.')
    if start_date is None and not period.legacy_unknown_start:
        raise ValueError('Enter the first eligible workday. Only an existing unrecorded start may remain blank.')
    if start_date and end_date and end_date < start_date:
        raise ValueError('The last active workday must be on or after the start date.')
    for other in employee.eligibility_periods:
        if other.id != period.id and (end_date is None or other.start_date is None or other.start_date <= end_date) and (other.end_date is None or start_date is None or start_date <= other.end_date):
            raise ValueError('These dates overlap another eligibility period. The next start must follow the previous last active workday.')
    def allows(day):
        return any((start_date is None or start_date <= day) and (end_date is None or day <= end_date)
                   if p.id == period.id else (p.start_date is None or p.start_date <= day) and
                   (p.end_date is None or day <= p.end_date) for p in employee.eligibility_periods)
    from zoneinfo import ZoneInfo
    today = today or datetime.now(ZoneInfo('America/Los_Angeles')).date()
    conflicts = eligibility_conflicts(db, employee.id, allows, date.min)
    affected = _review_eligibility_conflicts(db, conflicts, owner_review, today)
    before = {'period_id':period.id, 'start':str(period.start_date) if period.start_date else None,
              'end':str(period.end_date) if period.end_date else None}
    period.start_date = start_date; period.end_date = end_date
    period.legacy_unknown_start = start_date is None
    period.updated_by_principal_id = principal.id; period.updated_at = datetime.now(timezone.utc)
    db.flush()
    _confirm_eligibility_change(db, owner_review, employee.id, affected)
    write_v2_audit_event(db, event=V2AuditEvent(actor_principal_id=principal.id,
        action='SCHEDULING_ELIGIBILITY_CHANGED',domain='SCHEDULING',entity_type='employee',
        entity_id=employee.id,timestamp=datetime.now(timezone.utc),before=before,
        after={'period_id':period.id,'start':str(start_date) if start_date else None,'end':str(end_date) if end_date else None},
        metadata={'action':'edit','period_id':period.id,'reason':reason.strip()}),ip=None)
    return period


@atomic_schedule_change
def change_eligibility(db, *, principal, employee_id, action, day, today=None, owner_review=None):
    from zoneinfo import ZoneInfo
    from app.v2.audit import V2AuditEvent, write_v2_audit_event
    from app.services.access_control_service import principal_has_permission
    if not principal.active or not principal_has_permission(db, principal=principal, permission_key='scheduling.manage_preferences',
            fallback_allowed=str(getattr(principal.role, 'value', principal.role)) in {'ADMIN', 'MANAGER'}):
        raise PermissionError('Employee scheduling management permission is required.')
    # Same permission boundary as the existing employee scheduling editor.
    # Router capability checks also enforce explicit denials.
    employee = db.scalar(select(Employee).where(Employee.id == employee_id)
                         .with_for_update().execution_options(populate_existing=True))
    if employee is None:
        raise ValueError('Employee not found.')
    today = today or datetime.now(ZoneInfo('America/Los_Angeles')).date()
    periods = employee.eligibility_periods
    before = [{'id': p.id, 'start': str(p.start_date) if p.start_date else None,
               'end': str(p.end_date) if p.end_date else None} for p in periods]
    if action == 'end':
        current = next((p for p in periods if (p.start_date is None or p.start_date <= day)
                        and (p.end_date is None or day <= p.end_date)), None)
        if periods and current is None:
            raise ValueError('Choose a final workday within an existing eligibility period.')
        if not periods and not legacy_candidate(employee):
            raise ValueError('This employee has no active scheduling eligibility to end.')
        allows = lambda d: (within_employment_dates(employee, d) and
                           (d <= day or any(p is not current and (p.start_date is None or p.start_date <= d)
                            and (p.end_date is None or d <= p.end_date) for p in periods)))
        conflicts = eligibility_conflicts(db, employee.id, allows, today)
        affected = _review_eligibility_conflicts(db, conflicts, owner_review, today)
        if current:
            current.end_date = day
            current.updated_by_principal_id = principal.id
            current.updated_at = datetime.now(timezone.utc)
        else:
            periods.append(SchedulingEligibilityPeriod(employee_id=employee.id, start_date=None,
                end_date=day, legacy_unknown_start=True, created_by_principal_id=principal.id))
    elif action == 'reactivate':
        if any(p.end_date is None or p.end_date >= day for p in periods):
            raise ValueError('End the existing eligibility period first. Periods cannot overlap.')
        def proposed_allows(d):
            prior = (within_employment_dates(employee, d) if periods else
                     employee.last_effective_date is not None and d <= employee.last_effective_date)
            return prior or d >= day
        conflicts = eligibility_conflicts(db, employee.id, proposed_allows, today)
        affected = _review_eligibility_conflicts(db, conflicts, owner_review, today)
        if not periods:
            if employee.last_effective_date is not None:
                if employee.last_effective_date >= day:
                    raise ValueError('The first workday must follow the previous final workday.')
                periods.append(SchedulingEligibilityPeriod(employee_id=employee.id, start_date=None,
                    end_date=employee.last_effective_date, legacy_unknown_start=True,
                    created_by_principal_id=principal.id))
            elif legacy_candidate(employee):
                raise ValueError('This employee is already eligible. End the existing period before scheduling a return.')
        periods.append(SchedulingEligibilityPeriod(employee_id=employee.id, start_date=day,
            created_by_principal_id=principal.id))
    else:
        raise ValueError('Choose End Scheduling Eligibility or Reactivate for Scheduling.')
    db.flush()
    _confirm_eligibility_change(db, owner_review, employee.id, affected)
    after = [{'id': p.id, 'start': str(p.start_date) if p.start_date else None,
              'end': str(p.end_date) if p.end_date else None} for p in periods]
    write_v2_audit_event(db, event=V2AuditEvent(actor_principal_id=principal.id,
        action='SCHEDULING_ELIGIBILITY_CHANGED', domain='SCHEDULING', entity_type='employee',
        entity_id=employee.id, timestamp=datetime.now(timezone.utc), before={'periods': before},
        after={'periods': after}, metadata={'action': action, 'date': day.isoformat()}), ip=None)
    return employee
