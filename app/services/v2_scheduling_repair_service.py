"""Weekly allocation search and bounded repair of existing coverage positions.

A chain changes at most three existing positions. Each donor moves once, leaving
one vacancy for the next donor or a target/coverage endpoint. Donor eligibility can
therefore be checked immediately against its final dates; Lead preservation is
checked only on the completed arrangement. Trial changes are always restored.
"""
from collections import Counter
from datetime import datetime, timezone
from sqlalchemy import select
from app.models import ScheduleShift, SpecialStorePolicy
from app.services.v2_scheduling_context import assignment_context

MAX_CHANGED_POSITIONS = 3
MAX_SEARCH_EXTENSIONS = 256
MAX_REPAIR_ROOTS = 16
MAX_ACCEPTED_REPAIRS = 8


class _PlanningReads:
    """Replay immutable SELECT results during one frozen planning pass only.

    The caller has flushed and cleared movable positions. Existing validation
    stays authoritative; this avoids reloading the same policy/evidence for
    every hypothetical weekly pattern. Never reused while applying a plan.
    """
    def __init__(self, db):
        self.db = db
        self.cache = {}

    def __getattr__(self, name):
        return getattr(self.db, name)

    def execute(self, statement, *args, **kwargs):
        if args or kwargs or not getattr(statement, 'is_select', False) or getattr(statement, '_for_update_arg', None) is not None:
            return self.db.execute(statement, *args, **kwargs)
        compiled = statement.compile(dialect=self.db.get_bind().dialect)
        key = (str(compiled), repr(sorted(compiled.params.items())))
        if key not in self.cache:
            self.cache[key] = self.db.execute(statement).freeze()
        return self.cache[key]()

    def scalar(self, statement):
        return self.execute(statement).scalar()

    def scalars(self, statement):
        return self.execute(statement).scalars()

    def flush(self, *args, **kwargs):
        self.cache.clear()
        return self.db.flush(*args, **kwargs)


def solve_weekly_patterns(patterns, required_mask, *, node_limit=250000, employee_priority=None,
                          lead_ids=(), lead_day_masks=(), baseline_sizes=None, max_extra=None,
                          required_count=None, capacity_groups=()):
    """Exact cover of existing positions by one legal weekly pattern per person.

    Pattern order contains the existing fairness preference. Search never trades
    an allocation or coverage constraint for a better score. Exhausting the
    safety bound is explicitly inconclusive, never evidence for an exception.
    """
    nodes = 0
    failed = set()
    capacity_conflicts = set()
    required_count = required_mask.bit_count() if required_count is None else required_count

    def search(remaining, covered, lead_covered=0, extra=0):
        nonlocal nodes
        nodes += 1
        if nodes > node_limit:
            raise OverflowError('Weekly allocation search limit reached.')
        if not remaining:
            return {} if covered.bit_count() == required_count and all(lead_covered & day for day in lead_day_masks) else None
        key = (remaining, covered, lead_covered, extra)
        if key in failed:
            return None
        available = {eid: [mask for mask in patterns[eid] if not mask & covered]
                     for eid in remaining}
        if any(not values for values in available.values()):
            failed.add(key)
            return None
        missing = required_count-covered.bit_count()
        minimum = {eid:min(mask.bit_count() for mask in values) for eid,values in available.items()}
        maximum = {eid:max(mask.bit_count() for mask in values) for eid,values in available.items()}
        min_total = sum(minimum.values()); max_total = sum(maximum.values())
        for eid in remaining:
            lower = missing-(max_total-maximum[eid])
            upper = missing-(min_total-minimum[eid])
            available[eid] = [mask for mask in available[eid] if lower <= mask.bit_count() <= upper]
        if any(not values for values in available.values()):
            failed.add(key)
            return None
        for label,group in capacity_groups:
            slots = (group & ~covered).bit_count()
            lower = sum(min((mask & group).bit_count() for mask in values) for values in available.values())
            upper = sum(max((mask & group).bit_count() for mask in values) for values in available.values())
            if lower > slots or (required_count == required_mask.bit_count() and upper < slots):
                capacity_conflicts.add((label,lower,slots,upper))
                failed.add(key)
                return None
        union = covered
        for values in available.values():
            for mask in values:
                union |= mask
        possible_leads = lead_covered
        for eid in remaining:
            if eid in lead_ids:
                for mask in available[eid]:
                    possible_leads |= mask
        if any(not possible_leads & day for day in lead_day_masks):
            failed.add(key)
            return None
        if (union.bit_count() < required_count
                or sum(min(m.bit_count() for m in values) for values in available.values()) > missing
                or sum(max(m.bit_count() for m in values) for values in available.values()) < missing):
            failed.add(key)
            return None
        eid = min(remaining, key=lambda e: (len(available[e]), (employee_priority or {}).get(e, 0), e))
        alternatives = [(eid,mask) for mask in available[eid]]
        # Exact-cover columns are both employees and required positions. A
        # scarce store/day often constrains the search more than any employee.
        columns = {}
        if required_count == required_mask.bit_count():
            for candidate in remaining:
                for mask in available[candidate]:
                    bits = mask
                    while bits:
                        bit = bits & -bits; bits -= bit
                        column = columns.setdefault(bit,[])
                        if len(column) <= len(alternatives):
                            column.append((candidate,mask))
        for bit in sorted(columns):
            if len(columns[bit]) < len(alternatives):
                alternatives = columns[bit]
        alternatives.sort(key=lambda pair: ((employee_priority or {}).get(pair[0],0),
            available[pair[0]].index(pair[1]),pair[0]))
        for eid,mask in alternatives:
            rest = tuple(e for e in remaining if e != eid)
            additional = max(0,mask.bit_count()-(baseline_sizes or {}).get(eid,mask.bit_count()))
            if max_extra is not None and extra+additional > max_extra:
                continue
            result = search(rest, covered | mask, lead_covered | mask if eid in lead_ids else lead_covered,extra+additional)
            if result is not None:
                return {eid: mask, **result}
        failed.add(key)
        return None

    try:
        result = search(tuple(sorted(patterns)), 0)
        return result, {'nodes': nodes, 'exhaustive': True,'capacity_constraints':[
            {'group':label,'minimum_required':low,'available_positions':slots,'maximum_possible':high}
            for label,low,slots,high in sorted(capacity_conflicts)]}
    except OverflowError:
        return None, {'nodes': nodes, 'exhaustive': False}


@assignment_context
def allocate_weekly_positions(db, *, principal, period, planning_date):
    """Use existing validation to solve whole-employee weekly allocations.

    Only unlocked positions eligible for regeneration may change. Flexible cover is derived
    from allocations below three and needs a demonstrated hard capacity loss;
    an unsuccessful or bounded search alone cannot authorize extra shifts.
    """
    from itertools import combinations, product
    from datetime import timedelta
    from app.models import EmployeeSchedulingProfile, SpecialStoreRotationState, SpecialStoreParticipation
    from app.services.v2_scheduling_roster_service import list_scheduling_candidates
    from app.services.v2_scheduling_lifecycle_service import eligible_during
    from app.services.v2_scheduling_policy_service import (
        evaluate_assignment, SimulatedAssignment, assignment_score,
        weekend_fairness, longview_rotation_fairness, longview_fairness_rank,
        base_pattern_score, consecutive_preference_score,
        effective_assignment_rows, _longview_historical_credit_rows,
    )
    rows = list(db.scalars(select(ScheduleShift).where(
        ScheduleShift.schedule_period_id == period.id).order_by(
            ScheduleShift.shift_date, ScheduleShift.store_id, ScheduleShift.id)))
    movable = [r for r in rows if not r.manually_locked and not r.lead_of_day_manually_assigned
               and not r.double_coverage_manually_assigned]
    original = {r.id: r.employee_id for r in movable}
    for row in movable:
        row.employee_id = None
    db.flush()
    reads = _PlanningReads(db)
    candidates = list_scheduling_candidates(db)
    employees = [e for e in candidates
                 if eligible_during(e, period.week_start_date, period.week_end_date)]
    profiles = {p.employee_id: p for p in db.scalars(select(EmployeeSchedulingProfile))}
    targets = {e.id: profiles[e.id].target_shifts_per_week for e in employees
               if e.id in profiles and profiles[e.id].target_shifts_per_week is not None}
    diagnostics = []
    for row in rows:
        if row in movable or row.employee_id is None:
            continue
        result = evaluate_assignment(reads,employee_id=row.employee_id,store_id=row.store_id,
            shift_date=row.shift_date,start_time=row.start_time,end_time=row.end_time,
            unpaid_break_minutes=row.unpaid_break_minutes,exclude_shift_id=row.id,
            schedule_period_id=period.id)
        if not result.eligible:
            diagnostics.append({'code':'PROTECTED_ASSIGNMENT_CONFLICT','shift_id':row.id,
                'employee_id':row.employee_id,
                'constraints':[{'code':r.code,'message':r.message} for r in result.reasons],
                'message':'A protected assignment conflicts with a hard rule. Resolve it explicitly; generation preserves it.'})
    if len(targets) != len(employees):
        diagnostics.append({'code': 'WEEKLY_ALLOCATION_MISSING',
            'message': 'Every scheduling candidate needs a configured weekly allocation.',
            'employee_ids': [e.id for e in employees if e.id not in targets]})
    fixed = {e.id: len([r for r in effective_assignment_rows(reads, employee_id=e.id,
                    start_date=period.week_start_date, end_date=period.week_end_date)]) for e in employees}
    gap = len(movable)+sum(fixed.values())-sum(targets.values())
    departure_losses = {}
    if gap > 0:
        for employee in candidates:
            if employee in employees or employee.id not in profiles:
                continue
            target = profiles[employee.id].target_shifts_per_week
            if not target:
                continue
            ends = [p.end_date for p in employee.eligibility_periods
                    if p.end_date is not None and p.end_date < period.week_start_date]
            if not employee.eligibility_periods and employee.last_effective_date is not None:
                ends = [employee.last_effective_date] if employee.last_effective_date < period.week_start_date else []
            if ends:
                departure_losses[employee.id] = {'configured':target,'capacity':0,
                    'known_last_eligible_date':max(ends).isoformat(),
                    'constraints':[{'code':'DATED_ELIGIBILITY_CAPACITY_LOSS',
                        'message':'No eligibility period covers this week after the recorded last eligible date.'}]}
        # Do not guess which historical allocation fills a structural gap.
        # The complete dated losses must reconcile exactly with displaced demand.
        if sum(v['configured'] for v in departure_losses.values()) != gap:
            departure_losses = {}
    if sum(targets.values()) != len(movable) + sum(fixed.values()) and not departure_losses:
        diagnostics.append({'code': 'WEEKLY_ALLOCATION_COVERAGE_MISMATCH',
            'configured_shifts': sum(targets.values()),
            'required_positions': len(movable) + sum(fixed.values()),
            'message': 'Configured weekly allocations and required positions differ. Review the dated staffing configuration; fairness cannot change allocations.'})
    if any(fixed[eid] > target for eid, target in targets.items()):
        diagnostics.append({'code': 'PROTECTED_ASSIGNMENTS_EXCEED_ALLOCATION',
            'employee_ids': [eid for eid, target in targets.items() if fixed[eid] > target],
            'message': 'Protected assignments exceed an allocation; resolve them explicitly.'})
    partial_baseline = (len(diagnostics) == 1
        and diagnostics[0]['code'] == 'WEEKLY_ALLOCATION_COVERAGE_MISMATCH'
        and sum(targets.values()) < len(movable)+sum(fixed.values()))
    if diagnostics and not partial_baseline:
        for row in movable:
            row.employee_id = original[row.id]
        db.flush()
        return {'status': 'UNRESOLVED', 'diagnostics': diagnostics, 'exceptions': []}

    special = set(db.scalars(select(SpecialStorePolicy.store_id).where(SpecialStorePolicy.active.is_(True))))
    participants = {(s.store_id, s.employee_id): s.participation for s in db.scalars(select(SpecialStoreRotationState))}
    required_mask = (1 << len(movable)) - 1
    required_count = len(movable) if departure_losses else sum(targets.values())-sum(fixed.values())
    groups = {}
    for i,row in enumerate(movable):
        minutes = row.end_time.hour*60+row.end_time.minute-row.start_time.hour*60-row.start_time.minute-row.unpaid_break_minutes
        for label in (f'store:{row.store_id}',f'date:{row.shift_date}',f'paid_minutes:{minutes}'):
            groups[label] = groups.get(label,0) | (1 << i)
    capacity_groups = tuple(groups.items())
    patterns = {}; failures = {}; pattern_counts = {}; flex_priority = {}; rotation_ranks = {}; builders = {}; capacities = {}
    for employee in employees:
        eid = employee.id
        if targets[eid] < 3:
            # Existing effective coverage includes historical actual work and
            # future reservations without counting replaced revisions twice.
            prior = effective_assignment_rows(reads, employee_id=eid,
                start_date=period.week_start_date-timedelta(weeks=12),
                end_date=period.week_start_date-timedelta(days=1))
            from app.models import Store
            cutoff = min(planning_date,period.week_start_date)
            historical = {(f['shift_date'],f['shift_id']) for sid in db.scalars(select(Store.id))
                for f in _longview_historical_credit_rows(reads,store_id=sid,before_date=cutoff)
                if eid in f['credited_employee_ids']
                and period.week_start_date-timedelta(weeks=12) <= f['shift_date'] < cutoff}
            flex_priority[eid] = -len(historical)-sum(r.shift_date >= cutoff for r in prior)
        reasons = {}; choices = {}; scores = {}; base_scores = {}; legal_cache = {}
        for index, row in enumerate(movable):
            result = evaluate_assignment(reads, employee_id=eid, store_id=row.store_id,
                shift_date=row.shift_date, start_time=row.start_time, end_time=row.end_time,
                unpaid_break_minutes=row.unpaid_break_minutes, schedule_period_id=period.id)
            if not result.eligible:
                reasons[str(row.id)] = [{'code': x.code, 'message': x.message} for x in result.reasons]
                continue
            choices.setdefault(row.shift_date, []).append(index)
            score, _ = assignment_score(reads, employee_id=eid, store_id=row.store_id, shift_date=row.shift_date)
            burden = 0
            if row.store_id in special and participants.get((row.store_id,eid)) == SpecialStoreParticipation.ROTATION:
                fair = longview_rotation_fairness(reads,employee_id=eid,store_id=row.store_id,
                    before_date=row.shift_date,as_of_date=planning_date)
                burden = longview_fairness_rank(fair).toordinal() * 10000
                rotation_ranks[(row.store_id,eid)] = (longview_fairness_rank(fair), eid)
            elif row.shift_date.weekday() in (5,6):
                fair = weekend_fairness(reads,employee_id=eid,weekday=row.shift_date.weekday(),
                    before_date=row.shift_date,as_of_date=planning_date)
                burden = (fair.historical_assignment_count + fair.planned_future_assignment_count) * 100
            base_scores[index] = base_pattern_score(reads,employee_id=eid,shift_date=row.shift_date)
            scores[index] = burden - score - consecutive_preference_score(reads,employee_id=eid,shift_date=row.shift_date)
        by_count = {}; ranks = {}
        # Derive daily combinations from actual overlap, not an extra shifts-
        # per-day cap. Ordinary full-day positions overlap; explicitly configured
        # non-overlapping positions remain usable by the same employee.
        daily_options = {}
        for day,indexes in choices.items():
            daily_options[day] = [subset for size in range(1,len(indexes)+1)
                for subset in combinations(indexes,size)
                if all(not (movable[a].start_time < movable[b].end_time
                            and movable[a].end_time > movable[b].start_time)
                       for a,b in combinations(subset,2))]
        daily_max = max((len(option) for values in daily_options.values() for option in values),default=1)
        fixed_count = fixed[eid]
        def build_count(count, *, eid=eid, choices=choices, scores=scores, base_scores=base_scores,
                        legal_cache=legal_cache, reasons=reasons, ranks=ranks,
                        by_count=by_count, fixed_count=fixed_count,daily_options=daily_options,daily_max=daily_max):
            values = []
            day_sets = (days for size in range((count+daily_max-1)//daily_max,min(count,len(choices))+1)
                        for days in combinations(sorted(choices),size))
            for days in day_sets:
                for daily in product(*(daily_options[day] for day in days)):
                    indexes = tuple(i for option in daily for i in option)
                    if len(indexes) != count:
                        continue
                    proposed = [movable[i] for i in indexes]
                    key = tuple((r.shift_date,r.start_time,r.end_time,r.unpaid_break_minutes) for r in proposed)
                    if key not in legal_cache:
                        rejected = []
                        for row in proposed:
                            sims = tuple(SimulatedAssignment(r.shift_date,r.start_time,r.end_time,r.unpaid_break_minutes)
                                         for r in proposed if r.id != row.id)
                            result = evaluate_assignment(reads,employee_id=eid,store_id=row.store_id,
                                shift_date=row.shift_date,start_time=row.start_time,end_time=row.end_time,
                                unpaid_break_minutes=row.unpaid_break_minutes,schedule_period_id=period.id,
                                simulated_assignments=sims)
                            rejected.extend({'code': x.code,'message': x.message} for x in result.reasons)
                        legal_cache[key] = rejected
                    if legal_cache[key]:
                        reasons.setdefault('weekly_patterns', legal_cache[key])
                        continue
                    mask = sum(1 << i for i in indexes)
                    ranks[mask] = (sum(original[movable[i].id] is not None
                                       and original[movable[i].id] != eid for i in indexes),
                                   -sum(base_scores[i] for i in indexes),
                                   sum(scores[i] for i in indexes))
                    values.append(mask)
            by_count[count + fixed_count] = sorted(values, key=lambda mask: (ranks[mask],mask))
        builders[eid] = build_count
        capacities[eid] = sum(max(len(option) for option in values) for values in daily_options.values()) + fixed_count
        for count in range(max(0,targets[eid]-fixed_count), -1, -1):
            build_count(count)
            if by_count.get(count+fixed_count):
                break
        pattern_counts[eid] = by_count; failures[eid] = reasons
        patterns[eid] = by_count.get(targets[eid], [])

    # Reserve the seventh special-store shift by existing employee-week fairness,
    # but accept a traveler only if the complete cross-store week is feasible.
    # A local store choice cannot steal an employee's fourth weekly shift.
    rotation_options = []
    for store_id in sorted(special):
        primary_ids = {eid for (sid,eid), part in participants.items()
                       if sid == store_id and part == SpecialStoreParticipation.PRIMARY}
        store_rows = [r for r in rows if r.store_id == store_id]
        if store_rows and sum(targets.get(eid,0) for eid in primary_ids) == len(store_rows)-1:
            fixed_travelers = {r.employee_id for r in store_rows if r not in movable
                              and r.employee_id is not None and r.employee_id not in primary_ids}
            pool = sorted((eid for sid,eid in rotation_ranks if sid == store_id),
                          key=lambda eid: rotation_ranks[(store_id,eid)])
            if fixed_travelers:
                pool = [eid for eid in pool if fixed_travelers == {eid}]
            rotation_options.append((store_id,primary_ids,pool))
    plan = None; search = {'nodes':0,'exhaustive':True}
    lead_ids = {e.id for e in employees if e.scheduling_lead_capable}
    fixed_lead_days = {r.shift_date for r in rows if r not in movable and r.employee_id in lead_ids}
    lead_day_masks = tuple(sum(1 << i for i,r in enumerate(movable) if r.shift_date == day)
                          for day in sorted({r.shift_date for r in rows}-fixed_lead_days))
    # Lead staffing is optimized inside valid allocations; missing Leads cannot
    # authorize extra labor or a change to somebody's weekly allocation.
    trials = product(*(spec[2] for spec in rotation_options)) if rotation_options else [()]
    for selected in trials:
        normal = dict(patterns)
        for (store_id,primary_ids,_), traveler in zip(rotation_options,selected):
            store_mask = sum(1 << i for i,r in enumerate(movable) if r.store_id == store_id)
            for eid in normal:
                if eid not in primary_ids and eid != traveler:
                    normal[eid] = [mask for mask in normal[eid] if not mask & store_mask]
        remaining = 250000-search['nodes']
        candidate, attempt = solve_weekly_patterns(normal, required_mask, node_limit=max(0,remaining),
            required_count=required_count,capacity_groups=capacity_groups)
        search['nodes'] += attempt['nodes']; search['exhaustive'] &= attempt['exhaustive']
        search['capacity_constraints'] = attempt.get('capacity_constraints',[])
        if candidate is not None:
            plan = candidate
            break
        if not search['exhaustive']:
            break
    mode = 'EXACT_BASELINE'
    losses = dict(departure_losses)
    if plan is None and search['exhaustive']:
        # Record independent hard capacity losses first. Shared-position
        # competition is certified by the exhaustive normal-allocation search,
        # not by a fairness score or an arbitrary greedy failure.
        for eid, target in targets.items():
            capacity = max((n for n, values in pattern_counts[eid].items() if values and n <= target), default=fixed[eid])
            if capacity < target:
                losses[eid] = {'configured': target,'capacity': capacity,'constraints': failures[eid]}
        if any(target < 3 for target in targets.values()):
            for eid,target in targets.items():
                for total in range(fixed[eid],min(target,capacities[eid])+1):
                    if total not in pattern_counts[eid]:
                        builders[eid](total-fixed[eid])
                if target < 3:
                    for total in range(target+1,capacities[eid]+1):
                        builders[eid](total-fixed[eid])
                        if not pattern_counts[eid].get(total):
                            break
            exception_patterns = {}
            for eid, target in targets.items():
                counts = ([n for n in pattern_counts[eid] if n >= target] if target < 3
                          else [n for n in pattern_counts[eid] if n <= target])
                if eid in losses:
                    counts = [n for n in pattern_counts[eid] if n <= losses[eid]['capacity']]
                exception_patterns[eid] = [mask for n in sorted(counts)
                    for mask in pattern_counts[eid].get(n, [])]
            normal_nodes = search['nodes']
            for extra_budget in range(max(1,sum(v['configured']-v['capacity'] for v in losses.values())),len(movable)+1):
                plan, attempt = solve_weekly_patterns(exception_patterns, required_mask,
                    employee_priority=flex_priority,baseline_sizes={eid:target-fixed[eid] for eid,target in targets.items()},
                    max_extra=extra_budget,node_limit=max(0,250000-search['nodes']),required_count=required_count,capacity_groups=capacity_groups)
                search['nodes'] += attempt['nodes']; search['exhaustive'] &= attempt['exhaustive']
                search['capacity_constraints'] = attempt.get('capacity_constraints',[])
                if plan is not None or not search['exhaustive']:
                    break
            if plan is not None:
                for eid,mask in plan.items():
                    assigned = mask.bit_count()+fixed[eid]
                    if assigned < targets[eid] and eid not in losses:
                        # Explicit witness: every normal weekly placement for
                        # this employee competes with required assignments in
                        # the complete exception schedule. The exhaustive first
                        # search establishes that all baselines cannot coexist.
                        occupied = required_mask & ~mask
                        losses[eid] = {'configured':targets[eid],'capacity':assigned,
                            'constraints':failures[eid], 'code':'SHARED_REQUIRED_POSITION_CONFLICT',
                            'normal_search_exhaustive':True,'normal_search_nodes':normal_nodes,
                            'competing_shift_ids':[r.id for i,r in enumerate(movable) if occupied & (1 << i)
                                and any(p & (1 << i) for p in patterns[eid])]}
            mode = 'CONSTRAINT_DRIVEN_FLEXIBLE_COVERAGE'
    if plan is None:
        for row in movable:
            row.employee_id = original[row.id]
        db.flush()
        return {'status':'UNRESOLVED','exceptions':[], 'search':search,
            'diagnostics':[{'code':'WEEKLY_ALLOCATION_SEARCH_LIMIT' if not search['exhaustive'] else 'WEEKLY_ALLOCATION_CONFLICT',
                'message':'No complete valid allocation was found; review the affected positions and hard constraints. No baseline was silently redistributed.',
                'affected_shift_ids':[r.id for r in movable], 'hard_capacity_losses':losses,
                'employee_constraints':failures}]}
    lead_optimization = {'attempted':False}
    if mode == 'EXACT_BASELINE' and not partial_baseline and lead_ids:
        # Coverage is already secured. A bounded preference pass may improve
        # Lead distribution only by returning another complete exact solution.
        improved, attempt = solve_weekly_patterns(normal,required_mask,node_limit=2000,
            lead_ids=lead_ids,lead_day_masks=lead_day_masks,capacity_groups=capacity_groups)
        lead_optimization = dict(attempt,attempted=True,improved=improved is not None)
        if improved is not None:
            plan = improved
    exceptions = []
    displaced = [dict(employee_id=eid,**loss) for eid,loss in losses.items()]
    for eid, mask in plan.items():
        selected = [r for i,r in enumerate(movable) if mask & (1 << i)]
        total = len(selected) + fixed[eid]
        if total > targets[eid]:
            assert targets[eid] < 3 and displaced
            exceptions.append({'employee_id':eid,'configured_shifts':targets[eid],
                'assigned_shifts':total,'additional_shifts':total-targets[eid],
                'shift_ids':[r.id for r in selected],
                'additional_shift_ids':[r.id for r in selected[max(0,targets[eid]-fixed[eid]):]],
                'displaced_allocations':displaced})
        for row in selected:
            row.employee_id=eid;row.updated_by_principal_id=principal.id
            row.updated_at=datetime.now(timezone.utc)
    db.flush()
    return {'status':'PARTIAL' if partial_baseline else 'PASS','mode':mode,'search':search,'exceptions':exceptions,
            'diagnostics':diagnostics, 'hard_capacity_losses':losses,'lead_optimization':lead_optimization,
            'weekly_allocations':[{'employee_id':eid,'configured_shifts':targets[eid],
                'assigned_shifts':mask.bit_count()+fixed[eid]} for eid,mask in plan.items()]}


@assignment_context
def repair_required_assignments(db, *, principal, period, diagnostics=None):
    if period.is_historical:
        from app.services.v2_scheduling_service import SchedulingConflict
        raise SchedulingConflict('Historical weeks require manual reconstruction.')
    from app.services.v2_scheduling_policy_service import (
        assignment_score, evaluate_assignment, weekly_work_pattern,
    )
    from app.services.v2_scheduling_roster_service import list_scheduling_candidates
    rows = list(db.scalars(select(ScheduleShift).where(
        ScheduleShift.schedule_period_id == period.id).order_by(
        ScheduleShift.shift_date, ScheduleShift.store_id, ScheduleShift.id)))
    movable = [row for row in rows if row.generated_from_coverage_requirement
               and not row.manually_locked and not row.is_double_coverage
               and not row.lead_of_day_manually_assigned
               and not getattr(row, '_primary_rotation_reserved', False)]
    from app.services.v2_scheduling_lifecycle_service import eligible_during
    employees = [e for e in list_scheduling_candidates(db)
                 if eligible_during(e, period.week_start_date, period.week_end_date)]
    lead_ids = {e.id for e in employees if e.scheduling_lead_capable}
    special = set(db.scalars(select(SpecialStorePolicy.store_id).where(
        SpecialStorePolicy.active.is_(True))))
    extensions = 0
    rejected = Counter()
    accepted = 0
    attempted_roots = 0
    root_limit_reached = False

    eligibility_cache = {}

    def eligibility(row):
        # Other employees do not affect an employee's policy eligibility. Cache
        # only while that employee's complete proposed position set is identical.
        key = (row.employee_id, row.id,
               tuple(r.id for r in rows if r.employee_id == row.employee_id))
        if key not in eligibility_cache:
            eligibility_cache[key] = evaluate_assignment(
                db, employee_id=row.employee_id, store_id=row.store_id,
                shift_date=row.shift_date, start_time=row.start_time,
                end_time=row.end_time, unpaid_break_minutes=row.unpaid_break_minutes,
                exclude_shift_id=row.id)
        return eligibility_cache[key]

    def legal_employee(employee_id):
        for row in rows:
            if row.employee_id != employee_id:
                continue
            result = eligibility(row)
            if not result.eligible:
                rejected.update(reason.code for reason in result.reasons)
                return False
        return True

    def lead_days():
        return {row.shift_date for row in rows if row.employee_id in lead_ids
                and eligibility(row).eligible}

    while accepted < MAX_ACCEPTED_REPAIRS and extensions < MAX_SEARCH_EXTENSIONS:
        patterns = {e.id: weekly_work_pattern(db, employee_id=e.id,
                    shift_date=period.week_start_date) for e in employees}
        deficits = [e.id for e in employees if patterns[e.id].target_shifts is not None
                    and patterns[e.id].worked_shifts < patterns[e.id].target_shifts]
        if not deficits and not any(row.employee_id is None for row in movable):
            break
        # Bounded chains terminate only at baseline deficits. Flexible extras
        # require the whole-week resolver and its constraint evidence.
        endpoints = deficits
        original = {row.id: row.employee_id for row in rows}
        original_days = {e.id: {r.shift_date for r in rows if r.employee_id == e.id}
                         for e in employees}
        roots = [row for row in movable if row.employee_id is None]
        roots += [row for row in movable if deficits and row.employee_id in patterns
                  and patterns[row.employee_id].target_shifts is not None
                  and patterns[row.employee_id].worked_shifts > patterns[row.employee_id].target_shifts]
        root_limit_reached = root_limit_reached or len(roots) > MAX_REPAIR_ROOTS
        roots = roots[:MAX_REPAIR_ROOTS]
        attempted_roots += len(roots)
        if not roots:
            break
        protected_lead_days = lead_days()
        best = None
        for depth in range(1, MAX_CHANGED_POSITIONS + 1):
            def search(vacancy, path, used_employees):
                nonlocal extensions, best
                if extensions >= MAX_SEARCH_EXTENSIONS:
                    return
                for employee_id in endpoints:
                    if employee_id in used_employees or extensions >= MAX_SEARCH_EXTENSIONS:
                        continue
                    extensions += 1
                    vacancy.employee_id = employee_id
                    db.flush()
                    try:
                        if not legal_employee(employee_id):
                            continue
                        final_lead_days = lead_days()
                        if not protected_lead_days <= final_lead_days:
                            rejected['GLOBAL_LEAD_LOSS'] += 1
                            continue
                        changes = [(row, row.employee_id) for row in path]
                        # Targets cannot decline: donors keep a shift; only an
                        # above-target root occupant may lose one. The terminal
                        # employee gains one (above-target only for uncovered
                        # required coverage). Recheck through the
                        # shared effective-revision target accounting as well.
                        affected = used_employees | {employee_id}
                        if any(weekly_work_pattern(db, employee_id=e, shift_date=period.week_start_date).worked_shifts
                               < min(patterns[e].worked_shifts, patterns[e].target_shifts or 0)
                               for e in affected):
                            continue
                        if not all(legal_employee(e) for e in affected):
                            continue
                        missing_leads = len({row.shift_date for row in rows
                                             if row.employee_id is not None} - final_lead_days)
                        special_changes = sum(row.store_id in special for row, _ in changes)
                        preference = sum(assignment_score(db, employee_id=e, store_id=row.store_id,
                                            shift_date=row.shift_date)[0] for row, e in changes)
                        changed_days = sum(row.shift_date not in original_days[e]
                                           for row, e in changes if e in used_employees)
                        rank = (missing_leads, len(changes), original[path[0].id] is not None,
                                special_changes, changed_days, -preference,
                                tuple((row.shift_date, row.store_id, row.id, e) for row, e in changes))
                        if best is None or rank < best[0]:
                            best = (rank, changes)
                    finally:
                        vacancy.employee_id = None
                        db.flush()
                if len(path) >= depth:
                    return
                for source in sorted(movable, key=lambda row: (
                        row.shift_date != vacancy.shift_date, row.store_id != vacancy.store_id,
                        row.shift_date, row.store_id, row.id)):
                    employee_id = source.employee_id
                    if (source in path or employee_id is None or employee_id in used_employees
                            or employee_id not in patterns or extensions >= MAX_SEARCH_EXTENSIONS):
                        continue
                    extensions += 1
                    source.employee_id = None
                    vacancy.employee_id = employee_id
                    db.flush()
                    try:
                        if legal_employee(employee_id):
                            search(source, path + [source], used_employees | {employee_id})
                    finally:
                        source.employee_id = employee_id
                        vacancy.employee_id = None
                        db.flush()

            for root in roots:
                occupant = root.employee_id
                root.employee_id = None
                db.flush()
                try:
                    search(root, [root], {occupant} if occupant is not None else set())
                finally:
                    root.employee_id = occupant
                    db.flush()
            if (best is not None and best[0][0] == 0) or extensions >= MAX_SEARCH_EXTENSIONS:
                break
        if best is None:
            break
        changes = best[1]
        for row, employee_id in changes:
            row.employee_id = employee_id
            row.updated_by_principal_id = principal.id
            row.updated_at = datetime.now(timezone.utc)
        db.flush()
        accepted += 1
        if diagnostics is not None:
            diagnostics.append({
                'action': 'BOUNDED_REASSIGNMENT_SUCCEEDED',
                'reason': 'REQUIRED_COVERAGE' if original[changes[0][0].id] is None else 'TARGET_WITHOUT_OVERLAP',
                'changes': [{'shift_id': row.id, 'from_employee_id': original[row.id],
                             'to_employee_id': employee_id} for row, employee_id in changes],
                'special_store_changes': best[0][3], 'remaining_global_lead_days': best[0][0], 'extensions': extensions,
            })
    if diagnostics is not None:
        diagnostics.append({
            'action': 'BOUNDED_REASSIGNMENT_SEARCH', 'accepted': accepted,
            'outcome': 'REPAIRED' if accepted else ('NO_REPAIR_WITHIN_BOUNDS' if attempted_roots else 'NO_SEARCH_NEEDED'),
            'extensions': extensions, 'bound_reached': extensions >= MAX_SEARCH_EXTENSIONS,
            'max_changed_positions': MAX_CHANGED_POSITIONS,
            'max_extensions': MAX_SEARCH_EXTENSIONS, 'max_roots': MAX_REPAIR_ROOTS,
            'max_accepted_repairs': MAX_ACCEPTED_REPAIRS,
            'attempted_roots': attempted_roots, 'root_limit_reached': root_limit_reached,
            'repair_limit_reached': accepted >= MAX_ACCEPTED_REPAIRS,
            'rejected_constraints': dict(sorted(rejected.items())),
        })
    return accepted


def plan_primary_rotation_week(db, *, period, positions, primary, pool, planning_date, principal):
    """Jointly place six primary shifts and one traveler in seven existing positions.

    The two existing PRIMARY profiles retain their exact baseline allocations.
    Constraint-driven flexible coverage is solved across all stores by
    allocate_weekly_positions, never by redistributing these primary quotas.
    Try each traveler day before committing; never override locks, availability,
    date eligibility, hard hours or rest rules to satisfy the baseline.
    """
    from app.models import EmployeeSchedulingProfile, Employee
    from app.services.v2_scheduling_policy_service import (
        evaluate_assignment, SimulatedAssignment, longview_rotation_fairness,
        longview_fairness_rank, assignment_score,
    )
    from app.services.v2_scheduling_service import SchedulingValidationError
    profiles = {e.id: db.scalar(select(EmployeeSchedulingProfile).where(
        EmployeeSchedulingProfile.employee_id == e.id)) for e in primary}
    if (len(positions) != 7 or len({s.shift_date for s in positions}) != 7
            or len(primary) != 2 or any(not profiles[e.id] for e in primary)):
        return None
    failures = Counter(); failure_examples = {}; options = []
    positions = sorted(positions,key=lambda s:(s.shift_date,s.id))
    def legal(employee_id, shift, plan):
        sims = tuple(SimulatedAssignment(s.shift_date,s.start_time,s.end_time,s.unpaid_break_minutes)
                     for s in positions if s.id != shift.id and not s.manually_locked
                     and plan.get(s.id) == employee_id)
        result = evaluate_assignment(db,employee_id=employee_id,store_id=shift.store_id,
            shift_date=shift.shift_date,start_time=shift.start_time,end_time=shift.end_time,
            unpaid_break_minutes=shift.unpaid_break_minutes,exclude_shift_id=shift.id,
            schedule_period_id=period.id,simulated_assignments=sims)
        failures.update(r.code for r in result.reasons)
        if result.reasons:
            failure_examples.setdefault((employee_id,shift.shift_date), '; '.join(r.message for r in result.reasons))
        return result
    primary_ids = {e.id for e in primary}
    travelers = [s for s in positions if s.employee_id in {e.id for e in pool}]
    if (all(sum(s.employee_id == e.id for s in positions) == profiles[e.id].target_shifts_per_week for e in primary)
            and sum(s.employee_id in primary_ids for s in positions) == 6 and len(travelers) == 1
            and all(s.employee_id and legal(s.employee_id,s,{}).eligible for s in positions)):
        traveler = travelers[0]; employee = next(e for e in pool if e.id == traveler.employee_id)
        fairness = longview_rotation_fairness(db,employee_id=employee.id,store_id=traveler.store_id,
            before_date=traveler.shift_date,as_of_date=planning_date)
        for shift in positions:
            shift._primary_rotation_reserved = True
        return {'traveler':traveler,'employee':employee,'fairness':fairness,
                'protected_shift_ids':[s.id for s in positions]}
    previous = {s.id:s.employee_id for s in positions if not s.manually_locked}
    for shift in positions:
        if not shift.manually_locked:
            shift.employee_id = None
    db.flush()
    for traveler_shift in positions:
        preferred = [profiles[e.id].target_shifts_per_week or 0 for e in primary]
        splits = [tuple(preferred)] if sum(preferred) == 6 else []
        feasible = []
        for split in splits:
            plan = {}; counts = Counter(); quotas = dict(zip((e.id for e in primary),split))
            def assign_primary(index):
                if index == len(positions):
                    return all(counts[e.id] == quotas[e.id] for e in primary) and all(
                        legal(plan[s.id],s,plan).eligible for s in positions if s.id != traveler_shift.id)
                shift = positions[index]
                if shift.id == traveler_shift.id:
                    return assign_primary(index+1)
                for employee in sorted(primary,key=lambda e:(counts[e.id],e.id)):
                    if counts[employee.id] >= quotas[employee.id] or (shift.manually_locked and shift.employee_id != employee.id):
                        continue
                    if not legal(employee.id,shift,plan).eligible:
                        continue
                    plan[shift.id]=employee.id;counts[employee.id]+=1
                    if assign_primary(index+1):return True
                    counts[employee.id]-=1;plan.pop(shift.id)
                return False
            if assign_primary(0):
                feasible.append((sum(abs(split[i]-preferred[i]) for i in range(2)),dict(plan)))
                # Once the best feasible split is found for this traveler day,
                # less preferred splits cannot improve any traveler option.
                break
        if not feasible:continue
        split_cost,plan=feasible[0]
        for employee in pool:
            if traveler_shift.manually_locked and traveler_shift.employee_id != employee.id:continue
            ev=legal(employee.id,traveler_shift,plan)
            if not ev.eligible:continue
            fairness=longview_rotation_fairness(db,employee_id=employee.id,store_id=traveler_shift.store_id,
                before_date=traveler_shift.shift_date,as_of_date=planning_date)
            preference,gap=assignment_score(db,employee_id=employee.id,store_id=traveler_shift.store_id,
                shift_date=traveler_shift.shift_date)
            rank=(split_cost,longview_fairness_rank(fairness),ev.requires_hour_approval,-gap,-preference,
                  employee.id,traveler_shift.shift_date)
            options.append((rank,dict(plan),traveler_shift,employee,fairness))
    if not options:
        for shift in positions:
            if shift.id in previous:
                shift.employee_id = previous[shift.id]
        db.flush()
        details='; '.join(f'{db.get(Employee,eid).full_name} on {day}: {reason}' for (eid,day),reason in list(failure_examples.items())[:14])
        raise SchedulingValidationError('Longview cannot place the six primary shifts and one rotating shift '
            'without violating the configured restrictions. '+(details or 'Check primary availability and approved rotation participants.'))
    _,plan,traveler,employee,fairness=min(options,key=lambda x:x[0]);plan[traveler.id]=employee.id
    for shift in positions:
        shift._primary_rotation_reserved = True
        if not shift.manually_locked:
            shift.employee_id=plan[shift.id];shift.updated_by_principal_id=principal.id
    db.flush()
    return {'traveler':traveler,'employee':employee,'fairness':fairness,
            'protected_shift_ids':[s.id for s in positions]}
