"""Retrospective, management-attested work using ordinary schedule revisions.

No publication, attendance writes, employee notifications, or inventory grants.
An unlock creates a correction draft; the immutable previous lock remains the
fairness authority until its replacement is attested and locked.
"""
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from app.auth import Role
from app.models import SchedulePeriod, SchedulePeriodStatus, ScheduleLifecycleStage, ScheduleShift, Store
from app.services.v2_scheduling_service import (
    SchedulingConflict, SchedulingValidationError, _lock_period, _require_draft,
    create_draft_period, lock_scheduling, validate_week,
)
from app.v2.audit import V2AuditEvent, write_v2_audit_event


def require_admin(principal):
    if not principal.active or principal.role != Role.ADMIN:
        raise PermissionError('Historical reconstruction requires an active Admin.')


def require_past_week(db, week_start):
    from app.services.v2_scheduling_lead_duty_service import business_today
    validate_week(week_start)
    if week_start + timedelta(days=6) >= business_today(db):
        raise SchedulingValidationError('Historical reconstruction requires a fully completed week.')


def require_historical_editor(db, period, principal):
    if period.is_historical:
        require_admin(principal)
        require_past_week(db, period.week_start_date)
        _require_draft(period)


def _reason(value):
    value = value.strip()
    if not value or len(value) > 2000:
        raise SchedulingValidationError('Document the reconstruction or correction reason (1–2,000 characters).')
    return value


def _audit(db, principal, period, action, reason, **metadata):
    write_v2_audit_event(db, event=V2AuditEvent(actor_principal_id=principal.id,
        action=action, domain='SCHEDULING', entity_type='schedule_period', entity_id=period.id,
        timestamp=datetime.now(timezone.utc), reason=reason,
        metadata={'retrospectively_entered':True,'management_attested':bool(period.historical_locked_at),
                  'week_start_date':period.week_start_date.isoformat(),'version':period.version, **metadata}), ip=None)


def _copy_assignments(db, source, target, principal):
    for row in db.scalars(select(ScheduleShift).where(ScheduleShift.schedule_period_id==source.id)):
        values={c.key:getattr(row,c.key) for c in ScheduleShift.__table__.columns
                if c.key not in {'id','schedule_period_id','created_at','updated_at','created_by_principal_id',
                                 'updated_by_principal_id','source_shift_id','locked_at','locked_by_principal_id'}}
        # Automatic Lead designations are intent, not explicit historical responsibility.
        if not row.lead_of_day_manually_assigned:
            values['is_lead_of_day']=False
        db.add(ScheduleShift(**values,schedule_period_id=target.id,source_shift_id=row.id,
            created_by_principal_id=principal.id,updated_by_principal_id=principal.id))
    db.flush()


def open_historical_week(db, *, principal, week_start, reason):
    require_admin(principal); reason=_reason(reason); lock_scheduling(db); require_past_week(db,week_start)
    rows=list(db.scalars(select(SchedulePeriod).where(SchedulePeriod.week_start_date==week_start)
                        .order_by(SchedulePeriod.revision_number.desc()).with_for_update()))
    draft=next((p for p in rows if p.status==SchedulePeriodStatus.DRAFT),None)
    if draft:
        if not draft.is_historical:
            if draft.published_at or draft.cleared_at:
                raise SchedulingConflict('This revision cannot be converted to historical entry.')
            original_generation={'generated_at':draft.generated_at.isoformat() if draft.generated_at else None,
                'automatic_publication_at':draft.automatic_publication_at.isoformat() if draft.automatic_publication_at else None}
            draft.is_historical=True;draft.generated_at=None;draft.automatic_publication_at=None
            draft.historical_reason=reason;draft.version+=1
            draft.updated_by_principal_id=principal.id;draft.updated_at=datetime.now(timezone.utc)
            removed_automatic_lead_ids=[]
            # Never turn automatic operational intent into attested Lead responsibility.
            for shift in db.scalars(select(ScheduleShift).where(ScheduleShift.schedule_period_id==draft.id)):
                if shift.is_lead_of_day and not shift.lead_of_day_manually_assigned:
                    removed_automatic_lead_ids.append(shift.id);shift.is_lead_of_day=False
            db.flush();_audit(db,principal,draft,'HISTORICAL_ENTRY_OPENED',reason,original_generation=original_generation,removed_automatic_lead_shift_ids=removed_automatic_lead_ids)
        return draft
    locked=next((p for p in rows if p.historical_locked_at),None)
    if locked:return locked
    source=next((p for p in rows if p.cleared_at is None and (p.status==SchedulePeriodStatus.PUBLISHED or p.published_at)),None)
    period=create_draft_period(db,principal=principal,week_start=week_start,historical=True,
        source_schedule_period_id=source.id if source else None)
    period.historical_reason=reason
    if source:_copy_assignments(db,source,period,principal)
    _audit(db,principal,period,'HISTORICAL_ENTRY_OPENED',reason,source_period_id=source.id if source else None)
    return period


def lock_historical_week(db, *, principal, period_id, expected_version, reason):
    require_admin(principal);reason=_reason(reason);period=_lock_period(db,period_id)
    if not period.is_historical:raise SchedulingValidationError('Open this week for historical entry first.')
    require_past_week(db,period.week_start_date);_require_draft(period,expected_version=expected_version)
    shifts=list(db.scalars(select(ScheduleShift).where(ScheduleShift.schedule_period_id==period.id)))
    if not shifts or any(s.employee_id is None for s in shifts):
        raise SchedulingValidationError('Assign each historical entry to an employee before attesting the week.')
    for shift in shifts:
        if not period.week_start_date<=shift.shift_date<=period.week_end_date:
            raise SchedulingValidationError('Historical assignment is outside this week.')
        if shift.is_lead_of_day and not shift.lead_of_day_manually_assigned:
            raise SchedulingValidationError('Historical Lead responsibility must be explicitly established.')
    from app.services.v2_scheduling_service import _validate_shift_input, ShiftInput
    for shift in shifts:
        values=ShiftInput(**{key:getattr(shift,key) for key in ShiftInput.__dataclass_fields__})
        _validate_shift_input(db,period=period,values=values,
            allowed_store_ids=tuple({s.store_id for s in shifts}),
            allow_hard_unavailability_override=False,exclude_shift_id=shift.id)
    # Flush all assignment edits BEFORE the immutable-period transition.
    db.flush()
    period.status=SchedulePeriodStatus.ARCHIVED
    period.lifecycle_stage=ScheduleLifecycleStage.CLOSED
    period.historical_locked_at=datetime.now(timezone.utc)
    period.historical_locked_by_principal_id=principal.id
    period.historical_reason=reason;period.version+=1
    period.updated_by_principal_id=principal.id;period.updated_at=period.historical_locked_at
    db.flush();_audit(db,principal,period,'HISTORICAL_WEEK_LOCKED',reason,assignment_count=len(shifts))
    return period


def unlock_historical_week(db, *, principal, period_id, expected_version, reason):
    require_admin(principal);reason=_reason(reason);source=_lock_period(db,period_id)
    require_past_week(db,source.week_start_date)
    if not source.is_historical or not source.historical_locked_at:
        raise SchedulingValidationError('Choose a locked historical week.')
    if source.version!=expected_version:raise SchedulingConflict('Historical version changed; refresh the board.')
    latest=latest_locked_weeks(db).get(source.week_start_date)
    if latest.id!=source.id:raise SchedulingConflict('Unlock the latest historical version.')
    period=create_draft_period(db,principal=principal,week_start=source.week_start_date,historical=True,
        source_schedule_period_id=source.id,supersedes_schedule_period_id=source.id)
    period.historical_reason=reason;_copy_assignments(db,source,period,principal)
    _audit(db,principal,period,'HISTORICAL_WEEK_UNLOCKED',reason,preserved_period_id=source.id)
    return period


def latest_locked_weeks(db):
    rows=db.scalars(select(SchedulePeriod).where(SchedulePeriod.is_historical.is_(True),
        SchedulePeriod.historical_locked_at.is_not(None)).order_by(SchedulePeriod.revision_number))
    return {p.week_start_date:p for p in rows}


def historical_shifts(db, *, before_date):
    ids=[p.id for p in latest_locked_weeks(db).values() if p.week_end_date<before_date]
    return list(db.scalars(select(ScheduleShift).where(ScheduleShift.schedule_period_id.in_(ids)))) if ids else []


def merge_historical_work(db, facts, ordinary_rows, *, store_id, before_date):
    """Management attestation replaces intent; attendance/coverage wins over it."""
    from app.models import ScheduleAttendanceEvent, ScheduleCoverageCommitment
    from app.services.v2_scheduling_attendance_service import resolve_attendance_outcome
    historical=historical_shifts(db,before_date=before_date)
    if not historical:
        return sorted(facts,key=lambda f:(f['shift_date'],f['shift_id']))
    originals={s.id:s for s,p in ordinary_rows}
    events=list(db.scalars(select(ScheduleAttendanceEvent).where(
        ScheduleAttendanceEvent.schedule_shift_id.in_(originals),ScheduleAttendanceEvent.voided_at.is_(None))))
    commitments=list(db.scalars(select(ScheduleCoverageCommitment).where(
        ScheduleCoverageCommitment.schedule_shift_id.in_(originals),ScheduleCoverageCommitment.voided_at.is_(None))))
    evidence={event.schedule_shift_id for event in events}|{row.schedule_shift_id for row in commitments}
    people={s.id:{s.employee_id} for s in originals.values()}
    for event in events:
        people[event.schedule_shift_id].update((event.original_employee_id,event.replacement_employee_id))
    for row in commitments:people[row.schedule_shift_id].add(row.employee_id)
    from app.services.v2_scheduling_exception_service import shift_lineage
    roots={}
    def root(shift):
        if shift.id not in roots:roots[shift.id]=shift_lineage(db,shift)[-1].id
        return roots[shift.id]
    def overlaps(a,b):
        # Store/time alone does not identify a position: legitimate double
        # coverage by two different employees must retain both workers.
        return (a.shift_date==b.shift_date and a.start_time<b.end_time and b.start_time<a.end_time
                and (root(a)==root(b) or a.employee_id in people.get(b.id,{b.employee_id})))
    result=list(facts)
    for shift in historical:
        competing=[s for s in originals.values() if overlaps(shift,s)]
        if any(s.id in evidence for s in competing):continue
        result=[f for f in result if originals.get(f['shift_id']) is None or not overlaps(shift,originals.get(f['shift_id']))]
        if shift.store_id==store_id:
            outcome=resolve_attendance_outcome(shift,[],presumptive=True,db=db)
            if outcome['credited_employee_ids'] and not outcome['ambiguous']:
                outcome.update(outcome='ATTESTED_HISTORICAL',evidence_class='MANAGEMENT_ATTESTED',reason='RETROSPECTIVELY_ENTERED_AND_LOCKED')
            outcome.update(retrospectively_entered=True,management_attested=True)
            result.append({'shift_id':shift.id,'shift_date':shift.shift_date,**outcome})
    return sorted(result,key=lambda f:(f['shift_date'],f['shift_id']))


def set_historical_lead(db, *, principal, shift, assigned=True, expected_version=None, reason=None):
    require_admin(principal)
    period=_lock_period(db,shift.schedule_period_id)
    if not period.is_historical:raise SchedulingValidationError('Choose a historical entry week.')
    require_historical_editor(db,period,principal)
    _require_draft(period,expected_version=expected_version)
    reason=_reason(reason or period.historical_reason or '')
    if shift.employee_id is None:raise SchedulingValidationError('Choose the employee who held Lead responsibility.')
    previous=list(db.scalars(select(ScheduleShift).where(ScheduleShift.schedule_period_id==period.id,
            ScheduleShift.shift_date==shift.shift_date,ScheduleShift.is_lead_of_day.is_(True))))
    if assigned:
        for row in previous:
            row.is_lead_of_day=False;row.lead_of_day_manually_assigned=False
        db.flush()
    shift.is_lead_of_day=assigned;shift.lead_of_day_manually_assigned=assigned
    period.version+=1;period.updated_by_principal_id=principal.id;period.updated_at=datetime.now(timezone.utc)
    db.flush();_audit(db,principal,period,'HISTORICAL_LEAD_ATTESTED' if assigned else 'HISTORICAL_LEAD_CLEARED',reason,
                     shift_id=shift.id,employee_id=shift.employee_id,shift_date=shift.shift_date.isoformat(),
                     previous_shift_ids=[row.id for row in previous])
    return shift


def historical_workload_minutes(db, *, employee_id, start_date, end_date):
    """Resolved completed work only; never feed backfill into future reservations."""
    from app.services.v2_scheduling_policy_service import _longview_historical_credit_rows
    from app.services.v2_scheduling_service import scheduled_paid_minutes
    from app.services.v2_scheduling_lead_duty_service import business_today
    cutoff=min(end_date+timedelta(days=1),business_today(db))
    facts=[f for store_id in db.scalars(select(Store.id))
           for f in _longview_historical_credit_rows(db,store_id=store_id,before_date=cutoff)
           if start_date<=f['shift_date']<=end_date and employee_id in f['credited_employee_ids']]
    return sum(scheduled_paid_minutes(db.get(ScheduleShift,f['shift_id'])) for f in facts)


def resolved_historical_week_rows(db, *, employee_id, week_start, exclude_shift_id=None):
    """Historical workload adapter for existing weekly hour/count reporting.

    None preserves the operational planning path. Only an attested, completed
    week is eligible, and the shared resolver supplies attendance precedence.
    """
    from app.services.v2_scheduling_lead_duty_service import business_today
    if not db.scalar(select(SchedulePeriod.id).where(SchedulePeriod.week_start_date==week_start,
            SchedulePeriod.historical_locked_at.is_not(None)).limit(1)):
        return None
    cutoff=min(week_start+timedelta(days=7),business_today(db))
    from app.services.v2_scheduling_policy_service import _longview_historical_credit_rows
    facts=[f for store_id in db.scalars(select(Store.id))
        for f in _longview_historical_credit_rows(db,store_id=store_id,before_date=cutoff)
        if week_start<=f['shift_date']<week_start+timedelta(days=7)
        and employee_id in f['credited_employee_ids'] and f['shift_id']!=exclude_shift_id]
    return [db.get(ScheduleShift,f['shift_id']) for f in facts]
