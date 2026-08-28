from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    CoverageRequirement, Employee, EmployeeSchedulingProfile, SchedulePeriod,
    SchedulePeriodStatus, ScheduleShift, SchedulingStoreDefaults,
    SpecialStoreParticipation, SpecialStorePolicy, SpecialStoreRotationState,
    Store, TimeOffRequest,
)
from app.services.v2_scheduling_pattern_service import is_base_workday, scheduling_weekday
from app.services.v2_scheduling_policy_service import (
    SimulatedAssignment, assignment_score, effective_assignment_rows,
    base_pattern_score, evaluate_assignment, longview_rotation_fairness,
    scheduled_weekly_shift_count, weekend_fairness,
)
from app.services.v2_scheduling_roster_service import list_scheduling_candidates


SEVERITY_ORDER = {'LOW': 0, 'MODERATE': 1, 'HIGH': 2, 'CRITICAL': 3}
CONSTRAINT_BUCKETS = {
    'APPROVED_TIME_OFF': 'pto',
    'HARD_WEEKDAY_LOCKOUT': 'lockout',
    'STORE_NEVER': 'never_store',
    'MAX_CONSECUTIVE_DAYS': 'consecutive_days',
    'REQUIRED_DAYS_OFF': 'consecutive_days',
    'MAX_WEEKLY_HOURS': 'maximum_hours',
    'OVERLAPPING_SHIFT': 'overlap',
    'SIMULATED_OVERLAP': 'overlap',
    'LONGVIEW_NOT_PARTICIPATING': 'participation',
    'SPECIAL_STORE_PRIMARY_ONLY': 'participation',
    'INACTIVE_EMPLOYEE': 'inactive',
    'SCHEDULING_INACTIVE': 'inactive',
    'SQUARE_INACTIVE': 'inactive',
}


@dataclass(frozen=True)
class CandidateImpact:
    employee_id: int
    employee_name: str
    eligible: bool
    constraint_codes: tuple[str, ...]
    scheduled_hours: Decimal
    added_hours: Decimal
    resulting_hours: Decimal
    approval_threshold_hours: Decimal
    requires_hour_approval: bool
    existing_shift_count: int
    resulting_shift_count: int
    target_shifts_per_week: int
    beyond_target: bool
    creates_fourth_shift: bool
    lead_capable: bool
    base_pattern_expected: bool | None
    weekend_historical_burden: int | None
    weekend_planned_burden: int | None
    longview_participation: str | None
    longview_historical_burden: int | None
    longview_planned_burden: int | None


@dataclass(frozen=True)
class PositionImpact:
    shift_id: int | None
    store_id: int
    store_name: str
    shift_date: date
    start_time: object
    end_time: object
    projected: bool
    required_position: bool
    double_coverage: bool
    longview: bool
    lead_repair_required: bool
    legal_candidate_count: int
    non_approval_candidate_count: int
    eliminated_counts: tuple[tuple[str, int], ...]
    candidates: tuple[CandidateImpact, ...]
    preferred_candidate_id: int | None
    preferred_candidate_name: str | None
    preferred_requires_hour_approval: bool
    preferred_scheduled_hours: Decimal | None
    preferred_added_hours: Decimal | None
    preferred_resulting_hours: Decimal | None
    preferred_approval_threshold_hours: Decimal | None
    preferred_resulting_shift_count: int | None
    preferred_target_shifts_per_week: int | None
    weekend_burden_delta: int | None
    longview_repair_type: str | None
    unresolved_constraint: str | None
    severity: str
    explanation: str


@dataclass(frozen=True)
class DateOperationalImpact:
    request_date: date
    projected: bool
    scheduled_shift_count: int
    required_positions_affected: int
    coverable_positions: int
    uncovered_positions: int
    coworker_pto_eliminations: int
    lockout_eliminations: int
    never_store_eliminations: int
    consecutive_day_eliminations: int
    approval_pressure_positions: int
    lead_impact: str
    longview_impact: str
    weekend_fairness_impact: str
    double_coverage_impact: str
    target_shift_displacement: str
    positions: tuple[PositionImpact, ...]
    severity: str
    explanation: str


@dataclass(frozen=True)
class OperationalBurdenAnalysis:
    request_id: int
    employee_id: int
    analysis_date: date
    request_start: date
    request_end: date
    inside_materialized_horizon: bool
    schedule_context: tuple[tuple[date, int, int, str], ...]
    date_impacts: tuple[DateOperationalImpact, ...]
    severity: str
    required_positions_affected: int
    coverable_positions: int
    uncovered_positions: int
    approval_pressure_positions: int
    preferred_repairs: tuple[tuple[int | None, int], ...]
    hard_constraints: tuple[str, ...]
    explanation: str


@dataclass(frozen=True)
class _Position:
    shift_id: int | None
    store_id: int
    shift_date: date
    start_time: object
    end_time: object
    unpaid_break_minutes: int
    projected: bool
    required_position: bool
    double_coverage: bool
    longview: bool
    lead_repair_required: bool


def _request_dates(request: TimeOffRequest) -> tuple[date, ...]:
    return tuple(
        request.start_date + timedelta(days=offset)
        for offset in range((request.end_date - request.start_date).days + 1)
    )


def _request_overlaps_shift(request: TimeOffRequest, shift: ScheduleShift) -> bool:
    if not request.start_date <= shift.shift_date <= request.end_date:
        return False
    return bool(
        request.full_day
        or (request.start_time < shift.end_time and request.end_time > shift.start_time)
    )


def _materialized_period(db: Session, day: date) -> SchedulePeriod | None:
    periods = list(db.execute(select(SchedulePeriod).where(
        SchedulePeriod.week_start_date <= day,
        SchedulePeriod.week_end_date >= day,
        SchedulePeriod.status.in_((SchedulePeriodStatus.DRAFT, SchedulePeriodStatus.PUBLISHED)),
    )).scalars())
    if not periods:
        return None
    published = [row for row in periods if row.status == SchedulePeriodStatus.PUBLISHED]
    return max(published or periods, key=lambda row: (row.revision_number, row.id))


def _day_shifts(db: Session, day: date) -> list[ScheduleShift]:
    period = _materialized_period(db, day)
    if period is None:
        return []
    return list(db.execute(select(ScheduleShift).where(
        ScheduleShift.schedule_period_id == period.id,
        ScheduleShift.shift_date == day,
    ).order_by(ScheduleShift.start_time, ScheduleShift.id)).scalars())


def _required_position(db: Session, shift: ScheduleShift, day_shifts: list[ScheduleShift]) -> bool:
    if shift.is_double_coverage:
        return True
    rules = list(db.execute(select(CoverageRequirement).where(
        CoverageRequirement.store_id == shift.store_id,
        CoverageRequirement.day_of_week == scheduling_weekday(shift.shift_date),
        CoverageRequirement.active.is_(True),
        CoverageRequirement.minimum_employee_count > 0,
        CoverageRequirement.start_time < shift.end_time,
        CoverageRequirement.end_time > shift.start_time,
    )).scalars())
    if shift.generated_from_coverage_requirement and not rules:
        return True
    for rule in rules:
        remaining = sum(
            row.id != shift.id and not row.is_double_coverage
            and row.employee_id is not None
            and row.store_id == shift.store_id
            and row.start_time <= rule.start_time and row.end_time >= rule.end_time
            for row in day_shifts
        )
        if remaining < rule.minimum_employee_count:
            return True
    return False


def _lead_repair_required(
    db: Session, *, request_employee_id: int, day: date, affected_shift_ids: set[int],
) -> bool:
    remaining = [row for row in _day_shifts(db, day) if row.id not in affected_shift_ids]
    employee_ids = {row.employee_id for row in remaining if row.employee_id is not None}
    if not employee_ids:
        return True
    employees = {row.id: row for row in db.execute(select(Employee).where(
        Employee.id.in_(employee_ids))).scalars()}
    return not any(
        row.employee_id != request_employee_id
        and row.employee_id in employees
        and employees[row.employee_id].scheduling_lead_capable
        and evaluate_assignment(
            db, employee_id=row.employee_id, store_id=row.store_id,
            shift_date=row.shift_date, start_time=row.start_time,
            end_time=row.end_time, unpaid_break_minutes=row.unpaid_break_minutes,
            exclude_shift_id=row.id,
        ).eligible
        for row in remaining
    )


def _alternative_lead_repair_exists(
    db: Session, *, request_employee_id: int, day: date,
    affected_shift_ids: set[int], simulated_by_employee: dict[int, tuple[SimulatedAssignment, ...]],
) -> tuple[bool, bool]:
    lead_candidates = [
        row for row in list_scheduling_candidates(db)
        if row.id != request_employee_id and row.scheduling_lead_capable
    ]
    remaining_positions = [
        row for row in _day_shifts(db, day)
        if row.id not in affected_shift_ids
        and not row.is_double_coverage and not row.manually_locked
    ]
    results = [
        evaluate_assignment(
            db, employee_id=employee.id, store_id=shift.store_id,
            shift_date=shift.shift_date, start_time=shift.start_time,
            end_time=shift.end_time, unpaid_break_minutes=shift.unpaid_break_minutes,
            exclude_shift_id=shift.id,
            simulated_assignments=simulated_by_employee.get(employee.id, ()),
        )
        for shift in remaining_positions for employee in lead_candidates
    ]
    legal = [row for row in results if row.eligible]
    return bool(legal), bool(legal and all(row.requires_hour_approval for row in legal))


def _projected_positions(
    db: Session, *, employee_id: int, day: date, request: TimeOffRequest,
) -> list[_Position]:
    profile = db.execute(select(EmployeeSchedulingProfile).where(
        EmployeeSchedulingProfile.employee_id == employee_id,
        EmployeeSchedulingProfile.active.is_(True))).scalar_one_or_none()
    if profile is None or is_base_workday(profile, day) is not True:
        return []
    defaults = db.get(SchedulingStoreDefaults, 1)
    if defaults is None or defaults.standard_shift_start is None or defaults.standard_shift_end is None:
        return []
    if (
        not request.full_day
        and not (request.start_time < defaults.standard_shift_end
                 and request.end_time > defaults.standard_shift_start)
    ):
        return []
    special_ids = set(db.execute(select(SpecialStorePolicy.store_id).where(
        SpecialStorePolicy.active.is_(True))).scalars())
    store_id = profile.home_store_id
    if store_id is None:
        store_id = db.execute(select(Store.id).where(
            Store.active.is_(True), Store.id.not_in(special_ids)
        ).order_by(Store.id).limit(1)).scalar_one_or_none()
    if store_id is None:
        return []
    requirement = db.execute(select(CoverageRequirement).where(
        CoverageRequirement.store_id == store_id,
        CoverageRequirement.day_of_week == scheduling_weekday(day),
        CoverageRequirement.active.is_(True),
        CoverageRequirement.minimum_employee_count > 0,
    ).order_by(CoverageRequirement.minimum_employee_count.desc(), CoverageRequirement.id).limit(1)).scalar_one_or_none()
    if requirement is None:
        return []
    return [_Position(
        shift_id=None, store_id=store_id, shift_date=day,
        start_time=defaults.standard_shift_start, end_time=defaults.standard_shift_end,
        unpaid_break_minutes=0, projected=True, required_position=True,
        double_coverage=False, longview=store_id in special_ids,
        lead_repair_required=True,
    )]


def _candidate_impact(
    db: Session, *, employee: Employee, position: _Position,
    simulated: tuple[SimulatedAssignment, ...], planning_date: date,
) -> CandidateImpact:
    result = evaluate_assignment(
        db, employee_id=employee.id, store_id=position.store_id,
        shift_date=position.shift_date, start_time=position.start_time,
        end_time=position.end_time, unpaid_break_minutes=position.unpaid_break_minutes,
        simulated_assignments=simulated)
    profile = db.execute(select(EmployeeSchedulingProfile).where(
        EmployeeSchedulingProfile.employee_id == employee.id,
        EmployeeSchedulingProfile.active.is_(True))).scalar_one_or_none()
    existing_count = scheduled_weekly_shift_count(
        db, employee_id=employee.id, shift_date=position.shift_date)
    week_start = position.shift_date - timedelta(days=(position.shift_date.weekday() + 1) % 7)
    simulated_count = sum(
        week_start <= row.shift_date <= week_start + timedelta(days=6)
        for row in simulated)
    resulting_count = existing_count + simulated_count + 1
    target = profile.target_shifts_per_week if profile and profile.target_shifts_per_week is not None else 3
    weekend = (weekend_fairness(
        db, employee_id=employee.id, weekday=position.shift_date.weekday(),
        before_date=position.shift_date, as_of_date=planning_date)
        if position.shift_date.weekday() in (5, 6) and not position.longview else None)
    state = db.execute(select(SpecialStoreRotationState).where(
        SpecialStoreRotationState.store_id == position.store_id,
        SpecialStoreRotationState.employee_id == employee.id)).scalar_one_or_none() if position.longview else None
    longview = (longview_rotation_fairness(
        db, employee_id=employee.id, store_id=position.store_id,
        before_date=position.shift_date, as_of_date=planning_date)
        if state is not None else None)
    return CandidateImpact(
        employee_id=employee.id, employee_name=employee.full_name,
        eligible=result.eligible,
        constraint_codes=tuple(reason.code for reason in result.reasons),
        scheduled_hours=result.scheduled_hours,
        added_hours=result.resulting_hours - result.scheduled_hours,
        resulting_hours=result.resulting_hours,
        approval_threshold_hours=result.approval_threshold_hours,
        requires_hour_approval=result.requires_hour_approval,
        existing_shift_count=existing_count + simulated_count,
        resulting_shift_count=resulting_count, target_shifts_per_week=target,
        beyond_target=resulting_count > target,
        creates_fourth_shift=resulting_count >= 4,
        lead_capable=employee.scheduling_lead_capable,
        base_pattern_expected=is_base_workday(profile, position.shift_date) if profile else None,
        weekend_historical_burden=weekend.historical_assignment_count if weekend else None,
        weekend_planned_burden=weekend.planned_future_assignment_count if weekend else None,
        longview_participation=state.participation.value if state else None,
        longview_historical_burden=longview.historical_assignment_count if longview else None,
        longview_planned_burden=longview.planned_future_assignment_count if longview else None,
    )


def _candidate_key(
    db: Session, *, candidate: CandidateImpact, position: _Position,
) -> tuple:
    preference, need = assignment_score(
        db, employee_id=candidate.employee_id, store_id=position.store_id,
        shift_date=position.shift_date)
    base = base_pattern_score(
        db, employee_id=candidate.employee_id, shift_date=position.shift_date)
    lead_penalty = 0 if not position.lead_repair_required or candidate.lead_capable else 1
    if position.longview:
        participation = {
            SpecialStoreParticipation.PRIMARY.value: 0,
            SpecialStoreParticipation.ROTATION.value: 1,
        }.get(candidate.longview_participation, 2)
        burden = (
            candidate.longview_historical_burden or 0,
            candidate.longview_planned_burden or 0,
        )
    else:
        participation = 0
        burden = (
            candidate.weekend_historical_burden or 0,
            candidate.weekend_planned_burden or 0,
        )
    return (
        lead_penalty, 1 if candidate.requires_hour_approval else 0,
        participation, burden, 1 if candidate.beyond_target else 0,
        -base, -need, -preference, candidate.employee_id,
    )


def _position_impact(
    db: Session, *, position: _Position, request_employee_id: int,
    simulated_by_employee: dict[int, tuple[SimulatedAssignment, ...]],
    planning_date: date,
) -> PositionImpact:
    employees = [row for row in list_scheduling_candidates(db) if row.id != request_employee_id]
    if position.double_coverage:
        employees = [row for row in employees if row.scheduling_double_coverage]
    candidates = tuple(_candidate_impact(
        db, employee=row, position=position,
        simulated=simulated_by_employee.get(row.id, ()), planning_date=planning_date)
        for row in employees)
    legal = [row for row in candidates if row.eligible]
    if position.lead_repair_required:
        lead_legal = [row for row in legal if row.lead_capable]
        if lead_legal:
            legal = lead_legal
        else:
            legal = []
    legal.sort(key=lambda row: _candidate_key(db, candidate=row, position=position))
    preferred = legal[0] if legal else None
    weekend_delta = None
    if preferred is not None and position.shift_date.weekday() in (5, 6) and not position.longview:
        request_burden = weekend_fairness(
            db, employee_id=request_employee_id, weekday=position.shift_date.weekday(),
            before_date=position.shift_date, as_of_date=planning_date)
        preferred_burden = (preferred.weekend_historical_burden or 0) + (preferred.weekend_planned_burden or 0)
        weekend_delta = preferred_burden - request_burden.assignment_count
    longview_repair_type = None
    if preferred is not None and position.longview:
        longview_repair_type = (
            'PRIMARY' if preferred.longview_participation == SpecialStoreParticipation.PRIMARY.value
            else 'ROTATION')
    eliminated = Counter()
    for candidate in candidates:
        if candidate.eligible and (not position.lead_repair_required or candidate.lead_capable):
            continue
        codes = candidate.constraint_codes
        if candidate.eligible and position.lead_repair_required and not candidate.lead_capable:
            eliminated['lead_capability'] += 1
        for bucket in {CONSTRAINT_BUCKETS.get(code, code.lower()) for code in codes}:
            eliminated[bucket] += 1
    if preferred is None:
        severity = 'CRITICAL' if position.required_position or position.lead_repair_required else 'MODERATE'
        unresolved = ('NO_ELIGIBLE_LEAD' if position.lead_repair_required
                      else 'NO_LEGAL_REPLACEMENT')
        explanation = (
            'No legal Lead-capable repair exists for this required date.'
            if position.lead_repair_required
            else 'No legal replacement exists for this required position.'
        )
    else:
        unresolved = None
        if preferred.requires_hour_approval or (weekend_delta is not None and weekend_delta >= 2):
            severity = 'HIGH'
            explanation = (
                f'Coverage is possible, but the best legal repair would produce '
                f'{preferred.resulting_hours} scheduled hours against a '
                f'{preferred.approval_threshold_hours}-hour approval threshold.'
                if preferred.requires_hour_approval else
                f'Coverage is possible, but the best legal repair already carries '
                f'{weekend_delta} more comparable weekend assignment(s).')
        elif (
            preferred.beyond_target or preferred.base_pattern_expected is False
            or (position.longview and preferred.longview_participation == SpecialStoreParticipation.ROTATION.value)
            or (position.shift_date.weekday() in (5, 6)
                and (preferred.weekend_historical_burden or 0) > 0)
            or len(legal) == 1
        ):
            severity = 'MODERATE'
            explanation = (
                f'A legal repair exists through {preferred.employee_name}, but it creates '
                'a visible target, base-pattern, fairness, Longview, or candidate-pool tradeoff.')
        else:
            severity = 'LOW'
            explanation = (
                f'{len(legal)} legal repair candidate(s) are available without '
                'approval-hour, Lead, Longview, or hard-constraint pressure.')
    store = db.get(Store, position.store_id)
    return PositionImpact(
        shift_id=position.shift_id, store_id=position.store_id,
        store_name=store.name if store else f'Store {position.store_id}',
        shift_date=position.shift_date, start_time=position.start_time,
        end_time=position.end_time, projected=position.projected,
        required_position=position.required_position,
        double_coverage=position.double_coverage, longview=position.longview,
        lead_repair_required=position.lead_repair_required,
        legal_candidate_count=len(legal),
        non_approval_candidate_count=sum(not row.requires_hour_approval for row in legal),
        eliminated_counts=tuple(sorted(eliminated.items())), candidates=candidates,
        preferred_candidate_id=preferred.employee_id if preferred else None,
        preferred_candidate_name=preferred.employee_name if preferred else None,
        preferred_requires_hour_approval=bool(preferred and preferred.requires_hour_approval),
        preferred_scheduled_hours=preferred.scheduled_hours if preferred else None,
        preferred_added_hours=preferred.added_hours if preferred else None,
        preferred_resulting_hours=preferred.resulting_hours if preferred else None,
        preferred_approval_threshold_hours=(
            preferred.approval_threshold_hours if preferred else None),
        preferred_resulting_shift_count=(preferred.resulting_shift_count if preferred else None),
        preferred_target_shifts_per_week=(preferred.target_shifts_per_week if preferred else None),
        weekend_burden_delta=weekend_delta,
        longview_repair_type=longview_repair_type,
        unresolved_constraint=unresolved, severity=severity, explanation=explanation,
    )


def operational_burden_for_request(
    db: Session, *, request_id: int, analysis_date: date,
) -> OperationalBurdenAnalysis:
    request = db.get(TimeOffRequest, request_id)
    if request is None:
        raise ValueError('Time-off request not found.')
    request_employee = db.get(Employee, request.employee_id)
    if request_employee is None:
        raise ValueError('Request employee not found.')
    special_ids = set(db.execute(select(SpecialStorePolicy.store_id).where(
        SpecialStorePolicy.active.is_(True))).scalars())
    all_target_shifts = effective_assignment_rows(
        db, employee_id=request.employee_id,
        start_date=request.start_date, end_date=request.end_date)
    affected_shifts = [row for row in all_target_shifts if _request_overlaps_shift(request, row)]
    affected_by_date: dict[date, list[ScheduleShift]] = defaultdict(list)
    for shift in affected_shifts:
        affected_by_date[shift.shift_date].append(shift)
    simulated_by_employee: dict[int, tuple[SimulatedAssignment, ...]] = {}
    preferred_repairs: list[tuple[int | None, int]] = []
    date_impacts: list[DateOperationalImpact] = []
    all_materialized = True
    schedule_context: list[tuple[date, int, int, str]] = []
    for day in _request_dates(request):
        period = _materialized_period(db, day)
        projected = period is None
        all_materialized = all_materialized and not projected
        if period is not None:
            schedule_context.append((
                day, period.id, period.revision_number, period.status.value))
        day_shifts = _day_shifts(db, day)
        affected_ids = {row.id for row in affected_by_date.get(day, [])}
        positions = [
            _Position(
                shift_id=row.id, store_id=row.store_id, shift_date=row.shift_date,
                start_time=row.start_time, end_time=row.end_time,
                unpaid_break_minutes=row.unpaid_break_minutes,
                projected=False, required_position=_required_position(db, row, day_shifts),
                double_coverage=row.is_double_coverage,
                longview=row.store_id in special_ids,
                lead_repair_required=False,
            )
            for row in affected_by_date.get(day, [])
        ]
        if projected:
            positions = _projected_positions(
                db, employee_id=request.employee_id, day=day, request=request)
        remaining_day_shifts = [row for row in day_shifts if row.id not in affected_ids]
        needs_staffing = bool(remaining_day_shifts or any(row.required_position for row in positions))
        lead_required = needs_staffing and (
            projected or _lead_repair_required(
                db, request_employee_id=request.employee_id, day=day,
                affected_shift_ids=affected_ids))
        alternative_lead_repair = False
        alternative_lead_requires_approval = False
        if lead_required and not projected:
            alternative_lead_repair, alternative_lead_requires_approval = (
                _alternative_lead_repair_exists(
                    db, request_employee_id=request.employee_id, day=day,
                    affected_shift_ids=affected_ids,
                    simulated_by_employee=simulated_by_employee))
        if lead_required and not alternative_lead_repair and positions:
            first = positions[0]
            positions[0] = _Position(
                shift_id=first.shift_id, store_id=first.store_id,
                shift_date=first.shift_date, start_time=first.start_time,
                end_time=first.end_time,
                unpaid_break_minutes=first.unpaid_break_minutes,
                projected=first.projected, required_position=first.required_position,
                double_coverage=first.double_coverage, longview=first.longview,
                lead_repair_required=True)
        position_impacts: list[PositionImpact] = []
        for position in positions:
            impact = _position_impact(
                db, position=position, request_employee_id=request.employee_id,
                simulated_by_employee=simulated_by_employee,
                planning_date=analysis_date)
            position_impacts.append(impact)
            if impact.preferred_candidate_id is not None:
                assignment = SimulatedAssignment(
                    shift_date=position.shift_date, start_time=position.start_time,
                    end_time=position.end_time,
                    unpaid_break_minutes=position.unpaid_break_minutes)
                simulated_by_employee[impact.preferred_candidate_id] = (
                    simulated_by_employee.get(impact.preferred_candidate_id, ()) + (assignment,)
                )
                preferred_repairs.append((position.shift_id, impact.preferred_candidate_id))
        severity = max(
            (row.severity for row in position_impacts),
            key=lambda value: SEVERITY_ORDER[value], default='LOW')
        if alternative_lead_requires_approval and SEVERITY_ORDER[severity] < SEVERITY_ORDER['HIGH']:
            severity = 'HIGH'
        elif alternative_lead_repair and SEVERITY_ORDER[severity] < SEVERITY_ORDER['MODERATE']:
            severity = 'MODERATE'
        eliminated = Counter()
        for position in position_impacts:
            eliminated.update(dict(position.eliminated_counts))
        required = sum(row.required_position for row in position_impacts)
        uncovered = sum(row.required_position and row.preferred_candidate_id is None
                        for row in position_impacts)
        coverable = required - uncovered
        lead_impact = (
            'NO_ELIGIBLE_LEAD' if any(row.unresolved_constraint == 'NO_ELIGIBLE_LEAD' for row in position_impacts)
            else (('LEGAL_LEAD_REASSIGNMENT_REQUIRES_APPROVAL'
                   if alternative_lead_requires_approval else 'LEGAL_LEAD_REASSIGNMENT')
                  if alternative_lead_repair else ('LEGAL_LEAD_REPAIR' if lead_required else 'UNCHANGED')))
        longview_positions = [row for row in position_impacts if row.longview]
        longview_impact = (
            'UNCOVERED_LONGVIEW' if any(row.preferred_candidate_id is None for row in longview_positions)
            else ((
                'LONGVIEW_PRIMARY_REPAIR'
                if any(row.longview_repair_type == 'PRIMARY' for row in longview_positions)
                else 'LONGVIEW_ROTATION_REPAIR') if longview_positions else 'UNCHANGED'))
        weekend_positions = [row for row in position_impacts if row.shift_date.weekday() in (5, 6) and not row.longview]
        weekend_impact = (
            'WORSENS_WEEKEND_DISTRIBUTION'
            if any((row.weekend_burden_delta or 0) > 0 for row in weekend_positions)
            else ('VANCOUVER_WEEKEND_REASSIGNMENT_NO_GREATER_BURDEN'
                  if weekend_positions else 'UNCHANGED'))
        double_positions = [row for row in position_impacts if row.double_coverage]
        double_impact = (
            'DOUBLE_COVERAGE_UNFILLED' if any(row.preferred_candidate_id is None for row in double_positions)
            else ('DOUBLE_COVERAGE_REPAIR_REQUIRED' if double_positions else 'UNCHANGED'))
        profile = db.execute(select(EmployeeSchedulingProfile).where(
            EmployeeSchedulingProfile.employee_id == request.employee_id)).scalar_one_or_none()
        target = profile.target_shifts_per_week if profile and profile.target_shifts_per_week is not None else 3
        remaining_count = max(0, scheduled_weekly_shift_count(
            db, employee_id=request.employee_id, shift_date=day) - len(affected_by_date.get(day, [])))
        target_displacement = (
            f'Request employee would retain {remaining_count} of {target} target shifts in this week.'
            if positions else 'No materialized or projected target shift is affected.')
        explanation = (
            'No materialized assignment or projected base-pattern coverage position is affected.'
            if not positions else '; '.join(row.explanation for row in position_impacts))
        if alternative_lead_repair:
            explanation += (
                '; Lead coverage also requires reassigning an unlocked existing position'
                + (' through an hour-approval path.' if alternative_lead_requires_approval else '.'))
        date_impacts.append(DateOperationalImpact(
            request_date=day, projected=projected,
            scheduled_shift_count=len(affected_by_date.get(day, [])),
            required_positions_affected=required, coverable_positions=coverable,
            uncovered_positions=uncovered,
            coworker_pto_eliminations=eliminated['pto'],
            lockout_eliminations=eliminated['lockout'],
            never_store_eliminations=eliminated['never_store'],
            consecutive_day_eliminations=eliminated['consecutive_days'],
            approval_pressure_positions=(
                sum(row.preferred_requires_hour_approval for row in position_impacts)
                + int(alternative_lead_requires_approval)),
            lead_impact=lead_impact, longview_impact=longview_impact,
            weekend_fairness_impact=weekend_impact,
            double_coverage_impact=double_impact,
            target_shift_displacement=target_displacement,
            positions=tuple(position_impacts), severity=severity,
            explanation=explanation,
        ))
    severity = max(
        (row.severity for row in date_impacts),
        key=lambda value: SEVERITY_ORDER[value], default='LOW')
    hard_constraints = tuple(sorted({
        position.unresolved_constraint
        for day in date_impacts for position in day.positions
        if position.unresolved_constraint
    }))
    required = sum(row.required_positions_affected for row in date_impacts)
    coverable = sum(row.coverable_positions for row in date_impacts)
    uncovered = sum(row.uncovered_positions for row in date_impacts)
    approvals = sum(row.approval_pressure_positions for row in date_impacts)
    explanation = (
        f'{severity} operational scheduling pressure across {len(date_impacts)} requested date(s): '
        f'{coverable} of {required} affected required position(s) have a simulated legal repair; '
        f'{uncovered} remain uncovered and {approvals} preferred repair(s) require hour approval. '
        'This simulation does not approve, deny, or mutate the request or schedule.')
    return OperationalBurdenAnalysis(
        request_id=request.id, employee_id=request.employee_id,
        analysis_date=analysis_date, request_start=request.start_date,
        request_end=request.end_date,
        inside_materialized_horizon=all_materialized,
        schedule_context=tuple(schedule_context),
        date_impacts=tuple(date_impacts), severity=severity,
        required_positions_affected=required, coverable_positions=coverable,
        uncovered_positions=uncovered, approval_pressure_positions=approvals,
        preferred_repairs=tuple(preferred_repairs),
        hard_constraints=hard_constraints, explanation=explanation,
    )
