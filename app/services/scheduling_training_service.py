"""Manual paid training and atomic profile-driven transitions into regular work."""
from datetime import datetime, timedelta, timezone, date
from zoneinfo import ZoneInfo
from sqlalchemy import select
from app import models as m
from app.auth import Role
from app.services.v2_scheduling_service import (
    SchedulingConflict, SchedulingValidationError, atomic_schedule_change,
    clone_published_revision, _shift_values,
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
    # Duration is a planning guide. Only an explicitly saved First Schedule Date
    # ends training; elapsed calendar days never graduate an employee.
    return enrollment.completed_effective_date or date.max


def training_on(db, employee_id, day):
    from app.services.read_scope import read_value
    rows=read_value(db,('training_enrollments',employee_id),lambda:list(db.scalars(
        select(m.EmployeeTraining).where(m.EmployeeTraining.employee_id==employee_id))))
    applicable=[r for r in rows if r.start_date<=day]
    latest=max(applicable,key=lambda r:(r.start_date,r.id)) if applicable else None
    return latest if latest and day<training_end(latest) else None


def enrollments(db, employee_id):
    return list(db.scalars(select(m.EmployeeTraining).where(m.EmployeeTraining.employee_id==employee_id)
                           .order_by(m.EmployeeTraining.start_date,m.EmployeeTraining.id)))


def is_staffing(db, shift):
    return not getattr(shift,'is_training',False) and not (
        shift.employee_id and training_on(db,shift.employee_id,shift.shift_date))


def future_shift(shift, now):
    return shift.shift_date>now.date() or (shift.shift_date==now.date() and shift.start_time>now.time().replace(tzinfo=None))


def training_shift_reasons(db, shift):
    """Validate the enrollment while retaining immutable former schedule revisions."""
    enrollment=db.get(m.EmployeeTraining,shift.training_id) if shift.training_id else None
    if not enrollment or enrollment.employee_id!=shift.employee_id or shift.shift_date<enrollment.start_date:
        return ['Training assignment requires its employee’s training record.']
    return []


def designate_shift(db, shift):
    """Manual calendar actions derive classification from employee/date, never a form flag."""
    enrollment=training_on(db,shift.employee_id,shift.shift_date) if shift.employee_id else None
    shift.is_training=bool(enrollment)
    shift.training_id=enrollment.id if enrollment else None
    # Preserve former trainer history in old immutable revisions, not new edits.
    shift.trainer_shift_id=None;shift.trainer_employee_id=None;shift.trainer_override_reason=None
    if enrollment:
        shift.is_lead_of_day=False;shift.lead_of_day_manually_assigned=False
        shift.is_shift_lead=False;shift.is_double_coverage=False;shift.double_coverage_manually_assigned=False


@atomic_schedule_change
def start_training(db, *, principal, employee_id, start_date=None, duration_days=14, now=None):
    require_manage(db,principal)
    today=local_now(db,now).date();start_date=start_date or today
    if start_date<today:raise SchedulingValidationError('Training cannot be started in the past.')
    if not 1<=duration_days<=365:raise SchedulingValidationError('Choose a duration from 1 to 365 calendar days.')
    employee=db.scalar(select(m.Employee).where(m.Employee.id==employee_id).with_for_update().execution_options(populate_existing=True))
    if not employee:raise SchedulingValidationError('Employee not found.')
    if not employee.active:
        raise SchedulingValidationError('Activate the employee record before starting training.')
    if training_on(db,employee_id,start_date):return training_on(db,employee_id,start_date)
    if employee.first_schedule_date and employee.first_schedule_date<=start_date:
        raise SchedulingValidationError('Clear the future scheduling transition before starting a new training period.')
    end=employee.first_schedule_date or date.max
    if any(r.start_date<end and training_end(r)>start_date for r in enrollments(db,employee_id)):
        raise SchedulingValidationError('An existing training record overlaps these dates. Keep the existing training record.')
    if db.scalar(select(m.ScheduleShift.id).join(m.SchedulePeriod).where(
        m.ScheduleShift.employee_id==employee_id,m.ScheduleShift.is_training.is_(False),
        m.ScheduleShift.shift_date>=start_date,m.ScheduleShift.shift_date<end,
        m.SchedulePeriod.status.in_((m.SchedulePeriodStatus.DRAFT,m.SchedulePeriodStatus.PUBLISHED)),
        m.SchedulePeriod.is_historical.is_(False)).limit(1)):
        raise SchedulingValidationError('Resolve existing regular assignments before starting training.')
    from app.services.v2_scheduling_lifecycle_service import change_eligibility, eligible_on
    if not eligible_on(employee, start_date):
        from app.services.v2_scheduling_roster_service import square_allows_scheduling
        if start_date != today or not square_allows_scheduling(employee):
            raise SchedulingValidationError('This employee is not eligible to start training on that date.')
        # A dated start prevents automation from assigning dates before training.
        # The enrollment blocks regular assignments from this date until explicitly ended.
        change_eligibility(db, principal=principal, employee_id=employee_id,
                           action='reactivate', day=start_date, today=today)
    row=m.EmployeeTraining(employee_id=employee_id,start_date=start_date,duration_days=duration_days,
        completed_effective_date=employee.first_schedule_date,created_by_principal_id=principal.id)
    db.add(row);db.flush()
    _audit(db,principal=principal,action='TRAINING_STARTED',entity_type='employee_training',entity_id=row.id,
        metadata={'employee_id':employee_id,'start_date':str(start_date),'duration_days':duration_days,'manual_shifts':True})
    return row


@atomic_schedule_change
def adjust_duration(db, *, principal, training_id, duration_days, now=None):
    require_manage(db,principal)
    row=db.get(m.EmployeeTraining,training_id)
    if not row:raise SchedulingValidationError('Training period not found.')
    if not 1<=duration_days<=365:raise SchedulingValidationError('Choose 1 to 365 calendar days.')
    before=row.duration_days;row.duration_days=duration_days;db.flush()
    _audit(db,principal=principal,action='TRAINING_DURATION_CHANGED',entity_type='employee_training',entity_id=row.id,
        before={'duration_days':before},after={'duration_days':duration_days},metadata={'employee_id':row.employee_id})
    return row


@atomic_schedule_change
def save_transition(db, *, principal, employee_id, first_schedule_date, replacing_employee_id=None,
                    allowed_store_ids, now=None, owner_review=None):
    """One profile save prepares all changes or none; publication remains separate."""
    require_manage(db,principal)
    incoming=db.scalar(select(m.Employee).where(m.Employee.id==employee_id).with_for_update().execution_options(populate_existing=True))
    if not incoming:raise SchedulingValidationError('Employee not found.')
    if replacing_employee_id and not first_schedule_date:
        raise SchedulingValidationError('Choose a First Schedule Date for the replacement.')
    if replacing_employee_id==employee_id:
        raise SchedulingValidationError('An employee cannot replace themselves.')
    if replacing_employee_id and not db.get(m.Employee,replacing_employee_id):
        raise SchedulingValidationError('Replacing employee not found.')
    if replacing_employee_id:
        if db.scalar(select(m.Employee.id).where(m.Employee.replacing_employee_id==replacing_employee_id,
                m.Employee.first_schedule_date.is_not(None),m.Employee.id!=employee_id).limit(1)):
            raise SchedulingValidationError('This employee already has a scheduled replacement.')
        seen={employee_id};source=replacing_employee_id
        while source:
            if source in seen:raise SchedulingValidationError('Employee replacements cannot form a cycle.')
            seen.add(source);person=db.get(m.Employee,source);source=person.replacing_employee_id if person else None

    before={'first_schedule_date':str(incoming.first_schedule_date) if incoming.first_schedule_date else None,
            'replacing_employee_id':incoming.replacing_employee_id}
    if incoming.first_schedule_date==first_schedule_date and incoming.replacing_employee_id==replacing_employee_id:
        return {'changes':[],'training_removals':[],'period_ids':[],'unchanged':True}
    if incoming.first_schedule_date and incoming.first_schedule_date<=local_now(db,now).date():
        raise SchedulingValidationError('The First Schedule Date is already effective. Preserve historical work and adjust future assignments in the calendar.')
    if incoming.replacing_employee_id and incoming.replacing_employee_id!=replacing_employee_id:
        prepared=db.scalars(select(m.ScheduleShift).join(m.SchedulePeriod).where(
            m.ScheduleShift.employee_id==employee_id,
            m.ScheduleShift.lock_reason=='Employee First Schedule Date transition.',
            m.SchedulePeriod.status==m.SchedulePeriodStatus.DRAFT,
            m.SchedulePeriod.is_historical.is_(False),m.SchedulePeriod.cleared_at.is_(None)))
        if any(future_shift(row,local_now(db,now)) for row in prepared):
            raise SchedulingValidationError('Resolve the prepared replacement assignments in the calendar before choosing a different replacing employee.')
    if first_schedule_date and first_schedule_date<local_now(db,now).date():
        raise SchedulingValidationError('First Schedule Date cannot change historical work. Choose today or a future date.')
    records=enrollments(db,employee_id)
    if records and first_schedule_date and first_schedule_date<records[-1].start_date:
        raise SchedulingValidationError('First Schedule Date cannot precede training.')
    incoming.first_schedule_date=first_schedule_date;incoming.replacing_employee_id=replacing_employee_id
    result={'changes':[],'training_removals':[],'period_ids':[]}
    if first_schedule_date:
        result=_replacement(db,principal=principal,incoming_id=employee_id,outgoing_id=replacing_employee_id,
            effective_date=first_schedule_date,allowed_store_ids=allowed_store_ids,now=now,owner_review=owner_review)
        if result['conflicts']:
            raise SchedulingValidationError('Nothing was saved. ' + result['conflicts'][0]['message'])
    elif records:
        records[-1].completed_effective_date=None
    # Moving a regular-work start later must never silently reclassify regular coverage.
    for period in _periods(db,local_now(db,now).date()):
        for shift in db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==period.id,
                m.ScheduleShift.employee_id==employee_id,m.ScheduleShift.is_training.is_(False))):
            if future_shift(shift,local_now(db,now)) and (training_on(db,employee_id,shift.shift_date)
                    or first_schedule_date and shift.shift_date<first_schedule_date):
                raise SchedulingValidationError('Resolve existing regular assignments before moving the First Schedule Date.')
    if owner_review:
        owner_review.finish(db, entity_type='employee', entity_id=employee_id)
    from app.services.v2_scheduling_coverage_service import rebuild_schedule_warnings
    for period_id in result['period_ids']:
        rebuild_schedule_warnings(db, schedule_period_id=period_id)
    _audit(db,principal=principal,action='EMPLOYEE_SCHEDULE_TRANSITION_SAVED',entity_type='employee',entity_id=employee_id,
        before=before,after={'first_schedule_date':str(first_schedule_date) if first_schedule_date else None,
                           'replacing_employee_id':replacing_employee_id},metadata={'result':result,'outgoing_employment_unchanged':True})
    db.flush();return result


def _periods(db, effective_date):
    rows=list(db.scalars(select(m.SchedulePeriod).where(
        m.SchedulePeriod.status.in_((m.SchedulePeriodStatus.DRAFT,m.SchedulePeriodStatus.PUBLISHED)),
        m.SchedulePeriod.is_historical.is_(False),m.SchedulePeriod.cleared_at.is_(None),
        m.SchedulePeriod.week_end_date>=effective_date).order_by(m.SchedulePeriod.revision_number,m.SchedulePeriod.id)))
    selected={}
    for row in sorted(rows,key=lambda p:(p.status==m.SchedulePeriodStatus.DRAFT,p.revision_number,p.id)):
        selected[row.week_start_date]=row
    return sorted(selected.values(),key=lambda p:p.week_start_date)


def _replacement(db, *, principal, incoming_id, outgoing_id, effective_date, allowed_store_ids,
                 now=None, owner_review=None):
    """Prepare the selected profile transition inside the caller’s savepoint."""
    from app.services.v2_scheduling_policy_service import evaluate_assignment
    from app.services.v2_scheduling_horizon_service import planning_periods
    from app.services.scheduling_owner_review import LEAD, new_warnings, warning_snapshot
    from app.services.v2_scheduling_service import validate_employee_store_day
    require_manage(db,principal)
    incoming=db.get(m.Employee,incoming_id)
    records=enrollments(db,incoming_id)
    enrollment=records[-1] if records else None
    outgoing=db.get(m.Employee,outgoing_id) if outgoing_id else None
    if outgoing_id and (not outgoing or outgoing.id==incoming.id):raise SchedulingValidationError('Choose a different replacing employee.')
    local=local_now(db,now)
    if effective_date<local.date() or (enrollment and effective_date<enrollment.start_date):
        raise SchedulingValidationError('Replacement must start on or after today and the training start date.')
    changes=[];removals=[];conflicts=[];periods=[];created=[]; changed_rows={}; warning_baselines={}
    for source in _periods(db,effective_date):
        original=list(db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==source.id).order_by(m.ScheduleShift.id)))
        targets=[r for r in original if outgoing_id and r.employee_id==outgoing_id and not r.is_training
                 and r.shift_date>=effective_date and future_shift(r,local)]
        training=[r for r in original if r.employee_id==incoming.id and r.is_training
                  and r.shift_date>=effective_date and future_shift(r,local)]
        if not outgoing_id:targets=training;training=[]
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
            if principal.role!=Role.ADMIN and not r.is_training and (r.manually_locked or r.lead_of_day_manually_assigned or r.double_coverage_manually_assigned):
                conflicts.append({'shift_id':r.id,'code':'PROTECTED_ASSIGNMENT','message':'The Owner must save this replacement because it includes protected assignments.'})
        baseline = warning_snapshot(db, source)
        affected_days = {r.shift_date for r in [*targets, *training]}
        if source.status==m.SchedulePeriodStatus.PUBLISHED:
            from app.services.access_control_service import principal_has_permission
            if not principal_has_permission(db,principal=principal,permission_key='scheduling.modify_published',fallback_allowed=True):
                raise PermissionError('Creating a draft revision of a published week requires published-schedule modification permission.')
            period=clone_published_revision(db,principal=principal,published_period_id=source.id,allowed_store_ids=tuple(allowed_store_ids))
            copies={r.source_shift_id:r for r in db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==period.id))}
            created.append({'source_period_id':source.id,'week':str(source.week_start_date)})
        else:period=source;copies={r.id:r for r in original}
        periods.append(period)
        warning_baselines[period.id] = (baseline, affected_days)
        for r in training:
            row=copies[r.id]
            removals.append({'shift_id':r.id,'date':str(r.shift_date),'store_id':r.store_id,'start':str(r.start_time),'end':str(r.end_time),
                             'protected':r.manually_locked,'store_name':db.get(m.Store,r.store_id).name})
            _audit(db,principal=principal,action='TRAINING_RECONCILED',entity_type='schedule_shift',entity_id=row.id,
                before=_shift_values(row),metadata={'training_id':enrollment.id if enrollment else None,'replacement_effective_date':str(effective_date)})
            db.delete(row)
        db.flush()
        changed_rows[period.id]=[]
        for r in targets:
            row=copies[r.id]
            was_protected=bool(r.manually_locked or r.lead_of_day_manually_assigned or r.double_coverage_manually_assigned)
            if r.is_lead_of_day and not incoming.scheduling_lead_capable and not owner_review:
                conflicts.append({'shift_id':r.id,'code':'LEAD_QUALIFICATION','message':'This shift requires a lead-capable replacement. Reassign the lead responsibility first, or select a qualified employee.'})
            if r.is_shift_lead and principal.role!=Role.ADMIN:
                conflicts.append({'shift_id':r.id,'code':'SHIFT_LEAD_OWNER','message':'The Owner must approve transferring a temporary shift Lead designation.'})
            if row.is_double_coverage and db.scalar(select(m.ScheduleShift.id).where(
                    m.ScheduleShift.schedule_period_id==period.id,m.ScheduleShift.employee_id==incoming.id,
                    m.ScheduleShift.is_double_coverage.is_(True),m.ScheduleShift.id!=row.id).limit(1)):
                raise SchedulingValidationError('The replacement would duplicate this employee’s Double Coverage designation. Resolve the existing designation first.')
            before=_shift_values(row)
            row.employee_id=incoming.id;row.is_training=False;row.training_id=None
            if owner_review and not incoming.scheduling_lead_capable:
                row.is_lead_of_day = False
                row.lead_of_day_manually_assigned = False
            row.trainer_shift_id=None;row.trainer_employee_id=None;row.trainer_override_reason=None
            row.manually_locked=True
            row.locked_by_principal_id=principal.id;row.locked_at=datetime.now(timezone.utc);row.lock_reason='Employee First Schedule Date transition.'
            row.updated_by_principal_id=principal.id;row.updated_at=datetime.now(timezone.utc)
            changes.append({'shift_id':r.id,'period_id':source.id,'date':str(r.shift_date),'store_id':r.store_id,
                'start':str(r.start_time),'end':str(r.end_time),'break_minutes':r.unpaid_break_minutes,
                'store_name':db.get(m.Store,r.store_id).name,
                'from_employee_id':outgoing_id,'to_employee_id':incoming.id,
                'protected':was_protected,
                'lead':row.is_lead_of_day,'shift_lead':row.is_shift_lead})
            changed_rows[period.id].append((row,before,r.id))
        period.version+=1
    if enrollment:enrollment.completed_effective_date=effective_date
    db.flush()
    with planning_periods(db,periods):
        for period in periods:
            from app.services.v2_scheduling_opening_service import load_opening_calendar,preparation_start,OpeningConfigurationError
            calendar=load_opening_calendar(db,store_ids={r.store_id for r,_,_ in changed_rows[period.id]},
                start_date=period.week_start_date,end_date=period.week_end_date)
            for row,before,original_id in changed_rows[period.id]:
                store=db.get(m.Store,row.store_id)
                if not store or not store.active:
                    conflicts.append({'shift_id':original_id,'code':'INACTIVE_STORE','message':'Replacement requires an active store.'})
                if not owner_review:
                    try:
                        interval=calendar.interval(row.store_id,row.shift_date)
                        if (interval is None or row.start_time!=preparation_start(row.shift_date,interval[0]) or row.end_time>interval[1]):
                            conflicts.append({'shift_id':original_id,'code':'OPERATING_HOURS','message':'Resolve this shift’s operating hours in the Schedule Board before transferring it.'})
                    except OpeningConfigurationError as exc:
                        conflicts.append({'shift_id':original_id,'code':'OPERATING_HOURS','message':str(exc)})
                validate_employee_store_day(db, schedule_period_id=period.id, employee_id=incoming.id,
                    shift_date=row.shift_date, store_id=row.store_id, exclude_shift_id=row.id)
                verdict=evaluate_assignment(db,employee_id=incoming.id,store_id=row.store_id,shift_date=row.shift_date,
                    start_time=row.start_time,end_time=row.end_time,unpaid_break_minutes=row.unpaid_break_minutes,
                    exclude_shift_id=row.id,schedule_period_id=period.id)
                exceptions = []
                if owner_review:
                    # An employee-only transfer inherits the existing slot and its operating-hour warnings.
                    inherited = {'BUSINESS_CLOSED', 'SPECIAL_HOURS_INVALID', 'OUTSIDE_SPECIAL_HOURS'}
                    exceptions = owner_review.policy_exceptions(
                        [r for r in verdict.reasons if r.code not in inherited], row.shift_date)
                else:
                    conflicts.extend({'shift_id':original_id,'code':v.code,'message':v.message} for v in verdict.reasons)
                _audit(db,principal=principal,action='SHIFT_CHANGED',entity_type='schedule_shift',entity_id=row.id,
                    before=before,after=_shift_values(row),
                    metadata={'profile_schedule_transition':True,'operational_exceptions':exceptions},
                    reason='Owner confirmed scheduling warnings.' if exceptions else None)
            baseline, days = warning_baselines[period.id]
            if owner_review:
                owner_review.compare(baseline, warning_snapshot(db, period), days=days)
            elif any(w['code'] == LEAD for w in new_warnings(baseline, warning_snapshot(db, period), days=days)):
                conflicts.append({'shift_id':None,'code':'LEAD_COVERAGE','message':'This change would leave a date without eligible lead coverage.'})
    db.flush()
    return {'training_id':enrollment.id if enrollment else None,'incoming_id':incoming.id,'incoming_name':incoming.full_name,
        'outgoing_id':outgoing_id,'outgoing_name':outgoing.full_name if outgoing else None,'effective_date':str(effective_date),
        'changes':changes,'training_removals':removals,'draft_revisions':created,'conflicts':conflicts,
        'period_ids':[p.id for p in periods]}


def replacement_on(db, employee_id, day):
    from app.services.read_scope import read_value
    transitions=read_value(db,('employee_replacements',employee_id),lambda:list(db.scalars(select(m.Employee).where(
        m.Employee.replacing_employee_id==employee_id,m.Employee.first_schedule_date.is_not(None)))))
    return next((e for e in transitions if e.first_schedule_date<=day),None)


def regular_during(db, employee, first, last):
    if employee.first_schedule_date and employee.first_schedule_date>last:return False
    return any(not training_on(db,employee.id,day) and not replacement_on(db,employee.id,day) and
               (not employee.first_schedule_date or day>=employee.first_schedule_date)
               for day in (first+timedelta(days=i) for i in range((last-first).days+1)))
