"""Published-shift provenance and narrow, manager-attested coverage commitments."""
from datetime import datetime, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select
from app.models import (Employee, Principal as PrincipalModel, ScheduleShift, SchedulePeriod,
    SchedulePeriodStatus, ShiftTransferRequest, ShiftTransferStatus, ScheduleAttendanceEvent,
    ScheduleCoverageCommitment, AuditLog)
from app.services.v2_scheduling_service import SchedulingValidationError
from app.services.v2_scheduling_policy_service import evaluate_assignment, organization_policy
from app.v2.audit import V2AuditEvent, write_v2_audit_event


def shift_lineage(db, shift):
    rows, seen = [], set()
    while shift is not None and shift.id not in seen:
        rows.append(shift); seen.add(shift.id)
        source = db.get(ScheduleShift, shift.source_shift_id) if shift.source_shift_id else None
        if source and db.get(SchedulePeriod, source.schedule_period_id).week_start_date != db.get(SchedulePeriod, shift.schedule_period_id).week_start_date:
            break  # Template/previous-week copies start a new publication lineage.
        shift = source
    return rows


def published_provenance(db, shift):
    """Reverse audited transfers on the first published ancestor; fail unknown on broken chains."""
    lineage = shift_lineage(db, shift)
    unknown = {'employee_id': None, 'employee_name': None, 'state': 'AMBIGUOUS',
               'source_shift_id': None}
    if not lineage:
        return unknown
    last = lineage[-1]
    if last.source_shift_id:
        source = db.get(ScheduleShift, last.source_shift_id)
        if source is None or source.id in {s.id for s in lineage}:
            return unknown
    published = [s for s in reversed(lineage) if (p := db.get(SchedulePeriod, s.schedule_period_id))
                 and (p.status == SchedulePeriodStatus.PUBLISHED or p.published_at)]
    if not published:
        return unknown
    source = published[0]
    employee_id = source.employee_id
    period = db.get(SchedulePeriod, source.schedule_period_id)
    transfers = list(db.scalars(select(ShiftTransferRequest).where(
        ShiftTransferRequest.shift_id == source.id,
        ShiftTransferRequest.status == ShiftTransferStatus.COMPLETED).order_by(
        ShiftTransferRequest.completed_at.desc(), ShiftTransferRequest.id.desc())))
    published_at = period.published_at
    if transfers and published_at is None:
        # Legacy published rows may lack a timestamp; use the publication audit
        # only when it identifies one unambiguous instant for this exact period.
        audits = list(db.scalars(select(AuditLog).where(
            AuditLog.action.in_(('V2:SCHEDULING:SCHEDULE_PUBLISHED',
                                 'V2:SCHEDULING:SCHEDULE_PUBLISHED_WITH_WARNINGS')),
            AuditLog.meta['entity_id'].as_string() == str(period.id),
            AuditLog.meta['entity_type'].as_string() == 'schedule_period')))
        try:
            instants = {datetime.fromisoformat(a.meta['occurred_at']) for a in audits}
        except (KeyError, TypeError, ValueError):
            return unknown
        if len(instants) != 1 or next(iter(instants)).utcoffset() is None:
            return unknown
        published_at = next(iter(instants))
    for transfer in transfers:
        if transfer.completed_at is None:
            return unknown
        if published_at and transfer.completed_at < published_at:
            continue  # A draft transfer predates publication and is not original published intent.
        if transfer.to_employee_id != employee_id:
            return unknown
        employee_id = transfer.from_employee_id
    person = db.get(Employee, employee_id) if employee_id else None
    return {'employee_id': employee_id, 'employee_name': person.full_name if person else None,
            'state': 'KNOWN' if person else 'UNASSIGNED', 'source_shift_id': source.id}


def _audit(db, principal, row, action, reason=''):
    write_v2_audit_event(db, event=V2AuditEvent(actor_principal_id=principal.id,
        action=action, domain='SCHEDULING', entity_type='schedule_coverage_commitment',
        entity_id=row.id, timestamp=datetime.now(timezone.utc), correlation_id=str(uuid4()),
        reason=reason or None, metadata={'shift_id': row.schedule_shift_id,
            'employee_id': row.employee_id, 'note': row.note}), ip=None)


def commitment_eligibility(db, shift, employee_id, *, today=None):
    from app.services.v2_scheduling_policy_service import SimulatedAssignment
    from app.services.v2_scheduling_lead_duty_service import business_today
    day = today or business_today(db)
    now = datetime.now(ZoneInfo(organization_policy(db).timezone_name))
    ids = [s.id for s in shift_lineage(db, shift)]
    committed = list(db.scalars(select(ScheduleShift).join(ScheduleCoverageCommitment,
        ScheduleCoverageCommitment.schedule_shift_id == ScheduleShift.id).where(
        ScheduleCoverageCommitment.employee_id == employee_id,
        ScheduleCoverageCommitment.voided_at.is_(None), ScheduleShift.id.not_in(ids))))
    simulated = tuple(SimulatedAssignment(s.shift_date, s.start_time, s.end_time, s.unpaid_break_minutes)
                      for s in committed if s.shift_date > day or (s.shift_date == day
                          and (day != now.date() or s.end_time > now.time())))
    result = evaluate_assignment(db, employee_id=employee_id, store_id=shift.store_id,
        shift_date=shift.shift_date, start_time=shift.start_time, end_time=shift.end_time,
        unpaid_break_minutes=shift.unpaid_break_minutes, exclude_shift_id=shift.id,
        simulated_assignments=simulated)
    return result


def _authorize(db, principal):
    from app.services.access_control_service import principal_has_permission
    if not principal_has_permission(db, principal=principal, permission_key='scheduling.attendance.record',
                                    fallback_allowed=principal.role.value in ('ADMIN', 'MANAGER', 'LEAD')):
        raise PermissionError('Attendance recording permission is required.')


def record_commitment(db, *, principal, shift_id, employee_id, note='', today=None):
    _authorize(db, principal)
    from app.services.v2_scheduling_attendance_service import _published_shift
    from app.services.v2_scheduling_lead_duty_service import business_today
    shift, period = _published_shift(db, shift_id)
    day = today or business_today(db)
    if period.status != SchedulePeriodStatus.PUBLISHED or shift.shift_date < day:
        raise SchedulingValidationError('Commitments require a current published, uncompleted shift.')
    now = datetime.now(ZoneInfo(organization_policy(db).timezone_name))
    if shift.shift_date == day == now.date() and shift.end_time <= now.time():
        raise SchedulingValidationError('This shift has finished; record actual coverage instead.')
    if len(note.strip()) > 2000:
        raise SchedulingValidationError('Commitment note must be 2,000 characters or fewer.')
    if employee_id == shift.employee_id:
        raise SchedulingValidationError('Choose a replacement different from the scheduled employee.')
    # Serialize commitments across employees before evaluating prospective hours/overlap.
    person = db.scalar(select(Employee).where(Employee.id == employee_id).with_for_update())
    if person is None:
        raise SchedulingValidationError('Choose an existing employee.')
    result = commitment_eligibility(db, shift, employee_id, today=day)
    if not result.eligible:
        raise SchedulingValidationError('; '.join(r.message for r in result.reasons))
    if result.requires_hour_approval:
        from app.auth import Role
        if principal.role not in (Role.ADMIN, Role.MANAGER) or not note.strip():
            raise SchedulingValidationError('This commitment requires an Admin/Manager hours approval note.')
    lineage = shift_lineage(db, shift)
    db.scalar(select(ScheduleShift).where(ScheduleShift.id == lineage[-1].id).with_for_update())
    ids = [s.id for s in lineage]
    for existing, other in db.execute(select(ScheduleCoverageCommitment, ScheduleShift).join(
            ScheduleShift, ScheduleShift.id == ScheduleCoverageCommitment.schedule_shift_id).where(
            ScheduleCoverageCommitment.employee_id == employee_id,
            ScheduleCoverageCommitment.voided_at.is_(None), ScheduleShift.shift_date == shift.shift_date,
            ScheduleShift.start_time < shift.end_time, ScheduleShift.end_time > shift.start_time)):
        if existing.schedule_shift_id not in ids:
            raise SchedulingValidationError('Employee already committed to overlapping coverage.')
    for old in db.scalars(select(ScheduleCoverageCommitment).where(
            ScheduleCoverageCommitment.schedule_shift_id.in_(ids), ScheduleCoverageCommitment.voided_at.is_(None))):
        cancel_commitment(db, principal=principal, commitment_id=old.id,
                          reason='Superseded by a new replacement commitment.')
    row = ScheduleCoverageCommitment(schedule_shift_id=shift.id, employee_id=employee_id,
        recorded_by_principal_id=principal.id, note=note.strip())
    db.add(row); db.flush(); _audit(db, principal, row, 'COVERAGE_COMMITTED')
    return row


def cancel_commitment(db, *, principal, commitment_id, reason):
    _authorize(db, principal)
    if not reason.strip() or len(reason) > 2000:
        raise SchedulingValidationError('Enter a cancellation reason (at most 2,000 characters).')
    row = db.scalar(select(ScheduleCoverageCommitment).where(
        ScheduleCoverageCommitment.id == commitment_id).with_for_update())
    if row is None or row.voided_at:
        raise SchedulingValidationError('Commitment is missing or already canceled.')
    row.voided_at = datetime.now(timezone.utc); row.voided_by_principal_id = principal.id
    row.void_reason = reason.strip(); db.flush(); _audit(db, principal, row, 'COVERAGE_COMMITMENT_CANCELED', reason)
    return row


def exception_facts(db, shift, today):
    from app.services.v2_scheduling_attendance_service import resolve_attendance_outcome
    ids = [s.id for s in shift_lineage(db, shift)]
    history = list(db.scalars(select(ScheduleCoverageCommitment).where(
        ScheduleCoverageCommitment.schedule_shift_id.in_(ids)).order_by(ScheduleCoverageCommitment.id)))
    events = list(db.scalars(select(ScheduleAttendanceEvent).where(
        ScheduleAttendanceEvent.schedule_shift_id.in_(ids), ScheduleAttendanceEvent.voided_at.is_(None))))
    outcome = resolve_attendance_outcome(shift, events, db=db, presumptive=shift.shift_date < today)
    active = [r for r in history if not r.voided_at]
    now = datetime.now(ZoneInfo(organization_policy(db).timezone_name))
    ended = shift.shift_date < today or (shift.shift_date == today == now.date() and shift.end_time <= now.time())
    status = None
    if events or active:
        if outcome['outcome'] == 'UNRESOLVED' or len(active) > 1:
            status = 'Coverage unresolved/conflicting'
        elif outcome['outcome'] == 'CONFIRMED_REPLACEMENT':
            status = 'Actual coverage confirmed'
        elif outcome['outcome'] == 'CONFIRMED_ABSENCE':
            status = ('Coverage unresolved/conflicting' if ended else
                      'Replacement committed' if active else 'Coverage needed')
        elif active:
            status = 'Coverage unresolved/conflicting' if ended else 'Replacement committed'
    def name(eid):
        person = db.get(Employee, eid)
        return person.full_name if person else 'Unknown employee'
    return {'status': status, 'original': published_provenance(db, shift),
        'actual_worker_names': [name(eid) for eid in outcome['credited_employee_ids']]
            if outcome['evidence_class'] == 'CONFIRMED_EVENT' else [],
        'actual_evidence': outcome['evidence_class'],
        'commitment': {'id': active[0].id, 'employee_id': active[0].employee_id,
                       'employee_name': name(active[0].employee_id)} if len(active) == 1 else None,
        'commitment_history': [{'id': r.id, 'employee_name': name(r.employee_id), 'note': r.note,
            'recorded_at': r.created_at.isoformat(),
            'recorded_by': db.get(PrincipalModel, r.recorded_by_principal_id).username,
            'voided': r.voided_at is not None, 'void_reason': r.void_reason} for r in history]}


def record_exception(db, *, principal, shift_id, event_type=None, event_at=None,
                     replacement_employee_id=None, commitment_employee_id=None,
                     actual_employee_id=None, actual_event_at=None, note='',
                     override_store_restriction=False, override_reason='', today=None, ip=None):
    """One transaction/savepoint, separate facts. No commitment-to-attendance inference."""
    from app.models import AttendanceEventType
    from app.services.v2_scheduling_attendance_service import record_attendance_event
    if event_type is None and commitment_employee_id is None and actual_employee_id is None:
        raise SchedulingValidationError('Choose an exception to record.')
    if actual_employee_id and event_type == AttendanceEventType.COVERED_SHIFT:
        raise SchedulingValidationError('Record actual coverage only once.')
    _authorize(db, principal)
    results = []
    with db.begin_nested():
        if event_type is not None:
            if event_at is None:
                raise SchedulingValidationError('Enter the occurrence/report time.')
            results.append(record_attendance_event(db, principal=principal, shift_id=shift_id,
                event_type=event_type, event_at=event_at, replacement_employee_id=replacement_employee_id,
                note=note, override_store_restriction=override_store_restriction,
                override_reason=override_reason, today=today, ip=ip))
        if commitment_employee_id:
            record_commitment(db, principal=principal, shift_id=shift_id,
                employee_id=commitment_employee_id, note=note, today=today)
        if actual_employee_id:
            if actual_event_at is None:
                raise SchedulingValidationError('Enter the actual coverage occurrence time.')
            results.append(record_attendance_event(db, principal=principal, shift_id=shift_id,
                event_type=AttendanceEventType.COVERED_SHIFT, event_at=actual_event_at,
                replacement_employee_id=actual_employee_id, note=note,
                override_store_restriction=override_store_restriction, override_reason=override_reason, today=today, ip=ip))
    return results
