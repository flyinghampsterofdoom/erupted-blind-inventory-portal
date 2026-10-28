"""One pure candidate pipeline: evidence -> demand -> policy -> units -> packs.

No database, HTTP, clock, configuration discovery, purchasing/session writers,
randomness, cron or automatic policy conversion. All inputs are explicit.
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from fractions import Fraction
from zoneinfo import ZoneInfo
from .contracts import Availability, EvidenceState, ExposureDay, QuantityEvidence, final_pack_conversion
from .calculation_config import Configuration, Coverage, Parameters, parameter_document


ALGORITHM_VERSION = 'v4-disjoint-adaptive-candidate-1'
BUSINESS_ZONE = ZoneInfo('America/Los_Angeles')


@dataclass(frozen=True)
class SupplyLot:
    identity: str
    stage: str
    quantity: QuantityEvidence
    represented_in_on_hand: bool

    def __post_init__(self):
        if not isinstance(self.identity,str) or not isinstance(self.quantity,QuantityEvidence) or not self.identity or self.stage not in ('VENDOR_OPEN','AT_CENTRAL','IN_TRANSIT','ARRIVAL_PENDING'):
            raise ValueError('Explicit supply identity and supported physical stage required')
        if type(self.represented_in_on_hand) is not bool:
            raise ValueError('Explicit stock-representation evidence required')


@dataclass(frozen=True)
class Deduction:
    identity: str
    quantity: QuantityEvidence
    represented_in_on_hand: bool

    def __post_init__(self):
        if not isinstance(self.identity,str) or not self.identity or not isinstance(self.quantity,QuantityEvidence) or type(self.represented_in_on_hand) is not bool:
            raise ValueError('Explicit immutable deduction evidence required')


@dataclass(frozen=True)
class Supply:
    incoming: tuple[SupplyLot, ...]
    outward_reserved: tuple[Deduction, ...]
    unavailable_holds: tuple[Deduction, ...]
    coherent_with_on_hand: bool
    watermark: str
    coherence_reasons: tuple[str, ...] = ()

    def __post_init__(self):
        if any(not isinstance(x,tuple) for x in (self.incoming,self.outward_reserved,self.unavailable_holds)):
            raise ValueError('Immutable supply collections required')
        if not isinstance(self.coherence_reasons,tuple) or any(not isinstance(r,str) for r in self.coherence_reasons):
            raise ValueError('Immutable stock/supply coherence reasons required')
        if (any(not isinstance(x,SupplyLot) for x in self.incoming) or
                any(not isinstance(x,Deduction) for x in self.outward_reserved+self.unavailable_holds) or
                type(self.coherent_with_on_hand) is not bool):
            raise ValueError('Explicit coherence evidence required')


@dataclass(frozen=True)
class ManualRule:
    identity: str
    par_units: int
    level_units: int

    def __post_init__(self):
        if not self.identity or any(type(n) is not int or n < 0 for n in (self.par_units,self.level_units)):
            raise ValueError('Manual PAR/LEVEL are nonnegative individual-unit integers')


@dataclass(frozen=True)
class ProductCoverageOverride:
    coverage: Coverage
    authority_reference: str

    def __post_init__(self):
        if not isinstance(self.coverage,Coverage) or not self.authority_reference:
            raise ValueError('Explicit product coverage policy with authority provenance required')


@dataclass(frozen=True)
class StoreInput:
    variation_id: str
    store_id: str
    category_id: str | None
    history: tuple[ExposureDay, ...]
    on_hand: QuantityEvidence
    supply: Supply
    pack_size: int | None
    no_reorder: bool
    manual: ManualRule | None
    product_coverage: ProductCoverageOverride | None
    eligible_from: date | None
    identity_issues: tuple[str, ...]
    policy_issues: tuple[str, ...] = ()

    def __post_init__(self):
        if not self.variation_id or not self.store_id or not isinstance(self.history,tuple):
            raise ValueError('Immutable product/store evidence required')
        if (type(self.no_reorder) is not bool or not isinstance(self.identity_issues,tuple)
                or not isinstance(self.policy_issues,tuple)):
            raise ValueError('Explicit global eligibility and immutable issues required')
        if self.eligible_from is not None and type(self.eligible_from) is not date:
            raise ValueError('Listing boundary must be a business date or unknown')
        if any(not isinstance(r,ExposureDay) or type(r.day) is not date or not isinstance(r.availability,Availability) or not isinstance(r.sales,QuantityEvidence) for r in self.history):
            raise ValueError('Validated dated sales and availability evidence required')
        if not isinstance(self.on_hand,QuantityEvidence) or not isinstance(self.supply,Supply):
            raise ValueError('Validated inventory and supply evidence required')
        if len({r.day for r in self.history}) != len(self.history):
            raise ValueError('Duplicate product-store selling dates')


@dataclass(frozen=True)
class WindowStats:
    start: date
    end: date
    eligible_days: int
    available_days: int
    stockout_days: int
    unknown_days: int
    unavailable_sales_days: int
    observed_units: Fraction
    eligible_units: Fraction
    velocity: Fraction | None
    reliability_index: Fraction
    qualified: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class Forecast:
    state: str
    weekly_units: Fraction | None
    branch: str
    historical: WindowStats | None
    recent: WindowStats | None
    pooled: WindowStats | None
    direction: str | None
    trend_magnitude: Fraction | None
    raw_recent_weight: Fraction | None
    reliable_recent_weight: Fraction | None
    final_recent_weight: Fraction | None
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class Recommendation:
    variation_id: str
    store_id: str
    state: str
    policy_mode: str
    policy_source: str
    configuration_version: str
    parameters: tuple[tuple[str,str], ...]
    parameter_sources: tuple[tuple[str,str], ...]
    forecast: Forecast
    inventory_position: Fraction | None
    reorder_units: Fraction | None
    target_units: Fraction | None
    raw_need_units: Fraction | None
    suggested_packs: int
    suggested_actual_units: int | None
    pack_size: int | None
    reasons: tuple[str, ...]
    informational_message: str | None
    manual_addition_allowed: bool
    category_id: str | None
    observed_on_hand: QuantityEvidence
    supply_evidence: Supply
    policy_inputs: tuple[tuple[str,str], ...]


@dataclass(frozen=True)
class BatchResult:
    algorithm_version: str
    configuration_version: str
    configuration_digest: str
    as_of: datetime
    input_digest: str
    recommendations: tuple[Recommendation, ...]
    source_exceptions: tuple[str, ...]


def _window(days, start, end, parameters, as_of):
    m=parameters.model.windows.minimum_available_days
    available=stockout=unknown=unavailable=0
    observed=eligible=Fraction()
    reasons=set()
    day=start
    while day < end:
        row=days.get(day)
        if row is None:
            unknown+=1;reasons.add('MISSING_DAY_EVIDENCE')
        else:
            known=(row.sales.state==EvidenceState.KNOWN and row.sales.value >= 0 and
                   (row.sales.observed_at is None or row.sales.observed_at <= as_of))
            if known:
                observed+=Fraction(row.sales.value)
            if row.sales.state==EvidenceState.UNAVAILABLE:
                unavailable+=1
            status=row.availability
            if row.availability_reason:reasons.add('AVAILABILITY:'+row.availability_reason)
            if status==Availability.STOCKOUT and known and row.sales.value > 0:
                status=Availability.UNKNOWN;reasons.add('CONTRADICTORY_STOCKOUT_SALES')
            if not known:
                if row.sales.state==EvidenceState.KNOWN and row.sales.value < 0:
                    reasons.add('INVALID_GROSS_SALES')
                elif row.sales.observed_at is not None and row.sales.observed_at > as_of:
                    reasons.add('SALES_OBSERVATION_AFTER_CUTOFF')
                else:
                    reasons.add('SALES_EVIDENCE_'+row.sales.state.value)
            if status==Availability.AVAILABLE and known:
                available+=1;eligible+=Fraction(row.sales.value)
            elif status==Availability.STOCKOUT:
                stockout+=1
            else:
                unknown+=1;reasons.add('UNKNOWN_AVAILABILITY_OR_SALES')
        day+=timedelta(days=1)
    duration=max((end-start).days,0)
    qualified=available>=m
    if not qualified:reasons.add('INSUFFICIENT_CONFIRMED_AVAILABLE_DAYS')
    control=parameters.stockout
    if duration and control.kind=='minimum_available_share' and Fraction(available,duration)<control.threshold:
        qualified=False;reasons.add('MINIMUM_AVAILABLE_SHARE_GATE')
    if duration and control.kind=='maximum_unknown_share' and Fraction(unknown,duration)>control.threshold:
        qualified=False;reasons.add('MAXIMUM_UNKNOWN_SHARE_GATE')
    reliability=(min(Fraction(1),Fraction(available)/(parameters.reliability_depth_multiple*m))
                 *Fraction(available,duration)) if duration else Fraction()
    return WindowStats(start,end,duration,available,stockout,unknown,unavailable,observed,eligible,
                       eligible*7/available if available else None,reliability,qualified,tuple(sorted(reasons)))


def estimate(row,parameters,*,as_of):
    if as_of.utcoffset() is None:raise ValueError('Aware evidence cutoff required')
    cutoff=as_of.astimezone(BUSINESS_ZONE).date()
    old_bounds,recent_bounds=parameters.model.windows.split(cutoff)
    start=max(old_bounds[0],row.eligible_from) if row.eligible_from else old_bounds[0]
    # Dates after the generation cutoff never leak into training.
    days={r.day:r for r in row.history if start <= r.day < cutoff}
    historical=_window(days,start,max(start,old_bounds[1]),parameters,as_of)
    recent_start=max(start,recent_bounds[0])
    recent=_window(days,recent_start,max(recent_start,cutoff),parameters,as_of)
    pooled=_window(days,start,max(start,cutoff),parameters,as_of)
    reasons=set()
    if row.eligible_from is None:reasons.add('LISTING_DATE_UNKNOWN')
    if any(r.day>=cutoff for r in row.history):reasons.add('FUTURE_SELLING_DAYS_EXCLUDED')
    if not historical.qualified or not recent.qualified:
        if pooled.qualified:
            reasons.add('LIMITED_TREND_HISTORY')
            return Forecast('ESTIMATED_ZERO' if pooled.velocity==0 else 'ESTIMATED',pooled.velocity,
                'POOLED_CONFIRMED',historical,recent,pooled,None,None,None,None,None,tuple(sorted(reasons)))
        reasons.update(pooled.reasons)
        return Forecast('ABSTAIN',None,'INSUFFICIENT_EVIDENCE',historical,recent,pooled,
                        None,None,None,None,None,tuple(sorted(reasons)))
    h,j=historical.velocity,recent.velocity
    direction='INCREASING' if j>h else 'DECREASING' if j<h else 'STABLE'
    magnitude=abs(j-h)/(j+h) if j+h else Fraction()
    model=parameters.model
    sensitivity=(model.increasing_sensitivity if direction=='INCREASING' else
                 model.decreasing_sensitivity if direction=='DECREASING' else Fraction())
    raw=min(Fraction(1),model.recent_influence+sensitivity*magnitude)
    denominator=raw*recent.reliability_index+(1-raw)*historical.reliability_index
    if not denominator:
        reasons.add('DEGENERATE_WEIGHT_POOLED_FALLBACK')
        return Forecast('ESTIMATED_ZERO' if pooled.velocity==0 else 'ESTIMATED',pooled.velocity,
            'POOLED_CONFIRMED',historical,recent,pooled,None,None,raw,None,None,tuple(sorted(reasons)))
    reliable=raw*recent.reliability_index/denominator
    cap=model.increasing_limit if direction=='INCREASING' else model.decreasing_limit
    weight=reliable if direction=='STABLE' else min(reliable,cap)
    value=(1-weight)*h+weight*j
    return Forecast('ESTIMATED_ZERO' if value==0 else 'ESTIMATED',value,'DISJOINT_ADAPTIVE',
        historical,recent,pooled,direction,magnitude,raw,reliable,weight,tuple(sorted(reasons)))


def inventory_position(row,parameters,*,as_of):
    reasons=set()
    if row.on_hand.state!=EvidenceState.KNOWN:
        return None,('ON_HAND_'+row.on_hand.state.value,row.on_hand.reason)
    if row.on_hand.observed_at is not None and row.on_hand.observed_at>as_of:
        return None,('INVENTORY_OBSERVATION_AFTER_CUTOFF',)
    supply=row.supply
    if not supply.coherent_with_on_hand or not supply.watermark:
        return None,('UNQUALIFIED_SUPPLY_STOCK_WATERMARK',)+supply.coherence_reasons
    identities=[lot.identity for lot in supply.incoming+supply.outward_reserved+supply.unavailable_holds]
    if len(identities)!=len(set(identities)) or any(not i for i in identities):
        return None,('DUPLICATE_OR_OVERLAPPING_SUPPLY_IDENTITY',)
    hand=Fraction(row.on_hand.value)
    if hand<0:
        reasons.add('NEGATIVE_OBSERVED_ON_HAND')
        if parameters.negative_inventory=='abstain_on_negative':
            return None,tuple(sorted(reasons|{'NEGATIVE_INVENTORY_CANDIDATE_GATE'}))
    if hand.denominator!=1:reasons.add('FRACTIONAL_OBSERVED_ON_HAND')
    incoming=deductions=Fraction()
    for group,sign in ((supply.incoming,1),(supply.outward_reserved,-1),(supply.unavailable_holds,-1)):
        for lot in group:
            if lot.quantity.state!=EvidenceState.KNOWN:
                return None,tuple(sorted(reasons|{'SUPPLY_'+lot.quantity.state.value,lot.quantity.reason}))
            if lot.quantity.observed_at is not None and lot.quantity.observed_at>as_of:
                return None,('SUPPLY_OBSERVATION_AFTER_CUTOFF',)
            value=Fraction(lot.quantity.value)
            if value<0 or value.denominator!=1:
                return None,('INVALID_SUPPLY_QUANTITY',)
            if sign==1:
                if lot.stage=='ARRIVAL_PENDING':return None,('ARRIVAL_PENDING_RECONCILIATION',)
                if lot.represented_in_on_hand:return None,('INCOMING_ALREADY_REPRESENTED_IN_STOCK',)
                incoming+=value
            else:
                if not lot.represented_in_on_hand:return None,('DEDUCTION_NOT_REPRESENTED_IN_STOCK',)
                deductions+=value
    if deductions>max(hand,Fraction()) and deductions:
        return None,('DEDUCTIONS_EXCEED_REPRESENTED_ON_HAND',)
    return hand-deductions+incoming,tuple(sorted(reasons))


def _skipped(state):
    return Forecast(state,None,state,None,None,None,None,None,None,None,None,())


def _recommend(row,configuration,as_of,resolved):
    parameters,coverage,sources,coverage_source=resolved
    params=tuple((key,str(value)) for key,value in parameter_document(parameters).items())
    reasons=set(row.identity_issues)
    if row.no_reorder:
        return Recommendation(row.variation_id,row.store_id,'EXCLUDED_NO_REORDER','NO_REORDER','PRODUCT',
            configuration.version,params,sources,_skipped('SKIPPED_NO_REORDER'),None,None,None,
            Fraction(),0,0,row.pack_size,tuple(sorted(reasons|{'GLOBAL_NO_REORDER'})),None,True,
            row.category_id,row.on_hand,row.supply,(('global_no_reorder','true'),))
    # Preserve every unresolved supply reason even when on-hand already blocks IP.
    for lot in row.supply.incoming+row.supply.outward_reserved+row.supply.unavailable_holds:
        if lot.quantity.state!=EvidenceState.KNOWN:
            reasons.add('SUPPLY_'+lot.quantity.state.value)
            if lot.quantity.reason:reasons.add(lot.quantity.reason)
    reasons.update(row.supply.coherence_reasons)
    position,stock_reasons=inventory_position(row,parameters,as_of=as_of)
    reasons.update(stock_reasons)
    blocking_policy=tuple(code for code in row.policy_issues if not (row.manual is not None and code=='UNRESOLVED_CATEGORY_OVERRIDE_SCOPE'))
    if blocking_policy:
        reasons.update(blocking_policy)
        forecast=_skipped('SKIPPED_UNRESOLVED_POLICY')
        mode=policy_source='UNRESOLVED_POLICY'
        threshold=target=need=None
        policy_inputs=()
    elif row.manual is not None:
        forecast=_skipped('SKIPPED_MANUAL')
        mode,policy_source='MANUAL_UNITS',row.manual.identity
        threshold,target=Fraction(row.manual.par_units),Fraction(row.manual.level_units)
        policy_inputs=(('par_units',str(row.manual.par_units)),('level_units',str(row.manual.level_units)))
        if row.manual.level_units<=row.manual.par_units:
            reasons.add('UNRESOLVED_MANUAL_POLICY_BOUNDARY')
            need=None
        else:
            need=None if position is None else max(target-position,Fraction()) if position<=threshold else Fraction()
    else:
        forecast=estimate(row,parameters,as_of=as_of)
        reasons.update(forecast.reasons)
        mode='AUTOMATIC_WEEKS'
        if row.product_coverage:
            coverage=row.product_coverage.coverage
            coverage_source='PRODUCT:'+row.product_coverage.authority_reference
        policy_source=coverage_source
        policy_inputs=(('reorder_weeks',str(coverage.reorder_weeks)),('target_weeks',str(coverage.target_weeks)))
        threshold=forecast.weekly_units*coverage.reorder_weeks if forecast.weekly_units is not None else None
        target=forecast.weekly_units*coverage.target_weeks if forecast.weekly_units is not None else None
        need=(None if position is None or target is None else
              max(target-position,Fraction()) if position<=threshold else Fraction())
    if row.pack_size is None or type(row.pack_size) is not int or row.pack_size<1:
        reasons.add('UNKNOWN_OR_INVALID_PACK_SIZE')
        packs=0
        state='UNRESOLVED_PACK_CONFIGURATION'
        actual=None
    elif need is None:
        packs=0;actual=None;state='UNRESOLVED_EVIDENCE'
    else:
        packs=final_pack_conversion(need,row.pack_size)
        actual=packs*row.pack_size
        state='REVIEW_REQUIRED' if row.identity_issues else 'RECOMMENDED'
    prompt=('Please set a PAR and LEVEL' if row.manual is None and forecast.weekly_units is None
            and row.on_hand.state==EvidenceState.KNOWN and row.on_hand.value==0 else None)
    return Recommendation(row.variation_id,row.store_id,state,mode,policy_source,configuration.version,
        params,sources,forecast,position,threshold,target,need,packs,actual,row.pack_size,
        tuple(sorted(reasons)),prompt,True,row.category_id,row.on_hand,row.supply,policy_inputs)


def recommend_batch(rows,configuration,*,as_of,input_digest,source_exceptions=()):
    if not isinstance(rows,tuple) or not isinstance(configuration,Configuration):
        raise ValueError('Immutable store inputs and explicit configuration required')
    if as_of.utcoffset() is None:raise ValueError('Aware generation cutoff required')
    if not input_digest or not isinstance(source_exceptions,tuple):
        raise ValueError('Pinned input identity and immutable source exceptions required')
    keys=[(r.variation_id,r.store_id) for r in rows]
    if len(keys)!=len(set(keys)):raise ValueError('Duplicate product-store calculation')
    global_product={}
    for row in rows:
        policy=(row.no_reorder,row.category_id,row.pack_size,row.product_coverage)
        if row.variation_id in global_product and global_product[row.variation_id]!=policy:
            raise ValueError('Conflicting global product/supplier-pack policy across stores')
        global_product[row.variation_id]=policy
    resolved={category:configuration.resolve(category) for category in {r.category_id for r in rows}}
    results=tuple(_recommend(row,configuration,as_of,resolved[row.category_id]) for row in rows)
    return BatchResult(ALGORITHM_VERSION,configuration.version,configuration.content_digest,
                       as_of,input_digest,results,source_exceptions)


def compare_manual_history(rows,configuration,*,as_of,input_digest,owner_triggered):
    if owner_triggered is not True:raise PermissionError('Check Par/Level History is owner-triggered')
    from dataclasses import replace
    manual_rows=tuple(row for row in rows if row.manual is not None)
    manual=recommend_batch(manual_rows,configuration,as_of=as_of,input_digest=input_digest)
    automatic=recommend_batch(tuple(replace(row,manual=None) for row in manual_rows),configuration,
                              as_of=as_of,input_digest=input_digest)
    return tuple(dict(variation_id=m.variation_id,store_id=m.store_id,manual=m,automatic=a,
                      eligible_for_conversion=(a.state in ('RECOMMENDED','REVIEW_REQUIRED') and
                                               a.forecast.weekly_units is not None and not row.no_reorder),
                      selected_for_conversion=False,conversion_performed=False)
                 for row,m,a in zip(manual_rows,manual.recommendations,automatic.recommendations))
