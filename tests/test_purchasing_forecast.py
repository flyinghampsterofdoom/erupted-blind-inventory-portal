from dataclasses import replace
from decimal import Decimal
import pytest
from app.services.purchase_order_math_service import DemandEvidence,forecast_demand,purchase_multiple,allocate_purchase


def evidence(sales=None,**kw):return DemandEvidence(tuple(Decimal(str(x)) for x in (sales if sales is not None else [2]*84)),Decimal(0),**kw)


def test_stable_demand_defaults_exact_position_and_store_independence():
    first=forecast_demand(evidence())
    assert first.daily_velocity==2 and first.reorder_level==70 and first.target_level==140 and first.quantity==140
    assert forecast_demand(replace(evidence(),current_inventory=Decimal(25),incoming=20)).quantity==95
    assert forecast_demand(replace(evidence(),current_inventory=Decimal(71))).quantity==0
    assert forecast_demand(replace(evidence(),current_inventory=Decimal('-1.5'),incoming=2)).quantity==140


def test_sustained_growth_and_decline_change_forecast_in_the_right_direction():
    stable=forecast_demand(evidence([2]*84))
    growing=forecast_demand(evidence([2]*56+[4]*28))
    declining=forecast_demand(evidence([4]*56+[1]*28))
    assert growing.daily_velocity>stable.daily_velocity and growing.trend=='GROWING'
    assert declining.daily_velocity<Decimal(4) and declining.trend=='DECLINING'
    assert declining.daily_velocity<sum(Decimal(x) for x in ([4]*28+[1]*28))/56


def test_isolated_spike_is_bounded_while_sustained_growth_survives():
    row=forecast_demand(evidence([2]*83+[200]))
    assert row.spike_days==1 and row.daily_velocity<3 and row.quantity<210
    assert 'ISOLATED_SPIKES_CAPPED' in row.reasons


def test_new_product_positive_history_is_conservative_without_required_par_settings():
    young=forecast_demand(evidence([0]*77+[2]*7,product_age_days=7))
    established=forecast_demand(evidence([2]*84))
    assert young.trend=='NEW' and young.confidence=='LOW' and 0<young.quantity<established.quantity
    assert forecast_demand(evidence([0]*84,product_age_days=5)).quantity is None


def test_stockout_zero_days_are_not_treated_as_zero_demand():
    values=[2]*42+[0]*14
    adjusted=forecast_demand(evidence(values,availability=tuple([True]*42+[False]*14)))
    unqualified=forecast_demand(evidence(values))
    assert adjusted.stockout_days==14 and adjusted.daily_velocity>unqualified.daily_velocity
    assert adjusted.daily_velocity<=2
    assert forecast_demand(evidence([0]*56,availability=(False,)*56)).quantity is None


def test_slow_inventory_and_lifecycle_do_not_trigger_unnecessary_replenishment():
    slow=forecast_demand(evidence([2]*28+[0]*56))
    assert slow.trend=='SLOW' and slow.quantity==0
    assert forecast_demand(evidence(lifecycle='NO_FUTURE_REORDER')).quantity is None
    assert forecast_demand(evidence(lifecycle='ARCHIVED')).quantity is None


@pytest.mark.parametrize('change',[{'current_inventory':None},{'incoming':None},{'sales_complete':False}])
def test_unknown_sources_do_not_manufacture_zero(change):
    result=forecast_demand(replace(evidence(),**change))
    assert result.quantity is None and result.reasons


def test_reliable_lead_time_influences_threshold_and_unknown_age_does_not_invent_age():
    first=forecast_demand(replace(evidence(),current_inventory=Decimal(80)))
    lead=forecast_demand(replace(evidence(),current_inventory=Decimal(80),lead_time_days=50))
    assert first.quantity==0 and lead.quantity==60
    assert forecast_demand(evidence(reliable_stock_age_days=None))==forecast_demand(evidence())


@pytest.mark.parametrize('multiple',[3,4,5])
def test_practical_multiples_are_product_scoped(multiple):
    assert purchase_multiple(1,multiple)==multiple
    assert purchase_multiple(0,multiple)==0


def test_allocations_preserve_exact_total_and_deterministic_store_priorities():
    assert allocate_purchase(5,{1:1,2:1,3:1,4:0})=={1:2,2:2,3:1,4:0}
    assert sum(allocate_purchase(17,{1:3,2:0,3:9,4:2}).values())==17
    assert allocate_purchase(3,{1:0,2:0})=={1:3,2:0}


def test_historical_availability_never_backcasts_current_stock_or_ignores_today():
    from app.services.purchase_order_math_service import qualified_availability
    from datetime import date,timedelta
    days=tuple(date(2026,10,1)+timedelta(days=i) for i in range(3))
    # Today received five. All three completed days were unavailable.
    availability=qualified_availability(days,{}, {},Decimal(5),Decimal(5),reliable=True)
    assert availability==(False,False,False)
    assert qualified_availability(days,{}, {},Decimal(5),None,reliable=True)==(None,)*3
    assert qualified_availability(days,{}, {},Decimal(5),Decimal(5),reliable=False)==(None,)*3


@pytest.mark.parametrize('sales',[[2]*60,[1]*32+[3]*28,[4]*32+[1]*28,[2]*59+[200]])
def test_all_four_calculation_families_delegate_to_the_same_forecast(sales):
    from datetime import date,datetime,timedelta,timezone
    from types import SimpleNamespace
    from app.services.purchase_order_math_service import LineMathInput,OrderingMathParams,compute_line_recommendation
    from app.services.inventory_velocity_report_service import VelocityInventory,VelocitySale,calculate_velocity_metrics
    from app.services.v2_ordering_recommendation_service import calculate_recommendation
    from test_v2_ordering_recommendation_service import _candidate,DailyQuantity,NOW
    from app.services.v2_replenishment_report_service import ReplenishmentReport,build_replenishment_preview
    values=tuple(Decimal(v) for v in sales);hand=Decimal(3);incoming=2
    core=forecast_demand(DemandEvidence(values,hand,incoming))
    standard=compute_line_recommendation(LineMathInput('SKU',hand,incoming,list(values)),OrderingMathParams(history_lookback_days=60))
    end=NOW.date()-timedelta(days=1)
    history=tuple(DailyQuantity(end-timedelta(days=59-i),value) for i,value in enumerate(values))
    from app.services.v2_ordering_normalization_service import IncomingSupply
    intelligence=calculate_recommendation(_candidate(current_on_hand=hand,daily_sales=history,incoming_supply=(IncomingSupply(1,2,NOW),)))
    velocity=calculate_velocity_metrics([VelocitySale(row.day,'VAR','L1',row.quantity,Decimal(0)) for row in history],
        {'VAR':VelocityInventory('VAR','SKU','Product','Category','Vendor',Decimal(3),False,{1:hand})},
        days=60,end_date=end,store_names={1:'Store'},store_by_location={'L1':1},target_days=70,incoming_by_store_variation={(1,'VAR'):incoming})[0]
    row=SimpleNamespace(vendor_id=1,vendor='Vendor',excluded=False,key='1:VAR',demand_by_store=((1,DemandEvidence(values,hand,incoming)),),
        pack_size=1,unit_cost=Decimal(3),current_on_hand=hand,average_weekly_sales=Decimal(7),product_name='Product')
    report=ReplenishmentReport(end,end,(row,),1,0,())
    legacy=build_replenishment_preview(report,vendor_id=1,mode='replace_sales')
    assert core.quantity==standard.rounded_recommended_qty==intelligence.calculated_quantity==int(velocity.recommended_reorder_quantity)==legacy.lines[0].adjusted_suggested_qty
    assert build_replenishment_preview(report,vendor_id=1,mode='target_weeks').lines[0].adjusted_suggested_qty==core.quantity


def test_evaluation_uses_past_only_and_does_not_claim_actual_stockouts():
    from scripts.evaluate_purchasing_forecast import evaluate
    result=evaluate([{'name':'step','sales':[1]*28+[10]*14,'unit_cost':3}])
    metrics=result['results'][0]['metrics']
    assert metrics['unified']['mae_units']==126 and metrics['unified']['bias_units']==-126
    assert metrics['average_28']['mae_units']==126
    assert metrics['unified']['shortfall_units_proxy']==126
    assert metrics['unified']['excess_units_proxy']==0
    assert metrics['unified']['capital_required_proxy']==42
    assert 'not measured store stockouts' in result['risk_note']
