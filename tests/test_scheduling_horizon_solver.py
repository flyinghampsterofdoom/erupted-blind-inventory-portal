from datetime import date, time, timedelta
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
    assert result['assignments']=={1:2,2:1,3:1,4:2}


def test_protected_invalid_assignment_is_infeasible_not_removed():
    day=date(2026,11,1)
    result=solve([row(1,day,1,True)],{1:[2]},{(1,day):0,(2,day):1})
    assert result['status']=='INFEASIBLE' and result['assignments'] is None


def test_fairness_precedes_minimal_changes():
    day=date(2026,11,1)
    rows=[row(1,day,1),row(2,day+timedelta(days=2),2)]
    result=solve(rows,{1:[1,2],2:[1,2]},{(1,day):1,(2,day):1},historical_weekends={(1,6):3})
    assert result['assignments']=={1:2,2:1}


def test_hard_hours_never_relaxed_to_fill_position():
    day=date(2026,11,1)
    result=solve_horizon(positions=[row(1,day)],employees=[1],allowed={1:[1]},
        limits={1:dict(max_days=3,rest_days=1,max_minutes=60)},targets={(1,day):1},require_leads=False)
    assert result['status']=='INFEASIBLE'


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
    assert result['status']=='INFEASIBLE'
