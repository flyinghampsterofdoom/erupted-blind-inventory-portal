from dataclasses import replace,FrozenInstanceError
from datetime import date,datetime,timedelta,timezone
from decimal import Decimal,localcontext,ROUND_HALF_UP
from fractions import Fraction as F
import random
import pytest
from app.purchasing_v4.contracts import QuantityEvidence,EvidenceState,Availability,ExposureDay,WindowContract,CandidateModelInputs
from app.purchasing_v4.calculation_config import Parameters,StockoutControl,Configuration,Coverage,CategoryOverride
from app.purchasing_v4.calculation import (StoreInput,Supply,SupplyLot,Deduction,ManualRule,ProductCoverageOverride,
    recommend_batch,compare_manual_history,estimate,inventory_position)
from app.purchasing_v4.calculation_serialization import evidence_digest,canonical_bytes

ASOF=datetime(2026,10,27,12,tzinfo=timezone.utc)
CUTOFF=date(2026,10,27)


def known(value,source='fixture'):
    return QuantityEvidence(EvidenceState.KNOWN,Decimal(value),source,observed_at=ASOF)


def configuration(**kwargs):
    params=Parameters(CandidateModelInputs(WindowContract(56,28),F(2,5),F(3,5),F(3,10),F(1),F(1)),
                      F(2),StockoutControl('confirmed_only',None),'preserve_qualified_negative')
    return Configuration('explicit-fixture',params.override(kwargs),Coverage(F(4),F(8)),())


def history(old_units=20,recent_units=40,old_n=14,recent_n=14,old_length=28,recent_length=28):
    output=[]
    for length,n,total,end in [(old_length,old_n,old_units,CUTOFF-timedelta(days=recent_length)),
                              (recent_length,recent_n,recent_units,CUTOFF)]:
        available_indices=set((i*length//n) for i in range(n)) if n else set()
        sold=[total//n+(i<total%n) for i in range(n)] if n else []
        k=0
        for i in range(length):
            eligible=i in available_indices
            value=sold[k] if eligible else 0
            if eligible:k+=1
            output.append(ExposureDay(end-timedelta(days=length-i),Availability.AVAILABLE if eligible else Availability.UNKNOWN,known(value)))
    return tuple(output)


def row(**kwargs):
    defaults=dict(variation_id='fixture-variation',store_id='store1',category_id=None,history=history(),
        on_hand=known(0),supply=Supply((),(),(),True,'fixture-watermark'),pack_size=5,no_reorder=False,
        manual=None,product_coverage=None,eligible_from=CUTOFF-timedelta(days=56),identity_issues=())
    defaults.update(kwargs);return StoreInput(**defaults)


def run(record=None,config=None):
    rows=(record or row(),);return recommend_batch(rows,config or configuration(),as_of=ASOF,
        input_digest=evidence_digest(rows,as_of=ASOF)).recommendations[0]


def test_exact_adaptive_golden_and_complete_explanation():
    result=run()
    assert result.forecast.weekly_units==16
    assert result.forecast.historical.velocity==10 and result.forecast.recent.velocity==20
    assert result.forecast.historical.available_days==14 and result.forecast.recent.available_days==14
    assert result.forecast.final_recent_weight==F(3,5)
    assert result.inventory_position==0 and result.raw_need_units==128
    assert result.suggested_packs==26 and result.suggested_actual_units==130
    assert result.policy_inputs==( ('reorder_weeks','4'),('target_weeks','8'))
    assert result.observed_on_hand.value==0 and result.supply_evidence.watermark


def test_reliability_adjustment_independently_matches_three_sevenths():
    config=configuration(lookback_days=42,recent_days=14)
    result=run(row(history=history(40,40,28,14,28,14)),config)
    assert result.forecast.historical.reliability_index==1
    assert result.forecast.recent.reliability_index==F(1,2)
    assert result.forecast.final_recent_weight==F(3,7)
    assert result.forecast.weekly_units==F(100,7)


@pytest.mark.parametrize('old,new,expected,direction',[(40,20,F(15),'DECREASING'),(20,20,F(10),'STABLE'),(0,0,F(0),'STABLE')])
def test_declining_stable_and_confirmed_zero(old,new,expected,direction):
    result=run(row(history=history(old,new)))
    assert result.forecast.weekly_units==expected and result.forecast.direction==direction
    if expected==0:assert result.forecast.state=='ESTIMATED_ZERO' and result.suggested_packs==0


def test_trend_controls_independent_and_caps_bound_observed_gap():
    up=run();down=run(row(history=history(40,20)))
    assert run(config=configuration(increasing_sensitivity=F(0))).forecast.weekly_units<up.forecast.weekly_units
    assert run(row(history=history(40,20)),configuration(increasing_sensitivity=F(0))).forecast.weekly_units==down.forecast.weekly_units
    assert run(config=configuration(decreasing_sensitivity=F(1))).forecast.weekly_units==up.forecast.weekly_units
    assert run(row(history=history(40,20)),configuration(decreasing_sensitivity=F(1))).forecast.weekly_units<down.forecast.weekly_units
    capped=run(config=configuration(increasing_limit=F(1,2)))
    assert capped.forecast.final_recent_weight==F(1,2) and capped.forecast.weekly_units==15


@pytest.mark.parametrize('old_n,recent_n,state,branch',[(14,0,'ESTIMATED','POOLED_CONFIRMED'),
    (0,14,'ESTIMATED','POOLED_CONFIRMED'),(7,7,'ESTIMATED','POOLED_CONFIRMED'),
    (6,7,'ABSTAIN','INSUFFICIENT_EVIDENCE'),(0,0,'ABSTAIN','INSUFFICIENT_EVIDENCE')])
def test_history_sufficiency_nonconsecutive_and_pooled(old_n,recent_n,state,branch):
    result=run(row(history=history(old_n,recent_n,old_n,recent_n)))
    assert result.forecast.state==state and result.forecast.branch==branch
    if state=='ABSTAIN':
        assert result.raw_need_units is None and result.suggested_packs==0
        assert result.informational_message=='Please set a PAR and LEVEL'


def test_minimum_history_is_configurable_and_dates_do_not_double_count():
    source=row(history=history(6,7,6,7))
    assert run(source).forecast.weekly_units is None
    assert run(source,configuration(minimum_available_days=7)).forecast.weekly_units==7
    result=run();assert result.forecast.pooled.available_days==28
    assert result.forecast.historical.end==result.forecast.recent.start


def test_stockout_unknown_and_partial_exposure_never_fabricate_sales():
    source=row()
    stockouts=tuple(replace(d,availability=Availability.STOCKOUT) if d.availability==Availability.UNKNOWN else d for d in source.history)
    assert run(replace(source,history=stockouts)).forecast.weekly_units==16
    all_out=tuple(replace(d,availability=Availability.STOCKOUT,sales=known(0)) for d in source.history)
    assert run(replace(source,history=all_out)).forecast.state=='ABSTAIN'
    contradictory=tuple(replace(d,availability=Availability.STOCKOUT,sales=known(5)) for d in source.history)
    result=run(replace(source,history=contradictory))
    assert result.forecast.weekly_units is None
    assert result.forecast.pooled.observed_units==280 and result.forecast.pooled.eligible_units==0
    assert 'CONTRADICTORY_STOCKOUT_SALES' in result.forecast.reasons


def test_numeric_stockout_alternatives_are_explicit_and_never_scale_denominator():
    source=row()
    baseline=run(source)
    assert run(source,configuration(stockout=StockoutControl('maximum_unknown_share',F(1,4)))).forecast.state=='ABSTAIN'
    stockouts=replace(source,history=tuple(replace(d,availability=Availability.STOCKOUT) if d.availability==Availability.UNKNOWN else d for d in source.history))
    assert run(stockouts,configuration(stockout=StockoutControl('maximum_unknown_share',F(1,4)))).forecast.weekly_units==baseline.forecast.weekly_units
    assert run(stockouts,configuration(stockout=StockoutControl('minimum_available_share',F(3,4)))).forecast.state=='ABSTAIN'
    with pytest.raises(ValueError):StockoutControl('calendar_days',F(1))


def test_unknown_or_unavailable_sales_and_future_source_are_excluded():
    source=row()
    for state in (EvidenceState.UNKNOWN,EvidenceState.UNAVAILABLE):
        bad=tuple(replace(d,sales=QuantityEvidence(state,None,'fixture','missing')) for d in source.history)
        assert run(replace(source,history=bad)).forecast.weekly_units is None
    future=tuple(replace(d,sales=replace(d.sales,observed_at=ASOF+timedelta(seconds=1))) for d in source.history)
    assert run(replace(source,history=future)).forecast.weekly_units is None
    assert run(replace(source,history=source.history+(ExposureDay(CUTOFF,Availability.AVAILABLE,known(999)),))).forecast.weekly_units==16


def test_manual_units_independent_of_history_and_never_convert(monkeypatch):
    import app.purchasing_v4.calculation as engine
    monkeypatch.setattr(engine,'estimate',lambda *a,**k: (_ for _ in ()).throw(AssertionError('manual forecast called')))
    source=row(history=(),manual=ManualRule('owner-manual',5,20),on_hand=known(3),
        supply=Supply((SupplyLot('incoming','VENDOR_OPEN',known(2),False),),(),(),True,'watermark'))
    result=run(source)
    assert result.policy_mode=='MANUAL_UNITS' and result.raw_need_units==15 and result.suggested_packs==3
    assert result.forecast.state=='SKIPPED_MANUAL' and source.manual.identity=='owner-manual'
    assert run(replace(source,no_reorder=True)).state=='EXCLUDED_NO_REORDER'


def test_manual_comparison_owner_triggered_all_unchecked_and_inputs_unchanged():
    rows=(row(manual=ManualRule('owner-manual',5,20)),)
    before=canonical_bytes(rows)
    with pytest.raises(PermissionError):compare_manual_history(rows,configuration(),as_of=ASOF,input_digest='fixture',owner_triggered=False)
    compared=compare_manual_history(rows,configuration(),as_of=ASOF,input_digest='fixture',owner_triggered=True)
    assert compared[0]['eligible_for_conversion'] and not compared[0]['selected_for_conversion']
    assert not compared[0]['conversion_performed'] and canonical_bytes(rows)==before


def test_policy_precedence_category_inheritance_removal_and_product_override():
    base=configuration()
    override=CategoryOverride('category',(('recent_influence',F(1,2)),('decreasing_sensitivity',None)),Coverage(F(2),F(6)))
    configured=replace(base,categories=(override,))
    selected=run(row(category_id='category'),configured)
    assert dict(selected.parameter_sources)['recent_influence']=='CATEGORY'
    assert dict(selected.parameter_sources)['decreasing_sensitivity']=='GLOBAL'
    assert selected.policy_source=='CATEGORY' and dict(selected.policy_inputs)['target_weeks']=='6'
    restored=configured.without_category_parameter('category','recent_influence',new_version='removed')
    assert dict(run(row(category_id='category'),restored).parameter_sources)['recent_influence']=='GLOBAL'
    product=run(row(category_id='category',product_coverage=ProductCoverageOverride(Coverage(F(1),F(3)),'owner-policy')),configured)
    assert product.policy_source=='PRODUCT:owner-policy' and dict(product.policy_inputs)['target_weeks']=='3'
    manual=run(row(category_id='category',manual=ManualRule('manual',5,20),product_coverage=ProductCoverageOverride(Coverage(F(1),F(3)),'owner-policy')),configured)
    assert manual.policy_mode=='MANUAL_UNITS' and manual.target_units==20


def test_incoming_conservation_stages_and_pending_barrier():
    for stage in ('VENDOR_OPEN','AT_CENTRAL','IN_TRANSIT'):
        source=row(on_hand=known(20),supply=Supply((SupplyLot('lot',stage,known(30),False),),(),(),True,'watermark'))
        assert run(source).inventory_position==50
    pending=replace(source,supply=replace(source.supply,incoming=(SupplyLot('lot','ARRIVAL_PENDING',known(30),False),)))
    assert run(pending).inventory_position is None and run(pending).raw_need_units is None
    assert run(replace(source,on_hand=known(50),supply=Supply((),(),(),True,'represented'))).inventory_position==50
    central=row(on_hand=known(100),supply=Supply((),(Deduction('out',known(70),True),),(Deduction('hold',known(5),True),),True,'watermark'))
    assert run(central).inventory_position==25


@pytest.mark.parametrize('supply,reason',[
    (Supply((SupplyLot('lot','VENDOR_OPEN',QuantityEvidence(EvidenceState.UNKNOWN,None,'PO129','CONFLICTING_INCOMING_ALLOCATION'),False),),(),(),True,'watermark'),'CONFLICTING_INCOMING_ALLOCATION'),
    (Supply((SupplyLot('lot','VENDOR_OPEN',known(1),True),),(),(),True,'watermark'),'INCOMING_ALREADY_REPRESENTED_IN_STOCK'),
    (Supply((),(Deduction('same',known(1),True),),(Deduction('same',known(1),True),),True,'watermark'),'DUPLICATE_OR_OVERLAPPING_SUPPLY_IDENTITY'),
    (Supply((),(),(),False,'watermark'),'UNQUALIFIED_SUPPLY_STOCK_WATERMARK'),
    (Supply((SupplyLot('bad','VENDOR_OPEN',known(-1),False),),(),(),True,'watermark'),'INVALID_SUPPLY_QUANTITY')])
def test_unresolved_supply_never_becomes_confirmed_zero(supply,reason):
    result=run(row(on_hand=known(10),supply=supply,manual=ManualRule('manual',5,20)))
    assert result.inventory_position is None and result.raw_need_units is None and result.suggested_packs==0
    assert reason in result.reasons


def test_negative_inventory_clamped_for_legacy_configuration_values():
    preserved=run(row(on_hand=known(-2)))
    assert preserved.inventory_position==0 and preserved.raw_need_units==128
    assert 'NEGATIVE_OBSERVED_ON_HAND' in preserved.reasons
    guarded=run(row(on_hand=known(-2)),configuration(negative_inventory='abstain_on_negative'))
    assert guarded.inventory_position==0 and guarded.raw_need_units==128


def test_each_store_rounds_before_sum_and_zero_stores_stay_zero():
    rows=tuple(row(store_id=str(i),history=(),manual=ManualRule('manual-'+str(i),0,need),on_hand=known(0))
               for i,need in enumerate((1,6,8,3)))
    result=recommend_batch(rows,configuration(),as_of=ASOF,input_digest='pack-fixture')
    assert [r.suggested_packs for r in result.recommendations]==[1,1,2,1]
    assert sum(r.suggested_packs for r in result.recommendations)==5
    assert run(row(history=(),manual=ManualRule('manual',5,20),on_hand=known(30))).suggested_packs==0


def test_demand_remains_independent_across_four_stores():
    rows=tuple(row(store_id=str(i),history=history(20*(i+1),40*(i+1))) for i in range(4))
    result=recommend_batch(rows,configuration(),as_of=ASOF,input_digest='four-store-fixture')
    assert [r.forecast.weekly_units for r in result.recommendations]==[16,32,48,64]


def test_missing_pack_unknown_identity_and_unresolved_manual_policy_are_explicit():
    assert run(row(pack_size=None)).state=='UNRESOLVED_PACK_CONFIGURATION'
    assert run(row(identity_issues=('EXPECTED_COST_UNKNOWN',))).state=='REVIEW_REQUIRED'
    result=run(row(policy_issues=('UNRESOLVED_LEGACY_MANUAL_QUANTITIES',)))
    assert result.raw_need_units is None and result.policy_mode=='UNRESOLVED_POLICY'
    assert run(row(manual=ManualRule('ambiguous',5,5))).raw_need_units is None


def test_batch_and_input_immutability_global_rules_and_configuration_validation():
    source=row();before=canonical_bytes(source)
    first=run(source);assert canonical_bytes(source)==before and run(source)==first
    with pytest.raises(FrozenInstanceError):source.no_reorder=True
    with pytest.raises(ValueError):recommend_batch((source,source),configuration(),as_of=ASOF,input_digest='x')
    with pytest.raises(ValueError):recommend_batch((source,replace(source,store_id='other',no_reorder=True)),configuration(),as_of=ASOF,input_digest='x')
    with pytest.raises(ValueError):configuration(recent_days=56)


def test_decimal_independent_oracle_randomized_adaptive_and_rounding():
    rng=random.Random(7042)
    with localcontext() as ctx:
        ctx.prec=80
        for _ in range(100):
            old,recent=rng.randrange(0,100),rng.randrange(0,100)
            source=row(history=history(old,recent),on_hand=known(str(rng.randrange(0,50))))
            actual=run(source)
            # Independent Decimal oracle, no production estimator/stats/rounding.
            h=Decimal(old)/Decimal(14)*7;j=Decimal(recent)/Decimal(14)*7
            z=abs(j-h)/(j+h) if j+h else Decimal(0)
            sensitivity=Decimal('0.6') if j>h else Decimal('0.3') if j<h else Decimal(0)
            weight=min(Decimal(1),Decimal('0.4')+sensitivity*z)
            forecast=(1-weight)*h+weight*j
            observed=Decimal(actual.forecast.weekly_units.numerator)/Decimal(actual.forecast.weekly_units.denominator)
            assert abs(observed-forecast)<Decimal('1e-60')
            position=source.on_hand.value
            need=max(forecast*8-position,Decimal(0)) if position<=forecast*4 else Decimal(0)
            expected=0 if need==0 else max(1,int((need/5).to_integral_value(rounding=ROUND_HALF_UP)))
            assert actual.suggested_packs==expected


def test_intermittent_demand_exact_observed_rate():
    days=tuple(ExposureDay(CUTOFF-timedelta(days=56-i),Availability.AVAILABLE,known(14 if i%7==0 else 0)) for i in range(56))
    result=run(row(history=days))
    assert result.forecast.historical.velocity==14
    assert result.forecast.recent.velocity==14
    assert result.forecast.weekly_units==14
    assert result.forecast.pooled.observed_units==112


def test_missing_recent_sales_uses_only_qualified_pooled_observations():
    days=history(old_n=28,recent_n=28)
    days=tuple(replace(d,sales=QuantityEvidence(EvidenceState.UNAVAILABLE,None,'fixture','MISSING_RECENT')) if d.day>=CUTOFF-timedelta(days=28) else d for d in days)
    result=run(row(history=days))
    assert result.forecast.branch=='POOLED_CONFIRMED'
    assert result.forecast.weekly_units==5
    assert result.forecast.recent.available_days==0
    assert result.forecast.recent.unavailable_sales_days==28


def test_incoming_conflict_stays_visible_even_with_unknown_on_hand():
    source=row(on_hand=QuantityEvidence(EvidenceState.UNKNOWN,None,'fixture','MISSING_COUNT'),
        supply=Supply((SupplyLot('PO129','VENDOR_OPEN',QuantityEvidence(EvidenceState.UNKNOWN,None,'fixture','CONFLICTING_INCOMING_ALLOCATION'),False),),(),(),True,'snapshot'))
    result=run(source)
    assert 'MISSING_COUNT' in result.reasons
    assert 'CONFLICTING_INCOMING_ALLOCATION' in result.reasons
    assert result.inventory_position is None and result.raw_need_units is None
