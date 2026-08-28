from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AuditLog, Employee, Principal as PrincipalModel, SchedulePeriod,
    SchedulePeriodStatus, ScheduleShift, TimeOffReasonCategory, TimeOffRequest,
    TimeOffRequestStatus,
)
from app.services.v2_scheduling_operational_burden_service import (
    OperationalBurdenAnalysis, operational_burden_for_request,
)
from app.services.v2_scheduling_request_pattern_service import (
    RequestPatternAnalysis, projected_weekend_request_impact,
    weekend_request_pattern,
)


@dataclass(frozen=True)
class TimeOffScheduleImpact:
    period_id: int
    status: str
    revision_number: int
    shift_ids: tuple[int, ...]
    lead_shift_ids: tuple[int, ...]


@dataclass(frozen=True)
class TimeOffReviewContext:
    request: TimeOffRequest
    employee: Employee
    reason_category: TimeOffReasonCategory
    fairness: RequestPatternAnalysis
    fairness_projection: dict | None
    fairness_classification: str
    operational: OperationalBurdenAnalysis
    hard_warning_codes: tuple[str, ...]
    schedule_impacts: tuple[TimeOffScheduleImpact, ...]
    acknowledgement_required: bool
    approval_blocked: bool


def _request_dates(row: TimeOffRequest) -> tuple[date, ...]:
    return tuple(
        row.start_date + timedelta(days=offset)
        for offset in range((row.end_date - row.start_date).days + 1)
    )


def _overlaps_request(row: TimeOffRequest, shift: ScheduleShift) -> bool:
    if not row.start_date <= shift.shift_date <= row.end_date:
        return False
    if row.full_day:
        return True
    return shift.shift_date == row.start_date and (
        row.start_time < shift.end_time and row.end_time > shift.start_time
    )


def time_off_review_context(
    db: Session, *, request_id: int, analysis_date: date,
) -> TimeOffReviewContext:
    row = db.get(TimeOffRequest, request_id)
    if row is None:
        raise ValueError('Time-off request not found.')
    employee = db.get(Employee, row.employee_id)
    category = db.get(TimeOffReasonCategory, row.reason_category_id)
    if employee is None or category is None:
        raise ValueError('Time-off request references unavailable employee data.')
    fairness = weekend_request_pattern(
        db, employee_id=row.employee_id, as_of_date=analysis_date)
    weekend_related = any(day.weekday() in (5, 6) for day in _request_dates(row))
    projection = projected_weekend_request_impact(
        db, employee_id=row.employee_id, as_of_date=analysis_date,
        request_start=row.start_date, request_end=row.end_date,
        request_id=row.id,
    ) if weekend_related else None
    # Pending and approved requests are already part of the current signal. The
    # explicit projection is retained to show the before/after decision context.
    fairness_classification = (
        projection['after'].classification if projection else fairness.classification
    )
    operational = operational_burden_for_request(
        db, request_id=row.id, analysis_date=analysis_date)
    hard_codes = tuple(sorted({
        position.unresolved_constraint
        for day_impact in operational.date_impacts
        for position in day_impact.positions
        if position.unresolved_constraint
        and (position.required_position or position.lead_repair_required)
    }))
    periods = list(db.execute(select(SchedulePeriod).where(
        SchedulePeriod.week_start_date <= row.end_date,
        SchedulePeriod.week_end_date >= row.start_date,
        SchedulePeriod.status.in_((
            SchedulePeriodStatus.DRAFT, SchedulePeriodStatus.PUBLISHED)),
    ).order_by(SchedulePeriod.week_start_date, SchedulePeriod.id)).scalars())
    impacts: list[TimeOffScheduleImpact] = []
    for period in periods:
        shifts = [shift for shift in db.execute(select(ScheduleShift).where(
            ScheduleShift.schedule_period_id == period.id,
            ScheduleShift.employee_id == row.employee_id,
            ScheduleShift.shift_date.between(row.start_date, row.end_date),
        ).order_by(ScheduleShift.shift_date, ScheduleShift.id)).scalars()
                  if _overlaps_request(row, shift)]
        if shifts:
            impacts.append(TimeOffScheduleImpact(
                period_id=period.id, status=period.status.value,
                revision_number=period.revision_number,
                shift_ids=tuple(shift.id for shift in shifts),
                lead_shift_ids=tuple(
                    shift.id for shift in shifts if shift.is_lead_of_day),
            ))
    acknowledgement_required = (
        fairness_classification in {'MATERIAL', 'SEVERE'}
        or operational.severity in {'HIGH', 'CRITICAL'}
    )
    return TimeOffReviewContext(
        request=row, employee=employee, reason_category=category,
        fairness=fairness, fairness_projection=projection,
        fairness_classification=fairness_classification,
        operational=operational, hard_warning_codes=hard_codes,
        schedule_impacts=tuple(impacts),
        acknowledgement_required=acknowledgement_required,
        # Existing Scheduling semantics permit management to approve PTO and
        # surface uncovered draft/published obligations for explicit handling.
        # Operational CRITICAL is therefore a hard warning, not a newly invented
        # non-overridable leave-policy rule.
        approval_blocked=False,
    )


def time_off_decision_history(db: Session, *, request_id: int) -> list[dict]:
    rows = list(db.execute(select(AuditLog).where(
        AuditLog.action == 'V2:SCHEDULING:TIME_OFF_REVIEWED',
        AuditLog.meta['entity_type'].as_string() == 'time_off_request',
        AuditLog.meta['entity_id'].as_string() == str(request_id),
    ).order_by(AuditLog.created_at.desc(), AuditLog.id.desc())).scalars())
    actor_ids = {row.actor_principal_id for row in rows if row.actor_principal_id}
    actors = {
        actor.id: actor.username for actor in db.execute(select(PrincipalModel).where(
            PrincipalModel.id.in_(actor_ids))).scalars()
    } if actor_ids else {}
    return [{
        'created_at': row.created_at,
        'actor': actors.get(row.actor_principal_id, f'Principal {row.actor_principal_id}'),
        'before': (row.meta.get('before') or {}).get('status'),
        'after': (row.meta.get('after') or {}).get('status'),
        'reason': row.meta.get('reason'),
        'context': row.meta.get('metadata') or {},
    } for row in rows]
