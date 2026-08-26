from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from app.auth import Principal
from app.models import (
    Employee, SchedulePeriod, SchedulePeriodStatus, ScheduleShift,
    SchedulingStoreDefaults, Store,
)
from app.services.v2_scheduling_roster_service import is_scheduling_candidate, list_scheduling_candidates
from app.services.v2_scheduling_service import SchedulingValidationError, scheduled_paid_minutes
from app.v2.audit import V2AuditEvent, write_v2_audit_event


@dataclass(frozen=True)
class AssignmentFairness:
    assignment_count: int
    last_assignment_date: date | None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def get_store_defaults(db: Session) -> SchedulingStoreDefaults | None:
    return db.get(SchedulingStoreDefaults, 1)


def set_double_coverage_store(
    db: Session, *, principal: Principal, store_id: int | None,
) -> SchedulingStoreDefaults:
    store = db.get(Store, store_id) if store_id is not None else None
    if store_id is not None and (store is None or not store.active):
        raise SchedulingValidationError('Choose an active Double Coverage Store.')
    row = db.execute(select(SchedulingStoreDefaults).where(
        SchedulingStoreDefaults.id == 1).with_for_update()).scalar_one_or_none()
    before = {'double_coverage_store_id': row.double_coverage_store_id if row else None}
    if row is None:
        row = SchedulingStoreDefaults(id=1, updated_by_principal_id=principal.id)
        db.add(row)
    row.double_coverage_store_id = store_id
    row.updated_by_principal_id = principal.id
    row.updated_at = _now()
    after = {'double_coverage_store_id': store_id}
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
    rows = db.execute(select(ScheduleShift.shift_date).join(SchedulePeriod).where(
        ScheduleShift.employee_id == employee_id,
        field.is_(True),
        ScheduleShift.shift_date < before_date,
        ScheduleShift.shift_date >= before_date - timedelta(weeks=12),
        or_(
            SchedulePeriod.status.in_((SchedulePeriodStatus.PUBLISHED, SchedulePeriodStatus.ARCHIVED)),
            SchedulePeriod.id == current_period_id if current_period_id is not None else False,
        ),
    ).order_by(ScheduleShift.shift_date)).scalars().all()
    return AssignmentFairness(len(rows), max(rows) if rows else None)


def lead_fairness(
    db: Session, *, employee_id: int, before_date: date,
    current_period_id: int | None = None,
) -> AssignmentFairness:
    return _designation_fairness(
        db, employee_id=employee_id, before_date=before_date, field=ScheduleShift.is_lead_of_day,
        current_period_id=current_period_id)


def double_coverage_fairness(db: Session, *, employee_id: int, before_date: date) -> AssignmentFairness:
    return _designation_fairness(
        db, employee_id=employee_id, before_date=before_date, field=ScheduleShift.is_double_coverage)


def ensure_daily_lead_staffing(
    db: Session, *, principal: Principal, schedule_period_id: int,
) -> list[dict]:
    """Repair generated staffing so every staffed day contains a valid Lead.

    Only unlocked ordinary shifts may be reassigned; manual locks and explicit
    Double Coverage assignments remain authoritative.
    """
    shifts = list(db.execute(select(ScheduleShift).where(
        ScheduleShift.schedule_period_id == schedule_period_id).order_by(
        ScheduleShift.shift_date, ScheduleShift.start_time, ScheduleShift.id).with_for_update()).scalars())
    lead_candidates = [row for row in list_scheduling_candidates(db) if row.scheduling_lead_capable]
    employee_by_id = {row.id: row for row in lead_candidates}
    by_date: dict[date, list[ScheduleShift]] = defaultdict(list)
    for shift in shifts:
        by_date[shift.shift_date].append(shift)
    unresolved: list[dict] = []
    from app.services.v2_scheduling_policy_service import evaluate_assignment
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
                if not eligibility.eligible:
                    failures.extend(reason.code for reason in eligibility.reasons)
                    continue
                fairness = lead_fairness(
                    db, employee_id=employee.id, before_date=day,
                    current_period_id=schedule_period_id)
                options.append((
                    -scheduled_paid_minutes(shift), fairness.assignment_count,
                    fairness.last_assignment_date or date.min, employee.id, shift.id,
                    shift, employee,
                ))
        if not options:
            unresolved.append({'date': day.isoformat(), 'reason': 'NO_ELIGIBLE_LEAD',
                               'constraints': sorted(set(failures))})
            continue
        *_key, chosen_shift, chosen_employee = min(options)
        chosen_shift.employee_id = chosen_employee.id
        chosen_shift.updated_by_principal_id = principal.id
        chosen_shift.updated_at = _now()
    db.flush()
    return unresolved


def reconcile_lead_designations(
    db: Session, *, schedule_period_id: int, preserve_manual: bool = True,
    preferred_manual_by_date: dict[date, int] | None = None,
) -> list[dict]:
    shifts = list(db.execute(select(ScheduleShift).where(
        ScheduleShift.schedule_period_id == schedule_period_id).order_by(
        ScheduleShift.shift_date, ScheduleShift.id).with_for_update()).scalars())
    employees = {row.id: row for row in db.execute(select(Employee).where(
        Employee.id.in_({row.employee_id for row in shifts if row.employee_id is not None}))).scalars()} if shifts else {}
    by_date: dict[date, list[ScheduleShift]] = defaultdict(list)
    for shift in shifts:
        by_date[shift.shift_date].append(shift)
    missing: list[dict] = []
    from app.services.v2_scheduling_policy_service import evaluate_assignment
    for day, day_shifts in by_date.items():
        valid = []
        for row in day_shifts:
            employee = employees.get(row.employee_id) if row.employee_id is not None else None
            if employee is None or not is_scheduling_candidate(employee) or not employee.scheduling_lead_capable:
                continue
            eligibility = evaluate_assignment(
                db, employee_id=employee.id, store_id=row.store_id,
                shift_date=row.shift_date, start_time=row.start_time, end_time=row.end_time,
                unpaid_break_minutes=row.unpaid_break_minutes, exclude_shift_id=row.id)
            if eligibility.eligible:
                valid.append(row)
        preferred_employee_id = (preferred_manual_by_date or {}).get(day)
        preserved = next((row for row in valid if row.employee_id == preferred_employee_id), None)
        if preserved is None:
            preserved = next((row for row in valid
                          if preserve_manual and row.is_lead_of_day and row.lead_of_day_manually_assigned), None)
        for row in day_shifts:
            row.is_lead_of_day = False
            row.lead_of_day_manually_assigned = False
        db.flush()
        chosen = preserved
        if chosen is None and valid:
            def order_key(row: ScheduleShift):
                fairness = lead_fairness(
                    db, employee_id=row.employee_id, before_date=day,
                    current_period_id=schedule_period_id)
                return (-scheduled_paid_minutes(row), fairness.assignment_count,
                        fairness.last_assignment_date or date.min, row.employee_id, row.id)
            chosen = min(valid, key=order_key)
        if chosen is None:
            missing.append({'date': day.isoformat(), 'reason': 'NO_ELIGIBLE_LEAD'})
        else:
            chosen.is_lead_of_day = True
            chosen.lead_of_day_manually_assigned = chosen is preserved
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
        ScheduleShift.employee_id.is_not(None),
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
        for template in sorted(templates, key=lambda row: (
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
