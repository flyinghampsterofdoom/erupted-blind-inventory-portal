from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.auth import Principal
from app.models import (
    Employee, EmployeeSchedulingProfile, SchedulePeriod, SchedulePeriodStatus, ScheduleShift,
    SchedulingStoreDefaults, SpecialStorePolicy, Store,
)
from app.services.v2_scheduling_roster_service import is_scheduling_candidate, list_scheduling_candidates
from app.services.v2_scheduling_service import SchedulingValidationError, scheduled_paid_minutes
from app.services.v2_scheduling_pattern_service import is_base_workday
from app.v2.audit import V2AuditEvent, write_v2_audit_event


@dataclass(frozen=True)
class AssignmentFairness:
    assignment_count: int
    last_assignment_date: date | None


@dataclass(frozen=True)
class LeadDesignationFairness:
    historical_assignment_count: int
    last_historical_assignment_date: date | None
    planned_future_assignment_count: int
    current_week_assignment_count: int


def _now() -> datetime:
    return datetime.now(timezone.utc)


def get_store_defaults(db: Session) -> SchedulingStoreDefaults | None:
    return db.get(SchedulingStoreDefaults, 1)


def set_double_coverage_store(
    db: Session, *, principal: Principal, store_id: int | None,
) -> SchedulingStoreDefaults:
    row = get_store_defaults(db)
    return update_store_defaults(
        db, principal=principal, store_id=store_id,
        standard_shift_start=row.standard_shift_start if row else None,
        standard_shift_end=row.standard_shift_end if row else None,
        require_standard_shift=False,
    )


def update_store_defaults(
    db: Session, *, principal: Principal, store_id: int | None,
    standard_shift_start: time | None, standard_shift_end: time | None,
    require_standard_shift: bool = True,
) -> SchedulingStoreDefaults:
    store = db.get(Store, store_id) if store_id is not None else None
    if store_id is not None and (store is None or not store.active):
        raise SchedulingValidationError('Choose an active Double Coverage Store.')
    if require_standard_shift and (standard_shift_start is None or standard_shift_end is None):
        raise SchedulingValidationError('Standard shift start and end times are required.')
    if ((standard_shift_start is None) != (standard_shift_end is None)
            or standard_shift_start is not None and standard_shift_end <= standard_shift_start):
        raise SchedulingValidationError('Standard shift end must be later than its start time.')
    row = db.execute(select(SchedulingStoreDefaults).where(
        SchedulingStoreDefaults.id == 1).with_for_update()).scalar_one_or_none()
    before = {
        'double_coverage_store_id': row.double_coverage_store_id if row else None,
        'standard_shift_start': (
            row.standard_shift_start.isoformat() if row and row.standard_shift_start else None),
        'standard_shift_end': (
            row.standard_shift_end.isoformat() if row and row.standard_shift_end else None),
    }
    if row is None:
        row = SchedulingStoreDefaults(id=1, updated_by_principal_id=principal.id)
        db.add(row)
    row.double_coverage_store_id = store_id
    row.standard_shift_start = standard_shift_start
    row.standard_shift_end = standard_shift_end
    row.updated_by_principal_id = principal.id
    row.updated_at = _now()
    after = {
        'double_coverage_store_id': store_id,
        'standard_shift_start': standard_shift_start.isoformat() if standard_shift_start else None,
        'standard_shift_end': standard_shift_end.isoformat() if standard_shift_end else None,
    }
    if before != after:
        write_v2_audit_event(db, event=V2AuditEvent(
            actor_principal_id=principal.id, action='SCHEDULING_STORE_DEFAULTS_CHANGED',
            domain='SCHEDULING', entity_type='scheduling_store_defaults', entity_id=1,
            timestamp=_now(), before=before, after=after,
        ), ip=None)
    db.flush()
    return row


def _designation_fairness(
    db: Session, *, employee_id: int, before_date: date, field,
    current_period_id: int | None = None,
) -> AssignmentFairness:
    rows = db.execute(select(ScheduleShift.shift_date).distinct().join(SchedulePeriod).where(
        ScheduleShift.employee_id == employee_id,
        field.is_(True),
        ScheduleShift.shift_date < before_date,
        ScheduleShift.shift_date >= before_date - timedelta(weeks=12),
        SchedulePeriod.status.in_((
            SchedulePeriodStatus.DRAFT, SchedulePeriodStatus.PUBLISHED,
            SchedulePeriodStatus.ARCHIVED)),
    ).order_by(ScheduleShift.shift_date)).scalars().all()
    return AssignmentFairness(len(rows), max(rows) if rows else None)


def lead_fairness(
    db: Session, *, employee_id: int, before_date: date,
    planning_date: date | None = None, current_period_id: int | None = None,
) -> LeadDesignationFairness:
    """Separate durable Lead history, planned horizon burden, and target-week burden."""
    planning_date = min(planning_date or before_date, before_date)
    history_start = planning_date - timedelta(weeks=12)
    rows = list(db.execute(select(ScheduleShift, SchedulePeriod).join(SchedulePeriod).where(
        ScheduleShift.employee_id == employee_id,
        ScheduleShift.is_lead_of_day.is_(True),
        ScheduleShift.shift_date >= history_start,
        ScheduleShift.shift_date < before_date,
        SchedulePeriod.status.in_((SchedulePeriodStatus.DRAFT, SchedulePeriodStatus.PUBLISHED)),
    )).all())
    period_by_week: dict[date, SchedulePeriod] = {}
    for _shift, period in rows:
        selected = period_by_week.get(period.week_start_date)
        if selected is None or (
            selected.status != SchedulePeriodStatus.PUBLISHED
            and (period.status == SchedulePeriodStatus.PUBLISHED
                 or period.revision_number > selected.revision_number)
        ):
            period_by_week[period.week_start_date] = period
    effective = [(shift, period) for shift, period in rows
                 if period_by_week.get(period.week_start_date) is period]
    historical_dates = {
        shift.shift_date for shift, period in effective
        if shift.shift_date < planning_date
        and period.status == SchedulePeriodStatus.PUBLISHED
    }
    current_week_dates = {
        shift.shift_date for shift, period in effective
        if current_period_id is not None and period.id == current_period_id
    }
    planned_dates = {
        shift.shift_date for shift, period in effective
        if planning_date <= shift.shift_date < before_date
        and (current_period_id is None or period.id != current_period_id)
    }
    return LeadDesignationFairness(
        historical_assignment_count=len(historical_dates),
        last_historical_assignment_date=max(historical_dates) if historical_dates else None,
        planned_future_assignment_count=len(planned_dates),
        current_week_assignment_count=len(current_week_dates),
    )


def double_coverage_fairness(db: Session, *, employee_id: int, before_date: date) -> AssignmentFairness:
    return _designation_fairness(
        db, employee_id=employee_id, before_date=before_date, field=ScheduleShift.is_double_coverage)


def ensure_daily_lead_staffing(
    db: Session, *, principal: Principal, schedule_period_id: int,
    planning_date: date | None = None, diagnostics: list[dict] | None = None,
) -> list[dict]:
    """Repair generated staffing so every staffed day contains a valid Lead.

    Only unlocked ordinary shifts may be reassigned; manual locks and explicit
    Double Coverage assignments remain authoritative.
    """
    shifts = list(db.execute(select(ScheduleShift).where(
        ScheduleShift.schedule_period_id == schedule_period_id).order_by(
        ScheduleShift.shift_date, ScheduleShift.start_time, ScheduleShift.id).with_for_update()).scalars())
    special_store_ids = set(db.execute(select(SpecialStorePolicy.store_id).where(
        SpecialStorePolicy.active.is_(True))).scalars())
    lead_candidates = [row for row in list_scheduling_candidates(db) if row.scheduling_lead_capable]
    employee_by_id = {row.id: row for row in lead_candidates}
    by_date: dict[date, list[ScheduleShift]] = defaultdict(list)
    for shift in shifts:
        by_date[shift.shift_date].append(shift)
    unresolved: list[dict] = []
    from app.services.v2_scheduling_policy_service import (
        _below_target_priority,
        assignment_score,
        base_pattern_score,
        evaluate_assignment,
        weekend_fairness,
    )
    for day, day_shifts in by_date.items():
        has_valid_lead = False
        for row in day_shifts:
            if row.employee_id not in employee_by_id:
                continue
            eligibility = evaluate_assignment(
                db, employee_id=row.employee_id, store_id=row.store_id,
                shift_date=row.shift_date, start_time=row.start_time, end_time=row.end_time,
                unpaid_break_minutes=row.unpaid_break_minutes, exclude_shift_id=row.id)
            if eligibility.eligible:
                has_valid_lead = True
                break
        if has_valid_lead:
            continue
        options = []
        failures: list[str] = []
        for shift in day_shifts:
            if shift.manually_locked or shift.is_double_coverage:
                continue
            for employee in lead_candidates:
                eligibility = evaluate_assignment(
                    db, employee_id=employee.id, store_id=shift.store_id,
                    shift_date=shift.shift_date, start_time=shift.start_time,
                    end_time=shift.end_time, unpaid_break_minutes=shift.unpaid_break_minutes,
                    exclude_shift_id=shift.id)
                if not eligibility.eligible or eligibility.requires_hour_approval:
                    failures.extend(reason.code for reason in eligibility.reasons)
                    if eligibility.eligible and eligibility.requires_hour_approval:
                        failures.append('WEEKLY_HOURS_APPROVAL_REQUIRED')
                    continue
                assignment = assignment_score(
                    db, employee_id=employee.id, store_id=shift.store_id,
                    shift_date=shift.shift_date)
                weekend = (weekend_fairness(
                    db, employee_id=employee.id, weekday=day.weekday(),
                    before_date=day, as_of_date=planning_date)
                    if day.weekday() in (5, 6) else None)
                options.append((
                    1 if shift.store_id in special_store_ids else 0,
                    weekend.historical_assignment_count if weekend else 0,
                    weekend.last_historical_assignment_date or date.min if weekend else date.min,
                    weekend.planned_future_assignment_count if weekend else 0,
                    -_below_target_priority(assignment),
                    -base_pattern_score(db, employee_id=employee.id, shift_date=day),
                    -assignment[1], -assignment[0], employee.id, shift.id,
                    shift, employee,
                ))
        if not options:
            unresolved.append({'date': day.isoformat(), 'reason': 'NO_ELIGIBLE_LEAD',
                               'constraints': sorted(set(failures))})
            continue
        *_key, chosen_shift, chosen_employee = min(options)
        chosen_shift.employee_id = chosen_employee.id
        chosen_shift.base_pattern_deviation_reason = 'LEAD_COVERAGE'
        chosen_shift.updated_by_principal_id = principal.id
        chosen_shift.updated_at = _now()
        if diagnostics is not None:
            diagnostics.append({
                'date': day.isoformat(), 'action': 'LEAD_COVERAGE_REPAIR',
                'shift_id': chosen_shift.id, 'employee_id': chosen_employee.id,
                'store_id': chosen_shift.store_id,
                'longview_disrupted': chosen_shift.store_id in special_store_ids,
            })
    db.flush()
    return unresolved


def reconcile_lead_designations(
    db: Session, *, schedule_period_id: int, preserve_manual: bool = True,
    preferred_manual_by_date: dict[date, int] | None = None,
    planning_date: date | None = None, diagnostics: list[dict] | None = None,
) -> list[dict]:
    period = db.get(SchedulePeriod, schedule_period_id)
    if period is None:
        raise SchedulingValidationError('Schedule period not found.')
    shifts = list(db.execute(select(ScheduleShift).where(
        ScheduleShift.schedule_period_id == schedule_period_id).order_by(
        ScheduleShift.shift_date, ScheduleShift.id).with_for_update()).scalars())
    employees = {row.id: row for row in db.execute(select(Employee).where(
        Employee.id.in_({row.employee_id for row in shifts if row.employee_id is not None}))).scalars()} if shifts else {}
    by_date: dict[date, list[ScheduleShift]] = defaultdict(list)
    for shift in shifts:
        by_date[shift.shift_date].append(shift)
    # Remove stale automatic designations up front. Future valid manager overrides
    # remain in place so their planned burden is visible to earlier target days.
    for row in shifts:
        if row.is_lead_of_day and not row.lead_of_day_manually_assigned:
            row.is_lead_of_day = False
    db.flush()
    missing: list[dict] = []
    from app.services.v2_scheduling_policy_service import evaluate_assignment
    for day, day_shifts in by_date.items():
        valid = []
        invalid_by_shift: dict[int, list[str]] = {}
        for row in day_shifts:
            employee = employees.get(row.employee_id) if row.employee_id is not None else None
            if employee is None or not is_scheduling_candidate(employee) or not employee.scheduling_lead_capable:
                invalid_by_shift[row.id] = ['NOT_SCHEDULED_OR_NOT_LEAD_CAPABLE']
                continue
            eligibility = evaluate_assignment(
                db, employee_id=employee.id, store_id=row.store_id,
                shift_date=row.shift_date, start_time=row.start_time, end_time=row.end_time,
                unpaid_break_minutes=row.unpaid_break_minutes, exclude_shift_id=row.id)
            if eligibility.eligible:
                valid.append(row)
            else:
                invalid_by_shift[row.id] = [reason.code for reason in eligibility.reasons]
        preferred_employee_id = (preferred_manual_by_date or {}).get(day)
        preserved = next((row for row in valid if row.employee_id == preferred_employee_id), None)
        if preserved is None:
            preserved = next((row for row in valid
                          if preserve_manual and row.is_lead_of_day and row.lead_of_day_manually_assigned), None)
        invalid_manual = [row for row in day_shifts
                          if row.is_lead_of_day and row.lead_of_day_manually_assigned
                          and row not in valid]
        if invalid_manual and diagnostics is not None:
            diagnostics.append({
                'date': day.isoformat(), 'action': 'INVALID_MANUAL_LEAD_OVERRIDE',
                'shift_ids': [row.id for row in invalid_manual],
                'constraints': sorted({code for row in invalid_manual
                                       for code in invalid_by_shift.get(row.id, [])}),
            })
        for row in day_shifts:
            row.is_lead_of_day = False
            row.lead_of_day_manually_assigned = False
        db.flush()
        chosen = preserved
        if chosen is None and valid:
            def order_key(row: ScheduleShift):
                fairness = lead_fairness(
                    db, employee_id=row.employee_id, before_date=day,
                    planning_date=planning_date or period.week_start_date,
                    current_period_id=schedule_period_id)
                return (
                    fairness.historical_assignment_count,
                    fairness.last_historical_assignment_date or date.min,
                    fairness.planned_future_assignment_count,
                    fairness.current_week_assignment_count,
                    row.employee_id, row.id,
                )
            chosen = min(valid, key=order_key)
        if chosen is None:
            missing.append({'date': day.isoformat(), 'reason': 'NO_ELIGIBLE_LEAD'})
        else:
            chosen.is_lead_of_day = True
            chosen.lead_of_day_manually_assigned = chosen is preserved
            if diagnostics is not None:
                fairness = lead_fairness(
                    db, employee_id=chosen.employee_id, before_date=day,
                    planning_date=planning_date or period.week_start_date,
                    current_period_id=schedule_period_id)
                diagnostics.append({
                    'date': day.isoformat(), 'action': 'LEAD_OF_DAY_SELECTED',
                    'shift_id': chosen.id, 'employee_id': chosen.employee_id,
                    'manual_override': chosen is preserved,
                    'historical_12_week_count': fairness.historical_assignment_count,
                    'last_historical_date': (
                        fairness.last_historical_assignment_date.isoformat()
                        if fairness.last_historical_assignment_date else None),
                    'planned_future_count': fairness.planned_future_assignment_count,
                    'current_week_count': fairness.current_week_assignment_count,
                    'candidate_burdens': [{
                        'shift_id': candidate.id,
                        'employee_id': candidate.employee_id,
                        'historical_count': candidate_fairness.historical_assignment_count,
                        'last_historical_date': (
                            candidate_fairness.last_historical_assignment_date.isoformat()
                            if candidate_fairness.last_historical_assignment_date else None),
                        'planned_future_count': candidate_fairness.planned_future_assignment_count,
                        'current_week_count': candidate_fairness.current_week_assignment_count,
                        'manager_override': bool(
                            candidate.is_lead_of_day
                            and candidate.lead_of_day_manually_assigned),
                    } for candidate in valid for candidate_fairness in [lead_fairness(
                        db, employee_id=candidate.employee_id, before_date=day,
                        planning_date=planning_date or period.week_start_date,
                        current_period_id=schedule_period_id)]],
                    'skipped_candidates': [{
                        'shift_id': row.id, 'employee_id': row.employee_id,
                        'constraints': constraints,
                    } for row in day_shifts
                      for constraints in [invalid_by_shift.get(row.id)] if constraints],
                })
    db.flush()
    return missing


def set_lead_of_day(db: Session, *, principal: Principal, shift_id: int) -> ScheduleShift:
    selected = db.execute(select(ScheduleShift).where(
        ScheduleShift.id == shift_id).with_for_update()).scalar_one_or_none()
    if selected is None or selected.employee_id is None:
        raise SchedulingValidationError('Choose a scheduled employee for Lead of the Day.')
    employee = db.get(Employee, selected.employee_id)
    if employee is None or not is_scheduling_candidate(employee) or not employee.scheduling_lead_capable:
        raise SchedulingValidationError('Lead of the Day must be a scheduled, eligible Lead-capable employee.')
    from app.services.v2_scheduling_policy_service import evaluate_assignment
    eligibility = evaluate_assignment(
        db, employee_id=employee.id, store_id=selected.store_id,
        shift_date=selected.shift_date, start_time=selected.start_time, end_time=selected.end_time,
        unpaid_break_minutes=selected.unpaid_break_minutes, exclude_shift_id=selected.id)
    if not eligibility.eligible:
        raise SchedulingValidationError('Lead of the Day is not currently eligible: ' + '; '.join(
            reason.message for reason in eligibility.reasons))
    period = db.execute(select(SchedulePeriod).where(
        SchedulePeriod.id == selected.schedule_period_id).with_for_update()).scalar_one()
    previous = db.execute(select(ScheduleShift).where(
        ScheduleShift.schedule_period_id == period.id,
        ScheduleShift.shift_date == selected.shift_date,
        ScheduleShift.is_lead_of_day.is_(True)).with_for_update()).scalar_one_or_none()
    if previous is not None:
        previous.is_lead_of_day = False
        previous.lead_of_day_manually_assigned = False
        db.flush()
    selected.is_lead_of_day = True
    selected.lead_of_day_manually_assigned = True
    selected.updated_by_principal_id = principal.id
    selected.updated_at = _now()
    period.version += 1
    write_v2_audit_event(db, event=V2AuditEvent(
        actor_principal_id=principal.id, action='LEAD_OF_DAY_CHANGED', domain='SCHEDULING',
        entity_type='schedule_shift', entity_id=selected.id, timestamp=_now(),
        before={'shift_id': previous.id if previous else None},
        after={'shift_id': selected.id, 'employee_id': selected.employee_id,
               'date': selected.shift_date.isoformat()},
    ), ip=None)
    db.flush()
    from app.services.v2_scheduling_coverage_service import rebuild_schedule_warnings
    rebuild_schedule_warnings(db, schedule_period_id=period.id)
    return selected


def clear_automatic_double_coverage(db: Session, *, schedule_period_id: int) -> int:
    result = db.execute(delete(ScheduleShift).where(
        ScheduleShift.schedule_period_id == schedule_period_id,
        ScheduleShift.is_double_coverage.is_(True),
        ScheduleShift.double_coverage_manually_assigned.is_(False),
    ))
    return int(result.rowcount or 0)


def generate_double_coverage_assignments(
    db: Session, *, principal: Principal, schedule_period_id: int,
) -> dict:
    period = db.get(SchedulePeriod, schedule_period_id)
    if period is None:
        raise SchedulingValidationError('Schedule period not found.')
    defaults = get_store_defaults(db)
    employees = [row for row in list_scheduling_candidates(db) if row.scheduling_double_coverage]
    if not employees:
        return {'assigned': 0, 'uncovered': [], 'store_id': None}
    store = db.get(Store, defaults.double_coverage_store_id) if defaults and defaults.double_coverage_store_id else None
    if store is None or not store.active:
        return {'assigned': 0, 'uncovered': [
            {'employee_id': row.id, 'code': 'DOUBLE_COVERAGE_STORE_MISSING',
             'message': 'Double Coverage employees are configured, but no active Double Coverage Store has been selected.'}
            for row in employees], 'store_id': defaults.double_coverage_store_id if defaults else None}
    templates = list(db.execute(select(ScheduleShift).where(
        ScheduleShift.schedule_period_id == schedule_period_id,
        ScheduleShift.store_id == store.id,
        ScheduleShift.is_double_coverage.is_(False),
    ).order_by(ScheduleShift.shift_date, ScheduleShift.start_time, ScheduleShift.id)).scalars())
    existing = {row.employee_id for row in db.execute(select(ScheduleShift).where(
        ScheduleShift.schedule_period_id == schedule_period_id,
        ScheduleShift.is_double_coverage.is_(True))).scalars()}
    employees.sort(key=lambda row: (
        double_coverage_fairness(db, employee_id=row.id, before_date=period.week_start_date).assignment_count,
        double_coverage_fairness(db, employee_id=row.id, before_date=period.week_start_date).last_assignment_date or date.min,
        row.id,
    ))
    assigned = 0
    uncovered: list[dict] = []
    from app.services.v2_scheduling_policy_service import evaluate_assignment
    for employee in employees:
        if employee.id in existing:
            continue
        failures = []
        chosen = None
        profile = db.execute(select(EmployeeSchedulingProfile).where(
            EmployeeSchedulingProfile.employee_id == employee.id)).scalar_one_or_none()
        for template in sorted(templates, key=lambda row: (
                0 if profile and is_base_workday(profile, row.shift_date) is True else 1,
                -scheduled_paid_minutes(row), row.shift_date, row.start_time, row.id)):
            result = evaluate_assignment(
                db, employee_id=employee.id, store_id=store.id, shift_date=template.shift_date,
                start_time=template.start_time, end_time=template.end_time,
                unpaid_break_minutes=template.unpaid_break_minutes)
            if result.eligible:
                chosen = template
                break
            failures.extend(reason.code for reason in result.reasons)
        if chosen is None:
            uncovered.append({'employee_id': employee.id, 'code': 'DOUBLE_COVERAGE_UNFILLED',
                              'reasons': sorted(set(failures)),
                              'message': 'Double Coverage assignment could not be filled this week under hard constraints.'})
            continue
        row = ScheduleShift(
            schedule_period_id=period.id, employee_id=employee.id, store_id=store.id,
            shift_date=chosen.shift_date, start_time=chosen.start_time, end_time=chosen.end_time,
            unpaid_break_minutes=chosen.unpaid_break_minutes, shift_type_id=chosen.shift_type_id,
            is_opener=False, is_closer=False, source_shift_id=chosen.id,
            is_double_coverage=True, double_coverage_manually_assigned=False,
            created_by_principal_id=principal.id, updated_by_principal_id=principal.id,
        )
        db.add(row); db.flush(); assigned += 1
    return {'assigned': assigned, 'uncovered': uncovered, 'store_id': store.id}


def override_double_coverage_employee(
    db: Session, *, principal: Principal, shift_id: int, employee_id: int,
) -> ScheduleShift:
    shift = db.execute(select(ScheduleShift).where(
        ScheduleShift.id == shift_id, ScheduleShift.is_double_coverage.is_(True)).with_for_update()).scalar_one_or_none()
    employee = db.get(Employee, employee_id)
    if shift is None or employee is None or not is_scheduling_candidate(employee) or not employee.scheduling_double_coverage:
        raise SchedulingValidationError('Choose an eligible scheduled Double Coverage employee.')
    from app.services.v2_scheduling_policy_service import evaluate_assignment
    eligibility = evaluate_assignment(
        db, employee_id=employee.id, store_id=shift.store_id, shift_date=shift.shift_date,
        start_time=shift.start_time, end_time=shift.end_time,
        unpaid_break_minutes=shift.unpaid_break_minutes, exclude_shift_id=shift.id)
    if not eligibility.eligible:
        raise SchedulingValidationError('Double Coverage override is not eligible: ' + '; '.join(
            reason.message for reason in eligibility.reasons))
    before = {'employee_id': shift.employee_id}
    shift.employee_id = employee.id
    shift.double_coverage_manually_assigned = True
    shift.manually_locked = True
    shift.locked_by_principal_id = principal.id
    shift.locked_at = _now()
    shift.lock_reason = 'Manual Double Coverage override.'
    shift.updated_by_principal_id = principal.id
    shift.updated_at = _now()
    write_v2_audit_event(db, event=V2AuditEvent(
        actor_principal_id=principal.id, action='DOUBLE_COVERAGE_ASSIGNMENT_CHANGED',
        domain='SCHEDULING', entity_type='schedule_shift', entity_id=shift.id,
        timestamp=_now(), before=before,
        after={'employee_id': employee.id, 'double_coverage': True},
    ), ip=None)
    db.flush()
    from app.services.v2_scheduling_coverage_service import rebuild_schedule_warnings
    rebuild_schedule_warnings(db, schedule_period_id=shift.schedule_period_id)
    return shift
