from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    AttendancePointReason, CoverageRequirement, Employee,
    EmployeeSchedulingProfile, EmployeeSchedulingStorePreference,
    EmployeeSchedulingWindow, SchedulePeriod, SchedulePeriodStatus,
    SchedulingOrganizationPolicy, SchedulingStoreDefaults, SchedulingWindowKind,
    SpecialStoreParticipation, SpecialStorePolicy, SpecialStoreRotationState,
    Store, StorePreferenceLevel, TimeOffRequest, TimeOffRequestStatus,
)
from app.services.v2_scheduling_pattern_service import alternating_week_for_date
from app.services.v2_scheduling_roster_service import is_scheduling_candidate


@dataclass(frozen=True)
class ReadinessItem:
    code: str
    severity: str
    title: str
    message: str
    action_href: str | None = None


@dataclass(frozen=True)
class HorizonWeek:
    week_start: date
    week_end: date
    alternating_week: str
    period_id: int | None
    status: str


@dataclass(frozen=True)
class SchedulingReadiness:
    blocking: tuple[ReadinessItem, ...]
    warnings: tuple[ReadinessItem, ...]
    info: tuple[ReadinessItem, ...]
    horizon_weeks: tuple[HorizonWeek, ...]
    materialized_week_count: int
    desired_week_count: int
    missing_week_count: int
    pending_time_off_count: int

    @property
    def can_generate(self) -> bool:
        return not self.blocking

    @property
    def horizon_complete(self) -> bool:
        return self.missing_week_count == 0


def _sunday(value: date) -> date:
    return value - timedelta(days=(value.weekday() + 1) % 7)


def _item(code: str, severity: str, title: str, message: str, href: str | None = None) -> ReadinessItem:
    return ReadinessItem(code, severity, title, message, href)


def scheduling_readiness(db: Session, *, today: date) -> SchedulingReadiness:
    blocking: list[ReadinessItem] = []
    warnings: list[ReadinessItem] = []
    info: list[ReadinessItem] = []
    defaults = db.get(SchedulingStoreDefaults, 1)
    if defaults is None or defaults.standard_shift_start is None or defaults.standard_shift_end is None:
        blocking.append(_item(
            'STANDARD_SHIFT_MISSING', 'BLOCKING', 'Standard Shift is not configured',
            'Set the business-local Standard Shift start and end before generation.',
            '/v2/scheduling/store-defaults'))

    active_coverage = list(db.execute(select(CoverageRequirement).where(
        CoverageRequirement.active.is_(True),
        CoverageRequirement.minimum_employee_count > 0)).scalars())
    coverage_store_ids = {row.store_id for row in active_coverage}
    active_employees = list(db.execute(select(Employee).where(
        Employee.active.is_(True), Employee.scheduling_active.is_(True)).order_by(
        Employee.full_name, Employee.id)).scalars())
    candidates = [row for row in active_employees if is_scheduling_candidate(row)]
    profiles = {row.employee_id: row for row in db.execute(select(
        EmployeeSchedulingProfile).where(EmployeeSchedulingProfile.employee_id.in_(
        [employee.id for employee in active_employees] or (-1,)))).scalars()}
    special_store_ids = set(db.execute(select(SpecialStorePolicy.store_id).where(
        SpecialStorePolicy.active.is_(True))).scalars())
    required_store_ids = set(coverage_store_ids) | special_store_ids
    required_store_ids |= {
        profile.home_store_id for profile in profiles.values() if profile.home_store_id is not None
    }
    if defaults and defaults.double_coverage_store_id:
        required_store_ids.add(defaults.double_coverage_store_id)
    active_store_ids = set(db.execute(select(Store.id).where(Store.active.is_(True))).scalars())
    required_store_ids &= active_store_ids
    if not active_coverage:
        blocking.append(_item(
            'COVERAGE_MISSING', 'BLOCKING', 'Coverage requirements are not configured',
            'Add at least one active store/day coverage requirement before generation.',
            '/v2/scheduling/store-defaults#coverage-requirements'))
    else:
        missing_stores = required_store_ids - coverage_store_ids
        if missing_stores:
            names = list(db.execute(select(Store.name).where(Store.id.in_(missing_stores)).order_by(
                Store.name)).scalars())
            blocking.append(_item(
                'STORE_COVERAGE_MISSING', 'BLOCKING', 'A required store has no coverage requirement',
                f'Configure active coverage for: {", ".join(names)}.',
                '/v2/scheduling/store-defaults#coverage-requirements'))
    if not candidates:
        blocking.append(_item(
            'NO_SCHEDULING_EMPLOYEES', 'BLOCKING', 'No eligible Scheduling employees',
            'Add or activate Scheduling employees before generating.',
            '/v2/scheduling/employees'))

    for employee in active_employees:
        profile = profiles.get(employee.id)
        if profile is None or profile.target_shifts_per_week is None:
            warnings.append(_item(
                f'TARGET_SHIFTS_MISSING:{employee.id}', 'WARNING',
                f'{employee.full_name}: Target Shifts not configured',
                'Generation can continue using the established default, but the target needs review.',
                f'/v2/scheduling/employees/{employee.id}'))
        if profile is None or profile.week_a_workdays_mask is None or profile.week_b_workdays_mask is None:
            warnings.append(_item(
                f'BASE_PATTERN_MISSING:{employee.id}', 'WARNING',
                f'{employee.full_name}: Week A / Week B base pattern not configured',
                'The scheduler can use constraints and coverage, but baseline consistency is reduced.',
                f'/v2/scheduling/employees/{employee.id}'))

    lead_candidates = [row for row in candidates if row.scheduling_lead_capable]
    coverage_weekdays = {row.day_of_week for row in active_coverage}
    lockouts = list(db.execute(select(EmployeeSchedulingWindow).where(
        EmployeeSchedulingWindow.employee_id.in_([row.id for row in lead_candidates] or (-1,)),
        EmployeeSchedulingWindow.kind == SchedulingWindowKind.HARD_UNAVAILABLE,
        EmployeeSchedulingWindow.active.is_(True))).scalars())
    lead_lockouts = {(row.employee_id, row.day_of_week) for row in lockouts}
    impossible_lead_days = [weekday for weekday in coverage_weekdays if not any(
        (lead.id, weekday) not in lead_lockouts for lead in lead_candidates)]
    if active_coverage and (not lead_candidates or impossible_lead_days):
        blocking.append(_item(
            'LEAD_AVAILABILITY_MISSING', 'BLOCKING', 'Lead coverage is not plausibly available',
            ('No eligible Lead-capable employee exists.' if not lead_candidates else
             'Every Lead-capable employee is hard-unavailable on required coverage day(s): '
             + ', '.join(str(day) for day in impossible_lead_days) + '.'),
            '/v2/scheduling/employees'))

    preferences = list(db.execute(select(EmployeeSchedulingStorePreference).where(
        EmployeeSchedulingStorePreference.employee_id.in_([row.id for row in candidates] or (-1,)),
        EmployeeSchedulingStorePreference.active.is_(True))).scalars())
    never = {(row.employee_id, row.store_id) for row in preferences
             if row.preference_level == StorePreferenceLevel.NEVER}
    for employee in candidates:
        if required_store_ids and all((employee.id, store_id) in never for store_id in required_store_ids):
            blocking.append(_item(
                f'NO_ELIGIBLE_STORE:{employee.id}', 'BLOCKING',
                f'{employee.full_name}: no eligible required store',
                'Every required Scheduling store is set to Never for this employee.',
                f'/v2/scheduling/employees/{employee.id}'))
    if defaults and defaults.double_coverage_store_id and not any(
        employee.scheduling_double_coverage
        and (employee.id, defaults.double_coverage_store_id) not in never
        for employee in candidates
    ):
        blocking.append(_item(
            'DOUBLE_COVERAGE_POOL_EMPTY', 'BLOCKING',
            'Double Coverage has no eligible employee',
            'Mark at least one eligible Scheduling employee as Double Coverage capable, or remove the Double Coverage store.',
            '/v2/scheduling/employees'))

    for store_id in special_store_ids:
        states = list(db.execute(select(SpecialStoreRotationState).where(
            SpecialStoreRotationState.store_id == store_id,
            SpecialStoreRotationState.participation.in_((
                SpecialStoreParticipation.PRIMARY, SpecialStoreParticipation.ROTATION)),
        )).scalars())
        eligible_states = [row for row in states if (row.employee_id, store_id) not in never]
        store = db.get(Store, store_id)
        if not eligible_states:
            blocking.append(_item(
                f'LONGVIEW_POOL_EMPTY:{store_id}', 'BLOCKING',
                f'{store.name if store else "Longview"}: participant pool is unusable',
                'Configure a Primary or Rotation participant who is not restricted from this store.',
                '/v2/scheduling/employees'))
        elif len(eligible_states) == 1:
            warnings.append(_item(
                f'LONGVIEW_POOL_SMALL:{store_id}', 'WARNING',
                f'{store.name if store else "Longview"}: participant pool has one employee',
                'Generation can proceed, but date-specific restrictions may leave no legal repair.',
                '/v2/scheduling/employees'))

    if db.execute(select(func.count(AttendancePointReason.id)).where(
        AttendancePointReason.active.is_(True))).scalar_one() == 0:
        warnings.append(_item(
            'ATTENDANCE_POINTS_NOT_CONFIGURED', 'WARNING',
            'Attendance Point Policy is not configured',
            'This does not block schedule generation. Admin can configure reasons separately.',
            '/v2/scheduling/store-defaults#attendance-point-policy'))
    pending_pto = db.execute(select(func.count(TimeOffRequest.id)).where(
        TimeOffRequest.status == TimeOffRequestStatus.PENDING)).scalar_one()
    if pending_pto:
        warnings.append(_item(
            'PENDING_TIME_OFF', 'WARNING', f'{pending_pto} pending time-off request(s)',
            'Pending requests are management work and are not treated as approved exclusions.',
            '/v2/scheduling/time-off?status=PENDING'))

    policy = db.execute(select(SchedulingOrganizationPolicy).order_by(
        SchedulingOrganizationPolicy.id).limit(1)).scalar_one_or_none()
    desired = policy.schedule_length_weeks if policy else 8
    current_week = _sunday(today)
    periods = list(db.execute(select(SchedulePeriod).where(
        SchedulePeriod.week_start_date >= current_week,
        SchedulePeriod.status.in_((SchedulePeriodStatus.DRAFT, SchedulePeriodStatus.PUBLISHED)),
    ).order_by(SchedulePeriod.week_start_date, SchedulePeriod.revision_number.desc())).scalars())
    by_start: dict[date, SchedulePeriod] = {}
    for period in periods:
        existing = by_start.get(period.week_start_date)
        if existing is None or (
            existing.status == SchedulePeriodStatus.PUBLISHED
            and period.status == SchedulePeriodStatus.DRAFT
        ):
            by_start[period.week_start_date] = period
    horizon_start = current_week if current_week in by_start or not by_start else min(by_start)
    horizon: list[HorizonWeek] = []
    for offset in range(desired):
        start = horizon_start + timedelta(weeks=offset)
        period = by_start.get(start)
        horizon.append(HorizonWeek(
            week_start=start, week_end=start + timedelta(days=6),
            alternating_week=(period.alternating_week if period and period.alternating_week
                              else alternating_week_for_date(start)),
            period_id=period.id if period else None,
            status=period.status.value if period else 'MISSING'))
    materialized = sum(row.period_id is not None for row in horizon)
    missing = desired - materialized
    info.append(_item(
        'HORIZON_STATUS', 'INFO',
        'Rolling horizon is current' if missing == 0 else f'{missing} horizon week(s) are missing',
        f'{materialized} of {desired} configured weeks are materialized.'))
    return SchedulingReadiness(
        blocking=tuple(blocking), warnings=tuple(warnings), info=tuple(info),
        horizon_weeks=tuple(horizon), materialized_week_count=materialized,
        desired_week_count=desired, missing_week_count=missing,
        pending_time_off_count=int(pending_pto),
    )


def require_generation_readiness(db: Session, *, today: date) -> SchedulingReadiness:
    readiness = scheduling_readiness(db, today=today)
    if readiness.blocking:
        from app.services.v2_scheduling_service import SchedulingValidationError
        details = '; '.join(f'{item.title}: {item.message}' for item in readiness.blocking)
        raise SchedulingValidationError(f'Schedule generation is blocked. {details}')
    return readiness
