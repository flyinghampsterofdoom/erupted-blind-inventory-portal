from datetime import date, time, timedelta
import pytest
from app.services.scheduling_horizon_solver import solve_horizon


def row(i, day, employee=None, protected=False):
    return dict(id=i,date=day,week=day-timedelta(days=(day.weekday()+1)%7),
                store_id=1,start=time(9),end=time(17),minutes=480,
                employee_id=employee,protected=protected)


def solve(rows, allowed, targets, **kwargs):
    employees=sorted({eid for eid,_ in targets})
    return solve_horizon(positions=rows,employees=employees,allowed=allowed,
        targets=targets,limits={eid:dict(max_days=1,rest_days=1,max_minutes=None) for eid in employees},
        require_leads=False,**kwargs)


def test_five_employee_reassignment_cycle_has_no_swap_limit():
    day=date(2026,11,2);week=date(2026,11,1)
    rows=[dict(row(i,day,i),store_id=i) for i in range(1,6)]
    allowed={i:[i%5+1] for i in range(1,6)}
    result=solve(rows,allowed,{(i,week):1 for i in range(1,6)})
    assert result['status']=='OPTIMAL'
    assert result['assignments']=={i:i%5+1 for i in range(1,6)}


def test_adjacent_week_can_move_to_make_next_week_feasible():
    # A's original Saturday blocks their required Sunday. Move Saturday to B
    # and Monday to A, preserving each person's exact allocation in both weeks.
    sat=date(2026,10,31);sun=sat+timedelta(days=1);mon=sat-timedelta(days=5)
    rows=[row(1,sat,1),row(2,mon,2),row(3,sun,1),row(4,sun+timedelta(days=2),2)]
    targets={(e,w):1 for e in (1,2) for w in (date(2026,10,25),sun)}
    result=solve(rows,{1:[1,2],2:[1,2],3:[1],4:[2]},targets)
    assert result['status']=='OPTIMAL'
    # The necessary Saturday move alone leaves a 0/1 versus 2/1 imbalance.
    # One additional Monday move balances both weeks without violating rest.
    assert result['assignments']=={1:2,2:1,3:1,4:2}
    assert result['minimum_change_witness']['assignment_changes']==1
    assert result['fairness_search']['improved']
    assert result['fairness_search']['final_loss']==0
    assert result['optimality_scope']=='BOUNDED_REPAIR' and not result['global_optimal']


def test_protected_invalid_assignment_is_infeasible_not_removed():
    day=date(2026,11,1)
    result=solve([row(1,day,1,True)],{1:[2]},{(1,day):0,(2,day):1})
    assert result['status']=='INFEASIBLE' and result['assignments'] is None


def test_fairness_precedes_minimal_changes():
    day=date(2026,11,1)
    rows=[row(1,day,1),row(2,day+timedelta(days=2),2)]
    result=solve(rows,{1:[1,2],2:[1,2]},{(1,day):1,(2,day):1},historical_weekends={(1,6):3},repair=False)
    assert result['assignments']=={1:2,2:1}


def test_hard_hours_never_relaxed_to_fill_position():
    day=date(2026,11,1)
    result=solve_horizon(positions=[row(1,day)],employees=[1],allowed={1:[1]},
        limits={1:dict(max_days=3,rest_days=1,max_minutes=60)},targets={(1,day):1},require_leads=False)
    assert result['status']=='OPTIMAL' and result['assignments']=={1:None}


def test_lead_coverage_never_traded_for_preferences():
    day=date(2026,11,1)
    rows=[row(1,day,2),row(2,day+timedelta(days=2),1)]
    result=solve_horizon(positions=rows,employees=[1,2],allowed={1:[1,2],2:[1,2]},
        limits={e:dict(max_days=3,rest_days=1,max_minutes=None) for e in (1,2)},
        targets={(1,day):1,(2,day):1},lead_ids={1,2},preference_costs={(1,1):100,(2,2):100})
    assert result['status']=='OPTIMAL' and len(result['assignments'])==2


def test_rest_after_max_block_crosses_week_boundary():
    week=date(2026,11,1)
    fixed=[row(9,week-timedelta(days=2),1),row(10,week-timedelta(days=1),1)]
    result=solve_horizon(positions=[row(1,week+timedelta(days=1))],employees=[1],allowed={1:[1]},
        limits={1:dict(max_days=2,rest_days=2,max_minutes=None)},targets={(1,week):1},fixed=fixed,require_leads=False)
    assert result['status']=='OPTIMAL' and result['assignments']=={1:None}


def test_repair_preserves_valid_assignments_despite_fairness_and_travel_costs():
    day=date(2026,11,1)
    rows=[row(1,day,1),row(2,day+timedelta(days=2),2)]
    result=solve(rows,{1:[1,2],2:[1,2]},{(1,day):1,(2,day):1},
        historical_weekends={(1,6):3},traveler_costs={(1,1):100},repair=True)
    assert result['assignments']=={1:1,2:2}
    assert result['stability_optimal'] is True
    assert result['minimum_change_witness']['assignment_changes']==0


def imbalanced_week():
    week=date(2026,12,13)
    rows=[row(i+1,week+timedelta(days=offset),employee) for i,(offset,employee) in
          enumerate([(0,1),(1,1),(2,1),(3,2),(5,2),(6,1)])]
    return dict(positions=rows,employees=[1,2],allowed={r['id']:[1,2] for r in rows},
        limits={e:dict(max_days=3,rest_days=1,max_minutes=None,preferred_max_days=1) for e in [1,2]},
        targets={(1,week):3,(2,week):3},require_leads=False)


@pytest.mark.parametrize('budget',[1,2,3])
def test_bounded_repair_balances_four_and_two_with_one_change(budget):
    inputs=imbalanced_week()
    result=solve_horizon(**inputs,additional_change_budget=budget)
    assert list(result['assignments'].values()).count(1)==3
    assert list(result['assignments'].values()).count(2)==3
    assert sum(result['assignments'][r['id']]!=r['employee_id'] for r in inputs['positions'])==1
    assert result['fairness_search']['baseline_changes']==0
    assert result['fairness_search']['improved'] and result['fairness_search']['final_loss']==0
    assert result['bounded_optimal'] and not result['global_optimal']


def test_zero_extra_budget_retains_imbalance_as_negative_control():
    inputs=imbalanced_week();result=solve_horizon(**inputs,additional_change_budget=0)
    assert result['assignments']=={r['id']:r['employee_id'] for r in inputs['positions']}
    assert result['fairness_search']['final_loss']>0


def test_personal_consecutive_preference_yields_to_allocations():
    week=date(2026,12,13)
    rows=[row(1,week),row(2,week+timedelta(days=1))]
    result=solve_horizon(positions=rows,employees=[1],allowed={1:[1],2:[1]},
        limits={1:dict(max_days=3,preferred_max_days=1,rest_days=1,max_minutes=None)},
        targets={(1,week):2},require_leads=False,repair=False)
    assert result['assignments']=={1:1,2:1}
    assert result['stages'][-1]['consecutive_preference_cost']==100


def test_personal_preference_is_honored_between_equally_fair_fresh_solutions():
    week=date(2026,12,13)
    rows=[row(i+1,week+timedelta(days=i+1)) for i in range(4)]
    result=solve_horizon(positions=rows,employees=[1,2],allowed={r['id']:[1,2] for r in rows},
        limits={e:dict(max_days=3,preferred_max_days=1,rest_days=1,max_minutes=None) for e in [1,2]},
        targets={(1,week):2,(2,week):2},require_leads=False,repair=False)
    assert result['stages'][-1]['consecutive_preference_cost']==0
    assert list(result['assignments'].values()).count(1)==2
    assert list(result['assignments'].values()).count(2)==2


@pytest.mark.parametrize('store_cost,expected_personal_day',[(50,2),(250,1)])
def test_store_and_consecutive_preferences_have_a_real_tradeoff(store_cost,expected_personal_day):
    week=date(2026,12,13)
    rows=[row(1,week+timedelta(days=1)),row(2,week+timedelta(days=2))]
    result=solve_horizon(positions=rows,employees=[1,2],allowed={1:[1,2],2:[1,2]},
        fixed=[row(99,week,1)],targets={(1,week):2,(2,week):1},
        limits={e:dict(max_days=3,preferred_max_days=1,rest_days=1,max_minutes=None) for e in [1,2]},
        preference_costs={(2,1):store_cost},require_leads=False,repair=False)
    assert result['assignments'][expected_personal_day]==1
    assert result['fairness_search']['final_loss']==0
    assert result['stages'][-1]['consecutive_preference_cost']==(100 if expected_personal_day==1 else 0)


def test_soft_preferences_alone_cannot_create_extra_repair_churn():
    week=date(2026,12,13)
    rows=[row(i+1,week+timedelta(days=i+1),1 if i<2 else 2) for i in range(4)]
    result=solve_horizon(positions=rows,employees=[1,2],allowed={r['id']:[1,2] for r in rows},
        targets={(1,week):2,(2,week):2},additional_change_budget=3,require_leads=False,
        limits={e:dict(max_days=3,preferred_max_days=1,rest_days=1,max_minutes=None) for e in [1,2]})
    assert result['assignments']=={r['id']:r['employee_id'] for r in rows}
    assert not result['fairness_search']['improved']


def test_unproven_minimum_change_baseline_does_not_start_extra_search(monkeypatch):
    from ortools.sat.python import cp_model
    real=cp_model.CpSolver.solve;calls=0
    def unproven(self,model,*args,**kwargs):
        nonlocal calls
        calls+=1
        status=real(self,model,*args,**kwargs)
        return cp_model.FEASIBLE if calls==3 else status
    monkeypatch.setattr(cp_model.CpSolver,'solve',unproven)
    inputs=imbalanced_week();result=solve_horizon(**inputs)
    assert calls==3 and result['status']=='FEASIBLE'
    assert result['assignments']=={r['id']:r['employee_id'] for r in inputs['positions']}
    assert not result['stability_optimal'] and not result['fairness_search']['improved']


@pytest.mark.parametrize('stop_at',[4,5])
def test_timeout_retains_feasible_incumbent_without_spending_budget_on_preferences(monkeypatch,stop_at):
    from ortools.sat.python import cp_model
    real=cp_model.CpSolver.solve;calls=0
    def interrupted(self,model,*args,**kwargs):
        nonlocal calls
        calls+=1
        status=real(self,model,*args,**kwargs)
        return cp_model.UNKNOWN if calls>=stop_at else status
    monkeypatch.setattr(cp_model.CpSolver,'solve',interrupted)
    inputs=imbalanced_week();result=solve_horizon(**inputs)
    assert result['status']=='FEASIBLE' and not result['global_optimal']
    assert all(e is not None for e in result['assignments'].values())
    changes=sum(result['assignments'][r['id']]!=r['employee_id'] for r in inputs['positions'])
    if stop_at==4:
        assert changes==0 and not result['fairness_search']['improved']
    else:
        assert changes<=result['fairness_search']['additional_change_budget']
        assert result['fairness_search']['final_loss']==0
