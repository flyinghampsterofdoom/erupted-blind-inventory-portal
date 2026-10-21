"""Compact employee editor: lossless presentation, explicit audited saves.

No generator, assignment history or provider is mutated by this adapter. Legacy
patterns/windows remain evidence; ambiguous inputs cannot be flattened silently.
"""
from datetime import date, time
from decimal import Decimal
import hashlib
import json
from sqlalchemy import select
from app import models as m
from app.services.v2_scheduling_service import SchedulingValidationError, SchedulingConflict, lock_scheduling
from app.services.v2_scheduling_pattern_service import SCHEDULING_WEEKDAY_NAMES as DAYS

STATES = ('PREFER_WORK', 'AVAILABLE', 'PREFER_OFF', 'CANNOT_WORK')
LABELS = ('Prefer Work', 'Available', 'Prefer Off', 'Cannot Work')


def weekday_mapping(profile, windows):
    stored = profile.weekday_preferences if profile else None
    if stored is not None:
        if len(stored) != 7 or any(v not in STATES for v in stored):
            return [None]*7, ['Saved weekday settings need management review.']
    states = list(stored) if stored is not None else ['AVAILABLE']*7; issues = []
    a = (profile.week_a_workdays_mask or 0) if profile else 0
    b = (profile.week_b_workdays_mask or 0) if profile else 0
    if stored is None and a != b:
        issues.append('Different legacy alternating patterns are retained. Management must review them before replacing weekly preferences.')
    elif stored is None and a:
        states = ['PREFER_WORK' if a & (1 << day) else 'PREFER_OFF' for day in range(7)]
    for day in range(7):
        rows = [w for w in windows if w.active and w.day_of_week == day]
        if any(w.start_time != time.min or w.end_time != time.max for w in rows):
            issues.append(f'{DAYS[day]} has custom time windows. They are retained and require management review before using whole-day choices.')
            continue
        kinds = {w.kind for w in rows}
        if m.SchedulingWindowKind.HARD_UNAVAILABLE in kinds:
            states[day] = 'CANNOT_WORK'
        elif stored is None and m.SchedulingWindowKind.PREFERRED in kinds:
            if states[day] == 'PREFER_OFF':
                issues.append(f'{DAYS[day]} has conflicting legacy preferences; both sources are retained for review.')
            else:
                states[day] = 'PREFER_WORK'
    return states, issues


def _join(values):
    return ', '.join(values[:-1])+' and '+values[-1] if len(values)>1 else (values[0] if values else '')


def configuration_view(db, employee, *, today):
    from app.services.v2_scheduling_allocation_service import allocation_on, allocation_history
    profile = db.scalar(select(m.EmployeeSchedulingProfile).where(m.EmployeeSchedulingProfile.employee_id==employee.id))
    windows = list(db.scalars(select(m.EmployeeSchedulingWindow).where(m.EmployeeSchedulingWindow.employee_id==employee.id).order_by(m.EmployeeSchedulingWindow.id)))
    prefs = list(db.scalars(select(m.EmployeeSchedulingStorePreference).where(m.EmployeeSchedulingStorePreference.employee_id==employee.id).order_by(m.EmployeeSchedulingStorePreference.id)))
    rotation = list(db.scalars(select(m.SpecialStoreRotationState).where(m.SpecialStoreRotationState.employee_id==employee.id).order_by(m.SpecialStoreRotationState.id)))
    changes = allocation_history(db,employee.id)
    states,issues = weekday_mapping(profile,windows)
    special_ids = set(db.scalars(select(m.SpecialStorePolicy.store_id).where(m.SpecialStorePolicy.active.is_(True))))
    stores = list(db.scalars(select(m.Store).where(m.Store.active.is_(True)).order_by(m.Store.name)))
    by_store = {p.store_id:p for p in prefs if p.active}
    pool = {s.store_id:s for s in rotation}
    normal = [];special = []
    for s in stores:
        pref=by_store.get(s.id)
        value=pref.preference_level.value if pref else 'ACCEPTABLE'
        if s.id in special_ids:
            state=pool.get(s.id)
            special.append(dict(id=s.id,name=s.name,value='NONE' if value=='NEVER' else (state.participation.value if state else 'NONE')))
        else:
            normal.append(dict(id=s.id,name=s.name,value=value))
    def evidence(row):
        values={c.key:getattr(row,c.key) for c in row.__table__.columns
                if c.key not in {'created_at','updated_at'}}
        return {k:str(v.normalize()) if isinstance(v,Decimal) else v for k,v in values.items()}
    proof=dict(profile=evidence(profile) if profile else None,windows=[evidence(w) for w in windows],
        preferences=[evidence(p) for p in prefs],rotation=[evidence(s) for s in rotation],allocations=[evidence(c) for c in changes],
        capabilities=[employee.scheduling_lead_capable,employee.scheduling_double_coverage])
    token=hashlib.sha256(json.dumps(proof,sort_keys=True,default=str).encode()).hexdigest()
    allocation=allocation_on(db,employee.id,today,profile=profile)
    maximum=min(profile.max_consecutive_work_days or 3,3) if profile else 3
    rest=profile.minimum_days_off_after_max_block if profile else 1
    name=employee.full_name.split()[0]
    summary=[f'{name} has a target of {allocation} '+('shift' if allocation==1 else 'shifts')+f' each Sunday–Saturday week and prefers at most {maximum} consecutive workdays. The organization allows at most 3 consecutive workdays, followed by at least {rest} '+('day' if rest==1 else 'days')+' off.' if allocation is not None else f'{name} needs a weekly shift allocation.']
    if issues:
        summary.append('Existing weekly preferences are retained pending management review.')
    else:
        clauses=[]
        for state,words in [('PREFER_WORK','prefers working'),('PREFER_OFF','prefers off'),('CANNOT_WORK','cannot work')]:
            selected=[DAYS[i] for i,v in enumerate(states) if v==state]
            if selected:clauses.append(words+' '+_join(selected))
        summary.append(name+' '+('; '.join(clauses) if clauses else 'has no recurring weekday preferences or restrictions')+'.')
    clauses=[]
    for value,words in [('PREFERRED','prefers'),('ACCEPTABLE','can work'),('AVOID','prefers to avoid'),('NEVER','cannot work at')]:
        names=[s['name'] for s in normal if s['value']==value]
        if names:clauses.append(words+' '+_join(names))
    for s in special:
        clauses.append({'PRIMARY':'is primary staff at ','ROTATION':'participates in the rotation at ','NONE':'does not work at '}[s['value']]+s['name'])
    if clauses:summary.append(name+' '+'; '.join(clauses)+'.')
    return dict(states=states,issues=issues,days=DAYS,state_choices=list(zip(STATES,LABELS)),token=token,
        legacy_windows=windows,
        normal_stores=normal,special_stores=special,summary=summary,current_allocation=allocation,
        upcoming=[c for c in changes if c.active and c.effective_date and c.effective_date>today])


def save_configuration(db, *, principal, employee_id, form, allowed_store_ids, today):
    from app.services.v2_scheduling_rules_service import upsert_employee_profile,set_full_day_weekday_lockouts,set_store_preference,_audit
    from app.services.v2_scheduling_roster_service import set_scheduling_capabilities
    from app.services.v2_scheduling_policy_service import set_special_store_employee_participation
    from app.services.v2_scheduling_allocation_service import change_allocation
    lock_scheduling(db)
    employee=db.scalar(select(m.Employee).where(m.Employee.id==employee_id).with_for_update().execution_options(populate_existing=True))
    if employee is None:raise SchedulingValidationError('Employee not found.')
    profile=db.scalar(select(m.EmployeeSchedulingProfile).where(m.EmployeeSchedulingProfile.employee_id==employee_id).with_for_update().execution_options(populate_existing=True))
    had_profile = profile is not None
    view=configuration_view(db,employee,today=today)
    if form.get('configuration_token')!=view['token']:
        raise SchedulingConflict('These settings changed while this page was open. Reload before saving.')
    selected=[form.get(f'weekday_{i}') for i in range(7)]
    if view['issues']:
        if any(v is not None for v in selected):
            raise SchedulingValidationError('Legacy weekday settings need management review before replacement.')
    elif any(v not in STATES for v in selected) or any(len(form.getlist(f'weekday_{i}')) != 1 for i in range(7)):
        raise SchedulingValidationError('Choose exactly one of the four states for every weekday.')
    shifts=int(form['shifts_per_week'])
    if not 0<=shifts<=7:raise SchedulingValidationError('Choose zero to seven shifts per week.')
    fields=('home_store_id','target_weekly_hours','target_shifts_per_week','week_a_workdays_mask','week_b_workdays_mask',
        'minimum_weekly_hours','maximum_weekly_hours','preferred_workdays','scheduler_note','active',
        'approval_weekly_hours','max_consecutive_work_days','minimum_days_off_after_max_block','special_store_participation')
    values={k:getattr(profile,k) for k in fields} if profile else dict(home_store_id=None,target_weekly_hours=Decimal(0),target_shifts_per_week=shifts)
    values['scheduler_note']=values.get('scheduler_note') or ''
    if 'home_store_id' in form:
        values['home_store_id'] = int(form['home_store_id']) if form['home_store_id'] else None
    if 'target_weekly_hours' in form:
        values['target_weekly_hours'] = Decimal(form['target_weekly_hours'])
    if 'approval_weekly_hours' in form:
        values['approval_weekly_hours'] = Decimal(form['approval_weekly_hours']) if form['approval_weekly_hours'] else None
    if 'scheduler_note' in form:
        values['scheduler_note'] = str(form['scheduler_note'])
    values.update(max_consecutive_work_days=int(form['max_consecutive_work_days']) if form.get('max_consecutive_work_days') else None,
                  minimum_days_off_after_max_block=int(form['minimum_days_off_after_max_block']))
    if profile and profile.max_consecutive_work_days is None and values['max_consecutive_work_days'] == 3:
        # Display the effective organization default without converting an
        # inherited setting into a new explicit employee restriction.
        values['max_consecutive_work_days'] = None
    # Preserve literal legacy zero masks on an ordinary save as well as NULLs.
    old_masks=(profile.week_a_workdays_mask,profile.week_b_workdays_mask) if profile else (None,None)
    profile=upsert_employee_profile(db,principal=principal,employee_id=employee_id,allowed_store_ids=allowed_store_ids,preserve_legacy_patterns=True,**values)
    profile.week_a_workdays_mask,profile.week_b_workdays_mask=old_masks
    if not view['issues']:
        before=profile.weekday_preferences
        legacy_soft=list(db.scalars(select(m.EmployeeSchedulingWindow).where(m.EmployeeSchedulingWindow.employee_id==employee_id,
            m.EmployeeSchedulingWindow.active.is_(True),m.EmployeeSchedulingWindow.kind.in_((m.SchedulingWindowKind.PREFERRED,m.SchedulingWindowKind.AVAILABLE)),
            m.EmployeeSchedulingWindow.start_time==time.min,m.EmployeeSchedulingWindow.end_time==time.max)))
        for window in legacy_soft:
            window.active=False
            window.updated_by_principal_id=principal.id
        if before!=selected:
            profile.weekday_preferences=list(selected)
            _audit(db,principal=principal,action='WEEKDAY_PREFERENCES_CHANGED',entity_type='employee_scheduling_profile',entity_id=profile.id,
                   before={'weekday_preferences':before},after={'weekday_preferences':selected},metadata={'employee_id':employee_id,'legacy_patterns_retained':True,'retired_soft_window_ids':[w.id for w in legacy_soft]})
        old_bans={i for i,v in enumerate(view['states']) if v=='CANNOT_WORK'}
        new_bans={i for i,v in enumerate(selected) if v=='CANNOT_WORK'}
        if old_bans!=new_bans:
            set_full_day_weekday_lockouts(db,principal=principal,employee_id=employee_id,weekdays=tuple(new_bans))
    if shifts!=view['current_allocation'] and had_profile:
        change_allocation(db,principal=principal,employee_id=employee_id,effective_date=date.fromisoformat(form['allocation_effective_date']),shifts_per_week=shifts,today=today)
    for store in view['normal_stores']:
        key=f"preference_{store['id']}"
        if key not in form:continue
        value=m.StorePreferenceLevel(form[key])
        if value.value!=store['value']:
            old=db.scalar(select(m.EmployeeSchedulingStorePreference).where(m.EmployeeSchedulingStorePreference.employee_id==employee_id,m.EmployeeSchedulingStorePreference.store_id==store['id']))
            set_store_preference(db,principal=principal,employee_id=employee_id,store_id=store['id'],preference_level=value,
                preference_rank=old.preference_rank if old else None,allowed_store_ids=allowed_store_ids)
    for store in view['special_stores']:
        key=f"special_{store['id']}"
        if key not in form:continue
        value=m.SpecialStoreParticipation(form[key])
        if value.value!=store['value']:
            if store['id'] not in allowed_store_ids:raise PermissionError('Store is outside the authorized scope.')
            pref=db.scalar(select(m.EmployeeSchedulingStorePreference).where(m.EmployeeSchedulingStorePreference.employee_id==employee_id,m.EmployeeSchedulingStorePreference.store_id==store['id']))
            if pref and pref.active and pref.preference_level==m.StorePreferenceLevel.NEVER and value!=m.SpecialStoreParticipation.NONE:
                set_store_preference(db,principal=principal,employee_id=employee_id,store_id=store['id'],preference_level=m.StorePreferenceLevel.ACCEPTABLE,
                    preference_rank=pref.preference_rank,allowed_store_ids=allowed_store_ids)
            set_special_store_employee_participation(db,principal=principal,employee_id=employee_id,store_id=store['id'],participation=value)
    lead=form.get('lead_capable')=='true';double=form.get('double_coverage')=='true'
    if (lead,double)!=(employee.scheduling_lead_capable,employee.scheduling_double_coverage):
        set_scheduling_capabilities(db,principal=principal,employee_id=employee_id,lead_capable=lead,double_coverage=double)
    db.flush()
    return profile
