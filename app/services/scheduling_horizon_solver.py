"""Joint assignment optimization; no database or provider access.

Hard eligibility restrictions are constraints; allocations are preferences. Objective weights use proven integer bounds; a
lower priority can never spend an improvement at a higher priority. A time limit
is inconclusive, never a proof that staffing is impossible.
"""
from collections import defaultdict
from datetime import timedelta
from time import monotonic
from math import lcm


def solve_horizon(*, positions, employees, allowed, limits, targets, fixed=(),
                  lead_ids=(), traveler_store_ids=(), traveler_ids=(),
                  preference_costs=None, historical_weekends=None,
                  time_limit=30, require_leads=True, traveler_costs=None, traveler_history=None,
                  repair=True, additional_change_budget=None):
    from ortools.sat.python import cp_model
    from app.services.scheduling_preferences import CONSECUTIVE_PREFERENCE_COST
    movable = sum(r.get('employee_id') is not None and not r.get('protected') for r in positions)
    # One extra move for each ten movable assignments, capped at three per action.
    budget = min(3, (movable + 9) // 10) if additional_change_budget is None else additional_change_budget
    if not isinstance(budget, int) or not 0 <= budget <= 3:
        raise ValueError('Additional fairness changes must be between zero and three.')
    model = cp_model.CpModel()
    assumptions = {}
    def require(expression, label):
        if label not in assumptions:
            assumption = model.new_bool_var(label)
            assumptions[label] = assumption
            model.add_assumption(assumption)
        model.add(expression).only_enforce_if(assumptions[label])

    vacancies = {}; role_shortages = []
    assignments = {}; by_employee_day = defaultdict(list)
    by_employee_week = defaultdict(list)
    preference_costs = preference_costs or {}
    historical_weekends = historical_weekends or {}
    original = {r['id']: r.get('employee_id') for r in positions}
    for row in positions:
        if row.get('protected') and row.get('employee_id') is None:
            continue
        options = []
        for eid in allowed.get(row['id'], ()):
            var = model.new_bool_var(f"shift_{row['id']}_{eid}")
            assignments[row['id'], eid] = var
            options.append(var)
            by_employee_day[eid, row['date']].append((row, var))
            by_employee_week[eid, row['week']].append((row, var))
            if row.get('protected'):
                require(var == (eid == row.get('employee_id')), f"locked_shift:{row['id']}")
        # Keep every coverage position, including vacancies, in the draft.
        if row.get('protected'):
            require(sum(options) == 1, f"required_shift:{row['id']}")
        else:
            vacancy = model.new_bool_var(f"vacancy_{row['id']}")
            vacancies[row['id']] = vacancy
            model.add(sum(options) + vacancy == 1)
    fixed_days = defaultdict(set); fixed_weeks = defaultdict(list)
    for row in fixed:
        fixed_days[row['employee_id']].add(row['date'])
        fixed_weeks[row['employee_id'], row['week']].append(row)
    active_positions = [r for r in positions if not (r.get('protected') and r.get('employee_id') is None)]
    all_days = sorted({r['date'] for r in active_positions})
    if not all_days:
        all_days = sorted({week for _, week in targets})
    allocation_losses = []
    target_scale=lcm(*(max(1,n) for n in targets.values())) if targets else 1
    day_work = {}; consecutive_preferences = []
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
        preferred = limits[eid].get('preferred_max_days', max_days)
        if preferred < max_days:
            day = start
            while day + timedelta(days=preferred) <= end:
                block = [day + timedelta(days=n) for n in range(preferred + 1)]
                if any(d in all_days for d in block):
                    excess = model.new_bool_var(f'consecutive_preference_{eid}_{day}')
                    model.add(excess >= sum(day_work[eid,d] for d in block) - preferred)
                    consecutive_preferences.append(excess)
                day += timedelta(days=1)
    for (eid,week), target in targets.items():
        rows = by_employee_week[eid, week]
        retained = fixed_weeks[eid,week]
        # Targets never override hard rules or required operating coverage.
        actual = sum(v for _,v in rows)+len(retained)
        bound = max(target, len(rows)+len(retained), 1)
        deviation = model.new_int_var(0, bound, f'allocation_deviation_{eid}_{week}')
        model.add_abs_equality(deviation, actual-target)
        square = model.new_int_var(0, bound*bound, f'allocation_loss_square_{eid}_{week}')
        model.add_multiplication_equality(square, [deviation, deviation])
        # Compare deviation as a share of each person's weekly target.
        allocation_losses.append(square*(target_scale//max(1,target))**2)
        maximum = limits[eid].get('max_minutes')
        if maximum is not None:
            require(sum(r['minutes']*v for r,v in rows)+sum(r['minutes'] for r in retained) <= maximum, f'maximum_minutes_employee:{eid}:week:{week}:limit:{maximum}')
    for day in sorted({r['date'] for r in active_positions}):
        leads = [v for (sid,eid),v in assignments.items()
                 if eid in lead_ids and next(r for r in positions if r['id']==sid)['date']==day]
        if require_leads:
            missing = model.new_bool_var(f'missing_lead_{day}')
            model.add(sum(leads) + missing >= 1)
            role_shortages.append(missing)
    for week in sorted({r['week'] for r in positions}):
        for store in traveler_store_ids:
            rows = [r for r in positions if r['week']==week and r['store_id']==store]
            if rows:
                traveler_count = sum(v for r in rows for (sid,eid),v in assignments.items()
                                     if sid==r['id'] and (store,eid) in traveler_ids)
                # One weekly traveler is a coverage role, not an invented
                # maximum. PTO/consecutive limits can legitimately need more.
                missing=model.new_bool_var(f'missing_traveler_{store}_{week}')
                model.add(traveler_count+missing>=1)
                role_shortages.append(missing)
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
    store_preference=sum(preference_costs.get((sid,eid),0)*v for (sid,eid),v in assignments.items())
    consecutive_cost=sum(consecutive_preferences)*CONSECUTIVE_PREFERENCE_COST
    preference=store_preference+consecutive_cost
    changes=sum(1-assignments.get((r['id'], original[r['id']]), 0)
                for r in positions if original[r['id']] is not None)
    # Bounds make the integer objective exactly lexicographic, including when
    # returning a time-limited incumbent. There is no fixed arbitrary score
    # that could exchange hard coverage for a preference.
    size=len(positions)
    preference_bound=sum(max((abs(preference_costs.get((r['id'],e),0))
                              for e in allowed.get(r['id'],())),default=0) for r in positions)*2
    preference_bound += len(consecutive_preferences)*CONSECUTIVE_PREFERENCE_COST
    preference_weight=size+1
    fairness_weight=preference_bound*preference_weight+size+1
    objective=sum(fairness)*fairness_weight+preference*preference_weight+changes
    for (sid,eid),var in assignments.items():
        model.add_hint(var, int(original[sid]==eid))
    stages=[];prior_solution=None;travel_optimal=True;allocation_optimal=True;minimum_change_witness=None
    stability_optimal=False
    baseline_loss = None; equity_loss = None; baseline_changes = None

    def finish(status, solution, **extra):
        from collections import Counter
        def loss(values):
            counts=Counter((values.get(r['id']),r['week']) for r in positions)
            counts.update((r['employee_id'],r['week']) for r in fixed)
            return sum((counts[e,w]-target)**2*(target_scale//max(1,target))**2
                       for (e,w),target in targets.items())
        result=dict(status=status,assignments=solution,stages=stages,
            minimum_change_witness=minimum_change_witness,repair=repair,
            stability_optimal=stability_optimal if repair else None,
            optimality_scope='BOUNDED_REPAIR' if repair else 'FULL_MODEL',
            global_optimal=not repair and status=='OPTIMAL',
            bounded_optimal=repair and status=='OPTIMAL',
            fairness_search=dict(additional_change_budget=budget if repair else None,
                baseline_changes=baseline_changes,baseline_loss=baseline_loss,
                original_loss=loss(original),final_loss=loss(solution) if solution is not None else None,
                loss_scale=target_scale**2,
                improved=baseline_loss is not None and solution is not None and loss(solution)<baseline_loss))
        result.update(extra)
        return result

    deadline=monotonic()+time_limit
    # First maximize operating coverage. An incumbent remains usable on timeout;
    # only OPTIMAL proves that remaining vacancies cannot be filled in this model.
    for name, objective_value in [('unfilled_coverage', sum(vacancies.values())),
                                  ('unfilled_required_roles', sum(role_shortages))]:
        coverage = cp_model.CpSolver()
        coverage.parameters.max_time_in_seconds=max(.001, (deadline-monotonic())/3)
        coverage.parameters.num_search_workers=1
        model.minimize(objective_value)
        coverage_status = coverage.solve(model)
        stages.append(dict(objective=name,status=coverage.status_name(coverage_status),seconds=coverage.wall_time))
        if coverage_status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return dict(status='INFEASIBLE' if coverage_status==cp_model.INFEASIBLE else 'INCONCLUSIVE',
                assignments=None,stages=stages,conflicting_constraints=[label for label,var in assumptions.items()
                    if coverage_status==cp_model.INFEASIBLE and var.index in coverage.sufficient_assumptions_for_infeasibility()])
        prior_solution={r['id']:None for r in positions}
        prior_solution.update({sid:eid for (sid,eid),v in assignments.items() if coverage.value(v)})
        stages[-1]['value']=coverage.value(objective_value)
        model.add(objective_value <= coverage.value(objective_value))
        if coverage_status != cp_model.OPTIMAL:
            return finish('FEASIBLE',prior_solution,stability_optimal=False)
    stability_optimal=True

    if repair or (traveler_costs and len({r['week'] for r in positions})>1):
        # Give the fairness search a legal complete starting point rather than
        # a partial hint from stale assignments. This is an incumbent, not a
        # relaxed model or a certificate of optimality.
        seed=cp_model.CpSolver()
        seed.parameters.max_time_in_seconds=max(.001, (deadline-monotonic())/2)
        seed.parameters.num_search_workers=1 if len(positions)<=56 else 4
        seed.parameters.random_seed=0
        model.minimize(changes)
        seed_status=seed.solve(model)
        stability_optimal=seed_status==cp_model.OPTIMAL
        stages.append(dict(objective='minimum_changes' if repair else 'feasible_start',status=seed.status_name(seed_status),seconds=seed.wall_time))
        if seed_status in (cp_model.OPTIMAL,cp_model.FEASIBLE):
            prior_solution={r['id']: next((eid for (sid,eid),v in assignments.items() if sid==r['id'] and seed.value(v)), None) for r in positions}
            stages[-1]['assignment_changes']=seed.value(changes)
            minimum_change_witness=dict(assignments=dict(prior_solution),assignment_changes=seed.value(changes),status=seed.status_name(seed_status))
            baseline_changes=seed.value(changes)
            baseline_loss=seed.value(sum(allocation_losses))
            if repair:
                if not stability_optimal:
                    # A timeout is not a minimum-change certificate. Retain
                    # this legal incumbent instead of adding speculative churn.
                    return finish('FEASIBLE',prior_solution,stability_optimal=False)
                # Preserve the legal repair as an incumbent, not an equality
                # that prevents a small, meaningful allocation improvement.
                model.add(changes <= baseline_changes + budget)
                model.add(sum(allocation_losses) <= baseline_loss)
                # Fairness must not move an existing coverage/role shortage to
                # another store or date, even when the total stays unchanged.
                for vacancy in vacancies.values():model.add(vacancy == seed.value(vacancy))
                for shortage in role_shortages:model.add(shortage == seed.value(shortage))
            model.clear_hints()
            for (sid,eid),v in assignments.items():model.add_hint(v,int(prior_solution.get(sid)==eid))
        elif repair:
            return finish('INFEASIBLE' if seed_status==cp_model.INFEASIBLE else 'FEASIBLE',
                prior_solution if seed_status!=cp_model.INFEASIBLE else None,
                stability_optimal=False,conflicting_constraints=[label for label,var in assumptions.items()
                    if seed_status==cp_model.INFEASIBLE and var.index in seed.sufficient_assumptions_for_infeasibility()])
    if allocation_losses:
        equity = cp_model.CpSolver()
        equity.parameters.max_time_in_seconds=max(.001,min(3,(deadline-monotonic())/3))
        equity.parameters.num_search_workers=1
        model.minimize(sum(allocation_losses))
        equity_status=equity.solve(model)
        allocation_optimal=equity_status==cp_model.OPTIMAL
        stages.append(dict(objective='allocation_equity',status=equity.status_name(equity_status),seconds=equity.wall_time))
        if equity_status in (cp_model.OPTIMAL,cp_model.FEASIBLE):
            equity_loss=equity.value(sum(allocation_losses))
            stages[-1]['value']=equity_loss
            model.add(sum(allocation_losses) <= equity_loss)
            prior_solution={r['id']: next((eid for (sid,eid),v in assignments.items() if sid==r['id'] and equity.value(v)), None) for r in positions}
            model.clear_hints()
            for (sid,eid),v in assignments.items():
                model.add_hint(v,int(prior_solution.get(sid)==eid))
    if repair and baseline_changes is not None:
        if equity_loss is None or equity_loss >= baseline_loss:
            # Preferences alone cannot spend the extra change budget.
            model.add(changes <= baseline_changes)
            prior_solution=dict(minimum_change_witness['assignments'])
            model.clear_hints()
            for (sid,eid),v in assignments.items():model.add_hint(v,int(prior_solution.get(sid)==eid))
        else:
            stable=cp_model.CpSolver()
            stable.parameters.max_time_in_seconds=max(.001,min(3,(deadline-monotonic())/3))
            stable.parameters.num_search_workers=1
            model.minimize(changes)
            stable_status=stable.solve(model)
            stability_optimal=stable_status==cp_model.OPTIMAL
            stages.append(dict(objective='minimum_changes_after_equity',
                status=stable.status_name(stable_status),seconds=stable.wall_time))
            if stable_status not in (cp_model.OPTIMAL,cp_model.FEASIBLE):
                return finish('FEASIBLE',prior_solution,stability_optimal=False)
            model.add(changes <= stable.value(changes))
            stages[-1]['assignment_changes']=stable.value(changes)
            prior_solution={r['id']:next((eid for (sid,eid),v in assignments.items()
                if sid==r['id'] and stable.value(v)),None) for r in positions}
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
        travel_solver.parameters.max_time_in_seconds=max(.001,min(5,time_limit/3,(deadline-monotonic())/2))
        travel_solver.parameters.num_search_workers=4 if len({r['week'] for r in positions})>1 else 1
        model.minimize(travel_objective)
        travel_status=travel_solver.solve(model)
        travel_optimal=travel_status==cp_model.OPTIMAL
        stages.append(dict(objective='traveler_fairness',status=travel_solver.status_name(travel_status),seconds=travel_solver.wall_time))
        if travel_status in (cp_model.OPTIMAL,cp_model.FEASIBLE):
            value=travel_solver.value(travel_objective)
            model.add(travel_objective<=value)
            prior_solution={r['id']: next((eid for (sid,eid),v in assignments.items() if sid==r['id'] and travel_solver.value(v)), None) for r in positions}
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
        stages.append(stage)
        return finish('FEASIBLE' if prior_solution is not None else ('INFEASIBLE' if status==cp_model.INFEASIBLE else 'INCONCLUSIVE'),
                prior_solution,conflicting_constraints=[label for label,var in assumptions.items()
                    if status==cp_model.INFEASIBLE and var.index in solver.sufficient_assumptions_for_infeasibility()])
    solution={r['id']: next((eid for (sid,eid),v in assignments.items() if sid==r['id'] and solver.value(v)), None) for r in positions}
    stage.update(weekend_fairness=solver.value(sum(fairness)),preferences=solver.value(preference),
                 store_and_weekday_preferences=solver.value(store_preference),
                 consecutive_preference_cost=solver.value(consecutive_cost),
                 assignment_changes=solver.value(changes),objective_value=solver.objective_value,
                 best_objective_bound=solver.best_objective_bound)
    stages.append(stage)
    return finish('OPTIMAL' if status==cp_model.OPTIMAL and travel_optimal and allocation_optimal
                  and (not repair or stability_optimal) else 'FEASIBLE',solution)
