from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import (
    CoverageRequirement,
    Employee,
    EmployeeSchedulingProfile,
    EmployeeSchedulingStorePreference,
    EmployeeSchedulingWindow,
    SchedulePeriod,
    ScheduleShift,
    ScheduleWarning,
    ScheduleWarningSeverity,
    SchedulingWindowKind,
    Store,
    StoreOperatingHour,
    StoreSpecialHour,
    TimeOffRequest,
    TimeOffRequestStatus,
)
from app.services.v2_scheduling_service import scheduled_paid_minutes


def scheduling_weekday(value: date) -> int:
    """Return Sunday=0 through Saturday=6 for scheduling records."""
    return (value.weekday() + 1) % 7


def _overlaps(start_a: time, end_a: time, start_b: time, end_b: time) -> bool:
    return start_a < end_b and end_a > start_b


def _contains(start_a: time, end_a: time, start_b: time, end_b: time) -> bool:
    return start_a <= start_b and end_a >= end_b


def _day_range(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def _resolved_hours(
    day: date,
    *,
    ordinary: dict[tuple[int, int], list[tuple[time, time]]],
    specials: dict[tuple[int, date], StoreSpecialHour],
    store_id: int,
) -> tuple[list[tuple[time, time]], bool, bool]:
    special = specials.get((store_id, day))
    if special is not None:
        if special.closed_all_day:
            return [], True, True
        return [(special.opening_time, special.closing_time)], True, False  # type: ignore[list-item]
    intervals = ordinary.get((store_id, scheduling_weekday(day)), [])
    return intervals, bool(intervals), False


def _new_warning(
    *,
    period_id: int,
    warning_type: str,
    severity: ScheduleWarningSeverity,
    store_id: int,
    warning_date: date,
    message: str,
    start_time: time | None = None,
    end_time: time | None = None,
    employee_id: int | None = None,
    shift_id: int | None = None,
    required_count: int | None = None,
    actual_count: int | None = None,
    evaluated_at: datetime,
) -> ScheduleWarning:
    return ScheduleWarning(
        schedule_period_id=period_id,
        warning_type=warning_type,
        severity=severity,
        store_id=store_id,
        warning_date=warning_date,
        start_time=start_time,
        end_time=end_time,
        employee_id=employee_id,
        shift_id=shift_id,
        required_count=required_count,
        actual_count=actual_count,
        message=message,
        evaluated_at=evaluated_at,
    )


def rebuild_schedule_warnings(db: Session, *, schedule_period_id: int) -> list[ScheduleWarning]:
    """Rebuild the deterministic warning cache for one immutable or draft revision.

    Coverage presence uses the full shift span because phase one stores only break
    duration. Paid-hour and labor calculations separately subtract that duration.
    """
    period = db.get(SchedulePeriod, schedule_period_id)
    if period is None:
        raise ValueError('Schedule period not found.')
    db.execute(delete(ScheduleWarning).where(ScheduleWarning.schedule_period_id == period.id))
    shifts = db.execute(
        select(ScheduleShift).where(ScheduleShift.schedule_period_id == period.id)
    ).scalars().all()
    employees = {
        row.id: row
        for row in db.execute(
            select(Employee).where(Employee.id.in_({s.employee_id for s in shifts if s.employee_id is not None}))
        ).scalars().all()
    } if any(s.employee_id is not None for s in shifts) else {}
    stores = {
        row.id: row
        for row in db.execute(select(Store).where(Store.id.in_({s.store_id for s in shifts}))).scalars().all()
    } if shifts else {}
    all_store_ids = set(stores)
    all_store_ids.update(
        db.execute(select(StoreOperatingHour.store_id).distinct()).scalars().all()
    )
    ordinary_rows = db.execute(
        select(StoreOperatingHour).where(
            StoreOperatingHour.store_id.in_(all_store_ids),
            StoreOperatingHour.active.is_(True),
        )
    ).scalars().all() if all_store_ids else []
    ordinary: dict[tuple[int, int], list[tuple[time, time]]] = defaultdict(list)
    for row in ordinary_rows:
        ordinary[(row.store_id, row.day_of_week)].append((row.opening_time, row.closing_time))
    special_rows = db.execute(
        select(StoreSpecialHour).where(
            StoreSpecialHour.store_id.in_(all_store_ids),
            StoreSpecialHour.calendar_date.between(period.week_start_date, period.week_end_date),
            StoreSpecialHour.active.is_(True),
        )
    ).scalars().all() if all_store_ids else []
    specials = {(row.store_id, row.calendar_date): row for row in special_rows}
    rules = db.execute(
        select(CoverageRequirement).where(
            CoverageRequirement.store_id.in_(all_store_ids),
            CoverageRequirement.active.is_(True),
        )
    ).scalars().all() if all_store_ids else []
    rules_by_store_day: dict[tuple[int, int], list[CoverageRequirement]] = defaultdict(list)
    for row in rules:
        rules_by_store_day[(row.store_id, row.day_of_week)].append(row)

    profiles = {
        row.employee_id: row
        for row in db.execute(
            select(EmployeeSchedulingProfile).where(
                EmployeeSchedulingProfile.employee_id.in_(employees),
                EmployeeSchedulingProfile.active.is_(True),
            )
        ).scalars().all()
    } if employees else {}
    windows = db.execute(
        select(EmployeeSchedulingWindow).where(
            EmployeeSchedulingWindow.employee_id.in_(employees),
            EmployeeSchedulingWindow.active.is_(True),
        )
    ).scalars().all() if employees else []
    windows_by_employee_day: dict[tuple[int, int], list[EmployeeSchedulingWindow]] = defaultdict(list)
    for row in windows:
        windows_by_employee_day[(row.employee_id, row.day_of_week)].append(row)
    preferred_store_pairs = set(
        db.execute(
            select(EmployeeSchedulingStorePreference.employee_id, EmployeeSchedulingStorePreference.store_id).where(
                EmployeeSchedulingStorePreference.employee_id.in_(employees),
                EmployeeSchedulingStorePreference.active.is_(True),
            )
        ).all()
    ) if employees else set()
    employees_with_store_preferences = {employee_id for employee_id, _ in preferred_store_pairs}
    time_off = db.execute(
        select(TimeOffRequest).where(
            TimeOffRequest.employee_id.in_(employees),
            TimeOffRequest.status == TimeOffRequestStatus.APPROVED,
            TimeOffRequest.start_date <= period.week_end_date,
            TimeOffRequest.end_date >= period.week_start_date,
        )
    ).scalars().all() if employees else []
    time_off_by_employee: dict[int, list[TimeOffRequest]] = defaultdict(list)
    for row in time_off:
        time_off_by_employee[row.employee_id].append(row)

    evaluated_at = datetime.now(tz=timezone.utc)
    warnings: list[ScheduleWarning] = []
    assigned_by_employee: dict[int, list[ScheduleShift]] = defaultdict(list)
    assigned_by_store_date: dict[tuple[int, date], list[ScheduleShift]] = defaultdict(list)

    for shift in shifts:
        if shift.employee_id is not None:
            assigned_by_employee[shift.employee_id].append(shift)
            assigned_by_store_date[(shift.store_id, shift.shift_date)].append(shift)
        store_name = stores.get(shift.store_id).name if shift.store_id in stores else f'Store {shift.store_id}'
        intervals, configured, closed = _resolved_hours(
            shift.shift_date,
            ordinary=ordinary,
            specials=specials,
            store_id=shift.store_id,
        )
        if closed:
            warnings.append(_new_warning(
                period_id=period.id, warning_type='SHIFT_ON_CLOSED_DATE', severity=ScheduleWarningSeverity.SERIOUS,
                store_id=shift.store_id, warning_date=shift.shift_date, start_time=shift.start_time,
                end_time=shift.end_time, employee_id=shift.employee_id, shift_id=shift.id,
                message=f'Shift scheduled while {store_name} is closed.', evaluated_at=evaluated_at,
            ))
        elif configured and not any(_contains(open_at, close_at, shift.start_time, shift.end_time) for open_at, close_at in intervals):
            warnings.append(_new_warning(
                period_id=period.id, warning_type='SHIFT_OUTSIDE_OPERATING_HOURS', severity=ScheduleWarningSeverity.CONFLICT,
                store_id=shift.store_id, warning_date=shift.shift_date, start_time=shift.start_time,
                end_time=shift.end_time, employee_id=shift.employee_id, shift_id=shift.id,
                message=f'Shift lies outside operating hours for {store_name}.', evaluated_at=evaluated_at,
            ))
        if shift.employee_id is None:
            continue
        employee = employees.get(shift.employee_id)
        if employee is not None and not employee.active:
            warnings.append(_new_warning(
                period_id=period.id, warning_type='INACTIVE_EMPLOYEE', severity=ScheduleWarningSeverity.CONFLICT,
                store_id=shift.store_id, warning_date=shift.shift_date, employee_id=shift.employee_id,
                shift_id=shift.id, message=f'Inactive employee {employee.full_name} remains assigned.', evaluated_at=evaluated_at,
            ))
        for request in time_off_by_employee.get(shift.employee_id, []):
            if not (request.start_date <= shift.shift_date <= request.end_date):
                continue
            if request.full_day or _overlaps(request.start_time, request.end_time, shift.start_time, shift.end_time):  # type: ignore[arg-type]
                warnings.append(_new_warning(
                    period_id=period.id, warning_type='APPROVED_TIME_OFF', severity=ScheduleWarningSeverity.SERIOUS,
                    store_id=shift.store_id, warning_date=shift.shift_date, start_time=shift.start_time,
                    end_time=shift.end_time, employee_id=shift.employee_id, shift_id=shift.id,
                    message='Shift conflicts with approved time off.', evaluated_at=evaluated_at,
                ))
                break
        day_windows = windows_by_employee_day.get((shift.employee_id, scheduling_weekday(shift.shift_date)), [])
        hard = [row for row in day_windows if row.kind == SchedulingWindowKind.HARD_UNAVAILABLE]
        if any(_overlaps(row.start_time, row.end_time, shift.start_time, shift.end_time) for row in hard):
            warnings.append(_new_warning(
                period_id=period.id, warning_type='HARD_UNAVAILABLE', severity=ScheduleWarningSeverity.SERIOUS,
                store_id=shift.store_id, warning_date=shift.shift_date, start_time=shift.start_time,
                end_time=shift.end_time, employee_id=shift.employee_id, shift_id=shift.id,
                message='Shift conflicts with hard unavailability.', evaluated_at=evaluated_at,
            ))
        preferred = [row for row in day_windows if row.kind == SchedulingWindowKind.PREFERRED]
        if preferred and not any(_contains(row.start_time, row.end_time, shift.start_time, shift.end_time) for row in preferred):
            warnings.append(_new_warning(
                period_id=period.id, warning_type='OUTSIDE_PREFERENCE', severity=ScheduleWarningSeverity.INFO,
                store_id=shift.store_id, warning_date=shift.shift_date, start_time=shift.start_time,
                end_time=shift.end_time, employee_id=shift.employee_id, shift_id=shift.id,
                message='Shift is outside the employee’s preferred time.', evaluated_at=evaluated_at,
            ))
        profile = profiles.get(shift.employee_id)
        if (
            shift.employee_id in employees_with_store_preferences
            and (shift.employee_id, shift.store_id) not in preferred_store_pairs
            and (profile is None or profile.home_store_id != shift.store_id)
        ):
            warnings.append(_new_warning(
                period_id=period.id, warning_type='NONPREFERRED_STORE', severity=ScheduleWarningSeverity.CONFLICT,
                store_id=shift.store_id, warning_date=shift.shift_date, employee_id=shift.employee_id,
                shift_id=shift.id, message='Shift is assigned to a nonpreferred store.', evaluated_at=evaluated_at,
            ))

    for employee_id, employee_shifts in assigned_by_employee.items():
        ordered = sorted(employee_shifts, key=lambda row: (row.shift_date, row.start_time, row.end_time, row.id))
        for index, left in enumerate(ordered):
            for right in ordered[index + 1:]:
                if right.shift_date != left.shift_date:
                    if right.shift_date > left.shift_date:
                        break
                    continue
                if _overlaps(left.start_time, left.end_time, right.start_time, right.end_time):
                    warnings.append(_new_warning(
                        period_id=period.id, warning_type='EMPLOYEE_OVERLAP', severity=ScheduleWarningSeverity.CONFLICT,
                        store_id=right.store_id, warning_date=right.shift_date, start_time=max(left.start_time, right.start_time),
                        end_time=min(left.end_time, right.end_time), employee_id=employee_id, shift_id=right.id,
                        message='Employee has overlapping shifts.', evaluated_at=evaluated_at,
                    ))
        profile = profiles.get(employee_id)
        if profile is None:
            continue
        paid_hours = Decimal(sum(scheduled_paid_minutes(row) for row in employee_shifts)) / Decimal(60)
        workdays = len({row.shift_date for row in employee_shifts})
        anchor = employee_shifts[0]
        if profile.maximum_weekly_hours is not None and paid_hours > profile.maximum_weekly_hours:
            warnings.append(_new_warning(
                period_id=period.id, warning_type='ABOVE_MAXIMUM_HOURS', severity=ScheduleWarningSeverity.CONFLICT,
                store_id=anchor.store_id, warning_date=period.week_end_date, employee_id=employee_id,
                required_count=int(profile.maximum_weekly_hours), actual_count=int(paid_hours),
                message='Employee exceeds configured maximum weekly hours.', evaluated_at=evaluated_at,
            ))
        if paid_hours < profile.target_weekly_hours:
            warnings.append(_new_warning(
                period_id=period.id, warning_type='BELOW_TARGET_HOURS', severity=ScheduleWarningSeverity.INFO,
                store_id=anchor.store_id, warning_date=period.week_end_date, employee_id=employee_id,
                message='Employee is below target weekly hours.', evaluated_at=evaluated_at,
            ))
        if profile.preferred_workdays is not None and workdays > profile.preferred_workdays:
            warnings.append(_new_warning(
                period_id=period.id, warning_type='ABOVE_PREFERRED_WORKDAYS', severity=ScheduleWarningSeverity.INFO,
                store_id=anchor.store_id, warning_date=period.week_end_date, employee_id=employee_id,
                required_count=profile.preferred_workdays, actual_count=workdays,
                message='Employee exceeds preferred number of workdays.', evaluated_at=evaluated_at,
            ))

    for store_id in all_store_ids:
        store = stores.get(store_id) or db.get(Store, store_id)
        store_name = store.name if store else f'Store {store_id}'
        for day in _day_range(period.week_start_date, period.week_end_date):
            intervals, configured, _closed = _resolved_hours(day, ordinary=ordinary, specials=specials, store_id=store_id)
            if not configured or not intervals:
                continue
            day_rules = rules_by_store_day.get((store_id, scheduling_weekday(day)), [])
            assigned = [row for row in assigned_by_store_date.get((store_id, day), []) if employees.get(row.employee_id) and employees[row.employee_id].active]
            for rule in day_rules:
                if not any(_contains(open_at, close_at, rule.start_time, rule.end_time) for open_at, close_at in intervals):
                    warnings.append(_new_warning(
                        period_id=period.id, warning_type='COVERAGE_RULE_OUTSIDE_HOURS',
                        severity=ScheduleWarningSeverity.INFO, store_id=store_id, warning_date=day,
                        start_time=rule.start_time, end_time=rule.end_time,
                        message=f'Coverage rule for {store_name} extends outside resolved operating hours.',
                        evaluated_at=evaluated_at,
                    ))
            for open_at, close_at in intervals:
                points = {open_at, close_at}
                for shift in assigned:
                    if _overlaps(open_at, close_at, shift.start_time, shift.end_time):
                        points.update((max(open_at, shift.start_time), min(close_at, shift.end_time)))
                for rule in day_rules:
                    if _overlaps(open_at, close_at, rule.start_time, rule.end_time):
                        points.update((max(open_at, rule.start_time), min(close_at, rule.end_time)))
                ordered_points = sorted(points)
                for start_at, end_at in zip(ordered_points, ordered_points[1:]):
                    active_rules = [row for row in day_rules if _overlaps(start_at, end_at, row.start_time, row.end_time)]
                    required = max([1, *(row.minimum_employee_count for row in active_rules)])
                    present = [row for row in assigned if _overlaps(start_at, end_at, row.start_time, row.end_time)]
                    actual = len({row.employee_id for row in present})
                    if actual < required:
                        warning_type = 'NO_ASSIGNED_EMPLOYEE' if actual == 0 else 'INSUFFICIENT_COVERAGE'
                        warnings.append(_new_warning(
                            period_id=period.id, warning_type=warning_type, severity=ScheduleWarningSeverity.SERIOUS,
                            store_id=store_id, warning_date=day, start_time=start_at, end_time=end_at,
                            required_count=required, actual_count=actual,
                            message=f'{store_name} has {actual} assigned employee(s) from {start_at.strftime("%I:%M %p").lstrip("0")} to {end_at.strftime("%I:%M %p").lstrip("0")}; {required} required.',
                            evaluated_at=evaluated_at,
                        ))

    db.add_all(warnings)
    db.flush()
    return warnings
