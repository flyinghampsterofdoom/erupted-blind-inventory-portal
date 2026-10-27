"""Atomic exchanges and transfers on the existing request/audit infrastructure."""
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from sqlalchemy import select, or_
from app.auth import Role
from app.models import (Employee, Principal as PrincipalModel, PrincipalRole, ScheduleShift,
    SchedulePeriod, SchedulePeriodStatus, ShiftTransferRequest, ShiftTransferStatus, Store)
from app.services.access_control_service import principal_has_permission
from app.services.v2_scheduling_service import (atomic_schedule_change, SchedulingConflict,
    SchedulingValidationError, clear_base_pattern_annotation, validate_employee_store_day)
from app.services import v2_scheduling_policy_service as policy
from app.services.scheduling_operational_exception_service import (
    OPERATIONAL_CODES, PTO_CODES, validate_reasons)

ACTIVE=(ShiftTransferStatus.PENDING_RECIPIENT,ShiftTransferStatus.PENDING_MANAGER)


def now():return datetime.now(timezone.utc)


def access(db,principal,*,management=False,shifts=()):
    from app.config import settings
    if not settings.scheduling_employee_requests_enabled:
        raise SchedulingConflict('Shift requests are temporarily paused for release rollback; history is retained.')
    key='scheduling.approve_transfer_hours' if management else 'scheduling.transfer_own'
    fallback=principal.role in ((Role.ADMIN,Role.MANAGER,Role.LEAD) if management else tuple(Role))
    from app.services.scheduling_shift_lead_service import has_shift_lead_permission
    if management and shifts and all(has_shift_lead_permission(db, principal=principal,
            permission_key=key, shift=shift) for shift in shifts):
        return
    if not principal.active or not principal_has_permission(db,principal=principal,permission_key=key,fallback_allowed=fallback):
        raise PermissionError('Shift request permission is required.')


def snapshot(db,shift):
    return {key:str(getattr(shift,key)) for key in ('id','schedule_period_id','employee_id',
        'store_id','shift_date','start_time','end_time','unpaid_break_minutes','is_lead_of_day','updated_at')} | {
        'period_version':db.get(SchedulePeriod,shift.schedule_period_id).version}


def locked_shifts(db,ids):
    return list(db.scalars(select(ScheduleShift).where(ScheduleShift.id.in_(ids)).order_by(
        ScheduleShift.id).with_for_update().execution_options(populate_existing=True)))


def targets(row):
    result={row.shift_id:row.to_employee_id}
    if row.counterpart_shift_id:result[row.counterpart_shift_id]=row.from_employee_id
    return result


def boundary(db,row,shifts,*,today=None,owner_approval=False):
    by_id={s.id:s for s in shifts}
    for sid,eid in targets(row).items():
        shift=by_id.get(sid)
        if shift is None:raise SchedulingConflict('A requested shift no longer exists.')
        period=db.get(SchedulePeriod,shift.schedule_period_id)
        if period.is_historical or period.status!=SchedulePeriodStatus.PUBLISHED:
            raise SchedulingConflict('Only current published assignments may be exchanged.')
        expected=row.from_employee_id if sid==row.shift_id else row.to_employee_id
        policy._validate_transfer_boundary(db,SimpleNamespace(from_employee_id=expected),shift,today=today)
        saved=(row.assignment_snapshot or {}).get(str(sid))
        if saved and snapshot(db,shift)!=saved:
            raise SchedulingConflict('A shift or schedule changed since this request was sent. Send a new request.')
        employee=db.get(Employee,eid)
        principal=db.get(PrincipalModel,employee.principal_id) if employee and employee.principal_id else None
        if employee is None or not policy.is_scheduling_candidate(employee) or principal is None or not principal.active:
            raise SchedulingValidationError('A participant no longer has an active employee account.')
        if shift.is_lead_of_day and not employee.scheduling_lead_capable and not owner_approval:
            raise SchedulingValidationError('The replacement must hold the required Lead qualification.')
        span=(shift.end_time.hour*60+shift.end_time.minute)-(shift.start_time.hour*60+shift.start_time.minute)
        if span<=0 or shift.unpaid_break_minutes<0 or shift.unpaid_break_minutes>=span:
            raise SchedulingValidationError('Invalid shift times or break duration cannot be overridden.')
    # Employee row locks serialize overlapping requests and current policy checks.
    list(db.scalars(select(Employee).where(Employee.id.in_((row.from_employee_id,row.to_employee_id)))
        .order_by(Employee.id).with_for_update().execution_options(populate_existing=True)))


def evaluate_exchange(db,row,shifts):
    """Read the net result by excluding each person's outgoing assignment.

    The incoming shift still belongs to the coworker until the atomic completion.
    No speculative DB writes, warning rebuilds or generation are needed.
    """
    results=[]
    for shift in shifts:
        eid=targets(row)[shift.id]
        outgoing=(row.counterpart_shift_id if shift.id==row.shift_id else row.shift_id) if row.counterpart_shift_id else shift.id
        validate_employee_store_day(db,schedule_period_id=shift.schedule_period_id,
            employee_id=eid,shift_date=shift.shift_date,store_id=shift.store_id,exclude_shift_id=outgoing)
        verdict=policy.evaluate_assignment(db,employee_id=eid,store_id=shift.store_id,
            shift_date=shift.shift_date,start_time=shift.start_time,end_time=shift.end_time,
            unpaid_break_minutes=shift.unpaid_break_minutes,exclude_shift_id=outgoing,
            schedule_period_id=shift.schedule_period_id)
        hard=[r for r in verdict.reasons if r.code not in OPERATIONAL_CODES | PTO_CODES]
        if hard:raise SchedulingValidationError('; '.join(r.message for r in hard))
        results.append((shift.id,eid,verdict))
    return results


def warnings_for(results):
    reasons=[dict(shift_id=sid,employee_id=eid,code=r.code,message=r.message)
        for sid,eid,v in results for r in v.reasons]
    reasons.extend(dict(shift_id=sid,employee_id=eid,code='HOURS_APPROVAL',
        message=f'Resulting hours {v.resulting_hours} exceed the approval threshold {v.approval_threshold_hours}.')
        for sid,eid,v in results if v.requires_hour_approval)
    return reasons


def audit(db,principal,row,event,**data):
    from app.services.v2_scheduling_rules_service import _audit
    _audit(db,principal=principal,action=event,entity_type='shift_transfer_request',entity_id=row.id,
        reason=data.get('override_reason') or data.get('reason'),
        before=data.pop('before',None),after=data.pop('after',None),
        metadata={'kind':'SWAP' if row.counterpart_shift_id else 'TRANSFER',
         'shift_id':row.shift_id,'counterpart_shift_id':row.counterpart_shift_id,**data})


def notify(db,row,kind,message):
    for eid in (row.from_employee_id,row.to_employee_id):
        employee=db.get(Employee,eid)
        policy._notify(db,employee.principal_id if employee else None,kind,message,row.id)


@atomic_schedule_change
def create_request(db,*,principal,shift_id,to_employee_id=None,counterpart_shift_id=None,operation_key=None,today=None):
    access(db,principal)
    giver=policy._employee_for_principal(db,principal)
    if operation_key and (len(operation_key)>100 or not operation_key.strip()):
        raise SchedulingValidationError('Invalid request operation key.')
    previous=db.scalar(select(ShiftTransferRequest).where(
        ShiftTransferRequest.initiated_by_principal_id==principal.id,
        ShiftTransferRequest.operation_key==operation_key)) if operation_key else None
    if previous:
        if (previous.shift_id!=shift_id or previous.counterpart_shift_id!=counterpart_shift_id
                or (not counterpart_shift_id and previous.to_employee_id!=to_employee_id)):
            raise SchedulingConflict('This operation key already identifies a different request.')
        return previous
    if counterpart_shift_id==shift_id:raise SchedulingValidationError('Choose two different shifts.')
    shifts=locked_shifts(db,[shift_id]+([counterpart_shift_id] if counterpart_shift_id else []))
    by_id={s.id:s for s in shifts};source=by_id.get(shift_id)
    if source is None or source.employee_id!=giver.id:
        raise PermissionError('You may request changes only involving your own assignment.')
    if db.get(SchedulePeriod,source.schedule_period_id).status!=SchedulePeriodStatus.PUBLISHED:
        raise PermissionError('Employees may request changes only to published assignments.')
    if counterpart_shift_id:
        other=by_id.get(counterpart_shift_id)
        if other is None:raise SchedulingValidationError('Choose an existing coworker shift.')
        to_employee_id=other.employee_id
    if to_employee_id is None or to_employee_id==giver.id:
        raise SchedulingValidationError('Choose another employee’s assigned shift or an active coworker.')
    busy=db.scalar(select(ShiftTransferRequest).where(ShiftTransferRequest.status.in_(ACTIVE),or_(
        ShiftTransferRequest.shift_id.in_(by_id),ShiftTransferRequest.counterpart_shift_id.in_(by_id))).limit(1))
    if busy:
        if (busy.initiated_by_principal_id==principal.id and busy.shift_id==shift_id and
                busy.counterpart_shift_id==counterpart_shift_id and busy.to_employee_id==to_employee_id):return busy
        raise SchedulingConflict('One of these shifts already has a pending request. Resolve or cancel it first.')
    row=ShiftTransferRequest(shift_id=shift_id,counterpart_shift_id=counterpart_shift_id,
        from_employee_id=giver.id,to_employee_id=to_employee_id,initiated_by_principal_id=principal.id,
        operation_key=operation_key,status=ShiftTransferStatus.PENDING_RECIPIENT)
    boundary(db,row,shifts,today=today)
    verdicts=evaluate_exchange(db,row,shifts)
    row.assignment_snapshot={str(s.id):snapshot(db,s) for s in shifts}
    row.policy_warnings=warnings_for(verdicts)
    db.add(row);db.flush()
    policy._notify(db,db.get(Employee,to_employee_id).principal_id,'SHIFT_TRANSFER_REQUESTED',
        f'{giver.full_name} requested a '+('two-way swap.' if counterpart_shift_id else 'shift transfer.'),row.id)
    audit(db,principal,row,'SHIFT_SWAP_REQUESTED' if counterpart_shift_id else 'SHIFT_TRANSFER_REQUESTED',warnings=row.policy_warnings)
    return row


def invalidate(db,principal,row,exc):
    row.status=ShiftTransferStatus.CANCELLED;row.invalidated_at=now();row.updated_at=now()
    row.resolution_reason=str(exc)
    audit(db,principal,row,'SHIFT_REQUEST_INVALIDATED',reason=str(exc))
    notify(db,row,'SHIFT_REQUEST_INVALIDATED',str(exc));db.flush();return row


def complete(db,principal,row,shifts,verdicts,*,operational_override=False,override_pto=False,reason=''):
    for sid,eid,verdict in verdicts:
        validate_reasons(db,principal,verdict.reasons,confirmed=operational_override,
            reason=reason,override_pto=override_pto)
    from app.services.v2_scheduling_service import _shift_values
    before={str(s.id):_shift_values(s) for s in shifts}
    stamp=now();periods=set()
    for shift in shifts:
        shift.employee_id=targets(row)[shift.id];shift.updated_at=stamp;shift.updated_by_principal_id=principal.id
        from app.services.scheduling_shift_lead_service import apply_designation
        apply_designation(db, principal=principal, shift=shift, before=before[str(shift.id)],
            allowed_store_ids=(shift.store_id,))
        if shift.is_lead_of_day and not db.get(Employee, shift.employee_id).scheduling_lead_capable:
            shift.is_lead_of_day = False; shift.lead_of_day_manually_assigned = False
        clear_base_pattern_annotation(shift)
        shift.manually_locked=True;shift.locked_at=stamp;shift.locked_by_principal_id=principal.id
        shift.lock_reason='Completed employee '+('swap' if row.counterpart_shift_id else 'transfer')
        periods.add(shift.schedule_period_id)
    db.flush()
    for pid in periods:
        period=db.get(SchedulePeriod,pid);period.version+=1;period.updated_at=stamp;period.updated_by_principal_id=principal.id
    from app.services.v2_scheduling_rules_service import _audit
    for shift in shifts:
        _audit(db,principal=principal,action='SHIFT_REQUEST_ASSIGNMENT_CHANGED',entity_type='schedule_shift',entity_id=shift.id,
            before=before[str(shift.id)],after=_shift_values(shift),reason=reason or 'Employee consented request',
            metadata={'request_id':row.id,'shift_id':shift.id,'employee_id':shift.employee_id,
                'operational_override':operational_override,'restrictions':warnings_for(verdicts),
                'operational_exceptions':[w for w in warnings_for(verdicts) if w['shift_id']==shift.id and w['employee_id']==shift.employee_id] if operational_override else []})
    for eid in (row.from_employee_id,row.to_employee_id):db.get(Employee,eid).working_context_version+=1
    row.status=ShiftTransferStatus.COMPLETED;row.completed_at=stamp;row.updated_at=stamp
    row.policy_warnings=warnings_for(verdicts)
    db.flush()
    audit(db,principal,row,'SHIFT_SWAP_COMPLETED' if row.counterpart_shift_id else 'SHIFT_TRANSFER_COMPLETED',
        before=before,after={str(s.id):_shift_values(s) for s in shifts},
        original_employee_id=row.from_employee_id,new_employee_id=row.to_employee_id,
        override_authorizer=principal.id if operational_override else None,
        override_reason=reason if operational_override else None,restrictions=row.policy_warnings)
    notify(db,row,'SHIFT_TRANSFER_COMPLETED','Shift '+('swap' if row.counterpart_shift_id else 'transfer')+' completed.')
    # Cancel competing legacy requests safely; new requests reserve both legs.
    for other in db.scalars(select(ShiftTransferRequest).where(ShiftTransferRequest.id!=row.id,
            ShiftTransferRequest.status.in_(ACTIVE),or_(ShiftTransferRequest.shift_id.in_(targets(row)),
            ShiftTransferRequest.counterpart_shift_id.in_(targets(row))))):
        invalidate(db,principal,other,SchedulingConflict('The assignment changed through another completed request.'))
    from app.services.v2_scheduling_coverage_service import rebuild_schedule_warnings
    for pid in sorted(periods):rebuild_schedule_warnings(db,schedule_period_id=pid)
    return row


@atomic_schedule_change
def respond_request(db,*,principal,request_id,accept,today=None):
    access(db,principal);employee=policy._employee_for_principal(db,principal)
    row=db.scalar(select(ShiftTransferRequest).where(ShiftTransferRequest.id==request_id).with_for_update())
    if row is None or row.to_employee_id!=employee.id:raise PermissionError('This request is not directed to you.')
    if row.status!=ShiftTransferStatus.PENDING_RECIPIENT:
        if row.recipient_responded_at and ((accept and row.status in (ShiftTransferStatus.PENDING_MANAGER,ShiftTransferStatus.COMPLETED)) or (not accept and row.status==ShiftTransferStatus.DECLINED)):
            return row
        raise SchedulingConflict('This request is already resolved.')
    if not row.assignment_snapshot and not row.counterpart_shift_id:
        return policy.respond_to_transfer(db,principal=principal,request_id=request_id,accept=accept,today=today)
    if not accept:
        row.status=ShiftTransferStatus.DECLINED;row.recipient_responded_at=now();row.updated_at=now()
        audit(db,principal,row,'SHIFT_TRANSFER_DECLINED');notify(db,row,'SHIFT_TRANSFER_DECLINED','Shift request declined.');return row
    shifts=locked_shifts(db,list(targets(row)))
    try:
        boundary(db,row,shifts,today=today);verdicts=evaluate_exchange(db,row,shifts)
    except (SchedulingConflict,SchedulingValidationError) as exc:return invalidate(db,principal,row,exc)
    row.recipient_responded_at=now();row.updated_at=now();row.policy_warnings=warnings_for(verdicts)
    # Retain the existing hours-approval threshold for either participant.
    needs_hours=any(v.requires_hour_approval for _,_,v in verdicts)
    _,_,v=verdicts[0]
    row.existing_scheduled_hours=v.scheduled_hours;row.resulting_scheduled_hours=v.resulting_hours
    row.shift_hours=v.resulting_hours-v.scheduled_hours;row.approval_threshold_hours=v.approval_threshold_hours
    row.amount_over_threshold=max(Decimal(0),v.resulting_hours-v.approval_threshold_hours)
    if needs_hours or row.policy_warnings:
        row.status=ShiftTransferStatus.PENDING_MANAGER
        audit(db,principal,row,'SHIFT_TRANSFER_PENDING_MANAGER',warnings=row.policy_warnings)
        for manager in db.scalars(select(PrincipalModel).where(PrincipalModel.active.is_(True),PrincipalModel.role.in_((PrincipalRole.ADMIN,PrincipalRole.MANAGER)))):
            policy._notify(db,manager.id,'SHIFT_TRANSFER_APPROVAL_REQUIRED','Shift request requires management review.',row.id)
        notify(db,row,'SHIFT_TRANSFER_PENDING_MANAGER','Accepted; awaiting management approval.')
        return row
    return complete(db,principal,row,shifts,verdicts)


@atomic_schedule_change
def review_request(db,*,principal,request_id,approve,note='',today=None,operational_override=False,override_pto=False):
    row=db.scalar(select(ShiftTransferRequest).where(ShiftTransferRequest.id==request_id).with_for_update())
    if row is None:raise SchedulingConflict('Request not found.')
    access(db,principal,management=True,shifts=locked_shifts(db,list(targets(row))))
    if (approve and row.status==ShiftTransferStatus.COMPLETED) or (not approve and row.status==ShiftTransferStatus.REJECTED):return row
    if row.status!=ShiftTransferStatus.PENDING_MANAGER:raise SchedulingConflict('Request is not awaiting management approval.')
    if not row.assignment_snapshot and not row.counterpart_shift_id:
        return policy.review_transfer(db,principal=principal,request_id=request_id,approve=approve,note=note,today=today)
    if not approve:
        row.status=ShiftTransferStatus.REJECTED;row.manager_principal_id=principal.id
        row.manager_responded_at=now();row.manager_note=note;row.updated_at=now()
        audit(db,principal,row,'SHIFT_TRANSFER_REJECTED',reason=note);notify(db,row,'SHIFT_TRANSFER_REJECTED','Management rejected the request.');return row
    shifts=locked_shifts(db,list(targets(row)))
    try:
        boundary(db,row,shifts,today=today,owner_approval=principal.role == Role.ADMIN);verdicts=evaluate_exchange(db,row,shifts)
    except (SchedulingConflict,SchedulingValidationError) as exc:return invalidate(db,principal,row,exc)
    # Owner exceptions require a current, explicit authorization, never the sender's flag.
    for _,_,v in verdicts:
        validate_reasons(db,principal,v.reasons,confirmed=operational_override,reason=note,override_pto=override_pto)
    row.manager_principal_id=principal.id;row.manager_responded_at=now();row.manager_note=note
    audit(db,principal,row,'SHIFT_TRANSFER_APPROVED',reason=note)
    return complete(db,principal,row,shifts,verdicts,operational_override=operational_override,override_pto=override_pto,reason=note)


@atomic_schedule_change
def cancel_request(db,*,principal,request_id):
    access(db,principal);employee=policy._employee_for_principal(db,principal)
    row=db.scalar(select(ShiftTransferRequest).where(ShiftTransferRequest.id==request_id).with_for_update())
    if row is None or row.from_employee_id!=employee.id:raise PermissionError('You may cancel only your own request.')
    if row.status==ShiftTransferStatus.CANCELLED and not row.invalidated_at:return row
    if row.status not in ACTIVE:raise SchedulingConflict('Only pending requests may be cancelled.')
    row.status=ShiftTransferStatus.CANCELLED;row.updated_at=now()
    audit(db,principal,row,'SHIFT_REQUEST_CANCELLED');notify(db,row,'SHIFT_REQUEST_CANCELLED','Sender cancelled the request.');return row


def swap_options(db,*,principal,shift_id,today=None):
    """Lazy, bounded candidate evaluation; never called during schedule viewing."""
    from app.services.read_scope import reuse_reads
    access(db,principal);employee=policy._employee_for_principal(db,principal)
    source=db.get(ScheduleShift,shift_id)
    if source is None or source.employee_id!=employee.id:raise PermissionError('Choose your own shift.')
    period=db.get(SchedulePeriod,source.schedule_period_id)
    rows=list(db.scalars(select(ScheduleShift).where(ScheduleShift.schedule_period_id==period.id,
        ScheduleShift.employee_id.is_not(None),ScheduleShift.employee_id!=employee.id)
        .order_by(ScheduleShift.shift_date,ScheduleShift.start_time,ScheduleShift.id).limit(200)))
    result=[]
    with reuse_reads(db):
        for candidate in rows:
            row=ShiftTransferRequest(shift_id=source.id,counterpart_shift_id=candidate.id,
                from_employee_id=employee.id,to_employee_id=candidate.employee_id)
            try:
                boundary(db,row,[source,candidate],today=today)
                verdicts=evaluate_exchange(db,row,[source,candidate])
            except (SchedulingValidationError,SchedulingConflict):continue
            result.append({'shift_id':candidate.id,
                'label':f'{db.get(Employee,candidate.employee_id).full_name} · {candidate.shift_date} · {db.get(Store,candidate.store_id).name} · {candidate.start_time:%I:%M %p}–{candidate.end_time:%I:%M %p}',
                'requires_approval':any(v.requires_hour_approval or v.reasons for _,_,v in verdicts),
                'warnings':warnings_for(verdicts)})
    return result
