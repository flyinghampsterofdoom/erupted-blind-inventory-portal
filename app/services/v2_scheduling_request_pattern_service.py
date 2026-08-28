from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time, timedelta, timezone
from statistics import mean

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Employee, EmployeeSchedulingProfile, EmployeeSchedulingWindow,
    SchedulePeriod, SchedulePeriodStatus, ScheduleShift, SchedulingWindowKind,
    SpecialStoreParticipation, SpecialStorePolicy, TimeOffRequest, TimeOffRequestStatus,
)


@dataclass(frozen=True)
class WeekendDayPattern:
    eligible_dates: tuple[date, ...]
    requested_dates: tuple[date, ...]
    worked_dates: tuple[date, ...]
    longview_excluded_dates: tuple[date, ...]

    @property
    def eligible_count(self) -> int: return len(self.eligible_dates)
    @property
    def requested_count(self) -> int: return len(self.requested_dates)
    @property
    def worked_count(self) -> int: return len(self.worked_dates)
    @property
    def request_share(self) -> float:
        return round(self.requested_count / self.eligible_count, 4) if self.eligible_count else 0.0


@dataclass(frozen=True)
class PeerNormalization:
    employee_id: int
    eligible_weekend_count: int
    worked_weekend_count: int
    target_shifts_per_week: int
    target_factor: float
    normalized_work_rate: float


@dataclass(frozen=True)
class RequestPatternAnalysis:
    employee_id: int
    window_start: date
    window_end: date
    saturday: WeekendDayPattern
    sunday: WeekendDayPattern
    requested_fridays: tuple[date, ...]
    full_weekend_blocks: int
    friday_weekend_clusters: int
    target_shifts_per_week: int
    peer_employee_ids: tuple[int, ...]
    peer_normalization: tuple[PeerNormalization, ...]
    peer_expected_weekend_work: float
    employee_weekend_work: int
    variance_from_peer_expectation: float
    shared_burden_indicator: float
    classification: str
    explanation: str
    permanent_exemption_exclusions: tuple[date, ...]
    peer_excluded_reason: str | None = None

    @property
    def eligible_weekend_count(self) -> int:
        return self.saturday.eligible_count + self.sunday.eligible_count

    @property
    def requested_weekend_count(self) -> int:
        return self.saturday.requested_count + self.sunday.requested_count

    @property
    def weekend_request_share(self) -> float:
        return round(self.requested_weekend_count / self.eligible_weekend_count, 4) if self.eligible_weekend_count else 0.0


def _dates(start: date, end: date, python_weekday: int) -> tuple[date, ...]:
    return tuple(start + timedelta(days=offset) for offset in range((end - start).days + 1)
                 if (start + timedelta(days=offset)).weekday() == python_weekday)


def _requested_dates(
    db: Session, employee_id: int, start: date, end: date,
    exclude_request_id: int | None = None,
) -> set[date]:
    statement = select(TimeOffRequest).where(
        TimeOffRequest.employee_id == employee_id,
        TimeOffRequest.status.in_((TimeOffRequestStatus.PENDING, TimeOffRequestStatus.APPROVED)),
        TimeOffRequest.start_date <= end, TimeOffRequest.end_date >= start)
    if exclude_request_id is not None:
        statement = statement.where(TimeOffRequest.id != exclude_request_id)
    rows = db.execute(statement).scalars()
    result: set[date] = set()
    for row in rows:
        current = max(start, row.start_date)
        through = min(end, row.end_date)
        while current <= through:
            result.add(current); current += timedelta(days=1)
    return result


def _raw_pattern(
    db: Session, *, employee: Employee, profile: EmployeeSchedulingProfile | None,
    start: date, end: date, extra_requested_dates: set[date] | None = None,
    exclude_request_id: int | None = None,
) -> dict:
    introduced = employee.created_at.astimezone(timezone.utc).date() if employee.created_at else start
    eligibility_start = max(start, introduced)
    lockouts = set(db.execute(select(EmployeeSchedulingWindow.day_of_week).where(
        EmployeeSchedulingWindow.employee_id == employee.id,
        EmployeeSchedulingWindow.kind == SchedulingWindowKind.HARD_UNAVAILABLE,
        EmployeeSchedulingWindow.active.is_(True),
        EmployeeSchedulingWindow.start_time == time.min,
        EmployeeSchedulingWindow.end_time == time.max)).scalars())
    participating = employee.active and employee.scheduling_active
    saturday_all = _dates(eligibility_start, end, 5) if participating and eligibility_start <= end else ()
    sunday_all = _dates(eligibility_start, end, 6) if participating and eligibility_start <= end else ()
    saturday = () if 6 in lockouts else saturday_all
    sunday = () if 0 in lockouts else sunday_all
    exclusions = (() if 6 not in lockouts else saturday_all) + (() if 0 not in lockouts else sunday_all)
    requests = _requested_dates(
        db, employee.id, start, end, exclude_request_id=exclude_request_id
    ) | (extra_requested_dates or set())
    special_ids = set(db.execute(select(SpecialStorePolicy.store_id).where(
        SpecialStorePolicy.active.is_(True))).scalars())
    shifts = list(db.execute(select(ScheduleShift).join(SchedulePeriod).where(
        ScheduleShift.employee_id == employee.id,
        ScheduleShift.shift_date.between(start, end),
        SchedulePeriod.status == SchedulePeriodStatus.PUBLISHED)).scalars())
    longview = {row.shift_date for row in shifts if row.store_id in special_ids and row.shift_date.weekday() in (5, 6)}
    worked = {row.shift_date for row in shifts if row.store_id not in special_ids and row.shift_date.weekday() in (5, 6)}
    sat_set, sun_set = set(saturday), set(sunday)
    return {
        'saturday': WeekendDayPattern(tuple(saturday), tuple(sorted(requests & sat_set)), tuple(sorted(worked & sat_set)), tuple(sorted(longview & sat_set))),
        'sunday': WeekendDayPattern(tuple(sunday), tuple(sorted(requests & sun_set)), tuple(sorted(worked & sun_set)), tuple(sorted(longview & sun_set))),
        'requests': requests,
        'exclusions': tuple(sorted(exclusions)),
        'target': max(0, profile.target_shifts_per_week if profile and profile.target_shifts_per_week is not None else 3),
    }


def weekend_request_pattern(
    db: Session, *, employee_id: int, as_of_date: date,
    window_weeks: int = 12, extra_requested_dates: set[date] | None = None,
    exclude_request_id: int | None = None,
) -> RequestPatternAnalysis:
    end = as_of_date - timedelta(days=1)
    start = as_of_date - timedelta(weeks=window_weeks)
    employee = db.get(Employee, employee_id)
    if employee is None: raise ValueError('Employee not found.')
    profiles = {row.employee_id: row for row in db.execute(select(EmployeeSchedulingProfile)).scalars()}
    raw = _raw_pattern(db, employee=employee, profile=profiles.get(employee.id), start=start, end=end,
                       extra_requested_dates=extra_requested_dates,
                       exclude_request_id=exclude_request_id)
    peers = list(db.execute(select(Employee).where(
        Employee.active.is_(True), Employee.scheduling_active.is_(True), Employee.id != employee.id
    ).order_by(Employee.id)).scalars())
    peer_rows = []
    peer_ids = []
    peer_normalization = []
    for peer in peers:
        profile = profiles.get(peer.id)
        if profile and profile.special_store_participation == SpecialStoreParticipation.PRIMARY: continue
        peer_raw = _raw_pattern(db, employee=peer, profile=profile, start=start, end=end)
        eligible = peer_raw['saturday'].eligible_count + peer_raw['sunday'].eligible_count
        target_factor = peer_raw['target'] / 3 if peer_raw['target'] else 0
        if not eligible or not target_factor: continue
        worked = peer_raw['saturday'].worked_count + peer_raw['sunday'].worked_count
        normalized_rate = worked / eligible / target_factor
        peer_rows.append(normalized_rate)
        peer_ids.append(peer.id)
        peer_normalization.append(PeerNormalization(
            employee_id=peer.id, eligible_weekend_count=eligible,
            worked_weekend_count=worked, target_shifts_per_week=peer_raw['target'],
            target_factor=round(target_factor, 4),
            normalized_work_rate=round(normalized_rate, 4)))
    eligible = raw['saturday'].eligible_count + raw['sunday'].eligible_count
    target_factor = raw['target'] / 3 if raw['target'] else 0
    peer_rate = mean(peer_rows) if peer_rows else 0.0
    expected = round(peer_rate * eligible * target_factor, 2)
    worked = raw['saturday'].worked_count + raw['sunday'].worked_count
    variance = round(worked - expected, 2)
    requested = raw['saturday'].requested_count + raw['sunday'].requested_count
    share = requested / eligible if eligible else 0.0
    deficit_ratio = max(0.0, expected - worked) / expected if expected else 0.0
    # A complete-block signal only exists when both requested days belonged to
    # this employee's legitimate burden pool. A permanent Sunday exemption,
    # for example, cannot turn an adjoining Saturday request into a full block.
    requested_saturdays = set(raw['saturday'].requested_dates)
    requested_sundays = set(raw['sunday'].requested_dates)
    blocks = sum(day + timedelta(days=1) in requested_sundays for day in requested_saturdays)
    fridays = tuple(sorted(day for day in raw['requests'] if start <= day <= end and day.weekday() == 4))
    clusters = sum(
        day + timedelta(days=1) in requested_saturdays
        and day + timedelta(days=2) in requested_sundays
        for day in fridays)
    if share >= .70 and blocks >= 3 and deficit_ratio >= .50: classification = 'SEVERE'
    elif share >= .50 and (blocks >= 2 or deficit_ratio >= .35): classification = 'MATERIAL'
    elif share >= .35 or blocks >= 2 or deficit_ratio >= .25: classification = 'WATCH'
    else: classification = 'NORMAL'
    shared = round(min(float(requested), max(0.0, expected - worked)), 2)
    attention_explanation = {
        'SEVERE': 'This pattern is materially reducing the employee’s share of Vancouver weekend work and is associated with additional shared weekend obligation for coworkers.',
        'MATERIAL': 'This pattern shows a material reduction in weekend participation relative to the visible request and peer components.',
        'WATCH': 'One or more visible components warrant management attention, while the signal remains non-disciplinary.',
        'NORMAL': 'The visible request and peer-participation components do not cross an attention threshold.',
    }[classification]
    explanation = (
        f'Over the trailing {window_weeks} weeks, this employee was eligible for {eligible} Vancouver weekend days, '
        f'requested {requested} ({share:.0%}), and worked {worked}. Comparable peers imply approximately '
        f'{expected:.2f} weekend assignments after target-shift normalization. '
        f'{blocks} complete weekend block(s) and {clusters} Friday–Sunday cluster(s) were requested. '
        f'{attention_explanation} This is a transparent management-attention signal, not automatic discipline or PTO denial.')
    profile = profiles.get(employee.id)
    excluded_reason = ('Permanent Longview-primary staff are excluded from the Vancouver peer pool.'
                       if profile and profile.special_store_participation == SpecialStoreParticipation.PRIMARY else None)
    return RequestPatternAnalysis(
        employee.id, start, end, raw['saturday'], raw['sunday'], fridays, blocks, clusters,
        raw['target'], tuple(peer_ids), tuple(peer_normalization), expected, worked, variance, shared,
        classification, explanation, raw['exclusions'], excluded_reason)


def projected_weekend_request_impact(
    db: Session, *, employee_id: int, as_of_date: date,
    request_start: date, request_end: date, window_weeks: int = 12,
    request_id: int | None = None,
) -> dict:
    dates = {request_start + timedelta(days=i) for i in range((request_end - request_start).days + 1)}
    projection_as_of = max(as_of_date, request_end + timedelta(days=1))
    before = weekend_request_pattern(
        db, employee_id=employee_id, as_of_date=projection_as_of,
        window_weeks=window_weeks, exclude_request_id=request_id)
    after = weekend_request_pattern(db, employee_id=employee_id, as_of_date=projection_as_of,
                                    window_weeks=window_weeks, extra_requested_dates=dates,
                                    exclude_request_id=request_id)
    return {'before': before, 'after': after,
            'saturday_share_before': before.saturday.request_share,
            'saturday_share_after': after.saturday.request_share,
            'sunday_share_before': before.sunday.request_share,
            'sunday_share_after': after.sunday.request_share,
            'weekend_share_before': before.weekend_request_share,
            'weekend_share_after': after.weekend_request_share}
