"""Joint assignment optimization; no database or provider access.

All restrictions are constraints. Objective weights use proven integer bounds; a
lower priority can never spend an improvement at a higher priority. A time limit
is inconclusive, never a proof that staffing is impossible.
"""
from collections import defaultdict
from datetime import timedelta
from time import monotonic


def solve_horizon(*, positions, employees, allowed, limits, targets, fixed=(),
                  lead_ids=(), traveler_store_ids=(), traveler_ids=(),
                  preference_costs=None, historical_weekends=None,
                  time_limit=30, require_leads=True, traveler_costs=None, traveler_history=None):
    from ortools.sat.python import cp_model
    model = cp_model.CpModel()
    assumptions = {}
    def require(expression, label):
        if label not in assumptions:
            assumption = model.new_bool_var(label)
            assumptions[label] = assumption
            model.add_assumption(assumption)
        model.add(expression).only_enforce_if(assumptions[label])

    assignments = {}; by_employee_day = defaultdict(list)
    by_employee_week = defaultdict(list)
    preference_costs = preference_costs or {}
    historical_weekends = historical_weekends or {}
    original = {r['id']: r.get('employee_id') for r in positions}
    for row in positions:
        options = []
        for eid in allowed.get(row['id'], ()):
            var = model.new_bool_var(f"shift_{row['id']}_{eid}")
            assignments[row['id'], eid] = var
            options.append(var)
            by_employee_day[eid, row['date']].append((row, var))
            by_employee_week[eid, row['week']].append((row, var))
            if row.get('protected'):
                require(var == (eid == row.get('employee_id')), f"locked_shift:{row['id']}")
        # Required positions are never discarded to make a plan look valid.
        require(sum(options) == 1, f"required_shift:{row['id']}")
    fixed_days = defaultdict(set); fixed_weeks = defaultdict(list)
    for row in fixed:
        fixed_days[row['employee_id']].add(row['date'])
        fixed_weeks[row['employee_id'], row['week']].append(row)
    all_days = sorted({r['date'] for r in positions})
    day_work = {}
    for eid in employees:
        max_days = limits[eid]['max_days']; rest = limits[eid]['rest_days']
        start = min(all_days) - timedelta(days=max_days + rest)
        end = max(all_days) + timedelta(days=max_days + rest)
        day = start
        while day <= end:
            values = by_employee_day[eid, day]
            if day in fixed_days[eid]:
                work = 1
            elif values:
                work = model.new_bool_var(f'work_{eid}_{day}')
                model.add_max_equality(work, [v for _,v in values])
            else:
                work = 0
            day_work[eid, day] = work
            for index,(left, a) in enumerate(values):
                for right,b in values[index+1:]:
                    if left['start'] < right['end'] and right['start'] < left['end']:
                        require(a+b <= 1, f'overlap_employee:{eid}')
            day += timedelta(days=1)
        # Only enforce windows touching the variable horizon. Retained old
        # violations elsewhere must not turn this problem falsely infeasible.
        day = start
        while day + timedelta(days=max_days) <= end:
            block = [day+timedelta(days=i) for i in range(max_days+1)]
            if any(d in all_days for d in block):
                require(sum(day_work[eid,d] for d in block) <= max_days, f'max_consecutive_employee:{eid}:limit:{max_days}')
            run = [day+timedelta(days=i) for i in range(max_days)]
            for offset in range(rest):
                after = day+timedelta(days=max_days+offset)
                if after <= end and (after in all_days or any(d in all_days for d in run)):
                    require(sum(day_work[eid,d] for d in run)+day_work[eid,after] <= max_days, f'rest_days_employee:{eid}:limit:{rest}')
            day += timedelta(days=1)
    for (eid,week), target in targets.items():
        rows = by_employee_week[eid, week]
        retained = fixed_weeks[eid,week]
        # Dated weekly allocations are a configured operational constraint.
        require(sum(v for _,v in rows)+len(retained) == target, f'weekly_allocation_employee:{eid}:week:{week}:target:{target}')
        maximum = limits[eid].get('max_minutes')
        if maximum is not None:
            require(sum(r['minutes']*v for r,v in rows)+sum(r['minutes'] for r in retained) <= maximum, f'maximum_minutes_employee:{eid}:week:{week}:limit:{maximum}')
    for day in all_days:
        leads = [v for (sid,eid),v in assignments.items()
                 if eid in lead_ids and next(r for r in positions if r['id']==sid)['date']==day]
        if require_leads:
            require(sum(leads) >= 1, f'required_lead:{day}')
    for week in sorted({r['week'] for r in positions}):
        for store in traveler_store_ids:
            rows = [r for r in positions if r['week']==week and r['store_id']==store]
            if rows:
                require(sum(v for r in rows for (sid,eid),v in assignments.items()
                              if sid==r['id'] and (store,eid) in traveler_ids) == 1, f'weekly_traveler_store:{store}:week:{week}')
    # Equalize each person's weekend share relative to their allocated shifts.
    # Pairwise proportional burden handles different workload targets fairly.
    fairness = []
    for weekday in (5,6):
        counts = {}; quotas = {}
        for eid in employees:
            quota = sum(target for (person,_),target in targets.items() if person==eid)
            if not quota: continue
            eligible = any(eid in allowed.get(r['id'],()) for r in positions if r['date'].weekday()==weekday and r['store_id'] not in traveler_store_ids)
            if not eligible: continue
            counts[eid] = sum(v for (sid,person),v in assignments.items()
                              if person==eid and next(r for r in positions if r['id']==sid)['date'].weekday()==weekday and next(r for r in positions if r['id']==sid)['store_id'] not in traveler_store_ids)
            counts[eid] += historical_weekends.get((eid,weekday),0)
            quotas[eid] = quota
        ids=sorted(counts)
        for i,a in enumerate(ids):
            for b in ids[i+1:]:
                bound = (len(positions)+max(historical_weekends.values(),default=0))*max(quotas.values())*2
                delta=model.new_int_var(0,max(1,bound),f'fair_{weekday}_{a}_{b}')
                model.add_abs_equality(delta,counts[a]*quotas[b]-counts[b]*quotas[a])
                fairness.append(delta)
    preference=sum(preference_costs.get((sid,eid),0)*v for (sid,eid),v in assignments.items())
    changes=sum(v for (sid,eid),v in assignments.items() if original[sid] is not None and eid!=original[sid])
    # Bounds make the integer objective exactly lexicographic, including when
    # returning a time-limited incumbent. There is no fixed arbitrary score
    # that could exchange hard coverage for a preference.
    size=len(positions)
    preference_bound=sum(max((abs(preference_costs.get((r['id'],e),0))
                              for e in allowed.get(r['id'],())),default=0) for r in positions)*2
    preference_weight=size+1
    fairness_weight=preference_bound*preference_weight+size+1
    objective=sum(fairness)*fairness_weight+preference*preference_weight+changes
    for (sid,eid),var in assignments.items():
        model.add_hint(var, int(original[sid]==eid))
    stages=[];prior_solution=None;travel_optimal=True;minimum_change_witness=None
    deadline=monotonic()+time_limit
    if traveler_costs and len({r['week'] for r in positions})>1:
        # Give the fairness search a legal complete starting point rather than
        # a partial hint from stale assignments. This is an incumbent, not a
        # relaxed model or a certificate of optimality.
        seed=cp_model.CpSolver()
        seed.parameters.max_time_in_seconds=min(8,time_limit/4)
        seed.parameters.num_search_workers=4
        seed.parameters.random_seed=0
        model.minimize(changes)
        seed_status=seed.solve(model)
        stages.append(dict(objective='feasible_start',status=seed.status_name(seed_status),seconds=seed.wall_time))
        if seed_status in (cp_model.OPTIMAL,cp_model.FEASIBLE):
            prior_solution={sid:eid for (sid,eid),v in assignments.items() if seed.value(v)}
            stages[-1]['assignment_changes']=seed.value(changes)
            minimum_change_witness=dict(assignments=dict(prior_solution),assignment_changes=seed.value(changes),status=seed.status_name(seed_status))
            model.clear_hints()
            for (sid,eid),v in assignments.items():model.add_hint(v,int(prior_solution.get(sid)==eid))
    if traveler_costs:
        # Preserve recency ordering for weekly rotation. Across several weeks,
        # equalize the trip burden before using recency to break ties.
        travel_rank=sum(traveler_costs.get((sid,eid),0)*v for (sid,eid),v in assignments.items())
        spread=[]
        if len({r['week'] for r in positions})>1:
            for store in traveler_store_ids:
                counts={}
                for sid,eid in sorted(traveler_ids):
                    if sid!=store or not any(eid in allowed.get(r['id'],()) for r in positions if r['store_id']==store):
                        continue
                    counts[eid]=sum(v for r in positions if r['store_id']==store
                        for (rid,person),v in assignments.items() if rid==r['id'] and person==eid)
                    counts[eid]+=(traveler_history or {}).get((store,eid),0)
                pool=sorted(counts)
                bound=len(positions)+max((traveler_history or {}).values(),default=0)
                for i,a in enumerate(pool):
                    for b in pool[i+1:]:
                        delta=model.new_int_var(0,max(1,bound),f'travel_spread_{store}_{a}_{b}')
                        model.add_abs_equality(delta,counts[a]-counts[b]);spread.append(delta)
        travel_bound=max(traveler_costs.values(),default=0)*len(positions)
        travel_objective=sum(spread)*(travel_bound+1)+travel_rank
        travel_solver=cp_model.CpSolver()
        travel_solver.parameters.max_time_in_seconds=min(5,time_limit/3)
        travel_solver.parameters.num_search_workers=4 if len({r['week'] for r in positions})>1 else 1
        model.minimize(travel_objective)
        travel_status=travel_solver.solve(model)
        travel_optimal=travel_status==cp_model.OPTIMAL
        stages.append(dict(objective='traveler_fairness',status=travel_solver.status_name(travel_status),seconds=travel_solver.wall_time))
        if travel_status in (cp_model.OPTIMAL,cp_model.FEASIBLE):
            value=travel_solver.value(travel_objective)
            model.add(travel_objective<=value)
            prior_solution={sid:eid for (sid,eid),v in assignments.items() if travel_solver.value(v)}
            model.clear_hints()
            for (sid,eid),v in assignments.items():model.add_hint(v,int(prior_solution.get(sid)==eid))
            stages[-1]['value']=value
        else:
            travel_optimal=False
    solver=cp_model.CpSolver()
    solver.parameters.max_time_in_seconds=max(.001,deadline-monotonic())
    solver.parameters.num_search_workers=1
    solver.parameters.random_seed=0
    model.minimize(objective)
    status=solver.solve(model)
    stage={'objective':'lexicographic','status':solver.status_name(status),'seconds':solver.wall_time}
    if status not in (cp_model.OPTIMAL,cp_model.FEASIBLE):
        return {'status':'FEASIBLE' if prior_solution else ('INFEASIBLE' if status==cp_model.INFEASIBLE else 'INCONCLUSIVE'),
                'assignments':prior_solution,'stages':stages+[stage],
                'conflicting_constraints':[label for label,var in assumptions.items()
                    if status==cp_model.INFEASIBLE and var.index in solver.sufficient_assumptions_for_infeasibility()]}
    solution={sid:eid for (sid,eid),v in assignments.items() if solver.value(v)}
    stage.update(weekend_fairness=solver.value(sum(fairness)),preferences=solver.value(preference),
                 assignment_changes=solver.value(changes),objective_value=solver.objective_value,
                 best_objective_bound=solver.best_objective_bound)
    return {'status':'OPTIMAL' if status==cp_model.OPTIMAL and travel_optimal else 'FEASIBLE',
            'assignments':solution,'stages':stages+[stage],'minimum_change_witness':minimum_change_witness}
