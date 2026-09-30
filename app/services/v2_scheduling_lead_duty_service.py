"""Company-wide daily responsibility, separate from ordinary work attendance."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.auth import Principal
from app.models import Employee, LeadDutyOutcome, ScheduleAttendanceEvent, SchedulePeriod, SchedulePeriodStatus, ScheduleShift
from app.services.v2_scheduling_service import SchedulingValidationError
from app.v2.audit import V2AuditEvent, write_v2_audit_event


def business_today(db: Session) -> date:
    from app.services.v2_scheduling_policy_service import organization_policy
    return datetime.now(ZoneInfo(organization_policy(db).timezone_name)).date()


def lead_duty_facts(db: Session, *, start_date: date, end_date: date,
                    as_of_date: date | None = None, current_period_id: int | None = None) -> list[dict]:
    """Resolve revisions globally first, then at most one company-wide fact/day.

    Explicit exception outcomes survive revision/archival. Presumptions use only
    effective published intent and shared attendance evidence on its lineage.
    Draft intent can reserve future days but can never complete historical work.
    """
    from app.services.v2_scheduling_attendance_service import resolve_attendance_outcome
    from app.services.v2_scheduling_context import CONTEXT_KEY, effective_periods_by_week
    cutoff = as_of_date or business_today(db)
    periods = list(db.scalars(select(SchedulePeriod).where(
        SchedulePeriod.week_start_date <= end_date, SchedulePeriod.week_end_date >= start_date)))
    historical = {}
    for period in periods:
        week = period.week_start_date
        if period.status == SchedulePeriodStatus.PUBLISHED or period.published_at is not None:
            if week not in historical or period.revision_number > historical[week].revision_number:
                historical[week] = period
    context = db.get(SchedulePeriod, current_period_id or db.info.get(CONTEXT_KEY)) if (current_period_id or db.info.get(CONTEXT_KEY)) else None
    planned = effective_periods_by_week(periods, context)
    shifts = list(db.scalars(select(ScheduleShift).where(
        ScheduleShift.schedule_period_id.in_([p.id for p in periods]),
        ScheduleShift.shift_date.between(start_date, end_date))))
    by_id = {s.id: s for s in shifts}
    period_by_id = {p.id: p for p in periods}
    events = defaultdict(list)
    for event in db.scalars(select(ScheduleAttendanceEvent).where(
            ScheduleAttendanceEvent.schedule_shift_id.in_(by_id), ScheduleAttendanceEvent.voided_at.is_(None))):
        events[event.schedule_shift_id].append(event)
    resolutions = defaultdict(list)
    for row in db.scalars(select(LeadDutyOutcome).where(
            LeadDutyOutcome.business_date.between(start_date, end_date), LeadDutyOutcome.voided_at.is_(None))):
        resolutions[row.business_date].append(row)

    def root(shift):
        seen = set()
        while shift.source_shift_id in by_id and shift.id not in seen:
            seen.add(shift.id)
            shift = by_id[shift.source_shift_id]
        return shift.id

    designations = defaultdict(list)
    effective_days = set()
    for shift in shifts:
        period = period_by_id[shift.schedule_period_id]
        chosen = (historical if shift.shift_date < cutoff else planned).get(period.week_start_date)
        if chosen is not None and chosen.id == period.id:
            effective_days.add(shift.shift_date)
            if shift.is_lead_of_day:
                designations[shift.shift_date].append(shift)
    facts = []
    for day in sorted(effective_days | set(resolutions)):
        designated = designations.get(day, [])
        shift = designated[0] if len(designated) == 1 else None
        explicit = resolutions.get(day, [])
        fact = dict(business_date=day, scheduled_employee_id=shift.employee_id if shift else None,
                    scheduled_shift_id=shift.id if shift else None,
                    period_id=shift.schedule_period_id if shift else None,
                    employee_id=None, outcome='UNRESOLVED', evidence_class='UNRESOLVED',
                    reason='NO_UNAMBIGUOUS_PUBLISHED_LEAD', resolution_id=None)
        if day == cutoff and len(explicit) == 1:
            row = explicit[0]
            fact.update(outcome='CURRENT' if row.outcome == 'PERFORMED' else row.outcome,
                        evidence_class='CURRENT_LEAD' if row.outcome == 'PERFORMED' else 'UNRESOLVED',
                        employee_id=row.employee_id, reason=row.reason, resolution_id=row.id)
        elif day >= cutoff:
            if shift and not explicit:
                fact.update(outcome='RESERVATION', evidence_class='PLANNED_RESERVATION',
                            employee_id=shift.employee_id, reason='FUTURE_DESIGNATION')
                lineage_events = [e for s in shifts if s.employee_id == shift.employee_id
                                  and s.shift_date == day and root(s) == root(shift) for e in events[s.id]]
                if lineage_events:
                    work = resolve_attendance_outcome(shift, lineage_events, presumptive=True, db=db)
                    if work['outcome'] not in ('PRESUMPTIVE_SCHEDULED', 'CONFIRMED_SCHEDULED'):
                        fact.update(outcome='UNRESOLVED', evidence_class='UNRESOLVED', employee_id=None,
                                    reason='LEAD_REASSIGNMENT_NEEDED')
        elif len(explicit) == 1:
            row = explicit[0]
            fact.update(outcome={'PERFORMED': 'CONFIRMED', 'UNCOVERED': 'UNCOVERED', 'UNRESOLVED': 'UNRESOLVED'}[row.outcome],
                        evidence_class='CONFIRMED_LEAD' if row.outcome != 'UNRESOLVED' else 'UNRESOLVED',
                        employee_id=row.employee_id, reason=row.reason, resolution_id=row.id)
        elif len(explicit) > 1:
            fact['reason'] = 'CONFLICTING_LEAD_RESOLUTIONS'
        elif shift:
            # Old event-bearing copies remain evidence; do not attach a former
            # employee's attendance to a different employee in the new revision.
            lineage_events = [e for s in shifts if s.employee_id == shift.employee_id
                              and s.shift_date == day and root(s) == root(shift) for e in events[s.id]]
            work = resolve_attendance_outcome(shift, lineage_events, presumptive=True, db=db)
            if work['outcome'] in ('PRESUMPTIVE_SCHEDULED', 'CONFIRMED_SCHEDULED'):
                fact.update(outcome='PRESUMPTIVE', evidence_class='PRESUMPTIVE_PUBLISHED',
                            employee_id=shift.employee_id, reason='PUBLISHED_LEAD_NO_CONTRADICTION')
            else:
                fact['reason'] = 'LEAD_EXCEPTION_' + work['reason']
        facts.append(fact)
    return facts


def _authorize(db, principal):
    from app.services.access_control_service import principal_has_permission
    if not principal_has_permission(db, principal=principal, permission_key='scheduling.lead_duty.resolve',
                                    fallback_allowed=principal.role.value in ('ADMIN', 'MANAGER')):
        raise PermissionError('Company-wide Lead duty resolution permission is required.')


def _reason(value):
    value = value.strip()
    if not 1 <= len(value) <= 2000:
        raise SchedulingValidationError('A reason of 1–2,000 characters is required.')
    return value


def _lock_day(db, day):
    # Serializes insertion as well as corrections, including when no row exists.
    db.execute(text('SELECT pg_advisory_xact_lock(73030, :day)'), {'day': day.toordinal()})


def _audit(db, principal, row, action):
    write_v2_audit_event(db, event=V2AuditEvent(
        actor_principal_id=principal.id, action=action, domain='SCHEDULING',
        entity_type='lead_duty_outcome', entity_id=row.id, timestamp=datetime.now(timezone.utc),
        metadata={'business_date': row.business_date.isoformat(), 'outcome': row.outcome,
                  'employee_id': row.employee_id, 'scheduled_shift_id': row.scheduled_shift_id,
                  'reason': row.reason, 'void_reason': row.void_reason}), ip=None)


def record_lead_duty(db: Session, *, principal: Principal, shift_id: int, outcome: str,
                     employee_id: int | None, reason: str, today: date | None = None) -> LeadDutyOutcome:
    _authorize(db, principal)
    reason = _reason(reason)
    shift = db.get(ScheduleShift, shift_id)
    period = db.get(SchedulePeriod, shift.schedule_period_id) if shift else None
    if shift is None or period is None or not (period.status == SchedulePeriodStatus.PUBLISHED or period.published_at):
        raise SchedulingValidationError('Choose a published schedule as Lead-duty evidence.')
    day = today or business_today(db)
    if shift.shift_date > day:
        raise SchedulingValidationError('Resolve actual Lead duty on the business day or after the day has passed.')
    if shift.shift_date == day and period.status != SchedulePeriodStatus.PUBLISHED:
        raise SchedulingValidationError('Current Lead handoff requires the current published schedule.')
    if shift.shift_date == day and employee_id is not None:
        from app.services.v2_scheduling_roster_service import is_scheduling_candidate
        from app.services.v2_scheduling_lifecycle_service import within_employment_dates
        person = db.get(Employee, employee_id)
        if (person is None or not is_scheduling_candidate(person) or not person.scheduling_lead_capable
                or not within_employment_dates(person, day)):
            raise SchedulingValidationError('Current Lead must be an eligible company-wide Lead employee.')
    if outcome not in ('PERFORMED', 'UNCOVERED', 'UNRESOLVED'):
        raise SchedulingValidationError('Choose a valid Lead-duty outcome.')
    if (outcome == 'PERFORMED') != (employee_id is not None):
        raise SchedulingValidationError('Only a performed outcome must name the actual Lead.')
    if employee_id is not None and db.get(Employee, employee_id) is None:
        raise SchedulingValidationError('Choose an existing employee; historical eligibility is not rewritten.')
    _lock_day(db, shift.shift_date)
    previous = db.scalar(select(LeadDutyOutcome).where(
        LeadDutyOutcome.business_date == shift.shift_date, LeadDutyOutcome.voided_at.is_(None)).with_for_update())
    if previous:
        previous.voided_at = datetime.now(timezone.utc)
        previous.voided_by_principal_id = principal.id
        previous.void_reason = 'Superseded: ' + reason
        _audit(db, principal, previous, 'LEAD_DUTY_SUPERSEDED')
        db.flush()
    row = LeadDutyOutcome(business_date=shift.shift_date, outcome=outcome, employee_id=employee_id,
        scheduled_shift_id=shift.id, reason=reason, recorded_by_principal_id=principal.id)
    db.add(row); db.flush()
    _audit(db, principal, row, 'LEAD_DUTY_RESOLVED')
    return row


def void_lead_duty(db: Session, *, principal: Principal, outcome_id: int, reason: str) -> LeadDutyOutcome:
    _authorize(db, principal)
    reason = _reason(reason)
    row = db.get(LeadDutyOutcome, outcome_id)
    if row is None:
        raise SchedulingValidationError('Lead-duty outcome not found.')
    _lock_day(db, row.business_date)
    db.refresh(row, with_for_update=True)
    if row.voided_at is not None:
        raise SchedulingValidationError('Lead-duty outcome is already voided.')
    row.voided_at = datetime.now(timezone.utc)
    row.voided_by_principal_id = principal.id
    row.void_reason = reason
    _audit(db, principal, row, 'LEAD_DUTY_VOIDED')
    db.flush()
    return row
