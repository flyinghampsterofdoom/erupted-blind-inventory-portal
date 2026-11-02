"""Supplemental paid training and reviewed, atomic draft-only replacement.

No generation, publication, employment termination or provider writes occur here.
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from itertools import combinations, product
from zoneinfo import ZoneInfo
import hashlib
import json
import uuid
from sqlalchemy import select
from app import models as m
from app.auth import Role
from app.services.v2_scheduling_service import (
    SchedulingConflict, SchedulingValidationError, atomic_schedule_change,
    scheduled_paid_minutes, clone_published_revision, _shift_values,
)
from app.services.v2_scheduling_rules_service import _audit


def local_now(db, now=None):
    from app.services.v2_scheduling_policy_service import organization_policy
    return (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(organization_policy(db).timezone_name))


def require_manage(db, principal):
    from app.services.access_control_service import principal_has_permission
    if not principal or not principal.active or principal.role not in (Role.ADMIN, Role.MANAGER):
        raise PermissionError('Training and replacement require an authorized Owner or manager.')
    for key in ('scheduling.manage_preferences','scheduling.edit_draft_shifts'):
        if not principal_has_permission(db,principal=principal,permission_key=key,fallback_allowed=True):
            raise PermissionError('Training and replacement permission is required.')


def training_end(enrollment):
    end=enrollment.start_date+timedelta(days=enrollment.duration_days)
    return min(end,enrollment.completed_effective_date) if enrollment.completed_effective_date else end


def training_on(db, employee_id, day):
    from app.services.read_scope import read_value
    rows=read_value(db,('training_enrollments',employee_id),lambda:list(db.scalars(
        select(m.EmployeeTraining).where(m.EmployeeTraining.employee_id==employee_id))))
    return next((r for r in reversed(rows) if r.start_date<=day<training_end(r)),None)


def enrollments(db, employee_id):
    return list(db.scalars(select(m.EmployeeTraining).where(m.EmployeeTraining.employee_id==employee_id)
                           .order_by(m.EmployeeTraining.start_date,m.EmployeeTraining.id)))


def is_staffing(db, shift):
    return not getattr(shift,'is_training',False) and not (
        shift.employee_id and training_on(db,shift.employee_id,shift.shift_date))


def future_shift(shift, now):
    return shift.shift_date>now.date() or (shift.shift_date==now.date() and shift.start_time>now.time().replace(tzinfo=None))


def mark_training_trainers(db, rows):
    ids={r.id for r in rows}
    reserved=set(db.scalars(select(m.ScheduleShift.trainer_shift_id).where(
        m.ScheduleShift.is_training.is_(True),m.ScheduleShift.trainer_shift_id.in_(ids)))) if ids else set()
    for r in rows:r._training_reserved=r.id in reserved
    return reserved


@atomic_schedule_change
def start_training(db, *, principal, employee_id, start_date, duration_days=14, now=None):
    require_manage(db,principal)
    today=local_now(db,now).date()
    if start_date<today:raise SchedulingValidationError('Training cannot be started in the past.')
    if not 1<=duration_days<=365:raise SchedulingValidationError('Choose a training duration from 1 to 365 calendar days.')
    employee=db.get(m.Employee,employee_id)
    if employee is None:raise SchedulingValidationError('Employee not found.')
    end=start_date+timedelta(days=duration_days)
    if any(r.start_date<end and training_end(r)>start_date for r in enrollments(db,employee_id)):
        raise SchedulingValidationError('An existing training period overlaps these dates.')
    # Do not silently turn existing required staffing into supplemental coverage.
    existing=db.scalar(select(m.ScheduleShift.id).join(m.SchedulePeriod).where(
        m.ScheduleShift.employee_id==employee_id,m.ScheduleShift.is_training.is_(False),
        m.ScheduleShift.shift_date>=start_date,m.ScheduleShift.shift_date<end,
        m.SchedulePeriod.status.in_((m.SchedulePeriodStatus.DRAFT,m.SchedulePeriodStatus.PUBLISHED)),
        m.SchedulePeriod.is_historical.is_(False)).limit(1))
    if existing:raise SchedulingValidationError('Resolve this employee’s ordinary assignments in the training dates before starting training.')
    row=m.EmployeeTraining(employee_id=employee_id,start_date=start_date,duration_days=duration_days,
        created_by_principal_id=principal.id)
    db.add(row);db.flush()
    _audit(db,principal=principal,action='TRAINING_STARTED',entity_type='employee_training',entity_id=row.id,
        metadata={'employee_id':employee_id,'start_date':str(start_date),'duration_days':duration_days})
    return row


@atomic_schedule_change
def adjust_duration(db, *, principal, training_id, duration_days, now=None):
    require_manage(db,principal)
    row=db.get(m.EmployeeTraining,training_id)
    if row is None or row.completed_effective_date:raise SchedulingValidationError('Choose an unfinished training period.')
    if not 1<=duration_days<=365:raise SchedulingValidationError('Choose 1 to 365 calendar days.')
    new_end=row.start_date+timedelta(days=duration_days)
    if new_end<local_now(db,now).date():raise SchedulingValidationError('Use End Training for early completion; historical training dates are retained.')
    if any(other.id!=row.id and other.start_date<new_end and training_end(other)>row.start_date
           for other in enrollments(db,row.employee_id)):
        raise SchedulingValidationError('The new duration overlaps another training period.')
    if db.scalar(select(m.ScheduleShift.id).where(m.ScheduleShift.training_id==row.id,
            m.ScheduleShift.shift_date>=new_end).limit(1)):
        raise SchedulingValidationError('Move or remove training assignments beyond the new end date first.')
    if db.scalar(select(m.ScheduleShift.id).join(m.SchedulePeriod).where(
            m.ScheduleShift.employee_id==row.employee_id,m.ScheduleShift.is_training.is_(False),
            m.ScheduleShift.shift_date>=row.start_date,m.ScheduleShift.shift_date<new_end,
            m.SchedulePeriod.status.in_((m.SchedulePeriodStatus.DRAFT,m.SchedulePeriodStatus.PUBLISHED))).limit(1)):
        raise SchedulingValidationError('Resolve ordinary assignments inside the extended training period first.')
    before=row.duration_days;row.duration_days=duration_days;db.flush()
    _audit(db,principal=principal,action='TRAINING_DURATION_CHANGED',entity_type='employee_training',entity_id=row.id,
        before={'duration_days':before},after={'duration_days':duration_days},metadata={'employee_id':row.employee_id})
    return row


def trainer_reasons(db, shift):
    reasons=[]
    enrollment=db.get(m.EmployeeTraining,shift.training_id) if shift.training_id else None
    trainer=db.get(m.ScheduleShift,shift.trainer_shift_id) if shift.trainer_shift_id else None
    if not enrollment or enrollment.employee_id!=shift.employee_id or not enrollment.start_date<=shift.shift_date<training_end(enrollment):
        reasons.append('Training assignment falls outside its employee’s training period.')
    if (not trainer or not is_staffing(db,trainer) or trainer.employee_id!=shift.trainer_employee_id
            or trainer.employee_id==shift.employee_id or trainer.schedule_period_id!=shift.schedule_period_id
            or trainer.store_id!=shift.store_id or trainer.shift_date!=shift.shift_date
            or trainer.start_time>shift.start_time or trainer.end_time<shift.end_time):
        reasons.append('Choose a trainer scheduled at the same store for the entire training shift.')
    elif not db.get(m.Employee,trainer.employee_id).scheduling_lead_capable and not trainer.is_shift_lead and not shift.trainer_override_reason:
        reasons.append('A non-lead trainer requires an authorized trainer override with a reason.')
    if trainer and trainer.employee_id and not reasons:
        from app.services.v2_scheduling_policy_service import evaluate_assignment
        from app.services.scheduling_operational_exception_service import assignment_reasons_remaining
        verdict=evaluate_assignment(db,employee_id=trainer.employee_id,store_id=trainer.store_id,
            shift_date=trainer.shift_date,start_time=trainer.start_time,end_time=trainer.end_time,
            unpaid_break_minutes=trainer.unpaid_break_minutes,exclude_shift_id=trainer.id,schedule_period_id=trainer.schedule_period_id)
        if assignment_reasons_remaining(db,trainer,verdict.reasons):
            reasons.append('The selected trainer is not eligible for this scheduled assignment.')
    return reasons


def _add_training(db, *, principal, enrollment, trainer, override_reason='', shift=None, start_time=None, end_time=None, unpaid_break_minutes=None):
    from app.services.v2_scheduling_policy_service import evaluate_assignment
    if not trainer.employee_id or not is_staffing(db,trainer):raise SchedulingValidationError('Choose an ordinary assigned trainer shift.')
    person=db.get(m.Employee,trainer.employee_id)
    if not person.scheduling_lead_capable and not trainer.is_shift_lead and not override_reason.strip():
        raise SchedulingValidationError('Confirm a trainer override and give a reason for a non-lead trainer.')
    start_time=start_time or trainer.start_time;end_time=end_time or trainer.end_time
    unpaid_break_minutes=trainer.unpaid_break_minutes if unpaid_break_minutes is None else unpaid_break_minutes
    minutes=end_time.hour*60+end_time.minute-start_time.hour*60-start_time.minute
    if minutes<=0 or not 0<=unpaid_break_minutes<minutes:
        raise SchedulingValidationError('Enter valid training times and a break shorter than the shift.')
    verdict=evaluate_assignment(db,employee_id=enrollment.employee_id,store_id=trainer.store_id,
        shift_date=trainer.shift_date,start_time=start_time,end_time=end_time,
        unpaid_break_minutes=unpaid_break_minutes,exclude_shift_id=shift.id if shift else None,
        schedule_period_id=trainer.schedule_period_id,training_assignment=True)
    if not verdict.eligible:raise SchedulingValidationError('; '.join(r.message for r in verdict.reasons))
    row=shift or m.ScheduleShift(created_by_principal_id=principal.id)
    row.schedule_period_id=trainer.schedule_period_id;row.employee_id=enrollment.employee_id
    row.store_id=trainer.store_id;row.shift_date=trainer.shift_date
    row.start_time=start_time;row.end_time=end_time;row.unpaid_break_minutes=unpaid_break_minutes
    row.is_training=True;row.training_id=enrollment.id;row.trainer_shift_id=trainer.id
    row.trainer_employee_id=trainer.employee_id;row.trainer_override_reason=override_reason.strip() or None
    row.is_lead_of_day=False;row.is_shift_lead=False;row.is_double_coverage=False
    row.updated_by_principal_id=principal.id;row.updated_at=datetime.now(timezone.utc)
    db.add(row);db.flush()
    problems=trainer_reasons(db,row)
    if problems:raise SchedulingValidationError('; '.join(problems))
    return row


@atomic_schedule_change
def assign_trainer(db, *, principal, training_id, trainer_shift_id, allowed_store_ids,
                   shift_id=None, confirmed_override=False, override_reason='', start_time=None, end_time=None, unpaid_break_minutes=None, now=None):
    require_manage(db,principal)
    enrollment=db.get(m.EmployeeTraining,training_id)
    trainer=db.get(m.ScheduleShift,trainer_shift_id)
    if not enrollment or not trainer:raise SchedulingValidationError('Training period or trainer not found.')
    row=db.get(m.ScheduleShift,shift_id) if shift_id else None
    if shift_id and (not row or not row.is_training or row.training_id!=enrollment.id):
        raise SchedulingValidationError('Choose this employee’s training assignment.')
    for item in [trainer,*([row] if row else [])]:
        per=db.get(m.SchedulePeriod,item.schedule_period_id)
        if per.status!=m.SchedulePeriodStatus.DRAFT or per.is_historical or per.cleared_at or not future_shift(item,local_now(db,now)):
            raise SchedulingValidationError('Training changes require a future assignment in an operational draft.')
        if item.store_id not in allowed_store_ids:raise PermissionError('Training assignment is outside your store scope.')
    if confirmed_override and not override_reason.strip():raise SchedulingValidationError('Enter a trainer override reason.')
    before=_shift_values(row) if row else None
    old_period=db.get(m.SchedulePeriod,row.schedule_period_id) if row else None
    result=_add_training(db,principal=principal,enrollment=enrollment,trainer=trainer,
        override_reason=override_reason if confirmed_override else '',shift=row,
        start_time=start_time,end_time=end_time,unpaid_break_minutes=unpaid_break_minutes)
    result.manually_locked=True;result.locked_by_principal_id=principal.id;result.lock_reason='Manual training assignment.'
    for per in {p.id:p for p in [db.get(m.SchedulePeriod,result.schedule_period_id),old_period] if p}.values():per.version+=1
    _audit(db,principal=principal,action='TRAINER_ASSIGNED',entity_type='schedule_shift',entity_id=result.id,
        before=before,after=_shift_values(result),metadata={'training_id':enrollment.id,'trainer_employee_id':result.trainer_employee_id,
        'trainer_shift_id':trainer.id,'trainer_override':confirmed_override,'reason':override_reason if confirmed_override else None})
    return result


@atomic_schedule_change
def remove_training_shift(db, *, principal, shift_id, allowed_store_ids, confirmed=False, now=None):
    require_manage(db,principal)
    row=db.get(m.ScheduleShift,shift_id)
    if not row or not row.is_training:raise SchedulingValidationError('Training assignment not found.')
    per=db.get(m.SchedulePeriod,row.schedule_period_id)
    if per.status!=m.SchedulePeriodStatus.DRAFT or per.is_historical or per.cleared_at or not future_shift(row,local_now(db,now)):
        raise SchedulingValidationError('Only future draft training assignments can be removed.')
    if row.store_id not in allowed_store_ids:raise PermissionError('Store outside your scope.')
    if row.manually_locked and not confirmed:raise SchedulingValidationError('Explicitly confirm removal of this protected training assignment.')
    _audit(db,principal=principal,action='TRAINING_SHIFT_REMOVED',entity_type='schedule_shift',entity_id=row.id,
        before=_shift_values(row),metadata={'training_id':row.training_id})
    db.delete(row);per.version+=1;db.flush()


@atomic_schedule_change
def schedule_training(db, *, principal, period, allowed_store_ids, training_ids=None, now=None):
    """Add trainees alongside existing staffed leads; never move a regular shift."""
    require_manage(db,principal)
    if period.status!=m.SchedulePeriodStatus.DRAFT or period.is_historical or period.cleared_at:
        raise SchedulingValidationError('Training is scheduled only into operational drafts.')
    from app.services.v2_scheduling_context import CONTEXT_KEY
    from app.services.v2_scheduling_policy_service import evaluate_assignment, SimulatedAssignment, weekly_work_pattern
    previous=db.info.get(CONTEXT_KEY);db.info[CONTEXT_KEY]=period.id
    made=[];unplaced=[];local=local_now(db,now)
    try:
        rows=list(db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==period.id)))
        leads=[r for r in rows if r.employee_id and r.store_id in allowed_store_ids and future_shift(r,local)
               and is_staffing(db,r) and (db.get(m.Employee,r.employee_id).scheduling_lead_capable or r.is_shift_lead)]
        for enrollment in db.scalars(select(m.EmployeeTraining).where(
                m.EmployeeTraining.start_date<=period.week_end_date).order_by(m.EmployeeTraining.id)):
            if training_ids is not None and enrollment.id not in training_ids:continue
            if training_end(enrollment)<=max(period.week_start_date,local.date()):continue
            employee=db.get(m.Employee,enrollment.employee_id)
            if not employee or not employee.scheduling_active:continue
            pattern=weekly_work_pattern(db,employee_id=employee.id,shift_date=period.week_start_date)
            target=max(0,(pattern.target_shifts or 0)-pattern.worked_shifts)
            if not target:continue
            # Freeze reads during pattern comparison. Consider alternate lead
            # shifts on each day; the first lead may have a longer shift that
            # would otherwise hide a feasible pairing within weekly hours.
            from app.services.v2_scheduling_repair_service import _PlanningReads
            from app.services.v2_scheduling_policy_service import _work_dates,scheduled_weekly_hours
            from app.services.scheduling_preferences import prefers_three_day_blocks
            from app.services.scheduling_operational_exception_service import assignment_reasons_remaining
            db.flush()
            reads=_PlanningReads(db)
            prof=reads.scalar(select(m.EmployeeSchedulingProfile).where(m.EmployeeSchedulingProfile.employee_id==employee.id))
            existing_days=_work_dates(reads,employee.id,None)
            existing_hours=scheduled_weekly_hours(reads,employee_id=employee.id,shift_date=period.week_start_date)
            by_day={}
            for trainer in sorted(leads,key=lambda r:(not r.is_lead_of_day,not r.is_shift_lead,r.id)):
                if not enrollment.start_date<=trainer.shift_date<training_end(enrollment) or trainer.employee_id==employee.id:continue
                teacher=evaluate_assignment(reads,employee_id=trainer.employee_id,store_id=trainer.store_id,
                    shift_date=trainer.shift_date,start_time=trainer.start_time,end_time=trainer.end_time,
                    unpaid_break_minutes=trainer.unpaid_break_minutes,exclude_shift_id=trainer.id,schedule_period_id=period.id)
                if assignment_reasons_remaining(db,trainer,teacher.reasons):continue
                verdict=evaluate_assignment(reads,employee_id=employee.id,store_id=trainer.store_id,shift_date=trainer.shift_date,
                    start_time=trainer.start_time,end_time=trainer.end_time,unpaid_break_minutes=trainer.unpaid_break_minutes,
                    schedule_period_id=period.id,training_assignment=True)
                if verdict.eligible:by_day.setdefault(trainer.shift_date,[]).append(trainer)
            choices=[]
            for count in range(min(target,len(by_day)),0,-1):
                for option in combinations(sorted(by_day),count):
                    if prof and prof.maximum_weekly_hours is not None:
                        minimum=sum(min(scheduled_paid_minutes(r) for r in by_day[d]) for d in option)
                        if existing_hours+minimum/Decimal(60)>prof.maximum_weekly_hours:continue
                    for trainers in product(*(by_day[d] for d in option)):
                        simulated=[];valid=True
                        for r in trainers:
                            verdict=evaluate_assignment(reads,employee_id=employee.id,store_id=r.store_id,shift_date=r.shift_date,
                                start_time=r.start_time,end_time=r.end_time,unpaid_break_minutes=r.unpaid_break_minutes,
                                schedule_period_id=period.id,training_assignment=True,simulated_assignments=tuple(simulated))
                            if not verdict.eligible:valid=False;break
                            simulated.append(SimulatedAssignment(r.shift_date,r.start_time,r.end_time,r.unpaid_break_minutes))
                        if valid:
                            work=existing_days|set(option)
                            score=sum(sum(d+timedelta(days=n) in option for n in range(3)) for d in work
                                      if all(d+timedelta(days=n) in work for n in range(3))) if prefers_three_day_blocks(prof) else 0
                            choices.append((-count,-score,-sum(r.is_lead_of_day for r in trainers),tuple(r.id for r in trainers),trainers))
                            break
                if choices:break
            selected=min(choices)[-1] if choices else ()
            for trainer in selected:
                row=_add_training(db,principal=principal,enrollment=enrollment,trainer=trainer)
                made.append(row.id)
            if len(selected)<target:unplaced.append({'employee_id':employee.id,'missing_training_shifts':target-len(selected)})
        if made:
            period.version+=1
            _audit(db,principal=principal,action='TRAINING_SCHEDULED',entity_type='schedule_period',entity_id=period.id,
                   metadata={'training_shift_ids':made,'unplaced':unplaced})
        return {'training_shift_ids':made,'unplaced':unplaced}
    finally:
        if previous is None:db.info.pop(CONTEXT_KEY,None)
        else:db.info[CONTEXT_KEY]=previous


def _fingerprint(db):
    # The review is invalidated by changed schedules, availability, qualifications,
    # training, eligibility or policy, including adjacent-week reservations.
    names=['stores','employees','employee_scheduling_profiles','employee_scheduling_windows',
        'employee_scheduling_store_preferences','time_off_requests','schedule_periods','schedule_shifts',
        'employee_training','scheduling_eligibility_periods','coverage_requirements','store_operating_hours',
        'store_special_hours','scheduling_organization_policies','special_store_policies','special_store_rotation_states',
        'scheduling_allocation_changes','schedule_attendance_events','schedule_coverage_commitments']
    h=hashlib.sha256()
    for name in names:
        table=m.Base.metadata.tables[name]
        values=[dict(r) for r in db.execute(select(table).order_by(table.c.id)).mappings()]
        h.update(json.dumps([name,values],sort_keys=True,default=str).encode())
    return h.hexdigest()


def _periods(db, effective_date):
    rows=list(db.scalars(select(m.SchedulePeriod).where(
        m.SchedulePeriod.status.in_((m.SchedulePeriodStatus.DRAFT,m.SchedulePeriodStatus.PUBLISHED)),
        m.SchedulePeriod.is_historical.is_(False),m.SchedulePeriod.cleared_at.is_(None),
        m.SchedulePeriod.week_end_date>=effective_date).order_by(m.SchedulePeriod.revision_number,m.SchedulePeriod.id)))
    selected={}
    for row in sorted(rows,key=lambda p:(p.status==m.SchedulePeriodStatus.DRAFT,p.revision_number,p.id)):
        selected[row.week_start_date]=row
    return sorted(selected.values(),key=lambda p:p.week_start_date)


def _replacement(db, *, principal, enrollment_id, outgoing_id, effective_date, allowed_store_ids,
                 protected_ids=(), override_policy=False, override_pto=False, reason='', now=None):
    """Called inside a savepoint for preview or one atomic confirmation."""
    from app.services.v2_scheduling_policy_service import evaluate_assignment
    from app.services.v2_scheduling_horizon_service import planning_periods
    from app.services.scheduling_operational_exception_service import validate_reasons
    require_manage(db,principal)
    enrollment=db.get(m.EmployeeTraining,enrollment_id)
    if not enrollment:raise SchedulingValidationError('Training period not found.')
    incoming=db.get(m.Employee,enrollment.employee_id);outgoing=db.get(m.Employee,outgoing_id)
    if not outgoing or outgoing.id==incoming.id:raise SchedulingValidationError('Choose a different outgoing employee.')
    local=local_now(db,now)
    if effective_date<local.date() or effective_date<enrollment.start_date:
        raise SchedulingValidationError('Replacement must start on or after today and the training start date.')
    if enrollment.completed_effective_date:raise SchedulingValidationError('This training period has already been completed.')
    changes=[];removals=[];conflicts=[];periods=[];created=[]; changed_rows={}
    protected_ids=set(protected_ids)
    for source in _periods(db,effective_date):
        original=list(db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==source.id).order_by(m.ScheduleShift.id)))
        targets=[r for r in original if r.employee_id==outgoing_id and not r.is_training
                 and r.shift_date>=effective_date and future_shift(r,local)]
        training=[r for r in original if r.employee_id==incoming.id and r.is_training
                  and r.shift_date>=effective_date and future_shift(r,local)]
        if not targets and not training:continue
        if any(r.store_id not in allowed_store_ids for r in [*targets,*training]):
            raise PermissionError('Replacement includes assignments outside your store scope; nothing can be partially applied.')
        for r in [*targets,*training]:
            from app.services.v2_scheduling_exception_service import shift_lineage
            from app.services.read_scope import active_evidence
            lineage=[a.id for a in shift_lineage(db,r) if a.shift_date==r.shift_date]
            if active_evidence(db,m.ScheduleAttendanceEvent,lineage) or active_evidence(db,m.ScheduleCoverageCommitment,lineage):
                conflicts.append({'shift_id':r.id,'code':'EXISTING_SHIFT_EVIDENCE',
                    'message':'Resolve active attendance or coverage commitments for this future shift before replacement.'})
            if (r.manually_locked or r.lead_of_day_manually_assigned or r.double_coverage_manually_assigned) and r.id not in protected_ids:
                conflicts.append({'shift_id':r.id,'code':'PROTECTED_ASSIGNMENT','message':'Explicitly approve changing this protected assignment.'})
        if source.status==m.SchedulePeriodStatus.PUBLISHED:
            from app.services.access_control_service import principal_has_permission
            if not principal_has_permission(db,principal=principal,permission_key='scheduling.modify_published',fallback_allowed=True):
                raise PermissionError('Creating a draft revision of a published week requires published-schedule modification permission.')
            period=clone_published_revision(db,principal=principal,published_period_id=source.id,allowed_store_ids=tuple(allowed_store_ids))
            copies={r.source_shift_id:r for r in db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==period.id))}
            created.append({'source_period_id':source.id,'week':str(source.week_start_date)})
        else:period=source;copies={r.id:r for r in original}
        periods.append(period)
        for r in training:
            row=copies[r.id]
            removals.append({'shift_id':r.id,'date':str(r.shift_date),'store_id':r.store_id,'start':str(r.start_time),'end':str(r.end_time),
                             'protected':r.manually_locked,'store_name':db.get(m.Store,r.store_id).name})
            _audit(db,principal=principal,action='TRAINING_RECONCILED',entity_type='schedule_shift',entity_id=row.id,
                before=_shift_values(row),metadata={'training_id':enrollment.id,'replacement_effective_date':str(effective_date)})
            db.delete(row)
        db.flush()
        changed_rows[period.id]=[]
        for r in targets:
            row=copies[r.id]
            was_protected=bool(r.manually_locked or r.lead_of_day_manually_assigned or r.double_coverage_manually_assigned)
            if r.is_lead_of_day and not incoming.scheduling_lead_capable:
                conflicts.append({'shift_id':r.id,'code':'LEAD_QUALIFICATION','message':'This shift requires a lead-capable replacement. Reassign the lead responsibility first, or select a qualified employee.'})
            if r.is_shift_lead and principal.role!=Role.ADMIN:
                conflicts.append({'shift_id':r.id,'code':'SHIFT_LEAD_OWNER','message':'The Owner must approve transferring a temporary shift Lead designation.'})
            if row.is_double_coverage and db.scalar(select(m.ScheduleShift.id).where(
                    m.ScheduleShift.schedule_period_id==period.id,m.ScheduleShift.employee_id==incoming.id,
                    m.ScheduleShift.is_double_coverage.is_(True),m.ScheduleShift.id!=row.id).limit(1)):
                raise SchedulingValidationError('The replacement would duplicate this employee’s Double Coverage designation. Resolve the existing designation first.')
            before=_shift_values(row)
            row.employee_id=incoming.id;row.manually_locked=True
            row.locked_by_principal_id=principal.id;row.locked_at=datetime.now(timezone.utc);row.lock_reason='Confirmed one-for-one employee replacement.'
            row.updated_by_principal_id=principal.id;row.updated_at=datetime.now(timezone.utc)
            changes.append({'shift_id':r.id,'period_id':source.id,'date':str(r.shift_date),'store_id':r.store_id,
                'start':str(r.start_time),'end':str(r.end_time),'break_minutes':r.unpaid_break_minutes,
                'store_name':db.get(m.Store,r.store_id).name,
                'from_employee_id':outgoing_id,'to_employee_id':incoming.id,
                'protected':was_protected,
                'lead':row.is_lead_of_day,'shift_lead':row.is_shift_lead})
            changed_rows[period.id].append((row,before,r.id))
        period.version+=1
    enrollment.completed_effective_date=effective_date
    db.flush()
    with planning_periods(db,periods):
        for period in periods:
            from app.services.v2_scheduling_opening_service import load_opening_calendar,preparation_start,OpeningConfigurationError
            calendar=load_opening_calendar(db,store_ids={r.store_id for r,_,_ in changed_rows[period.id]},
                start_date=period.week_start_date,end_date=period.week_end_date)
            changed=[r for r,_,_ in changed_rows[period.id]]
            for row,before,original_id in changed_rows[period.id]:
                store=db.get(m.Store,row.store_id)
                if not store or not store.active:
                    conflicts.append({'shift_id':original_id,'code':'INACTIVE_STORE','message':'Replacement requires an active store.'})
                try:
                    interval=calendar.interval(row.store_id,row.shift_date)
                    if (interval is None or row.start_time!=preparation_start(row.shift_date,interval[0]) or row.end_time>interval[1]):
                        conflicts.append({'shift_id':original_id,'code':'OPERATING_HOURS','message':'Resolve this shift’s operating hours in the Schedule Board before transferring it.'})
                except OpeningConfigurationError as exc:
                    conflicts.append({'shift_id':original_id,'code':'OPERATING_HOURS','message':str(exc)})
                verdict=evaluate_assignment(db,employee_id=incoming.id,store_id=row.store_id,shift_date=row.shift_date,
                    start_time=row.start_time,end_time=row.end_time,unpaid_break_minutes=row.unpaid_break_minutes,
                    exclude_shift_id=row.id,schedule_period_id=period.id)
                try:
                    exceptions=validate_reasons(db,principal,verdict.reasons,confirmed=override_policy,reason=reason,override_pto=override_pto)
                except (SchedulingValidationError,PermissionError) as exc:
                    exceptions=[]
                    conflicts.extend({'shift_id':original_id,'code':v.code,'message':v.message} for v in verdict.reasons)
                    if not verdict.reasons:raise
                _audit(db,principal=principal,action='SHIFT_CHANGED',entity_type='schedule_shift',entity_id=row.id,
                    before=before,after=_shift_values(row),reason=reason or None,
                    metadata={'operational_exceptions':exceptions,'training_replacement':True})
            # Revalidate trainer dependencies and company-wide Lead coverage on
            # affected future dates only; never retag a past or unrelated shift.
            db.flush()
            all_rows=list(db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==period.id)))
            affected={r.shift_date for r in changed}
            for row in all_rows:
                if row.is_training:
                    for problem in trainer_reasons(db,row):
                        if row.shift_date>=effective_date:
                            conflicts.append({'shift_id':row.id,'code':'TRAINER_DEPENDENCY','message':problem})
            for day in affected:
                leads=[r for r in all_rows if r.shift_date==day and r.is_lead_of_day and r.employee_id and is_staffing(db,r)
                       and db.get(m.Employee,r.employee_id).scheduling_lead_capable]
                from app.services.scheduling_operational_exception_service import assignment_reasons_remaining
                eligible_leads=[]
                for lead in leads:
                    check=evaluate_assignment(db,employee_id=lead.employee_id,store_id=lead.store_id,
                        shift_date=lead.shift_date,start_time=lead.start_time,end_time=lead.end_time,
                        unpaid_break_minutes=lead.unpaid_break_minutes,exclude_shift_id=lead.id,schedule_period_id=period.id)
                    if not assignment_reasons_remaining(db,lead,check.reasons):eligible_leads.append(lead)
                if len(eligible_leads)!=1:
                    conflicts.append({'shift_id':None,'code':'LEAD_COVERAGE','message':f'{day}: retain one eligible company-wide scheduled Lead.'})
    db.flush()
    return {'training_id':enrollment_id,'incoming_id':incoming.id,'incoming_name':incoming.full_name,
        'outgoing_id':outgoing_id,'outgoing_name':outgoing.full_name,'effective_date':str(effective_date),
        'changes':changes,'training_removals':removals,'draft_revisions':created,'conflicts':conflicts,
        'period_ids':[p.id for p in periods]}


@atomic_schedule_change
def preview_replacement(db, *, principal, training_id, outgoing_id, effective_date, allowed_store_ids,
                        protected_ids=(), override_policy=False, override_pto=False, reason='', now=None):
    require_manage(db,principal)
    fingerprint=_fingerprint(db)
    tx=db.begin_nested()
    try:
        preview=_replacement(db,principal=principal,enrollment_id=training_id,outgoing_id=outgoing_id,
            effective_date=effective_date,allowed_store_ids=allowed_store_ids,protected_ids=protected_ids,
            override_policy=override_policy,override_pto=override_pto,reason=reason,now=now)
    finally:tx.rollback()
    # IDs of proposed draft copies are transient and never used as authority.
    preview.pop('period_ids')
    payload={'preview':preview,'training_id':training_id,'outgoing_id':outgoing_id,'effective_date':str(effective_date),
        'protected_ids':list(protected_ids),'override_policy':override_policy,'override_pto':override_pto,'reason':reason}
    proposal=m.SchedulingReplacementProposal(id=str(uuid.uuid4()),created_by_principal_id=principal.id,
        expires_at=(now or datetime.now(timezone.utc))+timedelta(minutes=30),fingerprint=fingerprint,payload=payload)
    db.add(proposal);db.flush();return proposal


@atomic_schedule_change
def confirm_replacement(db, *, principal, proposal_id, allowed_store_ids, confirmed=False, now=None):
    require_manage(db,principal)
    proposal=db.scalar(select(m.SchedulingReplacementProposal).where(m.SchedulingReplacementProposal.id==proposal_id).with_for_update())
    if not proposal or proposal.created_by_principal_id!=principal.id:raise PermissionError('Review your own replacement proposal before confirming.')
    if not confirmed:raise SchedulingValidationError('Explicit confirmation of the reviewed changes is required.')
    if proposal.applied_at:return proposal.result
    if (now or datetime.now(timezone.utc))>=proposal.expires_at:raise SchedulingConflict('This preview expired. Prepare a fresh preview.')
    if proposal.fingerprint!=_fingerprint(db):raise SchedulingConflict('Scheduling information changed after preview. Review a fresh proposal.')
    p=proposal.payload
    if p['preview']['conflicts']:raise SchedulingValidationError('Resolve the listed conflicts and prepare a fresh preview before confirming.')
    result=_replacement(db,principal=principal,enrollment_id=p['training_id'],outgoing_id=p['outgoing_id'],
        effective_date=date.fromisoformat(p['effective_date']),allowed_store_ids=allowed_store_ids,protected_ids=p['protected_ids'],
        override_policy=p['override_policy'],override_pto=p['override_pto'],reason=p['reason'],now=now)
    if result['conflicts']:raise SchedulingValidationError('Replacement failed revalidation: '+'; '.join(c['message'] for c in result['conflicts']))
    if result['changes']!=p['preview']['changes'] or result['training_removals']!=p['preview']['training_removals']:
        raise SchedulingConflict('The reviewed change set changed. Prepare a fresh preview.')
    proposal.applied_at=now or datetime.now(timezone.utc);proposal.result=result
    _audit(db,principal=principal,action='TRAINING_COMPLETED_REPLACEMENT',entity_type='employee_training',entity_id=p['training_id'],
        metadata={'proposal_id':proposal.id,'result':result,'outgoing_employment_unchanged':True})
    db.flush();return result
