from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Employee,
    EmployeeSchedulingProfile,
    EmployeeSchedulingStorePreference,
    EmployeeSchedulingWindow,
    Principal as PrincipalModel,
    SchedulePeriod,
    SchedulePeriodStatus,
    ScheduleShift,
    ScheduleShiftType,
    ScheduleWarning,
    ScheduleWarningSeverity,
    Store,
    TimeOffRequest,
    TimeOffRequestStatus,
)
from app.services.v2_scheduling_coverage_service import rebuild_schedule_warnings, scheduling_weekday
from app.services.v2_scheduling_rules_service import estimate_labor_cost
from app.services.v2_scheduling_service import scheduled_paid_minutes


COVERAGE_WARNING_TYPES = frozenset({
    'NO_ASSIGNED_EMPLOYEE',
    'INSUFFICIENT_COVERAGE',
    'REQUIRED_ROLE_ABSENT',
    'NO_OPENER',
    'NO_CLOSER',
    'SHIFT_ON_CLOSED_DATE',
    'SHIFT_OUTSIDE_OPERATING_HOURS',
})
WEEKDAY_NAMES = ('Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday')


def normalize_week_start(value: date) -> date:
    return value - timedelta(days=(value.weekday() + 1) % 7)


def _hours(minutes: int) -> float:
    return float((Decimal(minutes) / Decimal(60)).quantize(Decimal('0.01')))


def _clock(value) -> str:
    return value.strftime('%-I:%M %p')


def _period_for_week(db: Session, week_start: date) -> tuple[SchedulePeriod | None, SchedulePeriod | None, list[SchedulePeriod]]:
    periods = db.execute(
        select(SchedulePeriod).where(SchedulePeriod.week_start_date == week_start).order_by(
            SchedulePeriod.revision_number.desc()
        )
    ).scalars().all()
    draft = next((row for row in periods if row.status == SchedulePeriodStatus.DRAFT), None)
    published = next((row for row in periods if row.status == SchedulePeriodStatus.PUBLISHED), None)
    return draft or published, published, periods


def _indicator_label(kind: str) -> str:
    return {
        'PREFERRED': 'Preferred time',
        'AVAILABLE': 'Available time',
        'HARD_UNAVAILABLE': 'Hard unavailable',
        'TIME_OFF': 'Approved time off',
    }[kind]


def serialize_week_board(
    db: Session,
    *,
    week_start: date,
    selected_store_ids: tuple[int, ...],
    all_authorized_store_ids: tuple[int, ...],
    permission_flags: dict[str, bool],
) -> dict:
    week_start = normalize_week_start(week_start)
    week_end = week_start + timedelta(days=6)
    period, current_published, history = _period_for_week(db, week_start)
    selected_set = set(selected_store_ids)
    stores = db.execute(
        select(Store).where(Store.id.in_(selected_store_ids)).order_by(Store.name, Store.id)
    ).scalars().all() if selected_store_ids else []
    store_by_id = {row.id: row for row in stores}
    shift_types = db.execute(
        select(ScheduleShiftType).where(ScheduleShiftType.active.is_(True)).order_by(
            ScheduleShiftType.display_order, ScheduleShiftType.name
        )
    ).scalars().all()

    shifts = []
    warnings = []
    if period is not None:
        # The cache is derived and rebuildable. Rebuilding on board load ensures
        # copied, approved-time-off, and configuration changes are reflected.
        rebuild_schedule_warnings(db, schedule_period_id=period.id)
        shifts = db.execute(
            select(ScheduleShift).where(
                ScheduleShift.schedule_period_id == period.id,
                ScheduleShift.store_id.in_(selected_store_ids),
            ).order_by(ScheduleShift.shift_date, ScheduleShift.start_time, ScheduleShift.id)
        ).scalars().all()
        warnings = db.execute(
            select(ScheduleWarning).where(
                ScheduleWarning.schedule_period_id == period.id,
                ScheduleWarning.store_id.in_(selected_store_ids),
            ).order_by(
                ScheduleWarning.severity.desc(), ScheduleWarning.warning_date,
                ScheduleWarning.start_time, ScheduleWarning.id,
            )
        ).scalars().all()

    referenced_employee_ids = {row.employee_id for row in shifts if row.employee_id is not None}
    employee_rows = db.execute(
        select(Employee, EmployeeSchedulingProfile).outerjoin(
            EmployeeSchedulingProfile,
            EmployeeSchedulingProfile.employee_id == Employee.id,
        ).order_by(Employee.full_name, Employee.id)
    ).all()
    all_scope = set(selected_store_ids) == set(all_authorized_store_ids)
    included: list[tuple[Employee, EmployeeSchedulingProfile | None]] = []
    for employee, profile in employee_rows:
        referenced = employee.id in referenced_employee_ids
        if not employee.active and not referenced:
            continue
        if referenced:
            included.append((employee, profile))
            continue
        if profile is not None and profile.home_store_id in selected_set:
            included.append((employee, profile))
        elif employee.active and profile is None and all_scope:
            included.append((employee, profile))
    employee_ids = {row.id for row, _ in included}

    windows = db.execute(
        select(EmployeeSchedulingWindow).where(
            EmployeeSchedulingWindow.employee_id.in_(employee_ids),
            EmployeeSchedulingWindow.active.is_(True),
        ).order_by(EmployeeSchedulingWindow.day_of_week, EmployeeSchedulingWindow.start_time)
    ).scalars().all() if employee_ids else []
    windows_by_employee_day: dict[tuple[int, int], list[EmployeeSchedulingWindow]] = defaultdict(list)
    for row in windows:
        windows_by_employee_day[(row.employee_id, row.day_of_week)].append(row)

    preferences = db.execute(
        select(EmployeeSchedulingStorePreference).where(
            EmployeeSchedulingStorePreference.employee_id.in_(employee_ids),
            EmployeeSchedulingStorePreference.store_id.in_(selected_store_ids),
            EmployeeSchedulingStorePreference.active.is_(True),
        ).order_by(EmployeeSchedulingStorePreference.preference_rank.nullslast())
    ).scalars().all() if employee_ids else []
    preferred_stores: dict[int, list[int]] = defaultdict(list)
    for row in preferences:
        preferred_stores[row.employee_id].append(row.store_id)

    time_off = db.execute(
        select(TimeOffRequest).where(
            TimeOffRequest.employee_id.in_(employee_ids),
            TimeOffRequest.status == TimeOffRequestStatus.APPROVED,
            TimeOffRequest.start_date <= week_end,
            TimeOffRequest.end_date >= week_start,
        )
    ).scalars().all() if employee_ids else []
    time_off_by_employee_day: dict[tuple[int, date], list[dict]] = defaultdict(list)
    for row in time_off:
        day = max(row.start_date, week_start)
        through = min(row.end_date, week_end)
        while day <= through:
            time_off_by_employee_day[(row.employee_id, day)].append({
                'kind': 'TIME_OFF',
                'label': _indicator_label('TIME_OFF'),
                'full_day': row.full_day,
                'start_time': row.start_time.isoformat() if row.start_time else None,
                'end_time': row.end_time.isoformat() if row.end_time else None,
                'display': 'All day' if row.full_day else f'{_clock(row.start_time)}–{_clock(row.end_time)}',
            })
            day += timedelta(days=1)

    warning_count_by_employee: dict[int, int] = defaultdict(int)
    warning_ids_by_shift: dict[int, list[int]] = defaultdict(list)
    warning_count_by_store: dict[int, int] = defaultdict(int)
    warning_count_by_date: dict[date, int] = defaultdict(int)
    for warning in warnings:
        if warning.employee_id is not None and warning.severity in {
            ScheduleWarningSeverity.INFO, ScheduleWarningSeverity.CONFLICT, ScheduleWarningSeverity.SERIOUS,
        }:
            warning_count_by_employee[warning.employee_id] += 1
        if warning.shift_id is not None:
            warning_ids_by_shift[warning.shift_id].append(warning.id)
        warning_count_by_store[warning.store_id] += 1
        warning_count_by_date[warning.warning_date] += 1

    shifts_by_employee_day: dict[tuple[int, date], list[ScheduleShift]] = defaultdict(list)
    open_by_store_day: dict[tuple[int, date], list[ScheduleShift]] = defaultdict(list)
    paid_minutes_by_employee: dict[int, int] = defaultdict(int)
    assigned_minutes = 0
    open_minutes = 0
    for shift in shifts:
        minutes = scheduled_paid_minutes(shift)
        if shift.employee_id is None:
            open_minutes += minutes
            open_by_store_day[(shift.store_id, shift.shift_date)].append(shift)
        else:
            assigned_minutes += minutes
            paid_minutes_by_employee[shift.employee_id] += minutes
            shifts_by_employee_day[(shift.employee_id, shift.shift_date)].append(shift)

    def shift_dict(shift: ScheduleShift) -> dict:
        shift_type = next((row for row in shift_types if row.id == shift.shift_type_id), None)
        return {
            'id': shift.id,
            'schedule_period_id': shift.schedule_period_id,
            'employee_id': shift.employee_id,
            'store_id': shift.store_id,
            'store_name': store_by_id[shift.store_id].name if shift.store_id in store_by_id else f'Store {shift.store_id}',
            'shift_date': shift.shift_date.isoformat(),
            'start_time': shift.start_time.isoformat(timespec='minutes'),
            'end_time': shift.end_time.isoformat(timespec='minutes'),
            'time_label': f'{_clock(shift.start_time)}–{_clock(shift.end_time)}',
            'unpaid_break_minutes': shift.unpaid_break_minutes,
            'paid_hours': _hours(scheduled_paid_minutes(shift)),
            'shift_type_id': shift.shift_type_id,
            'shift_type_name': shift_type.name if shift_type else None,
            'is_opener': shift.is_opener,
            'is_closer': shift.is_closer,
            'employee_note': shift.employee_note,
            'is_open': shift.employee_id is None,
            'warning_ids': warning_ids_by_shift.get(shift.id, []),
            'has_warning': bool(warning_ids_by_shift.get(shift.id)),
        }

    days = [
        {
            'date': (week_start + timedelta(days=index)).isoformat(),
            'iso': (week_start + timedelta(days=index)).isoformat(),
            'weekday': WEEKDAY_NAMES[index],
            'short_weekday': WEEKDAY_NAMES[index][:3],
            'date_label': (week_start + timedelta(days=index)).strftime('%b %-d'),
            'warning_count': warning_count_by_date[week_start + timedelta(days=index)],
        }
        for index in range(7)
    ]

    employees_out = []
    for employee, profile in included:
        preferred_days = sorted({row.day_of_week for row in windows if row.employee_id == employee.id and row.kind.value == 'PREFERRED'})
        preferred_ranges = [row for row in windows if row.employee_id == employee.id and row.kind.value == 'PREFERRED']
        day_cells = []
        for day in days:
            indicators = [
                {
                    'kind': row.kind.value,
                    'label': _indicator_label(row.kind.value),
                    'start_time': row.start_time.isoformat(timespec='minutes'),
                    'end_time': row.end_time.isoformat(timespec='minutes'),
                    'display': f'{_clock(row.start_time)}–{_clock(row.end_time)}',
                }
                for row in windows_by_employee_day.get((employee.id, scheduling_weekday(date.fromisoformat(day['date']))), [])
            ]
            indicators.extend(time_off_by_employee_day.get((employee.id, date.fromisoformat(day['date'])), []))
            day_cells.append({
                **day,
                'cell_id': f'schedule-cell-{employee.id}-{day["iso"]}',
                'indicators': indicators,
                'shifts': [shift_dict(row) for row in shifts_by_employee_day.get((employee.id, date.fromisoformat(day['date'])), [])],
            })
        home_store_name = store_by_id.get(profile.home_store_id).name if profile and profile.home_store_id in store_by_id else None
        employee_out = {
            'id': employee.id,
            'name': employee.full_name,
            'active': employee.active,
            'home_store_id': profile.home_store_id if profile else None,
            'home_store_name': home_store_name or 'Unassigned',
            'target_hours': float(profile.target_weekly_hours) if profile else None,
            'scheduled_hours': _hours(paid_minutes_by_employee[employee.id]),
            'preferred_days': [WEEKDAY_NAMES[index] for index in preferred_days],
            'preferred_days_summary': ', '.join(WEEKDAY_NAMES[index][:3] for index in preferred_days) or 'No preferred days set',
            'preferred_time_summary': (
                f'{_clock(min(row.start_time for row in preferred_ranges))}–{_clock(max(row.end_time for row in preferred_ranges))}'
                if preferred_ranges else 'No preferred time set'
            ),
            'preferred_store_ids': preferred_stores.get(employee.id, []),
            'warning_count': warning_count_by_employee[employee.id],
            'days': day_cells,
        }
        if permission_flags.get('scheduling.manage_preferences') and profile is not None:
            employee_out['scheduler_note'] = profile.scheduler_note
        employees_out.append(employee_out)

    group_order = {store.id: index for index, store in enumerate(stores)}
    employees_out.sort(key=lambda row: (
        group_order.get(row['home_store_id'], len(group_order)),
        row['home_store_name'], row['name'], row['id'],
    ))
    groups = []
    for store in stores:
        group_employees = [row for row in employees_out if row['home_store_id'] == store.id]
        groups.append({
            'store_id': store.id,
            'store_name': store.name,
            'warning_count': warning_count_by_store[store.id],
            'employees': group_employees,
            'open_days': [
                {
                    **day,
                    'cell_id': f'schedule-cell-open-{store.id}-{day["iso"]}',
                    'shifts': [shift_dict(row) for row in open_by_store_day.get((store.id, date.fromisoformat(day['date'])), [])],
                }
                for day in days
            ],
        })
    unassigned = [row for row in employees_out if row['home_store_id'] not in selected_set]
    if unassigned:
        groups.append({
            'store_id': None,
            'store_name': 'Unassigned employees',
            'warning_count': sum(row['warning_count'] for row in unassigned),
            'employees': unassigned,
            'open_days': [],
        })

    warning_out = []
    for warning in warnings:
        target = (
            f'shift-card-{warning.shift_id}' if warning.shift_id is not None
            else f'schedule-day-{warning.warning_date.isoformat()}'
        )
        warning_out.append({
            'id': warning.id,
            'type': warning.warning_type,
            'severity': warning.severity.value,
            'store_id': warning.store_id,
            'store_name': store_by_id[warning.store_id].name if warning.store_id in store_by_id else f'Store {warning.store_id}',
            'date': warning.warning_date.isoformat(),
            'date_label': warning.warning_date.strftime('%A, %b %-d'),
            'start_time': warning.start_time.isoformat(timespec='minutes') if warning.start_time else None,
            'end_time': warning.end_time.isoformat(timespec='minutes') if warning.end_time else None,
            'employee_id': warning.employee_id,
            'shift_id': warning.shift_id,
            'required_count': warning.required_count,
            'actual_count': warning.actual_count,
            'message': warning.message,
            'target_id': target,
        })

    mode = 'EMPTY'
    if period is not None:
        if period.status == SchedulePeriodStatus.PUBLISHED:
            mode = 'PUBLISHED'
        elif period.supersedes_schedule_period_id is not None:
            mode = 'REPLACEMENT_DRAFT'
        else:
            mode = 'DRAFT'
    editable = bool(period and period.status == SchedulePeriodStatus.DRAFT and permission_flags.get('scheduling.edit_draft_shifts'))
    summary = {
        'assigned_hours': _hours(assigned_minutes),
        'open_hours': _hours(open_minutes),
        'total_hours': _hours(assigned_minutes + open_minutes),
        'unique_employee_count': len({row.employee_id for row in shifts if row.employee_id is not None}),
        'open_shift_count': sum(1 for row in shifts if row.employee_id is None),
        'coverage_warning_count': sum(1 for row in warnings if row.warning_type in COVERAGE_WARNING_TYPES),
        'info_warning_count': sum(1 for row in warnings if row.severity == ScheduleWarningSeverity.INFO),
        'conflict_count': sum(1 for row in warnings if row.severity == ScheduleWarningSeverity.CONFLICT),
        'serious_warning_count': sum(1 for row in warnings if row.severity == ScheduleWarningSeverity.SERIOUS),
    }
    labor = None
    if period is not None and permission_flags.get('scheduling.view_labor_cost'):
        estimate = estimate_labor_cost(
            db,
            schedule_period_id=period.id,
            permitted=True,
            allowed_store_ids=selected_store_ids,
        )
        labor = {
            'estimated_cost': float(estimate.estimated_cost),
            'costed_paid_hours': float(estimate.costed_paid_hours),
            'missing_rate_paid_hours': float(estimate.missing_rate_paid_hours),
            'missing_rate_shift_count': estimate.missing_rate_shift_count,
        }

    publisher = db.get(PrincipalModel, period.published_by_principal_id) if period and period.published_by_principal_id else None
    return {
        'week': {
            'start': week_start.isoformat(),
            'end': week_end.isoformat(),
            'range_label': f'{week_start.strftime("%b %-d")} – {week_end.strftime("%b %-d, %Y")}',
            'previous_start': (week_start - timedelta(days=7)).isoformat(),
            'next_start': (week_start + timedelta(days=7)).isoformat(),
            'days': days,
        },
        'mode': mode,
        'mode_label': mode.replace('_', ' ').title(),
        'period': None if period is None else {
            'id': period.id,
            'status': period.status.value,
            'revision_number': period.revision_number,
            'version': period.version,
            'supersedes_schedule_period_id': period.supersedes_schedule_period_id,
            'published_at': period.published_at.isoformat() if period.published_at else None,
            'publisher': publisher.username if publisher else None,
        },
        'current_published_period_id': current_published.id if current_published else None,
        'historical_revision_count': len(history),
        'editable': editable,
        'actions': {
            'create_draft': period is None and permission_flags.get('scheduling.create_draft', False),
            'clone_published': bool(period and period.status == SchedulePeriodStatus.PUBLISHED and permission_flags.get('scheduling.modify_published')),
            'edit_shifts': editable,
            'delete_shifts': bool(editable and permission_flags.get('scheduling.delete_draft_shifts')),
            'override_hard_unavailability': permission_flags.get('scheduling.override_hard_unavailability', False),
            'view_labor_cost': permission_flags.get('scheduling.view_labor_cost', False),
        },
        'stores': [{'id': row.id, 'name': row.name} for row in stores],
        'shift_types': [{'id': row.id, 'name': row.name} for row in shift_types],
        'employees': employees_out,
        'groups': groups,
        'shifts': [shift_dict(row) for row in shifts],
        'warnings': warning_out,
        'summary': summary,
        'labor': labor,
    }
