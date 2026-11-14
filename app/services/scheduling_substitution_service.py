"""Transfer existing assignments. No generation, optimization or warning rebuild.

The caller owns the scheduling transaction/savepoint. Published intent remains
immutable: only its editable revision changes. All hard conflicts are checked
before assignment writes; preference/fairness rules are not substitution gates.
"""
from collections import defaultdict
from datetime import datetime, timezone
from sqlalchemy import select
from app import models as m
from app.auth import Role
from app.services.v2_scheduling_service import (
    SchedulingValidationError, clone_published_revision, _shift_values,
)
from app.services.v2_scheduling_rules_service import _audit


def _hard_conflicts(db, incoming, targets, removed_ids, periods):
    from app.services.v2_scheduling_lifecycle_service import eligible_on, cutoff_message
    from app.services.v2_scheduling_policy_service import (effective_assignment_rows,
        consecutive_policy_reasons, MAX_ALLOWED_CONSECUTIVE_WORK_DAYS)
    if not targets:
        return
    first, last = min(s.shift_date for s in targets), max(s.shift_date for s in targets)
    # Rest boundaries include adjacent weeks, not only the replacement dates.
    # The edited drafts define these weeks. Adjacent unedited weeks retain
    # their normal authoritative revision (including published coverage evidence).
    previous = db.info.get('scheduling_swap_periods')
    db.info['scheduling_swap_periods'] = {**(previous or {}), **{p.week_start_date:p for p in periods}}
    try:
        existing = effective_assignment_rows(db, employee_id=incoming.id)
    finally:
        if previous is None:
            db.info.pop('scheduling_swap_periods',None)
        else:
            db.info['scheduling_swap_periods'] = previous
    by_day = defaultdict(list)
    for row in existing:
        if row.id not in removed_ids:
            by_day[row.shift_date].append(row)
    windows = list(db.scalars(select(m.EmployeeSchedulingWindow).where(
        m.EmployeeSchedulingWindow.employee_id == incoming.id,
        m.EmployeeSchedulingWindow.kind == m.SchedulingWindowKind.HARD_UNAVAILABLE,
        m.EmployeeSchedulingWindow.active.is_(True))))
    from app.services.v2_scheduling_lifecycle_service import profile_in_use_clause
    profile = db.scalar(select(m.EmployeeSchedulingProfile).where(
        m.EmployeeSchedulingProfile.employee_id == incoming.id, profile_in_use_clause()))
    requests = list(db.scalars(select(m.TimeOffRequest).where(
        m.TimeOffRequest.employee_id == incoming.id,
        m.TimeOffRequest.status == m.TimeOffRequestStatus.APPROVED,
        m.TimeOffRequest.start_date <= last, m.TimeOffRequest.end_date >= first)))
    work_dates = {r.shift_date for r in existing if r.id not in removed_ids} | {r.shift_date for r in targets}
    if profile:
        for day in {r.shift_date for r in targets}:
            hard_rest = [r for r in consecutive_policy_reasons(work_dates=work_dates,
                proposed_date=day, max_consecutive_work_days=MAX_ALLOWED_CONSECUTIVE_WORK_DAYS,
                minimum_days_off_after_max_block=profile.minimum_days_off_after_max_block)
                if r.code == 'REQUIRED_DAYS_OFF']
            if hard_rest:
                raise SchedulingValidationError(hard_rest[0].message)
    stores = {r.id:r for r in db.scalars(select(m.Store).where(
        m.Store.id.in_({s.store_id for s in targets})))}
    for row in targets:
        if not eligible_on(incoming, row.shift_date):
            raise SchedulingValidationError(cutoff_message(incoming))
        if (not stores.get(row.store_id) or not stores[row.store_id].active
                or row.start_time >= row.end_time or row.unpaid_break_minutes < 0
                or ((row.end_time.hour*60+row.end_time.minute)
                    -(row.start_time.hour*60+row.start_time.minute)) <= row.unpaid_break_minutes):
            raise SchedulingValidationError('Replacement requires an active store and valid shift times.')
        if row.is_lead_of_day and not incoming.scheduling_lead_capable:
            raise SchedulingValidationError('This shift requires a lead-capable replacement. Select a qualified employee or reassign Lead responsibility first.')
        weekday = (row.shift_date.weekday()+1)%7
        if (profile and profile.weekday_preferences and profile.weekday_preferences[weekday]=='CANNOT_WORK'
                or any(w.day_of_week==weekday and w.start_time<row.end_time and w.end_time>row.start_time for w in windows)):
            raise SchedulingValidationError('Recurring hard availability blocks this replacement.')
        if any(r.start_date<=row.shift_date<=r.end_date and
               (r.full_day or r.start_time is not None and r.end_time is not None
                and r.start_time<row.end_time and r.end_time>row.start_time) for r in requests):
            raise SchedulingValidationError('Approved time off overlaps this replacement.')
        for other in by_day[row.shift_date]:
            if other.store_id != row.store_id:
                raise SchedulingValidationError('Employees may work at only one store per day.')
            if other.start_time < row.end_time and other.end_time > row.start_time:
                raise SchedulingValidationError('Employee already has an overlapping assignment.')
        by_day[row.shift_date].append(row)


def substitute_assignments(db, *, principal, incoming_id, outgoing_id,
                           effective_date, allowed_store_ids, now=None):
    from app.services.scheduling_training_service import (
        require_manage, local_now, _periods, enrollments, future_shift,
    )
    require_manage(db, principal)
    if principal.role != Role.ADMIN:
        raise PermissionError('Administrator replacement permission is required.')
    incoming, outgoing = db.get(m.Employee,incoming_id), db.get(m.Employee,outgoing_id)
    local = local_now(db, now)
    records = enrollments(db,incoming_id)
    enrollment = records[-1] if records else None
    if not outgoing or incoming_id==outgoing_id:
        raise SchedulingValidationError('Choose a different replacing employee.')
    if effective_date<local.date() or enrollment and effective_date<enrollment.start_date:
        raise SchedulingValidationError('Replacement must start on or after today and the training start date.')
    periods = _periods(db,effective_date)
    originals = defaultdict(list)
    for row in db.scalars(select(m.ScheduleShift).where(
            m.ScheduleShift.schedule_period_id.in_([p.id for p in periods]))
            .order_by(m.ScheduleShift.id)):
        originals[row.schedule_period_id].append(row)
    selected = []
    for period in periods:
        targets = [s for s in originals[period.id] if s.employee_id==outgoing_id
            and not s.is_training and s.shift_date>=effective_date and future_shift(s,local)]
        training = [s for s in originals[period.id] if s.employee_id==incoming_id
            and s.is_training and s.shift_date>=effective_date and future_shift(s,local)]
        if targets or training:
            selected.append((period, targets, training))
    affected = [r for _,targets,training in selected for r in targets+training]
    if any(r.store_id not in allowed_store_ids for r in affected):
        raise PermissionError('Replacement includes assignments outside your store scope; nothing can be partially applied.')
    # Evidence is usually empty. Read it once, rather than issuing two evidence
    # queries and walking history for every assignment.
    evidence = set(db.scalars(select(m.ScheduleAttendanceEvent.schedule_shift_id).where(
        m.ScheduleAttendanceEvent.voided_at.is_(None))))
    evidence.update(db.scalars(select(m.ScheduleCoverageCommitment.schedule_shift_id).where(
        m.ScheduleCoverageCommitment.voided_at.is_(None))))
    if evidence:
        from app.services.v2_scheduling_exception_service import shift_lineage
        from app.services.read_scope import reuse_reads
        with reuse_reads(db):
            for row in affected:
                if any(s.id in evidence and s.shift_date==row.shift_date for s in shift_lineage(db,row)):
                    raise SchedulingValidationError('Resolve active attendance or coverage commitments for this future shift before replacement.')
    _hard_conflicts(db, incoming, [r for _,targets,_ in selected for r in targets],
                    {r.id for _,_,training in selected for r in training},
                    [period for period,_,_ in selected])
    for source, targets, _ in selected:
        doubles = [s for s in originals[source.id] if s.is_double_coverage
                   and (s.employee_id==incoming_id or s in targets)]
        if len(doubles)>1:
            raise SchedulingValidationError('The replacement would duplicate this employee’s Double Coverage designation.')
    stores = {r.id:r.name for r in db.scalars(select(m.Store).where(
        m.Store.id.in_({s.store_id for s in affected})))}
    changes, removals, created, period_ids = [], [], [], []
    timestamp = datetime.now(timezone.utc)
    for source, targets, training in selected:
        if source.status==m.SchedulePeriodStatus.PUBLISHED:
            from app.services.access_control_service import principal_has_permission
            if not principal_has_permission(db,principal=principal,
                    permission_key='scheduling.modify_published',fallback_allowed=True):
                raise PermissionError('Published-schedule modification permission is required.')
            period = clone_published_revision(db,principal=principal,
                published_period_id=source.id,allowed_store_ids=tuple(allowed_store_ids),
                _refresh_warnings=False)
            copies = {r.source_shift_id:r for r in db.scalars(select(m.ScheduleShift).where(
                m.ScheduleShift.schedule_period_id==period.id))}
            created.append({'source_period_id':source.id,'week':str(source.week_start_date)})
        else:
            period = source
            copies = {r.id:r for r in originals[source.id]}
        for original in training:
            row = copies[original.id]
            removals.append(dict(shift_id=original.id,date=str(row.shift_date),store_id=row.store_id,
                start=str(row.start_time),end=str(row.end_time),protected=row.manually_locked,
                store_name=stores[row.store_id]))
            _audit(db,principal=principal,action='TRAINING_RECONCILED',entity_type='schedule_shift',
                entity_id=row.id,before=_shift_values(row),metadata={'replacement_effective_date':str(effective_date)})
            db.delete(row)
        for original in targets:
            row = copies[original.id]
            before = _shift_values(row)
            protected = bool(row.manually_locked or row.lead_of_day_manually_assigned or row.double_coverage_manually_assigned)
            row.employee_id=incoming.id
            row.manually_locked=True;row.locked_by_principal_id=principal.id
            row.locked_at=timestamp;row.lock_reason='Employee First Schedule Date transition.'
            row.updated_by_principal_id=principal.id;row.updated_at=timestamp
            # Store/date/time/type and every coverage designation are inherited.
            changes.append(dict(shift_id=original.id,period_id=source.id,date=str(row.shift_date),
                store_id=row.store_id,start=str(row.start_time),end=str(row.end_time),
                break_minutes=row.unpaid_break_minutes,store_name=stores[row.store_id],
                from_employee_id=outgoing_id,to_employee_id=incoming_id,protected=protected,
                lead=row.is_lead_of_day,shift_lead=row.is_shift_lead))
            _audit(db,principal=principal,action='SHIFT_CHANGED',entity_type='schedule_shift',
                entity_id=row.id,before=before,after=_shift_values(row),
                reason='Administrator directed employee substitution.',
                metadata={'profile_schedule_transition':True,'direct_substitution':True})
        period.version+=1
        period.updated_by_principal_id=principal.id;period.updated_at=timestamp
        period_ids.append(period.id)
    if enrollment:
        enrollment.completed_effective_date=effective_date
    db.flush()
    return dict(training_id=enrollment.id if enrollment else None,
        incoming_id=incoming_id,incoming_name=incoming.full_name,outgoing_id=outgoing_id,
        outgoing_name=outgoing.full_name,effective_date=str(effective_date),changes=changes,
        training_removals=removals,draft_revisions=created,conflicts=[],period_ids=period_ids,
        direct_substitution=True)
