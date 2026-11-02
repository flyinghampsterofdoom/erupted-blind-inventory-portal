"""Three-day blocks are soft preferences, below existing scheduling priorities."""
from datetime import date, time, timedelta
import pytest
from app.services.scheduling_horizon_solver import solve_horizon
from test_scheduling_horizon_solver import row


def inputs():
    week = date(2026, 11, 1)
    # Monday is fixed for Lexi and Saturday for another employee. The two
    # equivalent completions are Mon/Tue/Wed and Mon/Thu/Fri.
    rows = [row(i+1, week+timedelta(days=d)) for i,d in enumerate([1,2,3,4,5,6])]
    return dict(positions=rows, employees=[1,2],
        allowed={1:[1],2:[1,2],3:[1,2],4:[1,2],5:[1,2],6:[2]},
        limits={e:dict(max_days=3,rest_days=1,max_minutes=None,
                       preferred_max_days=3,prefer_three_day_blocks=e==1) for e in [1,2]},
        targets={(1,week):3,(2,week):3},require_leads=False,repair=False)


def days(result, data, eid=1):
    return {r['date'] for r in data['positions'] if result['assignments'][r['id']]==eid}


def block(dates):
    return any({d,d+timedelta(days=1),d+timedelta(days=2)} <= dates for d in dates)


@pytest.mark.parametrize('minor_store_cost',[0,1])
def test_three_day_block_beats_equivalent_scattered_assignments(minor_store_cost):
    data=inputs()
    # A tiny soft tie-break exposes the old engine's missing block reward.
    data['preference_costs']={(2,1):minor_store_cost}
    result=solve_horizon(**data)
    assert result['status']=='OPTIMAL'
    assert block(days(result,data))
    assert all(e is not None for e in result['assignments'].values())
    assert result['fairness_search']['final_loss']==0


@pytest.mark.parametrize('reason',['availability','lead','hours','protected'])
def test_three_day_preference_yields_to_higher_requirements(reason):
    data=inputs()
    if reason=='availability':data['allowed'][2]=[2]
    elif reason=='lead':
        data['require_leads']=True;data['lead_ids']={2,3}
        data['employees'].append(3)
        data['limits'][3]=dict(max_days=3,rest_days=1,max_minutes=None)
        week=data['positions'][0]['week']
        data['targets'][3,week]=3
        for i,d in enumerate([1,4,5]):
            data['positions'].append(row(10+i,week+timedelta(days=d)))
            data['allowed'][10+i]=[3]
    elif reason=='hours':data['limits'][1]['max_minutes']=480*2
    else:
        data['positions'][1].update(employee_id=2,protected=True)
    result=solve_horizon(**data)
    assert all(e is not None for e in result['assignments'].values())
    assert not block(days(result,data))
    if reason=='lead':
        assert next(s['value'] for s in result['stages'] if s['objective']=='unfilled_required_roles')==0
        assert result['assignments'][2]==2
    if reason=='protected':assert result['assignments'][2]==2


def test_cross_week_block_is_rewarded_without_fourth_day():
    data=inputs();week=data['positions'][0]['week']
    data['fixed']=[row(99,week-timedelta(days=1),1),row(100,week,1)]
    data['targets'][(1,week)]=3
    result=solve_horizon(**data)
    all_dates=days(result,data)|{r['date'] for r in data['fixed']}
    assert block(all_dates)
    assert not any({d+timedelta(days=n) for n in range(4)} <= all_dates for d in all_dates)


def test_existing_three_day_arrangement_survives_bounded_repair():
    data=inputs()
    for r,e in zip(data['positions'],[1,1,1,2,2,2]):r['employee_id']=e
    data['repair']=True
    result=solve_horizon(**data)
    assert result['assignments']=={r['id']:r['employee_id'] for r in data['positions']}
    assert block(days(result,data))


def test_employee_without_explicit_three_day_preference_keeps_existing_behavior():
    data=inputs();data['limits'][1]['prefer_three_day_blocks']=False
    data['preference_costs']={(2,1):1}
    assert not block(days(solve_horizon(**data),data))


def test_same_day_double_shift_is_not_a_three_day_block():
    data=inputs();day=data['positions'][0]['date']
    data['positions']=[row(i,day) for i in range(1,4)]
    for i,r in enumerate(data['positions']):
        r.update(start=time(8+i*3),end=time(10+i*3),minutes=120)
    data['allowed']={i:[1] for i in range(1,4)}
    data['targets']={(1,data['positions'][0]['week']):3,(2,data['positions'][0]['week']):0}
    result=solve_horizon(**data)
    assert result['stages'][-1]['completed_three_day_blocks']==0


def test_allocation_priority_is_unchanged_by_block_reward():
    data=inputs();week=data['positions'][0]['week']
    data['targets']={(1,week):2,(2,week):4}
    result=solve_horizon(**data)
    assert result['fairness_search']['final_loss']==0
    assert len(days(result,data))==2


def test_three_day_preference_precedes_weekend_fairness():
    data=inputs();week=data['positions'][0]['week']
    data['positions']=[row(i+1,week+timedelta(days=d)) for i,d in enumerate([0,1,2,4,5,6])]
    data['allowed']={1:[1,2],2:[1],3:[1],4:[2],5:[2],6:[1,2]}
    data['historical_weekends']={(1,6):3,(2,6):0}
    result=solve_horizon(**data)
    assert result['assignments'][1]==1 and result['assignments'][6]==2
    assert block(days(result,data))
    assert result['fairness_search']['final_loss']==0


def test_weekend_fairness_still_selects_between_equal_three_day_blocks():
    data=inputs();week=data['positions'][0]['week']
    data['positions']=[row(i+1,week+timedelta(days=d)) for i,d in enumerate([0,1,2,4,5,6])]
    data['allowed']={r['id']:[1,2] for r in data['positions']}
    data['limits'][2]['prefer_three_day_blocks']=True
    data['historical_weekends']={(1,6):3,(2,6):0}
    result=solve_horizon(**data)
    assert result['stages'][-1]['completed_three_day_blocks']==2
    assert block(days(result,data,1)) and block(days(result,data,2))
    assert result['assignments'][1]==2


def test_full_week_block_beats_partial_boundary_block_and_stray_day():
    data=inputs();week=data['positions'][0]['week']
    data['positions']=[row(i+1,week+timedelta(days=d)) for i,d in enumerate([0,1,3,4,5,6])]
    data['allowed']={r['id']:[1,2] for r in data['positions']}
    data['fixed']=[row(99,week-timedelta(days=1),1)]
    data['historical_weekends']={(1,6):0,(2,6):3}
    result=solve_horizon(**data)
    assert days(result,data)=={week+timedelta(days=d) for d in [3,4,5]}
    assert result['stages'][-1]['three_day_preference_days']==3


@pytest.mark.parametrize('repair',[False,True])
def test_anonymized_latest_week_selects_three_consecutive_days(repair):
    import json
    from pathlib import Path
    data=json.loads((Path(__file__).parent/'fixtures/scheduling_three_day_week.json').read_text())
    data.pop('provenance')
    # Automatic initial generation already has a generation timestamp, so
    # exercise its repair-mode solve with initially unassigned positions too.
    data['repair']=repair
    for r in data['positions']+data['fixed']:
        for key in ['date','week']:r[key]=date.fromisoformat(r[key])
        for key in ['start','end']:r[key]=time.fromisoformat(r[key])
    for key in ['allowed','limits']:data[key]={int(k):v for k,v in data[key].items()}
    data['targets']={(e,date.fromisoformat(w)):n for e,w,n in data['targets']}
    for key in ['preference_costs','historical_weekends','traveler_costs','traveler_history']:
        data[key]={(a,b):n for a,b,n in data[key]}
    data['traveler_ids']={tuple(v) for v in data['traveler_ids']}
    result=solve_horizon(**data)
    requested_dates=days(result,data,5)
    assert len(requested_dates)==3 and max(requested_dates)-min(requested_dates)==timedelta(days=2)
    assert all(e is not None for e in result['assignments'].values())
    assert result['fairness_search']['final_loss']==0
    assert next(s['value'] for s in result['stages'] if s['objective']=='unfilled_required_roles')==0
    assert result['stages'][-1]['weekend_fairness']>0
