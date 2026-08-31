from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from app.auth import Principal
from app.models import (
    AttendanceEventType, CoverageRequirement, Employee, EmployeeSchedulingProfile,
    EmployeeSchedulingStorePreference,
    EmployeeSchedulingWindow, Principal as PrincipalModel, PrincipalRole, ScheduleLifecycleStage,
    ScheduleAttendanceEvent, SchedulePeriod, SchedulePeriodStatus, ScheduleShift,
    ScheduleWarning, ScheduleWarningSeverity,
    SchedulingNotification, SchedulingStoreDefaults,
    SchedulingOrganizationPolicy, SchedulingWindowKind, ShiftTransferRequest,
    ShiftTransferStatus, SpecialStoreParticipation, SpecialStorePolicy,
    SpecialStoreRotationState, StorePreferenceLevel, StoreShift, TimeOffRequest,
    TimeOffRequestStatus,
)
from app.services.v2_scheduling_service import SchedulingConflict, SchedulingValidationError, scheduled_paid_minutes
from app.services.v2_scheduling_roster_service import (
    is_scheduling_candidate,
    list_scheduling_candidates,
    square_allows_scheduling,
)
from app.services.v2_scheduling_pattern_service import (
    ALTERNATING_WEEK_A_ANCHOR, alternating_week_for_date, base_pattern_mask,
    is_base_workday, scheduling_weekday,
)
from app.v2.audit import V2AuditEvent, write_v2_audit_event


@dataclass(frozen=True)
class ConstraintReason:
    code: str
    message: str
    hard: bool = True


@dataclass(frozen=True)
class EligibilityResult:
    eligible: bool
    reasons: tuple[ConstraintReason, ...]
    scheduled_hours: Decimal
    resulting_hours: Decimal
    approval_threshold_hours: Decimal
    requires_hour_approval: bool


@dataclass(frozen=True)
class SimulatedAssignment:
    shift_date: date
    start_time: time
    end_time: time
    unpaid_break_minutes: int = 0


@dataclass(frozen=True)
class AutomationWindow:
    next_start: date
    next_end: date
    generate_at: datetime
    publish_at: datetime


@dataclass(frozen=True)
class WeekendFairness:
    weekday: int
    historical_assignment_count: int
    last_historical_assignment_date: date | None
    planned_future_assignment_count: int

    @property
    def assignment_count(self) -> int:
        """Backward-compatible total burden used by existing callers."""
        return self.historical_assignment_count + self.planned_future_assignment_count

    @property
    def last_assignment_date(self) -> date | None:
        return self.last_historical_assignment_date


@dataclass(frozen=True)
class LongviewRotationFairness:
    store_id: int
    historical_assignment_count: int
    last_historical_assignment_date: date | None
    planned_future_assignment_count: int
    scheduled_historical_assignment_count: int = 0
    callout_count: int = 0
    no_call_no_show_count: int = 0
    covered_for_others_count: int = 0
    ambiguous_outcome_count: int = 0
    credit_details: tuple[dict, ...] = ()


SCHEDULE_AUTOMATION_LOCK_KEY = 731_202_608_24
ACTIONABLE_WARNING_TYPES = frozenset({
    'NO_ASSIGNED_EMPLOYEE', 'INSUFFICIENT_COVERAGE', 'NO_LEAD_OF_DAY',
    'DOUBLE_COVERAGE_UNFILLED', 'DOUBLE_COVERAGE_STORE_MISSING',
})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sunday(day: date) -> date:
    return day - timedelta(days=(day.weekday() + 1) % 7)


def _overlaps(start_a: time, end_a: time, start_b: time, end_b: time) -> bool:
    return start_a < end_b and end_a > start_b


def _audit(db: Session, principal: Principal, action: str, entity_type: str, entity_id: int, metadata: dict) -> None:
    write_v2_audit_event(db, event=V2AuditEvent(
        actor_principal_id=principal.id, action=action, domain='SCHEDULING', entity_type=entity_type,
        entity_id=entity_id, timestamp=_now(), correlation_id=str(uuid.uuid4()), metadata=metadata,
    ), ip=None)


def organization_policy(db: Session, *, principal_id: int | None = None) -> SchedulingOrganizationPolicy:
    row = db.execute(select(SchedulingOrganizationPolicy).order_by(
        SchedulingOrganizationPolicy.id).limit(1)).scalar_one_or_none()
    if row is None:
        if principal_id is None:
            # Unsaved default is safe for read-only validation and never invents an actor.
            return SchedulingOrganizationPolicy(
                weekly_approval_hours=Decimal('40'), schedule_length_weeks=8,
                generate_days_before_end=7, publish_days_before_end=3,
                publication_local_time=time(9), timezone_name='America/Los_Angeles',
                active=True, updated_by_principal_id=0,
            )
        row = SchedulingOrganizationPolicy(updated_by_principal_id=principal_id)
        db.add(row)
        db.flush()
    return row


def update_organization_policy(
    db: Session, *, principal: Principal, weekly_approval_hours: Decimal,
    schedule_length_weeks: int, generate_days_before_end: int, publish_days_before_end: int,
    publication_local_time: time, timezone_name: str, active: bool = True,
) -> SchedulingOrganizationPolicy:
    if weekly_approval_hours <= 0 or not 1 <= schedule_length_weeks <= 8:
        raise SchedulingValidationError(
            'Approval hours must be positive and the rolling horizon must be between one and eight weeks.')
    if min(generate_days_before_end, publish_days_before_end) < 0:
        raise SchedulingValidationError('Automation offsets cannot be negative.')
    try:
        ZoneInfo(timezone_name)
    except Exception as exc:
        raise SchedulingValidationError('Choose a valid IANA timezone.') from exc
    row = organization_policy(db, principal_id=principal.id)
    row.weekly_approval_hours = weekly_approval_hours
    row.schedule_length_weeks = schedule_length_weeks
    row.generate_days_before_end = generate_days_before_end
    row.publish_days_before_end = publish_days_before_end
    row.publication_local_time = publication_local_time
    row.timezone_name = timezone_name
    row.active = active
    row.updated_by_principal_id = principal.id; row.updated_at = _now()
    db.flush()
    _audit(db, principal, 'SCHEDULE_AUTOMATION_POLICY_CHANGED', 'scheduling_organization_policy', row.id,
           {'weekly_approval_hours': str(weekly_approval_hours), 'schedule_length_weeks': schedule_length_weeks,
            'generate_days_before_end': generate_days_before_end,
            'publish_days_before_end': publish_days_before_end,
            'publication_local_time': publication_local_time.isoformat(), 'timezone_name': timezone_name,
            'active': active})
    return row


def configure_special_store(
    db: Session, *, principal: Principal, store_id: int,
    primary_employee_ids: tuple[int, ...], rotation_employee_ids: tuple[int, ...], active: bool = True,
) -> SpecialStorePolicy:
    if set(primary_employee_ids) & set(rotation_employee_ids):
        raise SchedulingValidationError('An employee cannot be both primary and rotation staff for one special store.')
    policy = db.execute(select(SpecialStorePolicy).where(
        SpecialStorePolicy.store_id == store_id).with_for_update()).scalar_one_or_none()
    if policy is None:
        policy = SpecialStorePolicy(store_id=store_id, active=active,
            created_by_principal_id=principal.id, updated_by_principal_id=principal.id)
        db.add(policy)
    policy.active = active; policy.updated_by_principal_id = principal.id; policy.updated_at = _now()
    all_ids = primary_employee_ids + rotation_employee_ids
    profiles = {row.employee_id: row for row in db.execute(select(EmployeeSchedulingProfile).where(
        EmployeeSchedulingProfile.employee_id.in_(all_ids or (-1,))).with_for_update()).scalars()}
    for employee_id in all_ids:
        employee = db.get(Employee, employee_id)
        if employee is None or not employee.active:
            raise SchedulingValidationError(f'Employee {employee_id} is not active.')
        profile = profiles.get(employee_id)
        if profile is None:
            profile = EmployeeSchedulingProfile(employee_id=employee_id, target_weekly_hours=Decimal('0'),
                created_by_principal_id=principal.id, updated_by_principal_id=principal.id)
            db.add(profile); db.flush(); profiles[employee_id] = profile
        profile.special_store_participation = (
            SpecialStoreParticipation.PRIMARY if employee_id in primary_employee_ids
            else SpecialStoreParticipation.ROTATION)
        profile.updated_by_principal_id = principal.id; profile.updated_at = _now()
    states = {row.employee_id: row for row in db.execute(select(SpecialStoreRotationState).where(
        SpecialStoreRotationState.store_id == store_id).with_for_update()).scalars()}
    next_position = max((row.queue_position for row in states.values()), default=0) + 1
    for employee_id in all_ids:
        participation = (SpecialStoreParticipation.PRIMARY if employee_id in primary_employee_ids
                         else SpecialStoreParticipation.ROTATION)
        if employee_id not in states:
            db.add(SpecialStoreRotationState(store_id=store_id, employee_id=employee_id,
                                             participation=participation, queue_position=next_position))
            next_position += 1
        else:
            states[employee_id].participation = participation
    db.flush()
    _audit(db, principal, 'SPECIAL_STORE_POLICY_CHANGED', 'special_store_policy', policy.id,
           {'store_id': store_id, 'primary_employee_ids': list(primary_employee_ids),
            'rotation_employee_ids': list(rotation_employee_ids), 'active': active})
    return policy


def set_special_store_employee_participation(
    db: Session, *, principal: Principal, store_id: int, employee_id: int,
    participation: SpecialStoreParticipation,
) -> SpecialStoreRotationState:
    policy = db.execute(select(SpecialStorePolicy).where(
        SpecialStorePolicy.store_id == store_id, SpecialStorePolicy.active.is_(True))).scalar_one_or_none()
    if policy is None:
        raise SchedulingValidationError('Special-store policy is not configured for this store.')
    row = db.execute(select(SpecialStoreRotationState).where(
        SpecialStoreRotationState.store_id == store_id,
        SpecialStoreRotationState.employee_id == employee_id).with_for_update()).scalar_one_or_none()
    if row is None:
        position = (db.execute(select(func.max(SpecialStoreRotationState.queue_position)).where(
            SpecialStoreRotationState.store_id == store_id)).scalar_one() or 0) + 1
        row = SpecialStoreRotationState(store_id=store_id, employee_id=employee_id,
                                        participation=participation, queue_position=position)
        db.add(row)
    else:
        row.participation = participation
    profile = db.execute(select(EmployeeSchedulingProfile).where(
        EmployeeSchedulingProfile.employee_id == employee_id).with_for_update()).scalar_one_or_none()
    if profile is not None:
        profile.special_store_participation = participation
        profile.updated_by_principal_id = principal.id; profile.updated_at = _now()
    db.flush()
    _audit(db, principal, 'SPECIAL_STORE_PARTICIPATION_CHANGED', 'special_store_rotation_state', row.id,
           {'store_id': store_id, 'employee_id': employee_id, 'participation': participation.value})
    return row


def scheduled_weekly_hours(
    db: Session, *, employee_id: int, shift_date: date, exclude_shift_id: int | None = None,
) -> Decimal:
    week_start = _sunday(shift_date)
    statement = select(ScheduleShift, SchedulePeriod).join(SchedulePeriod).where(
        ScheduleShift.employee_id == employee_id,
        ScheduleShift.shift_date.between(week_start, week_start + timedelta(days=6)),
        SchedulePeriod.status.in_((SchedulePeriodStatus.DRAFT, SchedulePeriodStatus.PUBLISHED)),
    )
    if exclude_shift_id is not None:
        statement = statement.where(ScheduleShift.id != exclude_shift_id)
    rows = db.execute(statement).all()
    # A replacement draft and its published predecessor describe the same week. Employee-visible
    # scheduled hours remain authoritative from the published revision until replacement publishes.
    published_exists = any(period.status == SchedulePeriodStatus.PUBLISHED for _shift, period in rows)
    minutes = sum(scheduled_paid_minutes(shift) for shift, period in rows
                  if not published_exists or period.status == SchedulePeriodStatus.PUBLISHED)
    return (Decimal(minutes) / Decimal(60)).quantize(Decimal('0.01'))


def scheduled_weekly_shift_count(
    db: Session, *, employee_id: int, shift_date: date, exclude_shift_id: int | None = None,
) -> int:
    week_start = _sunday(shift_date)
    return len(_effective_assignment_rows(
        db, employee_id=employee_id, start_date=week_start,
        end_date=week_start + timedelta(days=6), exclude_shift_id=exclude_shift_id))


def _effective_assignment_rows_with_period(
    db: Session, *, employee_id: int, start_date: date | None = None,
    end_date: date | None = None, exclude_shift_id: int | None = None,
) -> list[tuple[ScheduleShift, SchedulePeriod]]:
    statement = select(ScheduleShift, SchedulePeriod).join(SchedulePeriod).where(
        ScheduleShift.employee_id == employee_id,
        SchedulePeriod.status.in_((SchedulePeriodStatus.DRAFT, SchedulePeriodStatus.PUBLISHED)),
    )
    if start_date is not None:
        statement = statement.where(ScheduleShift.shift_date >= start_date)
    if end_date is not None:
        statement = statement.where(ScheduleShift.shift_date <= end_date)
    if exclude_shift_id is not None:
        statement = statement.where(ScheduleShift.id != exclude_shift_id)
    rows = db.execute(statement).all()
    period_by_week: dict[date, SchedulePeriod] = {}
    for _shift, period in rows:
        selected = period_by_week.get(period.week_start_date)
        if selected is None or (
            selected.status != SchedulePeriodStatus.PUBLISHED
            and (period.status == SchedulePeriodStatus.PUBLISHED or period.revision_number > selected.revision_number)
        ):
            period_by_week[period.week_start_date] = period
    return [(shift, period) for shift, period in rows
            if period_by_week.get(period.week_start_date) is period]


def _effective_assignment_rows(
    db: Session, *, employee_id: int, start_date: date | None = None,
    end_date: date | None = None, exclude_shift_id: int | None = None,
) -> list[ScheduleShift]:
    return [shift for shift, _period in _effective_assignment_rows_with_period(
        db, employee_id=employee_id, start_date=start_date,
        end_date=end_date, exclude_shift_id=exclude_shift_id)]


def effective_assignment_rows(
    db: Session, *, employee_id: int, start_date: date | None = None,
    end_date: date | None = None, exclude_shift_id: int | None = None,
) -> list[ScheduleShift]:
    """Return the authoritative effective draft/published assignment set."""
    return _effective_assignment_rows(
        db, employee_id=employee_id, start_date=start_date,
        end_date=end_date, exclude_shift_id=exclude_shift_id)


def _work_dates(db: Session, employee_id: int, exclude_shift_id: int | None) -> set[date]:
    return {row.shift_date for row in _effective_assignment_rows(
        db, employee_id=employee_id, exclude_shift_id=exclude_shift_id)}


def consecutive_policy_reasons(
    *, work_dates: set[date], proposed_date: date, max_consecutive_work_days: int,
    minimum_days_off_after_max_block: int,
) -> tuple[ConstraintReason, ...]:
    dates = set(work_dates); dates.add(proposed_date)
    left = proposed_date
    while left - timedelta(days=1) in dates:
        left -= timedelta(days=1)
    right = proposed_date
    while right + timedelta(days=1) in dates:
        right += timedelta(days=1)
    block_length = (right - left).days + 1
    reasons: list[ConstraintReason] = []
    if block_length > max_consecutive_work_days:
        reasons.append(ConstraintReason(
            'MAX_CONSECUTIVE_DAYS',
            f'Assignment would create {block_length} consecutive workdays; maximum is {max_consecutive_work_days}.',
        ))
        return tuple(reasons)
    required_off = minimum_days_off_after_max_block
    if not required_off:
        return ()

    previous_dates = [day for day in dates if day < left]
    if previous_dates:
        previous_end = max(previous_dates)
        previous_start = previous_end
        while previous_start - timedelta(days=1) in dates:
            previous_start -= timedelta(days=1)
        previous_length = (previous_end - previous_start).days + 1
        gap = (left - previous_end).days - 1
        if previous_length >= max_consecutive_work_days and gap < required_off:
            reasons.append(ConstraintReason(
                'REQUIRED_DAYS_OFF',
                f'Assignment leaves only {gap} day(s) off after a maximum work block; {required_off} required.',
            ))
    next_dates = [day for day in dates if day > right]
    if block_length >= max_consecutive_work_days and next_dates:
        next_start = min(next_dates)
        gap = (next_start - right).days - 1
        if gap < required_off:
            reasons.append(ConstraintReason(
                'REQUIRED_DAYS_OFF',
                f'Assignment leaves only {gap} day(s) off before the next work block; {required_off} required.',
            ))
    return tuple(reasons)


def evaluate_assignment(
    db: Session, *, employee_id: int, store_id: int, shift_date: date, start_time: time,
    end_time: time, unpaid_break_minutes: int = 0, exclude_shift_id: int | None = None,
    enforce_hour_limit: bool = True,
    simulated_assignments: tuple[SimulatedAssignment, ...] = (),
) -> EligibilityResult:
    reasons: list[ConstraintReason] = []
    employee = db.get(Employee, employee_id)
    if employee is None or not employee.active:
        reasons.append(ConstraintReason('INACTIVE_EMPLOYEE', 'Employee is inactive or missing.'))
    elif not employee.scheduling_active:
        reasons.append(ConstraintReason(
            'SCHEDULING_INACTIVE', 'Employee is inactive for Scheduling.'))
    elif not square_allows_scheduling(employee):
        reasons.append(ConstraintReason(
            'SQUARE_INACTIVE', 'Square reports this Team Member as inactive.'))
    profile = db.execute(select(EmployeeSchedulingProfile).where(
        EmployeeSchedulingProfile.employee_id == employee_id,
        EmployeeSchedulingProfile.active.is_(True))).scalar_one_or_none()
    weekday = (shift_date.weekday() + 1) % 7
    hard_window = db.execute(select(EmployeeSchedulingWindow.id).where(
        EmployeeSchedulingWindow.employee_id == employee_id,
        EmployeeSchedulingWindow.day_of_week == weekday,
        EmployeeSchedulingWindow.kind == SchedulingWindowKind.HARD_UNAVAILABLE,
        EmployeeSchedulingWindow.active.is_(True),
        EmployeeSchedulingWindow.start_time < end_time,
        EmployeeSchedulingWindow.end_time > start_time,
    ).limit(1)).scalar_one_or_none()
    if hard_window is not None:
        reasons.append(ConstraintReason('HARD_WEEKDAY_LOCKOUT', 'Recurring hard availability blocks this shift.'))
    pto = db.execute(select(TimeOffRequest).where(
        TimeOffRequest.employee_id == employee_id, TimeOffRequest.status == TimeOffRequestStatus.APPROVED,
        TimeOffRequest.start_date <= shift_date, TimeOffRequest.end_date >= shift_date,
        or_(TimeOffRequest.full_day.is_(True),
            (TimeOffRequest.start_time < end_time) & (TimeOffRequest.end_time > start_time)),
    ).limit(1)).scalar_one_or_none()
    if pto is not None:
        reasons.append(ConstraintReason('APPROVED_TIME_OFF', 'Approved time off overlaps this shift.'))
    overlap_stmt = select(ScheduleShift.id).join(SchedulePeriod).where(
        ScheduleShift.employee_id == employee_id, ScheduleShift.shift_date == shift_date,
        ScheduleShift.start_time < end_time, ScheduleShift.end_time > start_time,
        SchedulePeriod.status.in_((SchedulePeriodStatus.DRAFT, SchedulePeriodStatus.PUBLISHED)),
    )
    if exclude_shift_id is not None:
        overlap_stmt = overlap_stmt.where(ScheduleShift.id != exclude_shift_id)
    if db.execute(overlap_stmt.limit(1)).scalar_one_or_none() is not None:
        reasons.append(ConstraintReason('OVERLAPPING_SHIFT', 'Employee already has an overlapping assignment.'))
    if any(
        row.shift_date == shift_date
        and row.start_time < end_time and row.end_time > start_time
        for row in simulated_assignments
    ):
        reasons.append(ConstraintReason(
            'SIMULATED_OVERLAP', 'Employee already has an overlapping simulated repair.'))
    preference = db.execute(select(EmployeeSchedulingStorePreference).where(
        EmployeeSchedulingStorePreference.employee_id == employee_id,
        EmployeeSchedulingStorePreference.store_id == store_id,
        EmployeeSchedulingStorePreference.active.is_(True))).scalar_one_or_none()
    if preference is not None and preference.preference_level == StorePreferenceLevel.NEVER:
        reasons.append(ConstraintReason('STORE_NEVER', 'Employee is marked never schedule at this store.'))
    special_store = db.execute(select(SpecialStorePolicy.id).where(
        SpecialStorePolicy.store_id == store_id,
        SpecialStorePolicy.active.is_(True))).scalar_one_or_none()
    if special_store is not None:
        special_participation = db.execute(select(SpecialStoreRotationState.participation).where(
            SpecialStoreRotationState.store_id == store_id,
            SpecialStoreRotationState.employee_id == employee_id)).scalar_one_or_none()
        if special_participation not in (
            SpecialStoreParticipation.PRIMARY, SpecialStoreParticipation.ROTATION):
            reasons.append(ConstraintReason(
                'LONGVIEW_NOT_PARTICIPATING',
                'Employee is not approved for this Longview coverage pool.'))
    if profile is not None and profile.special_store_participation == SpecialStoreParticipation.PRIMARY:
        special_store_ids = set(db.execute(select(SpecialStorePolicy.store_id).where(SpecialStorePolicy.active.is_(True))).scalars())
        if special_store_ids and store_id not in special_store_ids:
            reasons.append(ConstraintReason('SPECIAL_STORE_PRIMARY_ONLY', 'Special-store-primary employee is excluded from the normal store rotation.'))
    if profile is not None and profile.max_consecutive_work_days:
        reasons.extend(consecutive_policy_reasons(
            work_dates=(
                _work_dates(db, employee_id, exclude_shift_id)
                | {row.shift_date for row in simulated_assignments}
            ), proposed_date=shift_date,
            max_consecutive_work_days=profile.max_consecutive_work_days,
            minimum_days_off_after_max_block=profile.minimum_days_off_after_max_block,
        ))
    existing = scheduled_weekly_hours(db, employee_id=employee_id, shift_date=shift_date, exclude_shift_id=exclude_shift_id)
    week_start = _sunday(shift_date)
    simulated_minutes = sum(
        max(0, (
            (row.end_time.hour * 60 + row.end_time.minute)
            - (row.start_time.hour * 60 + row.start_time.minute)
            - row.unpaid_break_minutes
        ))
        for row in simulated_assignments
        if week_start <= row.shift_date <= week_start + timedelta(days=6)
    )
    existing += (Decimal(simulated_minutes) / Decimal(60)).quantize(Decimal('0.01'))
    shift_hours = (Decimal(max(0, ((end_time.hour * 60 + end_time.minute) - (start_time.hour * 60 + start_time.minute) - unpaid_break_minutes))) / Decimal(60)).quantize(Decimal('0.01'))
    resulting = existing + shift_hours
    org = organization_policy(db)
    threshold = profile.approval_weekly_hours if profile is not None and profile.approval_weekly_hours is not None else org.weekly_approval_hours
    hard_max = profile.maximum_weekly_hours if profile is not None else None
    if enforce_hour_limit and hard_max is not None and resulting > hard_max:
        reasons.append(ConstraintReason('MAX_WEEKLY_HOURS', f'Assignment would result in {resulting} scheduled hours; maximum is {hard_max}.'))
    return EligibilityResult(not reasons, tuple(reasons), existing, resulting, threshold, resulting > threshold)


def assignment_score(db: Session, *, employee_id: int, store_id: int, shift_date: date) -> tuple[int, int]:
    profile = db.execute(select(EmployeeSchedulingProfile).where(EmployeeSchedulingProfile.employee_id == employee_id)).scalar_one_or_none()
    preference = db.execute(select(EmployeeSchedulingStorePreference).where(
        EmployeeSchedulingStorePreference.employee_id == employee_id,
        EmployeeSchedulingStorePreference.store_id == store_id,
        EmployeeSchedulingStorePreference.active.is_(True))).scalar_one_or_none()
    level_score = {StorePreferenceLevel.PREFERRED: 300, StorePreferenceLevel.ACCEPTABLE: 200,
                   StorePreferenceLevel.AVOID: 50, StorePreferenceLevel.NEVER: -10000}
    score = level_score.get(preference.preference_level, 150) if preference else 150
    if preference and preference.preference_rank:
        score -= preference.preference_rank
    current = scheduled_weekly_shift_count(db, employee_id=employee_id, shift_date=shift_date)
    target = profile.target_shifts_per_week if profile and profile.target_shifts_per_week is not None else 0
    return score, target - current


def _below_target_priority(assignment: tuple[int, int]) -> int:
    """Rank a configured, below-target workload ahead of at/above-target workloads."""
    return int(assignment[1] > 0)


def base_pattern_score(db: Session, *, employee_id: int, shift_date: date) -> int:
    profile = db.execute(select(EmployeeSchedulingProfile).where(
        EmployeeSchedulingProfile.employee_id == employee_id)).scalar_one_or_none()
    if profile is None:
        return 0
    expected = is_base_workday(profile, shift_date)
    return 0 if expected is None else (1 if expected else -1)


def weekend_fairness(
    db: Session, *, employee_id: int, weekday: int, before_date: date,
    as_of_date: date | None = None,
) -> WeekendFairness:
    if weekday not in (5, 6):
        raise ValueError('Weekend fairness is defined independently for Saturday (5) and Sunday (6).')
    special_store_ids = set(db.execute(select(SpecialStorePolicy.store_id).where(
        SpecialStorePolicy.active.is_(True))).scalars())
    # Without an explicit planning date, retain the established trailing-12-week
    # behavior for direct callers. Generation supplies an as-of date so actual
    # history and already-planned future burden remain independently visible.
    if as_of_date is None:
        dates = {row.shift_date for row in _effective_assignment_rows(
            db, employee_id=employee_id, start_date=before_date - timedelta(weeks=12),
            end_date=before_date - timedelta(days=1))
            if row.shift_date.weekday() == weekday and row.store_id not in special_store_ids}
        return WeekendFairness(
            weekday=weekday, historical_assignment_count=len(dates),
            last_historical_assignment_date=max(dates) if dates else None,
            planned_future_assignment_count=0)

    planning_date = min(as_of_date, before_date)
    history_start = planning_date - timedelta(weeks=12)
    rows = _effective_assignment_rows_with_period(
        db, employee_id=employee_id, start_date=history_start,
        end_date=before_date - timedelta(days=1))
    historical_dates = {
        row.shift_date for row, period in rows
        if (row.shift_date < planning_date
            and period.status == SchedulePeriodStatus.PUBLISHED
            and row.shift_date.weekday() == weekday
            and row.store_id not in special_store_ids)
    }
    planned_dates = {
        row.shift_date for row, _period in rows
        if (planning_date <= row.shift_date < before_date
            and row.shift_date.weekday() == weekday
            and row.store_id not in special_store_ids)
    }
    return WeekendFairness(
        weekday=weekday,
        historical_assignment_count=len(historical_dates),
        last_historical_assignment_date=max(historical_dates) if historical_dates else None,
        planned_future_assignment_count=len(planned_dates),
    )


def _longview_historical_credit_rows(
    db: Session, *, store_id: int, before_date: date,
) -> list[dict]:
    """Resolve one historical fact per published shift lineage.

    Attendance is activated per exact shift, not by a guessed global date. A lineage
    with no active attendance event retains legacy scheduled credit. If attendance
    exists on an archived source revision, that exact event-bearing shift wins over
    a later copied revision so correction history never attaches to the wrong row.
    """
    rows = list(db.execute(select(ScheduleShift, SchedulePeriod).join(SchedulePeriod).where(
        ScheduleShift.store_id == store_id,
        ScheduleShift.shift_date < before_date,
        or_(SchedulePeriod.status == SchedulePeriodStatus.PUBLISHED,
            SchedulePeriod.published_at.is_not(None)),
    )).all())
    if not rows:
        return []
    shift_by_id = {shift.id: shift for shift, _period in rows}
    period_by_shift_id = {shift.id: period for shift, period in rows}
    active_events = list(db.execute(select(ScheduleAttendanceEvent).where(
        ScheduleAttendanceEvent.schedule_shift_id.in_(tuple(shift_by_id)),
        ScheduleAttendanceEvent.voided_at.is_(None),
    ).order_by(ScheduleAttendanceEvent.created_at, ScheduleAttendanceEvent.id)).scalars())
    events_by_shift: dict[int, list[ScheduleAttendanceEvent]] = defaultdict(list)
    for event in active_events:
        events_by_shift[event.schedule_shift_id].append(event)

    def lineage_root(shift: ScheduleShift) -> int:
        current = shift
        seen: set[int] = set()
        while current.source_shift_id in shift_by_id and current.id not in seen:
            seen.add(current.id)
            current = shift_by_id[current.source_shift_id]
        return current.id

    lineages: dict[int, list[ScheduleShift]] = defaultdict(list)
    for shift, _period in rows:
        lineages[lineage_root(shift)].append(shift)

    facts: list[dict] = []
    for lineage in lineages.values():
        event_shifts = [shift for shift in lineage if events_by_shift.get(shift.id)]
        if event_shifts:
            shift = max(event_shifts, key=lambda row: (
                period_by_shift_id[row.id].revision_number, row.id))
        else:
            published = [shift for shift in lineage
                         if period_by_shift_id[shift.id].status == SchedulePeriodStatus.PUBLISHED]
            shift = max(published or lineage, key=lambda row: (
                period_by_shift_id[row.id].revision_number, row.id))
        events = events_by_shift.get(shift.id, [])
        types = {event.event_type for event in events}
        coverage_events = [event for event in events
                           if event.event_type == AttendanceEventType.COVERED_SHIFT]
        absence = types & {
            AttendanceEventType.CALLED_OUT, AttendanceEventType.NO_CALL_NO_SHOW}
        worked_evidence = types & {
            AttendanceEventType.WORKED_AS_SCHEDULED, AttendanceEventType.LATE,
            AttendanceEventType.OPENED_STORE_LATE}
        ambiguous = bool(
            (worked_evidence and (absence or coverage_events))
            or len(absence) > 1
            or len({event.replacement_employee_id for event in coverage_events}) > 1)
        scheduled_employee_id = (
            events[0].original_employee_id if events else shift.employee_id)
        credited_employee_ids = [
            event.replacement_employee_id for event in coverage_events
            if event.replacement_employee_id is not None]
        if len(set(credited_employee_ids)) > 1:
            # The write service prevents this state, but imported/corrupt data must
            # not turn one Longview shift into credit for multiple replacements.
            credited_employee_ids = []
        if not absence and not coverage_events:
            credited_employee_ids.append(scheduled_employee_id)
        if ambiguous:
            reason = 'AMBIGUOUS_ACTIVE_ATTENDANCE'
        elif AttendanceEventType.CALLED_OUT in types and coverage_events:
            reason = 'CALLED_OUT_COVERED_BY_OTHER'
        elif AttendanceEventType.NO_CALL_NO_SHOW in types and coverage_events:
            reason = 'NO_CALL_NO_SHOW_COVERED_BY_OTHER'
        elif AttendanceEventType.CALLED_OUT in types:
            reason = 'CALLED_OUT_NO_WORKED_CREDIT'
        elif AttendanceEventType.NO_CALL_NO_SHOW in types:
            reason = 'NO_CALL_NO_SHOW_NO_WORKED_CREDIT'
        elif coverage_events:
            reason = 'COVERED_BY_OTHER'
        elif AttendanceEventType.WORKED_AS_SCHEDULED in types:
            reason = 'EXPLICIT_WORKED_AS_SCHEDULED'
        elif worked_evidence:
            reason = 'WORKED_WITH_ATTENDANCE_EXCEPTION'
        else:
            reason = 'SCHEDULED_DEFAULT_NO_ACTIVE_ATTENDANCE'
        facts.append({
            'shift_id': shift.id,
            'shift_date': shift.shift_date,
            'scheduled_employee_id': scheduled_employee_id,
            'credited_employee_ids': tuple(dict.fromkeys(credited_employee_ids)),
            'attendance_event_types': tuple(sorted(item.value for item in types)),
            'reason': reason,
            'ambiguous': ambiguous,
            'attendance_activated': bool(events),
        })
    return sorted(facts, key=lambda row: (row['shift_date'], row['shift_id']))


def longview_rotation_fairness(
    db: Session, *, employee_id: int, store_id: int, before_date: date,
    as_of_date: date | None = None,
) -> LongviewRotationFairness:
    """Return past attendance-adjusted credit plus future scheduled obligation.

    Cutover is per-shift and event-driven. Historical shifts without active
    attendance facts keep legacy published-assignment credit, including all
    pre-0027 history. Active exceptions override only their exact shift. Future
    draft/published assignments remain planned burden and need no attendance.
    """
    planning_date = min(as_of_date or before_date, before_date)
    facts = _longview_historical_credit_rows(
        db, store_id=store_id, before_date=planning_date)
    credited = [row for row in facts if employee_id in row['credited_employee_ids']]
    scheduled = [row for row in facts if row['scheduled_employee_id'] == employee_id]
    planned = [
        row for row, _period in _effective_assignment_rows_with_period(
            db, employee_id=employee_id, start_date=planning_date,
            end_date=before_date - timedelta(days=1))
        if row.store_id == store_id
    ]
    employee_details = tuple({
        **row,
        'shift_date': row['shift_date'].isoformat(),
        'credited': employee_id in row['credited_employee_ids'],
        'scheduled_for_employee': row['scheduled_employee_id'] == employee_id,
        'covered_for_other': (
            employee_id in row['credited_employee_ids']
            and row['scheduled_employee_id'] != employee_id),
    } for row in facts if (
        row['scheduled_employee_id'] == employee_id
        or employee_id in row['credited_employee_ids']))
    return LongviewRotationFairness(
        store_id=store_id,
        historical_assignment_count=len(credited),
        last_historical_assignment_date=(
            max(row['shift_date'] for row in credited) if credited else None),
        planned_future_assignment_count=len(planned),
        scheduled_historical_assignment_count=len(scheduled),
        callout_count=sum(
            row['scheduled_employee_id'] == employee_id
            and AttendanceEventType.CALLED_OUT.value in row['attendance_event_types']
            for row in facts),
        no_call_no_show_count=sum(
            row['scheduled_employee_id'] == employee_id
            and AttendanceEventType.NO_CALL_NO_SHOW.value in row['attendance_event_types']
            for row in facts),
        covered_for_others_count=sum(
            employee_id in row['credited_employee_ids']
            and row['scheduled_employee_id'] != employee_id
            for row in facts),
        ambiguous_outcome_count=sum(
            row['ambiguous'] and (
                row['scheduled_employee_id'] == employee_id
                or employee_id in row['credited_employee_ids'])
            for row in facts),
        credit_details=employee_details,
    )


def choose_employee_for_shift(
    db: Session, *, shift: ScheduleShift, planning_date: date | None = None,
    weekend_diagnostics: list[dict] | None = None,
    longview_diagnostics: list[dict] | None = None,
) -> tuple[Employee | None, tuple[ConstraintReason, ...]]:
    employees = list_scheduling_candidates(db)
    special = db.execute(select(SpecialStorePolicy).where(
        SpecialStorePolicy.store_id == shift.store_id, SpecialStorePolicy.active.is_(True))).scalar_one_or_none()
    reasons: list[ConstraintReason] = []
    eligible: list[tuple[Employee, tuple[int, int], int]] = []
    if special:
        states = {s.employee_id: s for s in db.execute(select(SpecialStoreRotationState).where(
            SpecialStoreRotationState.store_id == shift.store_id).with_for_update()).scalars()}
        primary = [e for e in employees if states.get(e.id) and states[e.id].participation == SpecialStoreParticipation.PRIMARY]
        rotation = [e for e in employees if states.get(e.id) and states[e.id].participation == SpecialStoreParticipation.ROTATION]
        eligible_by_population: dict[SpecialStoreParticipation, list[tuple[Employee, tuple[int, int], int]]] = {
            SpecialStoreParticipation.PRIMARY: [], SpecialStoreParticipation.ROTATION: []}
        for participation, population in (
            (SpecialStoreParticipation.PRIMARY, primary),
            (SpecialStoreParticipation.ROTATION, rotation),
        ):
            for employee in population:
                result = evaluate_assignment(
                    db, employee_id=employee.id, store_id=shift.store_id,
                    shift_date=shift.shift_date, start_time=shift.start_time,
                    end_time=shift.end_time,
                    unpaid_break_minutes=shift.unpaid_break_minutes)
                if result.eligible and not result.requires_hour_approval:
                    eligible_by_population[participation].append((
                        employee,
                        assignment_score(
                            db, employee_id=employee.id, store_id=shift.store_id,
                            shift_date=shift.shift_date),
                        base_pattern_score(
                            db, employee_id=employee.id, shift_date=shift.shift_date),
                    ))
                    continue
                reasons.extend(result.reasons)
                if result.eligible and result.requires_hour_approval:
                    reasons.append(ConstraintReason(
                        'WEEKLY_HOURS_APPROVAL_REQUIRED',
                        f'Assignment would exceed the {result.approval_threshold_hours}-hour approval threshold.'))
                state = states[employee.id]
                state.temporarily_skipped_at = _now()
                state.skip_reason = ', '.join(
                    reason.code for reason in result.reasons) or 'WEEKLY_HOURS_APPROVAL_REQUIRED'
                if participation == SpecialStoreParticipation.ROTATION:
                    # Preserve the existing near-front skip behavior: a date-specific
                    # restriction moves the due participant only one place, never to
                    # the queue tail or out of the rotation obligation.
                    next_state = db.execute(select(SpecialStoreRotationState).where(
                        SpecialStoreRotationState.store_id == shift.store_id,
                        SpecialStoreRotationState.participation == SpecialStoreParticipation.ROTATION,
                        SpecialStoreRotationState.queue_position > state.queue_position,
                    ).order_by(SpecialStoreRotationState.queue_position).limit(1).with_for_update()).scalar_one_or_none()
                    if next_state is not None:
                        state.queue_position, next_state.queue_position = (
                            next_state.queue_position, state.queue_position)

        primary_eligible = eligible_by_population[SpecialStoreParticipation.PRIMARY]
        if primary_eligible:
            # Permanent Longview staff retain first coverage priority. Their normal
            # workload is deliberately not compared with Vancouver rotation burden.
            primary_eligible.sort(key=lambda row: (
                -_below_target_priority(row[1]),
                -row[2], -row[1][1], -row[1][0], row[0].id))
            chosen = primary_eligible[0]
            if longview_diagnostics is not None:
                longview_diagnostics.append({
                    'shift_id': shift.id, 'employee_id': chosen[0].id,
                    'store_id': shift.store_id, 'participant_type': 'PRIMARY',
                    'base_pattern_expected': chosen[2] > 0,
                    'reason': 'Eligible permanent Longview-primary staff retain coverage priority.',
                })
            return chosen[0], ()

        rotation_eligible = eligible_by_population[SpecialStoreParticipation.ROTATION]
        if not rotation_eligible:
            if not primary and not rotation:
                reasons.append(ConstraintReason(
                    'NO_LONGVIEW_PARTICIPANTS',
                    'No employee participates in this Longview coverage pool.'))
            return None, tuple(dict.fromkeys(reasons))
        rotation_fairness = {
            row[0].id: longview_rotation_fairness(
                db, employee_id=row[0].id, store_id=shift.store_id,
                before_date=shift.shift_date, as_of_date=planning_date)
            for row in rotation_eligible
        }
        rotation_eligible.sort(key=lambda row: (
            rotation_fairness[row[0].id].historical_assignment_count,
            rotation_fairness[row[0].id].last_historical_assignment_date or date.min,
            rotation_fairness[row[0].id].planned_future_assignment_count,
            -_below_target_priority(row[1]),
            -row[2], -row[1][1], -row[1][0], row[0].id,
        ))
        chosen = rotation_eligible[0]
        chosen_fairness = rotation_fairness[chosen[0].id]
        chosen_burden_key = (
            chosen_fairness.historical_assignment_count,
            chosen_fairness.last_historical_assignment_date or date.min,
            chosen_fairness.planned_future_assignment_count,
        )
        base_candidates = [row for row in rotation_eligible if row[2] > 0]
        fairness_override = bool(
            chosen[2] < 0 and base_candidates
            and chosen_burden_key < min((
                rotation_fairness[row[0].id].historical_assignment_count,
                rotation_fairness[row[0].id].last_historical_assignment_date or date.min,
                rotation_fairness[row[0].id].planned_future_assignment_count,
            ) for row in base_candidates)
        )
        if fairness_override:
            shift.base_pattern_deviation_reason = 'LONGVIEW_ROTATION'
        if longview_diagnostics is not None:
            longview_diagnostics.append({
                'shift_id': shift.id, 'employee_id': chosen[0].id,
                'store_id': shift.store_id, 'participant_type': 'ROTATION',
                'historical_assignment_count': chosen_fairness.historical_assignment_count,
                'scheduled_historical_assignment_count': (
                    chosen_fairness.scheduled_historical_assignment_count),
                'longview_callout_count': chosen_fairness.callout_count,
                'longview_no_call_no_show_count': chosen_fairness.no_call_no_show_count,
                'covered_for_others_count': chosen_fairness.covered_for_others_count,
                'ambiguous_attendance_count': chosen_fairness.ambiguous_outcome_count,
                'last_historical_assignment_date': (
                    chosen_fairness.last_historical_assignment_date.isoformat()
                    if chosen_fairness.last_historical_assignment_date else None),
                'planned_future_assignment_count': chosen_fairness.planned_future_assignment_count,
                'base_pattern_expected': chosen[2] > 0,
                'fairness_overrode_base_pattern': fairness_override,
                'candidate_burdens': [{
                    'employee_id': row[0].id,
                    'historical_count': rotation_fairness[row[0].id].historical_assignment_count,
                    'scheduled_historical_count': (
                        rotation_fairness[row[0].id].scheduled_historical_assignment_count),
                    'callout_count': rotation_fairness[row[0].id].callout_count,
                    'no_call_no_show_count': (
                        rotation_fairness[row[0].id].no_call_no_show_count),
                    'covered_for_others_count': (
                        rotation_fairness[row[0].id].covered_for_others_count),
                    'attendance_adjusted': (
                        rotation_fairness[row[0].id].historical_assignment_count
                        != rotation_fairness[row[0].id].scheduled_historical_assignment_count
                        or rotation_fairness[row[0].id].covered_for_others_count > 0),
                    'last_historical_date': (
                        rotation_fairness[row[0].id].last_historical_assignment_date.isoformat()
                        if rotation_fairness[row[0].id].last_historical_assignment_date else None),
                    'planned_future_count': rotation_fairness[row[0].id].planned_future_assignment_count,
                    'below_weekly_shift_target': bool(_below_target_priority(row[1])),
                    'weekly_shift_target_gap': row[1][1],
                    'base_pattern_expected': row[2] > 0,
                } for row in rotation_eligible],
                'reason': (
                    'Least attendance-credited historical Longview burden, oldest last credited '
                    'work date, least planned future burden, then below-target workload, base '
                    'pattern, weekly target gap, store preference, and employee ID. Historical '
                    'shifts without active attendance '
                    'facts retain scheduled credit.'),
            })
        return chosen[0], ()
    for employee in employees:
        result = evaluate_assignment(db, employee_id=employee.id, store_id=shift.store_id,
            shift_date=shift.shift_date, start_time=shift.start_time, end_time=shift.end_time,
            unpaid_break_minutes=shift.unpaid_break_minutes)
        if result.eligible and not result.requires_hour_approval:
            eligible.append((
                employee,
                assignment_score(db, employee_id=employee.id, store_id=shift.store_id,
                                 shift_date=shift.shift_date),
                base_pattern_score(db, employee_id=employee.id, shift_date=shift.shift_date),
            ))
        else:
            reasons.extend(result.reasons)
            if result.eligible and result.requires_hour_approval:
                reasons.append(ConstraintReason(
                    'WEEKLY_HOURS_APPROVAL_REQUIRED',
                    f'Assignment would exceed the {result.approval_threshold_hours}-hour approval threshold.'))
    if not eligible:
        return None, tuple(dict.fromkeys(reasons))
    if shift.shift_date.weekday() in (5, 6):
        fairness_by_employee = {
            row[0].id: weekend_fairness(
                db, employee_id=row[0].id, weekday=shift.shift_date.weekday(),
                before_date=shift.shift_date, as_of_date=planning_date)
            for row in eligible
        }
        eligible.sort(key=lambda row: (
            fairness_by_employee[row[0].id].historical_assignment_count,
            (fairness_by_employee[row[0].id].last_historical_assignment_date or date.min),
            fairness_by_employee[row[0].id].planned_future_assignment_count,
            -_below_target_priority(row[1]),
            -row[2], -row[1][0], -row[1][1], row[0].id,
        ))
        chosen = eligible[0]
        chosen_fairness = fairness_by_employee[chosen[0].id]
        chosen_fairness_key = (
            chosen_fairness.historical_assignment_count,
            chosen_fairness.last_historical_assignment_date or date.min,
            chosen_fairness.planned_future_assignment_count,
        )
        base_candidates = [row for row in eligible if row[2] > 0]
        fairness_override = bool(
            chosen[2] < 0 and base_candidates
            and chosen_fairness_key < min((
                fairness_by_employee[row[0].id].historical_assignment_count,
                fairness_by_employee[row[0].id].last_historical_assignment_date or date.min,
                fairness_by_employee[row[0].id].planned_future_assignment_count,
            ) for row in base_candidates)
        )
        if fairness_override:
            shift.base_pattern_deviation_reason = 'WEEKEND_FAIRNESS'
        if weekend_diagnostics is not None:
            weekend_diagnostics.append({
                'shift_id': shift.id,
                'employee_id': chosen[0].id,
                'weekend_day': 'SATURDAY' if shift.shift_date.weekday() == 5 else 'SUNDAY',
                'historical_12_week_assignment_count': chosen_fairness.historical_assignment_count,
                'last_historical_assignment_date': (
                    chosen_fairness.last_historical_assignment_date.isoformat()
                    if chosen_fairness.last_historical_assignment_date else None),
                'planned_future_assignment_count': chosen_fairness.planned_future_assignment_count,
                'base_pattern_expected': chosen[2] > 0,
                'fairness_overrode_base_pattern': fairness_override,
                'candidate_burdens': [{
                    'employee_id': row[0].id,
                    'historical_count': fairness_by_employee[row[0].id].historical_assignment_count,
                    'last_historical_date': (
                        fairness_by_employee[row[0].id].last_historical_assignment_date.isoformat()
                        if fairness_by_employee[row[0].id].last_historical_assignment_date else None),
                    'planned_future_count': fairness_by_employee[row[0].id].planned_future_assignment_count,
                    'below_weekly_shift_target': bool(_below_target_priority(row[1])),
                    'weekly_shift_target_gap': row[1][1],
                    'base_pattern_expected': row[2] > 0,
                } for row in eligible],
                'reason': (
                    'Fewest equivalent-day historical assignments, oldest last assignment, '
                    'least planned future burden, then below-target workload, base pattern, '
                    'store preference, and weekly target gap.'),
            })
    else:
        eligible.sort(key=lambda row: (
            _below_target_priority(row[1]), row[2], row[1][0], row[1][1], -row[0].id),
            reverse=True)
    return eligible[0][0], ()


def annotate_base_pattern_deviations(
    db: Session, *, period: SchedulePeriod,
) -> list[dict]:
    profiles = {row.employee_id: row for row in db.execute(select(
        EmployeeSchedulingProfile).where(EmployeeSchedulingProfile.active.is_(True))).scalars()}
    rows = list(db.execute(select(ScheduleShift).where(
        ScheduleShift.schedule_period_id == period.id,
        ScheduleShift.employee_id.is_not(None))).scalars())
    assigned_dates: dict[int, set[date]] = defaultdict(set)
    for row in rows:
        assigned_dates[row.employee_id].add(row.shift_date)
    diagnostics: list[dict] = []
    for row in rows:
        profile = profiles.get(row.employee_id)
        expected = is_base_workday(profile, row.shift_date) if profile else None
        reason_hint = row.base_pattern_deviation_reason
        row.base_pattern_expected_day = expected
        row.base_pattern_deviation_reason = None
        if expected is not False:
            continue
        reason = ('MANUAL_LOCKED_OVERRIDE' if row.manually_locked else
                  reason_hint or 'COVERAGE_REQUIREMENT')
        mask = base_pattern_mask(profile, period.alternating_week or alternating_week_for_date(
            period.week_start_date))
        expected_dates = [period.week_start_date + timedelta(days=offset) for offset in range(7)
                          if mask is not None and mask & (1 << scheduling_weekday(
                              period.week_start_date + timedelta(days=offset)))]
        missing_expected = [day for day in expected_dates if day not in assigned_dates[row.employee_id]]
        for expected_date in missing_expected:
            result = evaluate_assignment(
                db, employee_id=row.employee_id, store_id=row.store_id,
                shift_date=expected_date, start_time=row.start_time, end_time=row.end_time,
                unpaid_break_minutes=row.unpaid_break_minutes)
            codes = {item.code for item in result.reasons}
            if 'APPROVED_TIME_OFF' in codes:
                reason = 'APPROVED_PTO'; break
            if 'HARD_WEEKDAY_LOCKOUT' in codes:
                reason = 'WEEKDAY_LOCKOUT'; break
            if {'MAX_CONSECUTIVE_DAYS', 'REQUIRED_DAYS_OFF'} & codes:
                reason = 'CONSECUTIVE_DAY_AVOIDANCE'; break
        else:
            if row.manually_locked or reason_hint:
                pass
            elif row.is_double_coverage:
                reason = 'DOUBLE_COVERAGE'
            elif len(assigned_dates[row.employee_id]) <= (profile.target_shifts_per_week or 0):
                reason = 'WEEKLY_TARGET_BALANCING'
        row.base_pattern_deviation_reason = reason
        diagnostics.append({
            'shift_id': row.id, 'employee_id': row.employee_id,
            'date': row.shift_date.isoformat(), 'reason': reason,
            'base_week': period.alternating_week,
        })
    return diagnostics


def materialize_coverage_positions(
    db: Session, *, principal: Principal, period: SchedulePeriod,
) -> dict:
    defaults = db.execute(select(SchedulingStoreDefaults).where(
        SchedulingStoreDefaults.id == 1)).scalar_one_or_none()
    if (defaults is None or defaults.standard_shift_start is None
            or defaults.standard_shift_end is None
            or defaults.standard_shift_end <= defaults.standard_shift_start):
        raise SchedulingValidationError(
            'Configure valid Standard Shift Start and End times before generating a schedule.')
    requirements = list(db.execute(select(CoverageRequirement).where(
        CoverageRequirement.active.is_(True),
        CoverageRequirement.minimum_employee_count > 0,
    ).order_by(CoverageRequirement.store_id, CoverageRequirement.day_of_week,
               CoverageRequirement.id)).scalars())
    if not requirements:
        raise SchedulingValidationError(
            'Configure at least one active coverage requirement before generating a schedule.')

    # A day's overlapping coverage windows describe required concurrent headcount. Each
    # resulting position is deliberately expanded to the authoritative complete shift.
    by_store_day: dict[tuple[int, int], dict] = {}
    for requirement in requirements:
        key = (requirement.store_id, requirement.day_of_week)
        spec = by_store_day.setdefault(key, {
            'count': 0, 'shift_type_ids': set(), 'opener': False, 'closer': False})
        spec['count'] = max(spec['count'], requirement.minimum_employee_count)
        if requirement.required_shift_type_id is not None:
            spec['shift_type_ids'].add(requirement.required_shift_type_id)
        spec['opener'] = spec['opener'] or requirement.requires_opener
        spec['closer'] = spec['closer'] or requirement.requires_closer

    store_shifts_by_store: dict[int, list[StoreShift]] = defaultdict(list)
    store_shift_rows = db.execute(select(StoreShift).where(
        StoreShift.active.is_(True),
        StoreShift.store_id.in_(tuple({store_id for store_id, _weekday in by_store_day})),
    ).order_by(
        StoreShift.store_id, StoreShift.display_order, StoreShift.start_time,
        StoreShift.end_time, StoreShift.label, StoreShift.id,
    )).scalars()
    for store_shift in store_shift_rows:
        store_shifts_by_store[store_shift.store_id].append(store_shift)

    protected_source_ids = set(db.execute(select(ScheduleShift.source_shift_id).where(
        ScheduleShift.schedule_period_id == period.id,
        ScheduleShift.is_double_coverage.is_(True),
        ScheduleShift.double_coverage_manually_assigned.is_(True),
        ScheduleShift.source_shift_id.is_not(None))).scalars())
    auto_statement = select(ScheduleShift).where(
        ScheduleShift.schedule_period_id == period.id,
        ScheduleShift.is_double_coverage.is_(False),
        ScheduleShift.manually_locked.is_(False),
        or_(ScheduleShift.generated_from_coverage_requirement.is_(True),
            ScheduleShift.source_shift_id.is_not(None)),
    )
    if protected_source_ids:
        auto_statement = auto_statement.where(ScheduleShift.id.not_in(protected_source_ids))
    auto_rows = list(db.execute(auto_statement).scalars())
    auto_ids = [row.id for row in auto_rows]
    if auto_ids:
        db.execute(delete(ScheduleShift).where(ScheduleShift.id.in_(auto_ids)))

    preserved = list(db.execute(select(ScheduleShift).where(
        ScheduleShift.schedule_period_id == period.id,
        ScheduleShift.is_double_coverage.is_(False),
    )).scalars())
    preserved_counts: dict[tuple[int, date], int] = defaultdict(int)
    for row in preserved:
        preserved_counts[(row.store_id, row.shift_date)] += 1

    created = 0
    for day_offset in range((period.week_end_date - period.week_start_date).days + 1):
        shift_date = period.week_start_date + timedelta(days=day_offset)
        # Scheduling coverage weekdays use Sunday=0, while date.weekday() uses Monday=0.
        for (store_id, weekday), spec in by_store_day.items():
            if weekday != (shift_date.weekday() + 1) % 7:
                continue
            missing = max(0, spec['count'] - preserved_counts[(store_id, shift_date)])
            shift_type_id = next(iter(spec['shift_type_ids'])) if len(spec['shift_type_ids']) == 1 else None
            weekday_bit = 1 << scheduling_weekday(shift_date)
            store_shift = next((row for row in store_shifts_by_store[store_id]
                                if row.active_weekdays & weekday_bit), None)
            shift_start = store_shift.start_time if store_shift else defaults.standard_shift_start
            shift_end = store_shift.end_time if store_shift else defaults.standard_shift_end
            for _ in range(missing):
                db.add(ScheduleShift(
                    schedule_period_id=period.id, employee_id=None, store_id=store_id,
                    shift_date=shift_date, start_time=shift_start,
                    end_time=shift_end, unpaid_break_minutes=0,
                    shift_type_id=shift_type_id, is_opener=spec['opener'],
                    is_closer=spec['closer'], generated_from_coverage_requirement=True,
                    source_store_shift_id=store_shift.id if store_shift else None,
                    created_by_principal_id=principal.id,
                    updated_by_principal_id=principal.id,
                ))
                created += 1
    db.flush()
    if created == 0 and not preserved:
        raise SchedulingValidationError(
            'Active coverage requirements do not create any positions in this schedule period.')
    return {
        'created': created, 'replaced': len(auto_ids), 'preserved': len(preserved),
        'standard_shift_start': defaults.standard_shift_start.isoformat(),
        'standard_shift_end': defaults.standard_shift_end.isoformat(),
    }


def regenerate_period(db: Session, *, principal: Principal, schedule_period_id: int) -> dict:
    period = db.execute(select(SchedulePeriod).where(SchedulePeriod.id == schedule_period_id).with_for_update()).scalar_one_or_none()
    if period is None or period.status != SchedulePeriodStatus.DRAFT:
        raise SchedulingConflict('Only a draft schedule can be generated.')
    period.alternating_week = alternating_week_for_date(period.week_start_date)
    from app.services.v2_scheduling_assignments_service import (
        clear_automatic_double_coverage, generate_double_coverage_assignments,
        ensure_daily_lead_staffing, reconcile_lead_designations,
    )
    manual_leads = {
        row.shift_date: row.employee_id for row in db.execute(select(ScheduleShift).where(
            ScheduleShift.schedule_period_id == period.id,
            ScheduleShift.is_lead_of_day.is_(True),
            ScheduleShift.lead_of_day_manually_assigned.is_(True),
            ScheduleShift.employee_id.is_not(None))).scalars()
    }
    positions = materialize_coverage_positions(db, principal=principal, period=period)
    clear_automatic_double_coverage(db, schedule_period_id=period.id)
    # Double Coverage is a real full-shift assignment and therefore consumes one
    # employee target before ordinary positions are ranked.
    double_coverage = generate_double_coverage_assignments(
        db, principal=principal, schedule_period_id=period.id)
    special_store_ids = set(db.execute(select(SpecialStorePolicy.store_id).where(
        SpecialStorePolicy.active.is_(True))).scalars())
    shifts = list(db.execute(select(ScheduleShift).where(
        ScheduleShift.schedule_period_id == period.id,
        ScheduleShift.is_double_coverage.is_(False)).order_by(
        ScheduleShift.shift_date, ScheduleShift.start_time, ScheduleShift.id).with_for_update()).scalars())
    candidates = list_scheduling_candidates(db)
    candidate_profiles = {row.employee_id: row for row in db.execute(select(
        EmployeeSchedulingProfile).where(EmployeeSchedulingProfile.employee_id.in_(
            [employee.id for employee in candidates] or [-1]))).scalars()}
    def base_demand(row: ScheduleShift) -> int:
        return sum(is_base_workday(candidate_profiles.get(employee.id), row.shift_date) is True
                   for employee in candidates if candidate_profiles.get(employee.id) is not None)
    shifts.sort(key=lambda row: (
        0 if row.store_id in special_store_ids else 1,
        -base_demand(row),
        row.shift_date, row.start_time, row.id))
    uncovered: list[dict] = []
    weekend_decisions: list[dict] = []
    longview_decisions: list[dict] = []
    lead_decisions: list[dict] = []
    generated_special_shift_ids: set[int] = set()
    scheduling_policy = organization_policy(db)
    planning_date = datetime.now(ZoneInfo(scheduling_policy.timezone_name)).date()
    assigned = 0
    for shift in shifts:
        if shift.manually_locked:
            continue
        shift.employee_id = None
        employee, reasons = choose_employee_for_shift(
            db, shift=shift, planning_date=planning_date,
            weekend_diagnostics=weekend_decisions,
            longview_diagnostics=longview_decisions)
        if employee is None:
            uncovered.append({'shift_id': shift.id, 'store_id': shift.store_id, 'date': shift.shift_date.isoformat(),
                              'start_time': shift.start_time.isoformat(), 'end_time': shift.end_time.isoformat(),
                              'reasons': [{'code': r.code, 'message': r.message} for r in reasons]})
            continue
        shift.employee_id = employee.id
        shift.updated_by_principal_id = principal.id
        shift.updated_at = _now()
        assigned += 1
        special = db.execute(select(SpecialStorePolicy.id).where(SpecialStorePolicy.store_id == shift.store_id,
            SpecialStorePolicy.active.is_(True))).scalar_one_or_none()
        if special:
            generated_special_shift_ids.add(shift.id)
    lead_staffing_uncovered = ensure_daily_lead_staffing(
        db, principal=principal, schedule_period_id=period.id,
        planning_date=planning_date, diagnostics=lead_decisions)
    # Lead repair may legally change a generated Longview assignee. Advance the
    # persistent queue only after repair so credit and debug output describe the
    # final assignment rather than the provisional candidate.
    for shift_id in generated_special_shift_ids:
        final_shift = db.get(ScheduleShift, shift_id)
        if final_shift is None or final_shift.employee_id is None:
            continue
        state = db.execute(select(SpecialStoreRotationState).where(
            SpecialStoreRotationState.store_id == final_shift.store_id,
            SpecialStoreRotationState.employee_id == final_shift.employee_id).with_for_update()).scalar_one_or_none()
        if state is None:
            continue
        max_position = db.execute(select(func.max(SpecialStoreRotationState.queue_position)).where(
            SpecialStoreRotationState.store_id == final_shift.store_id)).scalar_one() or 0
        state.queue_position = max_position + 1
        state.assignment_count += 1
        state.last_assigned_at = _now(); state.last_assigned_shift_id = final_shift.id
        state.temporarily_skipped_at = None; state.skip_reason = None
        decision = next((row for row in longview_decisions if row['shift_id'] == shift_id), None)
        if decision is not None and decision['employee_id'] != final_shift.employee_id:
            decision['rotation_selected_employee_id'] = decision['employee_id']
            decision['employee_id'] = final_shift.employee_id
            decision['participant_type'] = state.participation.value
            decision['lead_repair_changed_assignment'] = True
    lead_uncovered = reconcile_lead_designations(
        db, schedule_period_id=period.id, preferred_manual_by_date=manual_leads,
        planning_date=planning_date, diagnostics=lead_decisions)
    lead_uncovered = lead_staffing_uncovered or lead_uncovered
    deviations = annotate_base_pattern_deviations(db, period=period)
    period.lifecycle_stage = ScheduleLifecycleStage.REVIEW
    period.generated_at = _now(); period.version += 1
    from app.services.v2_scheduling_coverage_service import rebuild_schedule_warnings
    rebuild_schedule_warnings(db, schedule_period_id=period.id)
    _audit(db, principal, 'SCHEDULE_REGENERATED', 'schedule_period', period.id,
           {'assigned': assigned, 'uncovered': uncovered, 'lead_uncovered': lead_uncovered,
            'positions': positions,
            'base_pattern_week': period.alternating_week,
            'base_pattern_deviations': deviations,
            'lead_fairness': lead_decisions,
            'double_coverage': double_coverage, 'weekend_fairness': weekend_decisions,
            'longview_rotation': longview_decisions,
            'locked_preserved': sum(s.manually_locked for s in shifts)})
    targets = [{
        'employee_id': employee.id,
        'target_shifts': profile.target_shifts_per_week if profile else None,
        'assigned_shifts': scheduled_weekly_shift_count(
            db, employee_id=employee.id, shift_date=period.week_start_date),
    } for employee in list_scheduling_candidates(db)
      for profile in [db.execute(select(EmployeeSchedulingProfile).where(
          EmployeeSchedulingProfile.employee_id == employee.id)).scalar_one_or_none()]]
    return {'assigned': assigned, 'uncovered': uncovered, 'lead_uncovered': lead_uncovered,
            'positions': positions, 'shift_targets': targets,
            'base_pattern_week': period.alternating_week,
            'base_pattern_deviations': deviations,
            'lead_fairness': lead_decisions,
            'double_coverage': double_coverage, 'weekend_fairness': weekend_decisions,
            'longview_rotation': longview_decisions,
            'locked_preserved': sum(s.manually_locked for s in shifts)}


def set_manual_lock(db: Session, *, principal: Principal, shift_id: int, locked: bool, reason: str = '') -> ScheduleShift:
    shift = db.execute(select(ScheduleShift).where(ScheduleShift.id == shift_id).with_for_update()).scalar_one_or_none()
    if shift is None:
        raise SchedulingValidationError('Shift not found.')
    shift.manually_locked = locked; shift.locked_by_principal_id = principal.id if locked else None
    shift.locked_at = _now() if locked else None; shift.lock_reason = reason.strip() or None if locked else None
    _audit(db, principal, 'MANUAL_ASSIGNMENT_LOCK_CHANGED', 'schedule_shift', shift.id,
           {'locked': locked, 'reason': shift.lock_reason})
    return shift


def compute_automation_window(current_end: date, policy: SchedulingOrganizationPolicy) -> AutomationWindow:
    next_start = current_end + timedelta(days=1)
    next_end = next_start + timedelta(weeks=policy.schedule_length_weeks) - timedelta(days=1)
    tz = ZoneInfo(policy.timezone_name)
    generate_local = datetime.combine(current_end - timedelta(days=policy.generate_days_before_end), policy.publication_local_time, tzinfo=tz)
    publish_local = datetime.combine(current_end - timedelta(days=policy.publish_days_before_end), policy.publication_local_time, tzinfo=tz)
    return AutomationWindow(next_start, next_end, generate_local.astimezone(timezone.utc), publish_local.astimezone(timezone.utc))


def set_publication_hold(db: Session, *, principal: Principal, schedule_period_id: int, held: bool, reason: str = '') -> SchedulePeriod:
    period = db.execute(select(SchedulePeriod).where(SchedulePeriod.id == schedule_period_id).with_for_update()).scalar_one_or_none()
    if period is None or period.status != SchedulePeriodStatus.DRAFT:
        raise SchedulingConflict('Only a draft schedule may be held from publication.')
    if held and not reason.strip():
        raise SchedulingValidationError('A publication hold reason is required.')
    period.publication_hold = held; period.publication_hold_reason = reason.strip() or None
    _audit(db, principal, 'PUBLICATION_HOLD_CHANGED', 'schedule_period', period.id,
           {'held': held, 'reason': period.publication_hold_reason})
    return period


def _copy_generation_source(
    db: Session, *, principal: Principal, period: SchedulePeriod,
    source: SchedulePeriod | None, week_offset: int,
) -> None:
    if source is None:
        return
    for old in db.execute(select(ScheduleShift).where(
            ScheduleShift.schedule_period_id == source.id,
            ScheduleShift.is_double_coverage.is_(False)).order_by(ScheduleShift.id)).scalars():
        db.add(ScheduleShift(
            schedule_period_id=period.id, employee_id=None, store_id=old.store_id,
            shift_date=old.shift_date + timedelta(weeks=week_offset),
            start_time=old.start_time, end_time=old.end_time,
            unpaid_break_minutes=old.unpaid_break_minutes, shift_type_id=old.shift_type_id,
            is_opener=old.is_opener, is_closer=old.is_closer,
            employee_note=old.employee_note, source_shift_id=old.id,
            source_store_shift_id=old.source_store_shift_id,
            created_by_principal_id=principal.id, updated_by_principal_id=principal.id,
        ))
    db.flush()


def _create_generated_period(
    db: Session, *, principal: Principal, week_start: date,
    source: SchedulePeriod | None, source_week_offset: int,
    publication_at: datetime | None, generated_at: datetime, note: str,
) -> tuple[SchedulePeriod, dict]:
    from app.services.v2_scheduling_service import create_draft_period

    period = create_draft_period(
        db, principal=principal, week_start=week_start, notes=note)
    _copy_generation_source(
        db, principal=principal, period=period, source=source,
        week_offset=source_week_offset)
    period.generated_at = generated_at
    period.automatic_publication_at = publication_at
    period.lifecycle_stage = ScheduleLifecycleStage.GENERATED
    db.flush()
    result = regenerate_period(db, principal=principal, schedule_period_id=period.id)
    return period, result


def _effective_planning_periods(db: Session, *, start_date: date) -> dict[date, SchedulePeriod]:
    rows = list(db.execute(select(SchedulePeriod).where(
        SchedulePeriod.week_start_date >= start_date,
        SchedulePeriod.status.in_((SchedulePeriodStatus.DRAFT, SchedulePeriodStatus.PUBLISHED)),
    ).order_by(SchedulePeriod.week_start_date, SchedulePeriod.revision_number.desc()).with_for_update()).scalars())
    selected: dict[date, SchedulePeriod] = {}
    for row in rows:
        current = selected.get(row.week_start_date)
        if current is None or (
            current.status == SchedulePeriodStatus.PUBLISHED
            and row.status == SchedulePeriodStatus.DRAFT
        ):
            selected[row.week_start_date] = row
    return selected


def ensure_rolling_schedule_horizon(
    db: Session, *, principal: Principal, now: datetime | None = None,
) -> dict:
    """Fill, but never rewrite, the current rolling planning horizon."""
    now = now or _now()
    policy = organization_policy(db, principal_id=principal.id)
    business_timezone = ZoneInfo(policy.timezone_name)
    today = now.astimezone(business_timezone).date()
    current_week = _sunday(today)
    existing = _effective_planning_periods(db, start_date=current_week)
    if existing:
        horizon_start = current_week if current_week in existing else min(existing)
    else:
        last_published_end = db.execute(select(func.max(SchedulePeriod.week_end_date)).where(
            SchedulePeriod.status == SchedulePeriodStatus.PUBLISHED)).scalar_one()
        horizon_start = max(
            current_week,
            _sunday(last_published_end + timedelta(days=1)) if last_published_end else current_week,
        )
    desired_starts = [horizon_start + timedelta(weeks=offset)
                      for offset in range(policy.schedule_length_weeks)]
    generated: list[SchedulePeriod] = []
    results: list[dict] = []
    for week_start in desired_starts:
        if week_start in existing:
            continue
        publication_local = datetime.combine(
            week_start - timedelta(days=policy.publish_days_before_end + 1),
            policy.publication_local_time, tzinfo=business_timezone)
        period, result = _create_generated_period(
            db, principal=principal, week_start=week_start, source=None,
            source_week_offset=0,
            publication_at=publication_local.astimezone(timezone.utc),
            generated_at=now,
            note='Appended by rolling schedule generation.')
        existing[week_start] = period
        generated.append(period)
        results.append(result)
    ordered = [existing[start] for start in desired_starts]
    drafts = [row for row in ordered if row.status == SchedulePeriodStatus.DRAFT]
    return {
        'created': bool(generated),
        'created_period_ids': [row.id for row in generated],
        'period_ids': [row.id for row in drafts],
        'planned_period_ids': [row.id for row in ordered],
        'primary_period_id': (generated[0].id if generated else
                              (drafts[0].id if drafts else ordered[0].id)),
        'week_start_date': ordered[0].week_start_date,
        'week_end_date': ordered[-1].week_end_date,
        'publication_at': generated[0].automatic_publication_at if generated else None,
        'results': results,
        'horizon_weeks': len(ordered),
        'anchor_date': ALTERNATING_WEEK_A_ANCHOR,
    }


def manual_generate_draft_schedule(
    db: Session, *, principal: Principal, now: datetime | None = None,
) -> dict:
    """Idempotently fill the rolling horizon without regenerating existing weeks."""
    db.execute(select(func.pg_advisory_xact_lock(SCHEDULE_AUTOMATION_LOCK_KEY))).scalar_one()
    now = now or _now()
    policy = organization_policy(db, principal_id=principal.id)
    from app.services.v2_scheduling_readiness_service import require_generation_readiness
    require_generation_readiness(
        db, today=now.astimezone(ZoneInfo(policy.timezone_name)).date())
    result = ensure_rolling_schedule_horizon(
        db, principal=principal, now=now)
    _audit(db, principal, 'MANUAL_SCHEDULE_GENERATED', 'schedule_period',
           result['primary_period_id'], {
               'created_period_ids': result['created_period_ids'],
               'planned_period_ids': result['planned_period_ids'],
               'week_start_date': result['week_start_date'].isoformat(),
               'week_end_date': result['week_end_date'].isoformat(),
               'rolling_horizon': True,
           })
    return result


def automation_draft_dashboard(
    db: Session, *, today: date,
) -> dict:
    drafts = list(db.execute(select(SchedulePeriod).where(
        SchedulePeriod.status == SchedulePeriodStatus.DRAFT).order_by(
        SchedulePeriod.week_start_date, SchedulePeriod.revision_number.desc())).scalars())
    period_ids = [row.id for row in drafts]
    shift_counts = dict(db.execute(select(
        ScheduleShift.schedule_period_id, func.count(ScheduleShift.id)).where(
        ScheduleShift.schedule_period_id.in_(period_ids)).group_by(
        ScheduleShift.schedule_period_id)).all()) if period_ids else {}
    warning_rows = db.execute(select(
        ScheduleWarning.schedule_period_id,
        func.count(ScheduleWarning.id).filter(
            ScheduleWarning.warning_type.in_(ACTIONABLE_WARNING_TYPES)),
        func.count(ScheduleWarning.id).filter(
            ScheduleWarning.severity == ScheduleWarningSeverity.SERIOUS),
    ).where(ScheduleWarning.schedule_period_id.in_(period_ids)).group_by(
        ScheduleWarning.schedule_period_id)).all() if period_ids else []
    warning_counts = {period_id: (actionable, serious)
                      for period_id, actionable, serious in warning_rows}

    def summary(row: SchedulePeriod) -> dict:
        actionable, serious = warning_counts.get(row.id, (0, 0))
        return {
            'period': row, 'shift_count': int(shift_counts.get(row.id, 0)),
            'uncovered_count': int(actionable or 0), 'serious_warning_count': int(serious or 0),
        }

    upcoming = [summary(row) for row in drafts if row.week_end_date >= today]
    historical = [summary(row) for row in drafts if row.week_end_date < today]
    return {
        'upcoming': upcoming, 'historical': historical,
        'range_start': upcoming[0]['period'].week_start_date if upcoming else None,
        'range_end': upcoming[-1]['period'].week_end_date if upcoming else None,
        'publication_at': upcoming[0]['period'].automatic_publication_at if upcoming else None,
        'publication_hold': any(row['period'].publication_hold for row in upcoming),
        'uncovered_count': sum(row['uncovered_count'] for row in upcoming),
        'serious_warning_count': sum(row['serious_warning_count'] for row in upcoming),
    }


def run_schedule_automation(db: Session, *, principal: Principal, now: datetime | None = None) -> dict:
    """Idempotent job entry point; callers own the transaction and invocation cadence."""
    # One transaction-wide PostgreSQL advisory lock makes overlapping cron invocations serialize.
    db.execute(select(func.pg_advisory_xact_lock(SCHEDULE_AUTOMATION_LOCK_KEY))).scalar_one()
    now = now or _now()
    policy = organization_policy(db, principal_id=principal.id)
    if not policy.active:
        return {'generated_period_ids': [], 'published_period_ids': [], 'blocked_period_ids': [],
                'message': 'Schedule automation is disabled.'}
    published = list(db.execute(select(SchedulePeriod).where(
        SchedulePeriod.status == SchedulePeriodStatus.PUBLISHED).order_by(SchedulePeriod.week_start_date)).scalars())
    if not published:
        return {'generated_period_ids': [], 'published_period_ids': [], 'blocked_period_ids': [],
                'message': 'No current published schedule anchors automation.'}
    current_end = max(row.week_end_date for row in published)
    window = compute_automation_window(current_end, policy)
    generated_ids: list[int] = []
    if now >= window.generate_at:
        horizon = ensure_rolling_schedule_horizon(
            db, principal=principal, now=now)
        generated_ids = horizon['created_period_ids']
    published_ids: list[int] = []
    blocked_ids: list[int] = []
    if now >= window.publish_at:
        from app.services.v2_scheduling_service import publish_schedule
        due = list(db.execute(select(SchedulePeriod).where(
            SchedulePeriod.status == SchedulePeriodStatus.DRAFT,
            SchedulePeriod.automatic_publication_at.is_not(None),
            SchedulePeriod.automatic_publication_at <= now).order_by(SchedulePeriod.week_start_date).with_for_update()).scalars())
        for period in due:
            if period.publication_hold:
                blocked_ids.append(period.id); continue
            store_ids = tuple(db.execute(select(ScheduleShift.store_id).where(
                ScheduleShift.schedule_period_id == period.id).distinct()).scalars())
            try:
                publish_schedule(db, principal=principal, schedule_period_id=period.id,
                    expected_version=period.version, allowed_store_ids=store_ids,
                    allow_serious_warnings=False, confirmed=False)
                published_ids.append(period.id)
            except (PermissionError, SchedulingValidationError):
                # Automatic publication never overrides warnings or constraints.
                blocked_ids.append(period.id)
    return {'generated_period_ids': generated_ids, 'published_period_ids': published_ids,
            'blocked_period_ids': blocked_ids, 'generate_at': window.generate_at.isoformat(),
            'publish_at': window.publish_at.isoformat(), 'next_start': window.next_start.isoformat(),
            'next_end': window.next_end.isoformat()}


def _notify(db: Session, principal_id: int | None, event: str, message: str, entity_id: int) -> None:
    if principal_id:
        db.add(SchedulingNotification(principal_id=principal_id, event_type=event, message=message,
            entity_type='shift_transfer_request', entity_id=entity_id))


def _employee_for_principal(db: Session, principal: Principal) -> Employee:
    employee = db.execute(select(Employee).where(Employee.principal_id == principal.id, Employee.active.is_(True))).scalar_one_or_none()
    if employee is None:
        raise PermissionError('Your account is not linked to an active employee.')
    return employee


def create_transfer_request(db: Session, *, principal: Principal, shift_id: int, to_employee_id: int, today: date | None = None) -> ShiftTransferRequest:
    giver = _employee_for_principal(db, principal)
    shift = db.execute(select(ScheduleShift).where(ScheduleShift.id == shift_id).with_for_update()).scalar_one_or_none()
    if shift is None or shift.employee_id != giver.id:
        raise PermissionError('You may transfer only your own assigned shift.')
    if shift.shift_date <= (today or datetime.now(ZoneInfo('America/Los_Angeles')).date()):
        raise SchedulingValidationError('Only future shifts may be transferred.')
    recipient = db.get(Employee, to_employee_id)
    if recipient is None or not is_scheduling_candidate(recipient) or recipient.id == giver.id:
        raise SchedulingValidationError('Choose another active Scheduling employee.')
    if shift.is_double_coverage and not recipient.scheduling_double_coverage:
        raise SchedulingValidationError('Double Coverage shifts may transfer only to a Double Coverage employee.')
    period = db.get(SchedulePeriod, shift.schedule_period_id)
    if (period and period.status == SchedulePeriodStatus.PUBLISHED and shift.is_lead_of_day
            and not recipient.scheduling_lead_capable):
        raise SchedulingValidationError(
            'A manager must change Lead of the Day before this Lead shift can be offered to a non-Lead employee.')
    eligibility = evaluate_assignment(db, employee_id=recipient.id, store_id=shift.store_id,
        shift_date=shift.shift_date, start_time=shift.start_time, end_time=shift.end_time,
        unpaid_break_minutes=shift.unpaid_break_minutes, exclude_shift_id=shift.id)
    if not eligibility.eligible:
        raise SchedulingValidationError('Recipient is not eligible: ' + '; '.join(r.message for r in eligibility.reasons))
    row = ShiftTransferRequest(shift_id=shift.id, from_employee_id=giver.id, to_employee_id=recipient.id,
        initiated_by_principal_id=principal.id, status=ShiftTransferStatus.PENDING_RECIPIENT)
    db.add(row); db.flush()
    _notify(db, recipient.principal_id, 'SHIFT_TRANSFER_REQUESTED', f'{giver.full_name} offered you a shift on {shift.shift_date}.', row.id)
    _audit(db, principal, 'SHIFT_TRANSFER_REQUESTED', 'shift_transfer_request', row.id,
           {'shift_id': shift.id, 'from_employee_id': giver.id, 'to_employee_id': recipient.id})
    return row


def _complete_transfer(db: Session, *, principal: Principal, request: ShiftTransferRequest, shift: ScheduleShift) -> None:
    # Revalidate under the request and shift row locks immediately before mutation.
    result = evaluate_assignment(db, employee_id=request.to_employee_id, store_id=shift.store_id,
        shift_date=shift.shift_date, start_time=shift.start_time, end_time=shift.end_time,
        unpaid_break_minutes=shift.unpaid_break_minutes, exclude_shift_id=shift.id)
    if not result.eligible:
        raise SchedulingValidationError('Recipient is no longer eligible: ' + '; '.join(r.message for r in result.reasons))
    recipient_check = db.get(Employee, request.to_employee_id)
    if shift.is_double_coverage and (recipient_check is None or not recipient_check.scheduling_double_coverage):
        raise SchedulingValidationError('Recipient is no longer eligible for Double Coverage.')
    period = db.get(SchedulePeriod, shift.schedule_period_id)
    if (period and period.status == SchedulePeriodStatus.PUBLISHED and shift.is_lead_of_day
            and (recipient_check is None or not recipient_check.scheduling_lead_capable)):
        raise SchedulingValidationError(
            'Change Lead of the Day before transferring this published Lead shift to a non-Lead employee.')
    shift.employee_id = request.to_employee_id; shift.updated_by_principal_id = principal.id; shift.updated_at = _now()
    request.status = ShiftTransferStatus.COMPLETED; request.completed_at = _now(); request.updated_at = _now()
    giver = db.get(Employee, request.from_employee_id); recipient = db.get(Employee, request.to_employee_id)
    _notify(db, giver.principal_id if giver else None, 'SHIFT_TRANSFER_COMPLETED', f'Shift transfer to {recipient.full_name if recipient else "recipient"} completed.', request.id)
    for manager in db.execute(select(PrincipalModel).where(PrincipalModel.active.is_(True),
            PrincipalModel.role.in_((PrincipalRole.ADMIN, PrincipalRole.MANAGER)))).scalars():
        _notify(db, manager.id, 'SHIFT_TRANSFER_COMPLETED',
                f'{giver.full_name if giver else "Employee"} transferred a shift to {recipient.full_name if recipient else "recipient"}.', request.id)
    _audit(db, principal, 'SHIFT_TRANSFER_COMPLETED', 'shift_transfer_request', request.id,
           {'shift_id': shift.id, 'original_employee_id': request.from_employee_id, 'new_employee_id': request.to_employee_id})
    if period and period.status == SchedulePeriodStatus.DRAFT:
        from app.services.v2_scheduling_assignments_service import reconcile_lead_designations
        missing = reconcile_lead_designations(db, schedule_period_id=period.id)
        if any(row['date'] == shift.shift_date.isoformat() for row in missing):
            raise SchedulingValidationError('Transfer would leave this operating day without an eligible Lead of the Day.')
    from app.services.v2_scheduling_coverage_service import rebuild_schedule_warnings
    rebuild_schedule_warnings(db, schedule_period_id=shift.schedule_period_id)


def respond_to_transfer(db: Session, *, principal: Principal, request_id: int, accept: bool) -> ShiftTransferRequest:
    recipient = _employee_for_principal(db, principal)
    # Serialize all acceptance/hour calculations for this recipient.
    db.execute(select(Employee).where(Employee.id == recipient.id).with_for_update()).scalar_one()
    request = db.execute(select(ShiftTransferRequest).where(ShiftTransferRequest.id == request_id).with_for_update()).scalar_one_or_none()
    if request is None or request.to_employee_id != recipient.id or request.status != ShiftTransferStatus.PENDING_RECIPIENT:
        raise SchedulingConflict('Transfer request is unavailable or already resolved.')
    shift = db.execute(select(ScheduleShift).where(ScheduleShift.id == request.shift_id).with_for_update()).scalar_one()
    if shift.employee_id != request.from_employee_id:
        raise SchedulingConflict('The shift assignment changed before this request was accepted.')
    request.recipient_responded_at = _now(); request.updated_at = _now()
    giver = db.get(Employee, request.from_employee_id)
    if not accept:
        request.status = ShiftTransferStatus.DECLINED
        _notify(db, giver.principal_id if giver else None, 'SHIFT_TRANSFER_DECLINED', f'{recipient.full_name} declined the transfer.', request.id)
        _audit(db, principal, 'SHIFT_TRANSFER_DECLINED', 'shift_transfer_request', request.id, {'shift_id': shift.id})
        return request
    result = evaluate_assignment(db, employee_id=recipient.id, store_id=shift.store_id,
        shift_date=shift.shift_date, start_time=shift.start_time, end_time=shift.end_time,
        unpaid_break_minutes=shift.unpaid_break_minutes, exclude_shift_id=shift.id)
    if not result.eligible:
        raise SchedulingValidationError('Cannot accept this shift: ' + '; '.join(r.message for r in result.reasons))
    request.existing_scheduled_hours = result.scheduled_hours
    request.shift_hours = result.resulting_hours - result.scheduled_hours
    request.resulting_scheduled_hours = result.resulting_hours
    request.approval_threshold_hours = result.approval_threshold_hours
    request.amount_over_threshold = max(Decimal('0'), result.resulting_hours - result.approval_threshold_hours)
    if result.requires_hour_approval:
        request.status = ShiftTransferStatus.PENDING_MANAGER
        managers = db.execute(select(PrincipalModel).where(PrincipalModel.active.is_(True),
            PrincipalModel.role.in_((PrincipalRole.ADMIN, PrincipalRole.MANAGER)))).scalars()
        for manager in managers:
            _notify(db, manager.id, 'SHIFT_TRANSFER_APPROVAL_REQUIRED',
                    f'Transfer would schedule {recipient.full_name} for {result.resulting_hours} hours.', request.id)
        _notify(db, giver.principal_id if giver else None, 'SHIFT_TRANSFER_PENDING_MANAGER', 'Transfer requires manager approval for scheduled hours.', request.id)
        _audit(db, principal, 'SHIFT_TRANSFER_PENDING_MANAGER', 'shift_transfer_request', request.id,
               {'existing_hours': str(result.scheduled_hours), 'shift_hours': str(request.shift_hours),
                'resulting_hours': str(result.resulting_hours), 'threshold': str(result.approval_threshold_hours),
                'amount_over': str(request.amount_over_threshold)})
    else:
        _complete_transfer(db, principal=principal, request=request, shift=shift)
    return request


def review_transfer(db: Session, *, principal: Principal, request_id: int, approve: bool, note: str = '') -> ShiftTransferRequest:
    request = db.execute(select(ShiftTransferRequest).where(ShiftTransferRequest.id == request_id).with_for_update()).scalar_one_or_none()
    if request is None or request.status != ShiftTransferStatus.PENDING_MANAGER:
        raise SchedulingConflict('Transfer is not awaiting manager approval.')
    db.execute(select(Employee).where(Employee.id == request.to_employee_id).with_for_update()).scalar_one()
    shift = db.execute(select(ScheduleShift).where(ScheduleShift.id == request.shift_id).with_for_update()).scalar_one()
    if shift.employee_id != request.from_employee_id:
        raise SchedulingConflict('The shift assignment changed before manager review.')
    request.manager_principal_id = principal.id; request.manager_responded_at = _now()
    request.manager_note = note.strip() or None; request.updated_at = _now()
    giver = db.get(Employee, request.from_employee_id)
    if not approve:
        request.status = ShiftTransferStatus.REJECTED
        _notify(db, giver.principal_id if giver else None, 'SHIFT_TRANSFER_REJECTED', 'Manager rejected the transfer.', request.id)
        _audit(db, principal, 'SHIFT_TRANSFER_REJECTED', 'shift_transfer_request', request.id, {'note': request.manager_note})
        return request
    request.status = ShiftTransferStatus.APPROVED
    _audit(db, principal, 'SHIFT_TRANSFER_APPROVED', 'shift_transfer_request', request.id,
           {'existing_hours': str(request.existing_scheduled_hours), 'shift_hours': str(request.shift_hours),
            'resulting_hours': str(request.resulting_scheduled_hours), 'threshold': str(request.approval_threshold_hours),
            'amount_over': str(request.amount_over_threshold), 'note': request.manager_note})
    _complete_transfer(db, principal=principal, request=request, shift=shift)
    return request
