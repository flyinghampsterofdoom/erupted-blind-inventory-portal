"""Plan complete unlocked weeks together while retaining all external evidence."""
from collections import Counter
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager
import hashlib
import json
from sqlalchemy import select
from app.models import (SchedulePeriod, SchedulePeriodStatus, ScheduleShift,
    EmployeeSchedulingProfile, SpecialStorePolicy, SpecialStoreRotationState,
    SpecialStoreParticipation)
from app.services.v2_scheduling_repair_service import _PlanningReads
from app.services.v2_scheduling_service import SchedulingConflict, SchedulingValidationError


@contextmanager
def planning_periods(db, periods):
    previous = db.info.get('scheduling_swap_periods')
    db.info['scheduling_swap_periods'] = {**(previous or {}), **{p.week_start_date:p for p in periods}}
    try:
        yield
    finally:
        if previous is None:
            db.info.pop('scheduling_swap_periods',None)
        else:
            db.info['scheduling_swap_periods'] = previous


def input_signature(rows, periods):
    values = {'positions':[position(r) for r in sorted(rows,key=lambda r:r.id)],
              'versions':[(p.id,p.version,p.constraints_changed_at.astimezone(timezone.utc).isoformat() if p.constraints_changed_at else None) for p in sorted(periods,key=lambda p:p.id)]}
    return hashlib.sha256(json.dumps(values,default=str,sort_keys=True).encode()).hexdigest()


def protected(row):
    return row.manually_locked or row.lead_of_day_manually_assigned or row.double_coverage_manually_assigned


def position(row, week=None):
    return dict(id=row.id, date=row.shift_date,
        week=week or row.shift_date-timedelta(days=(row.shift_date.weekday()+1)%7),
        employee_id=row.employee_id, store_id=row.store_id, start=row.start_time, end=row.end_time,
        minutes=(row.end_time.hour*60+row.end_time.minute-row.start_time.hour*60-row.start_time.minute-row.unpaid_break_minutes),
        protected=protected(row))


def _plan_schedule_horizon(db, *, periods, planning_date, time_limit=30, repair=True):
    """Return a reviewable plan. Temporary clearing is restored even on errors.

    Published, historical, cleared and duplicate-week inputs fail closed. Locked
    assignments remain fixed; external published/draft reservations are included
    in consecutive-day and hours constraints through the central evaluator.
    """
    from app.services.v2_scheduling_policy_service import (evaluate_assignment,
        effective_assignment_rows, assignment_score, base_pattern_score,
        weekend_fairness, longview_rotation_fairness, longview_fairness_rank, MAX_ALLOWED_CONSECUTIVE_WORK_DAYS)
    from app.services.v2_scheduling_allocation_service import weekly_allocation
    from app.services.v2_scheduling_roster_service import list_scheduling_candidates
    from app.services.v2_scheduling_lifecycle_service import eligible_during
    from app.services.scheduling_horizon_solver import solve_horizon
    if not periods or len({p.week_start_date for p in periods}) != len(periods):
        raise SchedulingValidationError('Choose distinct operational draft weeks.')
    if any(p.status != SchedulePeriodStatus.DRAFT or p.is_historical or p.cleared_at is not None for p in periods):
        raise SchedulingConflict('Only uncleared operational draft weeks can be optimized.')
    period_ids={p.id for p in periods}
    rows=list(db.scalars(select(ScheduleShift).where(ScheduleShift.schedule_period_id.in_(period_ids)).order_by(ScheduleShift.id)))
    original={r.id:r.employee_id for r in rows}
    specs=[position(r) for r in rows]
    empty_ids = {r.id for r in rows if protected(r) and r.employee_id is None}
    signature=input_signature(rows,periods)
    profiles={p.employee_id:p for p in db.scalars(select(EmployeeSchedulingProfile))}
    employees=list_scheduling_candidates(db)
    eids=[e.id for e in employees]
    from app.services.scheduling_preferences import preferred_consecutive_days
    limits={e.id:dict(max_days=MAX_ALLOWED_CONSECUTIVE_WORK_DAYS,
                     preferred_max_days=preferred_consecutive_days(profiles[e.id]),
                     rest_days=profiles[e.id].minimum_days_off_after_max_block,
                     max_minutes=int(profiles[e.id].maximum_weekly_hours*60) if profiles[e.id].maximum_weekly_hours is not None else None)
            for e in employees if e.id in profiles}
    manual_leads=Counter(r.shift_date for r in rows if r.is_lead_of_day and r.lead_of_day_manually_assigned)
    lead_ids={e.id for e in employees if e.scheduling_lead_capable}
    if any(n>1 for n in manual_leads.values()) or any(r.lead_of_day_manually_assigned
        and (not r.is_lead_of_day or r.employee_id not in lead_ids) for r in rows):
        return dict(status='INFEASIBLE',assignments=None,diagnostics=[{
            'code':'PROTECTED_LEAD_CONFLICT',
            'message':'Protected Lead designations conflict; they require an explicit management correction.'}])
    targets={};allowed={};rejections={};costs={};fixed=[];historical={}
    for r in rows:
        if not protected(r):
            r.employee_id=None
            r.is_lead_of_day=False
    db.flush()
    reads=_PlanningReads(db)
    conflicts=[]
    for row in rows:
        if not protected(row) or row.employee_id is None:
            continue
        verdict=evaluate_assignment(reads,employee_id=row.employee_id,store_id=row.store_id,
            shift_date=row.shift_date,start_time=row.start_time,end_time=row.end_time,
            unpaid_break_minutes=row.unpaid_break_minutes,exclude_shift_id=row.id,schedule_period_id=row.schedule_period_id)
        if not verdict.eligible:
            conflicts.append(dict(code='PROTECTED_ASSIGNMENT_CONFLICT',shift_id=row.id,
                employee_id=row.employee_id,store_id=row.store_id,date=row.shift_date.isoformat(),
                message=f'Locked shift {row.id}, store {row.store_id}, {row.shift_date}: '+ '; '.join(r.message for r in verdict.reasons),
                constraints=[dict(code=r.code,message=r.message) for r in verdict.reasons]))
    if conflicts:
        return dict(status='INFEASIBLE',diagnostics=conflicts,assignments=None)
    if set(limits)!=set(eids):
        return dict(status='UNRESOLVED',diagnostics=[{'code':'WEEKLY_ALLOCATION_MISSING','employee_ids':sorted(set(eids)-set(limits)),
            'message':'Configure scheduling profiles and weekly targets for the identified employees.'}],assignments=None)
    for p in periods:
        for e in employees:
            target=weekly_allocation(reads,e.id,p.week_start_date,profile=profiles[e.id]) if eligible_during(e,p.week_start_date,p.week_end_date) else 0
            if target is None:
                return dict(status='UNRESOLVED',diagnostics=[{'code':'WEEKLY_ALLOCATION_MISSING','employee_id':e.id}],assignments=None)
            targets[e.id,p.week_start_date]=target
    for e in employees:
        fixed.extend(position(r) for r in effective_assignment_rows(reads,employee_id=e.id)
                     if r.schedule_period_id not in period_ids)
        for weekday in (5,6):
            fair=weekend_fairness(reads,employee_id=e.id,weekday=weekday,
                before_date=min(p.week_start_date for p in periods),as_of_date=planning_date)
            historical[e.id,weekday]=fair.assignment_count
    for r in rows:
        if r.id in empty_ids:
            allowed[r.id] = []
            continue
        choices=[];rejected={}
        for eid in ([original[r.id]] if protected(r) and original[r.id] is not None else eids):
            result=evaluate_assignment(reads,employee_id=eid,store_id=r.store_id,
                shift_date=r.shift_date,start_time=r.start_time,end_time=r.end_time,
                unpaid_break_minutes=r.unpaid_break_minutes,exclude_shift_id=r.id,
                schedule_period_id=r.schedule_period_id)
            if not result.eligible:
                rejected[eid]=[{'code':v.code,'message':v.message} for v in result.reasons]
                continue
            choices.append(eid)
            score,_=assignment_score(reads,employee_id=eid,store_id=r.store_id,shift_date=r.shift_date)
            costs[r.id,eid]=400-score-10*base_pattern_score(reads,employee_id=eid,shift_date=r.shift_date)
        allowed[r.id]=choices;rejections[r.id]=rejected
    special=set(db.scalars(select(SpecialStorePolicy.store_id).where(SpecialStorePolicy.active.is_(True))))
    travelers={(r.store_id,r.employee_id) for r in db.scalars(select(SpecialStoreRotationState))
               if r.participation==SpecialStoreParticipation.ROTATION}
    traveler_costs={};traveler_history={}
    for store_id in special:
        pool={eid for sid,eid in travelers if sid==store_id}
        for p in periods:
            fairs={eid:longview_rotation_fairness(reads,employee_id=eid,store_id=store_id,
                before_date=p.week_start_date,as_of_date=planning_date) for eid in pool}
            ordered=sorted(pool,key=lambda eid:(longview_fairness_rank(fairs[eid]),eid))
            for rank,eid in enumerate(ordered):
                for row in rows:
                    if row.schedule_period_id==p.id and row.store_id==store_id:
                        traveler_costs[row.id,eid]=rank
                if p.week_start_date==min(v.week_start_date for v in periods):
                    traveler_history[store_id,eid]=fairs[eid].historical_assignment_count+fairs[eid].planned_future_assignment_count
    solver_inputs=dict(positions=specs,employees=eids,allowed=allowed,limits=limits,
        targets=targets,fixed=fixed,lead_ids={e.id for e in employees if e.scheduling_lead_capable},
        traveler_store_ids=special,traveler_ids=travelers,preference_costs=costs,
        historical_weekends=historical,time_limit=time_limit,traveler_costs=traveler_costs,traveler_history=traveler_history,
        repair=repair)
    result=solve_horizon(**solver_inputs)
    if result.get('assignments') is not None:
        result['assignments'].update({sid: None for sid in empty_ids})
    # Keep configured targets unchanged; assignment deviations are reported separately.
    result['allocation_exceptions']=[]
    from app.services.v2_scheduling_allocation_service import allocation_report
    reviews = {p.week_start_date.isoformat():allocation_report(db,p) for p in periods}
    for p in periods:
        prior_counts = Counter(original[r.id] for r in rows if r.schedule_period_id==p.id)
        proposed_counts = Counter(result['assignments'][r.id] for r in rows if r.schedule_period_id==p.id) if result.get('assignments') is not None else None
        for entry in reviews[p.week_start_date.isoformat()]:
            entry['actual_shifts'] = prior_counts[entry['employee_id']]
            entry['planned_shifts'] = proposed_counts[entry['employee_id']] if proposed_counts is not None else None
    result['effective_allocations'] = reviews
    result['completion_state'] = 'PLANNED' if result.get('assignments') is not None else 'HARD_CONSTRAINT_CONFLICT'
    result['diagnostics']=[]
    for row in rows:
        if protected(row) and original[row.id] is not None and not allowed.get(row.id):
            result['diagnostics'].append(dict(code='PROTECTED_ASSIGNMENT_CONFLICT',shift_id=row.id,
                employee_id=original[row.id],store_id=row.store_id,date=row.shift_date.isoformat(),
                message=f'Locked shift {row.id}, store {row.store_id}, {row.shift_date}: employee {original[row.id]} violates a hard rule.',
                constraints=rejections.get(row.id,{}).get(original[row.id],[])))
    result['input_signature']=signature
    if result['assignments'] is None:
        result['diagnostics'] += [{'code':'HORIZON_CONSTRAINT_CONFLICT' if result['status']=='INFEASIBLE' else 'HORIZON_SEARCH_LIMIT',
            'message': ('Protected assignments conflict with hard rules; resolve the identified locks explicitly.' if result['status']=='INFEASIBLE' else 'Search ended without proof of infeasibility.'),
            'conflicting_constraints':result.get('conflicting_constraints',[]),
            'employee_constraints':rejections,'weekly_allocations':[{'employee_id':e,'week':w.isoformat(),'shifts':n} for (e,w),n in targets.items()],
            'unfillable_shift_ids':[sid for sid,ids in allowed.items() if not ids]}]
    else:
        result['changes']=[dict(shift_id=r.id,date=r.shift_date.isoformat(),store_id=r.store_id,
            before_employee_id=original[r.id],after_employee_id=result['assignments'][r.id])
            for r in rows if original[r.id]!=result['assignments'][r.id]]
    if result.get('assignments') is not None:
        # Explain each vacancy against the complete proposed week, including
        # cross-position hours/consecutive constraints, not just isolated slots.
        for row in rows:
            row.employee_id=result['assignments'][row.id]
        db.flush()
        final_reads=_PlanningReads(db)
        for row in rows:
            if row.employee_id is not None or row.id in empty_ids:
                continue
            for employee in employees:
                verdict=evaluate_assignment(final_reads,employee_id=employee.id,store_id=row.store_id,
                    shift_date=row.shift_date,start_time=row.start_time,end_time=row.end_time,
                    unpaid_break_minutes=row.unpaid_break_minutes,exclude_shift_id=row.id,schedule_period_id=row.schedule_period_id)
                if verdict.reasons:
                    rejections.setdefault(row.id,{})[employee.id]=[dict(code=v.code,message=v.message) for v in verdict.reasons]
        from app.models import Store, StoreSpecialHour
        store_names={store.id:store.name for store in db.scalars(select(Store))}
        from app.models import EmployeeSchedulingStorePreference, StorePreferenceLevel
        from app.services.scheduling_optimization_explanations import explain_optimization
        result['management_explanations'] = explain_optimization(
            positions=specs, original=original, assignments=result['assignments'],
            names={e.id:e.full_name for e in employees}, targets=targets,
            preferred_days={eid:limits[eid]['preferred_max_days'] for eid in eids},
            # The same authoritative external reservations and proposed rows
            # used by the solver; explanations need no extra roster scans.
            effective_dates={eid:({r['date'] for r in fixed if r['employee_id']==eid}
                                  | {r.shift_date for r in rows if r.employee_id==eid}) for eid in eids},
            avoid_stores={(p.employee_id,p.store_id) for p in db.scalars(select(
                EmployeeSchedulingStorePreference).where(EmployeeSchedulingStorePreference.active.is_(True),
                EmployeeSchedulingStorePreference.preference_level==StorePreferenceLevel.AVOID))},
            store_names=store_names)
        closures=[dict(store_id=r.store_id,store_name=store_names.get(r.store_id),date=r.calendar_date.isoformat(),event_name=r.event_name) for r in db.scalars(select(StoreSpecialHour).where(StoreSpecialHour.active.is_(True),StoreSpecialHour.closed_all_day.is_(True)))]
        by_id = {r.id:r for r in rows}
        for sid,eid in result['assignments'].items():
            if eid is None:
                row=by_id[sid]
                result['diagnostics'].append(dict(code='UNFILLED_COVERAGE',shift_id=sid,
                    store_id=row.store_id,store_name=store_names.get(row.store_id),date=row.shift_date.isoformat(),
                    message=f'{store_names.get(row.store_id, row.store_id)}, {row.shift_date}: unfilled shift {sid}.',
                    manual_removal=sid in empty_ids,employee_constraints=rejections.get(sid,{}),
                    employee_names={e.id:e.full_name for e in employees},
                    constraints=['Approved PTO, availability, eligibility, hours, overlap, consecutive-day and special-store participation rules remain enforced.'],
                    binding_store_rules=([f'Weekly eligible traveler coverage required at store {row.store_id}.'] if row.store_id in special else []),
                    search_proven=all(stage['status']=='OPTIMAL' for stage in result.get('stages',[]) if stage['objective']=='unfilled_coverage')))
        for week, entries in reviews.items():
            for entry in entries:
                actual=entry['planned_shifts'];target=entry['effective_shifts']
                if target is not None and actual < target:
                    result['diagnostics'].append(dict(code='ALLOCATION_SHORTFALL',employee_id=entry['employee_id'],
                        week=week,target_shifts=target,assigned_shifts=actual,
                        closures=[closure for closure in closures if week <= closure['date'] <= next(p.week_end_date.isoformat() for p in periods if p.week_start_date.isoformat()==week)],
                        employee_name=next(e.full_name for e in employees if e.id==entry['employee_id']),
                        constraint_positions=[dict(shift_id=r.id,store_id=r.store_id,date=r.shift_date.isoformat(),reasons=rejections.get(r.id,{}).get(entry['employee_id'],[])) for r in rows if rejections.get(r.id,{}).get(entry['employee_id'])],
                        message=f"Employee {entry['employee_id']}, week {week}: {actual} assigned against target {target}; this does not block approval when coverage and hard rules pass."))
        result['completion_state']='INCOMPLETE' if any(eid is None for eid in result['assignments'].values()) else 'PLANNED'
    return result


def apply_schedule_horizon_plan(db, *, periods, plan, principal):
    """Apply a complete or partial independently revalidated draft in one savepoint."""
    from app.services.v2_scheduling_policy_service import evaluate_assignment
    if plan.get('assignments') is None:
        details=[]
        for item in plan.get('diagnostics',[]):
            details.append(item.get('message',item.get('code','Unresolved planning constraints.')))
            if item.get('conflicting_constraints'):
                details.append('Conflicting constraints: '+', '.join(item['conflicting_constraints'][:20]))
            if item.get('unfillable_shift_ids'):
                details.append('No eligible employee for shifts: '+', '.join(map(str,item['unfillable_shift_ids'][:20])))
        raise SchedulingValidationError('No valid horizon plan was found. '+'; '.join(details))
    ids={p.id for p in periods}
    rows=list(db.scalars(select(ScheduleShift).where(ScheduleShift.schedule_period_id.in_(ids))))
    if any(p.status!=SchedulePeriodStatus.DRAFT or p.is_historical or p.cleared_at is not None for p in periods):
        raise SchedulingConflict('Only operational draft weeks can be optimized.')
    assignments=plan['assignments']
    if set(assignments)!= {r.id for r in rows}:
        raise SchedulingConflict('The schedule positions changed; calculate a fresh plan.')
    if plan.get('input_signature') != input_signature(rows,periods):
        raise SchedulingConflict('The schedule changed; calculate a fresh plan.')
    with db.begin_nested(), planning_periods(db,periods):
        for r in rows:
            eid=assignments[r.id]
            if protected(r) and eid!=r.employee_id:
                raise SchedulingConflict('Optimization cannot change protected assignments.')
            if eid!=r.employee_id:
                if not protected(r):
                    r.is_lead_of_day=False
                    if eid is None:
                        r.is_double_coverage=False
                r.employee_id=eid;r.updated_by_principal_id=principal.id;r.updated_at=datetime.now(timezone.utc)
        db.flush()
        reads=_PlanningReads(db)
        for r in rows:
            if r.employee_id is None:
                continue
            ev=evaluate_assignment(reads,employee_id=r.employee_id,store_id=r.store_id,
                shift_date=r.shift_date,start_time=r.start_time,end_time=r.end_time,
                unpaid_break_minutes=r.unpaid_break_minutes,exclude_shift_id=r.id,
                schedule_period_id=r.schedule_period_id)
            if not ev.eligible:
                raise SchedulingValidationError(f'Plan failed independent validation at shift {r.id}: '+str(ev.reasons))


def plan_schedule_horizon(db, *, periods, planning_date, time_limit=30, repair=True):
    transaction = db.begin_nested()
    try:
        with planning_periods(db,periods):
            return _plan_schedule_horizon(db, periods=periods, planning_date=planning_date, time_limit=time_limit, repair=repair)
    finally:
        transaction.rollback()


def optimize_weekly_positions(db, *, principal, period, planning_date):
    """Use the shared target-based planner for initial generation and repair.

    Cross-week optimization is only available through the explicit horizon action.
    """
    plan=plan_schedule_horizon(db,periods=[period],planning_date=planning_date,time_limit=10,repair=period.generated_at is not None)
    if plan.get('assignments') is None:
        return dict(status='UNRESOLVED',mode='HARD_CONSTRAINT_CONFLICT',diagnostics=plan['diagnostics'],exceptions=[],search={'stages':plan.get('stages',[])})
    apply_schedule_horizon_plan(db,periods=[period],plan=plan,principal=principal)
    from app.services.v2_scheduling_allocation_service import allocation_report
    return dict(status='PASS',mode='JOINT_CONSTRAINT_OPTIMIZATION',diagnostics=plan.get('diagnostics',[]),exceptions=[],
        management_explanations=plan.get('management_explanations',[]),
        search={'exhaustive':plan.get('global_optimal',False),
                'optimality_scope':plan.get('optimality_scope'),
                'bounded_optimal':plan.get('bounded_optimal',False),
                'fairness':plan.get('fairness_search',{}),'stages':plan['stages']},
        weekly_allocations=[dict(employee_id=r['employee_id'],configured_shifts=r['effective_shifts'],assigned_shifts=r['actual_shifts'])
                            for r in allocation_report(db, period)],changes=plan.get('changes',[]))


def regenerate_schedule_horizon(db, *, principal, period_ids, planning_date, time_limit=60):
    """Explicit full-horizon regeneration; atomic snapshots for every revision.

    This is deliberately separate from append-only horizon maintenance. Merely
    running hourly maintenance must never rewrite an existing draft.
    """
    from app.services.v2_scheduling_service import lock_scheduling
    from app.services.v2_scheduling_policy_service import materialize_coverage_positions, _audit, ACTIONABLE_WARNING_TYPES
    from app.services.v2_scheduling_snapshot_service import serialize_schedule,capture_snapshot
    from app.services.v2_scheduling_assignments_service import reconcile_lead_designations
    from app.services.v2_scheduling_coverage_service import rebuild_schedule_warnings,validate_coverage_headcount
    from app.models import ScheduleLifecycleStage
    import uuid
    lock_scheduling(db)
    with db.begin_nested():
        periods=list(db.scalars(select(SchedulePeriod).where(SchedulePeriod.id.in_(period_ids))
            .order_by(SchedulePeriod.week_start_date).with_for_update().execution_options(populate_existing=True)))
        if len(periods)!=len(set(period_ids)):
            raise SchedulingValidationError('Some requested schedule periods no longer exist.')
        if any(p.status!=SchedulePeriodStatus.DRAFT or p.is_historical or p.cleared_at is not None for p in periods):
            raise SchedulingConflict('Only uncleared operational draft weeks can be optimized.')
        with planning_periods(db,periods):
            before={p.id:serialize_schedule(db,p) for p in periods}
            for p in periods:
                materialize_coverage_positions(db,principal=principal,period=p,preserve_existing=True)
                validate_coverage_headcount(db,period=p)
            plan=plan_schedule_horizon(db,periods=periods,planning_date=planning_date,time_limit=time_limit)
            apply_schedule_horizon_plan(db,periods=periods,plan=plan,principal=principal)
            batch=str(uuid.uuid4());snapshots=[];validation_errors=[]
            for p in periods:
                missing=reconcile_lead_designations(db,schedule_period_id=p.id,planning_date=planning_date)
                if missing:
                    validation_errors.extend(f'Week {p.week_start_date}: missing Lead: {item}' for item in missing)
                p.version+=1;p.generated_at=datetime.now(timezone.utc);p.lifecycle_stage=ScheduleLifecycleStage.REVIEW
                validate_coverage_headcount(db,period=p)
                final_warnings=rebuild_schedule_warnings(db,schedule_period_id=p.id)
                hard_codes=ACTIONABLE_WARNING_TYPES | {'SHIFT_ON_CLOSED_DATE','SHIFT_OUTSIDE_OPERATING_HOURS',
                    'EMPLOYEE_OVERLAP','ABOVE_MAXIMUM_HOURS'}
                hard=[w for w in final_warnings if w.warning_type in hard_codes]
                if hard:
                    validation_errors.extend(w.message for w in hard)
                from app.services.scheduling_approval_validation import validate_complete_schedule
                try:
                    validate_complete_schedule(db,period=p,allowed_store_ids=tuple({r.store_id for r in db.scalars(select(ScheduleShift).where(ScheduleShift.schedule_period_id==p.id))}))
                except SchedulingValidationError as exc:
                    validation_errors.append(f'Week {p.week_start_date}: {exc}')
                snapshot=capture_snapshot(db,period=p,principal=principal,batch_id=batch,origin='horizon_regeneration',
                    before=before[p.id],diagnostics={'optimization':plan},initial=False)
                snapshots.append(snapshot.id)
                _audit(db,principal,'SCHEDULE_REGENERATED','schedule_period',p.id,
                    {'generation_batch_id':batch,'snapshot_id':snapshot.id,'optimization_status':plan['status']})
            from app.services.v2_scheduling_allocation_service import allocation_report
            return dict(plan,validation_status='INCOMPLETE' if validation_errors else 'VALID',completion_state='INCOMPLETE' if validation_errors else 'VALID',final_validation_errors=validation_errors,effective_allocations={p.week_start_date.isoformat():allocation_report(db,p) for p in periods},snapshot_ids=snapshots,period_ids=[p.id for p in periods])
