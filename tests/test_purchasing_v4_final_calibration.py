from dataclasses import replace
from datetime import datetime,timedelta,time
from decimal import Decimal as D
from fractions import Fraction as F
import pytest
from app.purchasing_v4.calculation import estimate,recommend_batch
from app.purchasing_v4.calculation_documents import configuration_from_document
from app.purchasing_v4.calculation_config import parameter_document
from app.purchasing_v4.contracts import Availability,EvidenceState,ExposureDay,QuantityEvidence
from app.purchasing_v4.absolute_availability import forward_days,reconcile_projection
from app.purchasing_v4.evidence_rules import BUSINESS_ZONE
from test_purchasing_v4_calculation import row,configuration,ASOF,CUTOFF


def source(**changes):
    days=tuple(ExposureDay(CUTOFF-timedelta(days=126-i),Availability.AVAILABLE if i<14 else Availability.STOCKOUT,
               QuantityEvidence(EvidenceState.KNOWN,D(1 if i<14 else 0),'fixture',observed_at=ASOF)) for i in range(126))
    return row(history=days,eligible_from=CUTOFF-timedelta(days=126),**changes)


def config():return configuration(lookback_days=126,forecast_method='calendar_average')


def test_simple_average_uses_verified_calendar_sales_and_original_history_gate():
    result=estimate(source(),config().global_parameters,as_of=ASOF)
    assert result.weekly_units==F(7,9) and result.branch=='CALENDAR_AVERAGE'
    assert result.pooled.available_days==14
    bad=replace(source(),history=tuple(replace(d,availability=Availability.STOCKOUT,sales=replace(d.sales,value=D(0))) for d in source().history))
    assert estimate(bad,config().global_parameters,as_of=ASOF).weekly_units is None


@pytest.mark.parametrize('kind',['missing_date','unknown_sales','future_sales','short_listing'])
def test_calendar_average_never_zero_fills_incomplete_or_future_sales(kind):
    record=source();history=list(record.history)
    if kind=='missing_date':history.pop()
    if kind=='unknown_sales':history[-1]=replace(history[-1],sales=QuantityEvidence(EvidenceState.UNKNOWN,None,'fixture','missing'))
    if kind=='future_sales':history[-1]=replace(history[-1],sales=replace(history[-1].sales,observed_at=ASOF+timedelta(seconds=1)))
    if kind=='short_listing':record=replace(record,eligible_from=CUTOFF-timedelta(days=20))
    result=estimate(replace(record,history=tuple(history)),config().global_parameters,as_of=ASOF)
    assert result.weekly_units is None and 'INCOMPLETE_CALENDAR_SALES' in result.reasons


def test_average_controls_remain_independent_but_do_not_silently_change_method():
    first=config();second=replace(first,global_parameters=first.global_parameters.override(dict(increasing_sensitivity=F(0),decreasing_sensitivity=F(1))))
    assert estimate(source(),first.global_parameters,as_of=ASOF).weekly_units==estimate(source(),second.global_parameters,as_of=ASOF).weekly_units
    assert second.global_parameters.model.increasing_sensitivity==0 and second.global_parameters.model.decreasing_sensitivity==1
    with pytest.raises(ValueError):first.global_parameters.override({'forecast_method':'unsupported'})


def test_legacy_configuration_and_algorithm_identity_remain_compatible():
    legacy=configuration();assert 'forecast_method' not in parameter_document(legacy.global_parameters)
    assert recommend_batch((row(),),legacy,as_of=ASOF,input_digest='fixture').algorithm_version=='v4-disjoint-adaptive-candidate-2'
    assert parameter_document(config().global_parameters)['forecast_method']=='calendar_average'
    assert recommend_batch((source(),),config(),as_of=ASOF,input_digest='fixture').algorithm_version=='v4-calendar-average-2'


def test_manual_authority_and_unknown_inventory_are_preserved():
    from app.purchasing_v4.calculation import ManualRule
    manual=replace(source(),manual=ManualRule('fixture-owner',5,20))
    result=recommend_batch((manual,),config(),as_of=ASOF,input_digest='fixture').recommendations[0]
    assert result.policy_mode=='MANUAL_UNITS' and result.suggested_actual_units==20
    unknown=replace(source(),on_hand=QuantityEvidence(EvidenceState.UNKNOWN,None,'fixture','missing'))
    result=recommend_batch((unknown,),config(),as_of=ASOF,input_digest='fixture').recommendations[0]
    assert result.raw_need_units is None and result.suggested_actual_units is None


def event(day,hour,physical=False,value='1',id='e'):
    at=datetime.combine(CUTOFF-timedelta(days=4-day),time(),BUSINESS_ZONE)+timedelta(hours=hour)
    return dict(time=at,id=id,physical=physical,absolute=D(value) if physical else None,delta=D(0) if physical else D(value))


def forward(events):
    return forward_days(events,start_day=CUTOFF-timedelta(days=4),end_day=CUTOFF,
                        anchor=datetime.combine(CUTOFF,time(12),BUSINESS_ZONE),zone=BUSINESS_ZONE)


def test_absolute_counts_cannot_be_guessed_or_borrowed_from_future():
    days,balance=forward([event(0,3,False,'10')]);assert balance is None and all(d['state']==-1 for d in days.values())
    days,balance=forward([event(5,3,True,'10')]);assert balance is None


def test_reset_day_mixed_negative_and_ambiguous_intervals_stay_unknown():
    days,balance=forward([event(0,3,True,'4'),event(2,3,False,'-4',id='sale')]);values=list(days.values())
    assert [r['state'] for r in values]==[-1,1,-1,0]
    days,balance=forward([event(0,3,True,'1'),event(1,3,False,'-2',id='a'),event(1,4,False,'2',id='b')]);assert list(days.values())[1]['state']==-1
    days,balance=forward([event(0,3,True,'4'),event(1,3,True,'4',id='p'),event(1,3,False,'2',id='a')]);assert balance is None


def test_source_reconciliation_needs_exact_current_anchor_and_matching_payload():
    day=CUTOFF-timedelta(days=4);anchor=datetime.combine(CUTOFF,time(12),BUSINESS_ZONE)
    when=datetime.combine(day,time(3),BUSINESS_ZONE);hand=QuantityEvidence(EvidenceState.KNOWN,D(4),'fixture',observed_at=anchor,references=('count:fixture',))
    base=tuple((day+timedelta(days=i),Availability.UNKNOWN,EvidenceState.UNKNOWN,'unproved',()) for i in range(4))
    sales={d:QuantityEvidence(EvidenceState.KNOWN,D(0),'fixture',observed_at=anchor) for d,*_ in base}
    physical=dict(event_type='PHYSICAL_COUNT',event_id='p',location_id='loc',variation_id='var',occurred_at=when,quantity_delta=D(0),physical_count=True,source_updated_at=when,synchronized_at=anchor,calculated_before_at=anchor,source_digest='hash')
    # The linked inferred adjustment represents the same reset and must not be applied twice.
    adjustment=dict(physical,event_type='ADJUSTMENT',event_id='generated',physical_count=False,quantity_delta=D(4),source_digest='adjustment')
    counts={'hash':dict(id='p',location_id='loc',catalog_object_id='var',state='IN_STOCK',quantity='4',adjustment_id='generated',occurred_at=when.isoformat(),created_at=when.isoformat())}
    args=dict(absolute_counts=counts,events=[physical,adjustment],inventory=hand,anchor_row={'started_at':anchor-timedelta(seconds=1)},coverage=(EvidenceState.KNOWN,'',anchor),sales=sales,start_day=day,end_day=CUTOFF,as_of=anchor,zone=BUSINESS_ZONE)
    result=reconcile_projection(base,**args);assert [r[1] for r in result]==[Availability.UNKNOWN]+[Availability.AVAILABLE]*3
    assert all(r[4] for r in result[1:])
    assert reconcile_projection(base,**dict(args,inventory=replace(hand,value=D(5))))==base
    assert reconcile_projection(base,**dict(args,inventory=QuantityEvidence(EvidenceState.UNKNOWN,None,'fixture','missing',anchor)))==base
    assert reconcile_projection(base,**dict(args,absolute_counts={}))==base
    future=dict(physical,synchronized_at=anchor+timedelta(seconds=1));assert reconcile_projection(base,**dict(args,events=[future]))==base
    conflict=tuple((d,Availability.STOCKOUT,EvidenceState.KNOWN,'',()) for d,*_ in base)
    assert all(r[1]==Availability.UNKNOWN for r in reconcile_projection(conflict,**args))


def test_final_package_is_implementable_but_cannot_self_approve_or_activate():
    from app.purchasing_v4.operating_profile import load_operating_package
    package=load_operating_package();config=configuration_from_document(dict(package['global_control'],id=package['version']))
    assert config.global_parameters.forecast_method=='calendar_average'
    assert config.global_parameters.model.increasing_sensitivity==config.global_parameters.model.decreasing_sensitivity==0
    assert config.global_coverage.reorder_weeks==3 and config.global_coverage.target_weeks==6
    assert package['global_control']['status']=='DRAFT'
    assert package['owner_activation_required'] and not package['automatic_purchasing_enabled']
    assert not package['supplier_commitments_enabled'] and not package['inventory_posting_enabled'] and not package['email_enabled']


@pytest.mark.parametrize('reason',['UNKNOWN_OR_INVALID_EXPECTED_COST:1308','OWNER_OFFER_EXPECTED_COST_UNKNOWN','UNRESOLVED_PRIMARY_SUPPLIER_EVIDENCE'])
def test_unresolved_supplier_evidence_cannot_emit_order_quantities(reason):
    record=replace(source(),identity_issues=(reason,))
    result=recommend_batch((record,),config(),as_of=ASOF,input_digest='fixture').recommendations[0]
    assert result.forecast.weekly_units is not None
    assert result.raw_need_units is None and result.suggested_actual_units is None
    assert 'SUPPLIER_EVIDENCE_EXCLUDED' in result.reasons
