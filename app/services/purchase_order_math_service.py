from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
from math import sqrt

from app.models import ParLevelSource, PurchaseOrderConfidenceState
from app.services.purchasing_inventory_service import inventory_position


@dataclass(frozen=True)
class OrderingMathParams:
    reorder_weeks: int = 5
    stock_up_weeks: int = 10
    history_lookback_days: int = 120


@dataclass(frozen=True)
class MathOverrides:
    reorder_weeks: int | None = None
    stock_up_weeks: int | None = None
    history_lookback_days: int | None = None


@dataclass(frozen=True)
class LineMathInput:
    sku: str
    current_on_hand: Decimal
    in_transit_qty: int
    history_daily_units: list[Decimal]
    unit_pack_size: int = 1
    min_order_qty: int = 0
    manual_level: int | None = None
    manual_par: int | None = None
    par_source: ParLevelSource = ParLevelSource.DYNAMIC
    confidence_threshold: Decimal = Decimal('0.80')


@dataclass(frozen=True)
class LineMathResult:
    sku: str
    avg_weekly_units: Decimal
    suggested_reorder_level: int
    suggested_stock_up_level: int
    effective_reorder_level: int
    effective_stock_up_level: int
    raw_recommended_qty: int
    rounded_recommended_qty: int
    confidence_score: Decimal
    confidence_state: PurchaseOrderConfidenceState
    par_source: ParLevelSource


def _validate_params(params: OrderingMathParams) -> None:
    if params.reorder_weeks <= 0:
        raise ValueError('Reorder weeks must be greater than zero')
    if params.stock_up_weeks <= params.reorder_weeks:
        raise ValueError('Stock-up weeks must be greater than reorder weeks')
    if params.history_lookback_days < 7 or params.history_lookback_days > 730:
        raise ValueError('History lookback days must be between 7 and 730')


def resolve_math_params(defaults: OrderingMathParams, overrides: MathOverrides | None = None) -> OrderingMathParams:
    if overrides is None:
        _validate_params(defaults)
        return defaults

    resolved = OrderingMathParams(
        reorder_weeks=overrides.reorder_weeks if overrides.reorder_weeks is not None else defaults.reorder_weeks,
        stock_up_weeks=overrides.stock_up_weeks if overrides.stock_up_weeks is not None else defaults.stock_up_weeks,
        history_lookback_days=(
            overrides.history_lookback_days
            if overrides.history_lookback_days is not None
            else defaults.history_lookback_days
        ),
    )
    _validate_params(resolved)
    return resolved


def _round_up_to_pack(qty: int, pack_size: int) -> int:
    if qty <= 0:
        return 0
    if pack_size <= 1:
        return qty
    units = (Decimal(qty) / Decimal(pack_size)).to_integral_value(rounding=ROUND_CEILING)
    return int(units * pack_size)


def _non_negative_int(value: Decimal | int) -> int:
    if isinstance(value, Decimal):
        if value <= 0:
            return 0
        return int(value.to_integral_value(rounding=ROUND_CEILING))
    return max(int(value), 0)


def _compute_confidence(history: list[Decimal], lookback_days: int) -> Decimal:
    if not history:
        return Decimal('0.00')

    capped = history[-lookback_days:]
    n = len(capped)
    if n == 0:
        return Decimal('0.00')

    total = sum(capped, Decimal('0'))
    mean = total / Decimal(n)
    activity = Decimal('1.00') if total > 0 else Decimal('0.20')
    sufficiency = min(Decimal(n) / Decimal(lookback_days), Decimal('1.00'))

    if mean <= 0:
        stability = Decimal('0.00')
    else:
        variance = sum((point - mean) ** 2 for point in capped) / Decimal(n)
        stddev = Decimal(str(sqrt(float(variance))))
        cv = stddev / mean if mean > 0 else Decimal('2.00')
        stability = max(Decimal('0.00'), Decimal('1.00') - min(cv / Decimal('2.00'), Decimal('1.00')))

    # Weighted toward enough observations while still rewarding signal quality.
    score = (sufficiency * Decimal('0.50')) + (stability * Decimal('0.30')) + (activity * Decimal('0.20'))
    return score.quantize(Decimal('0.0001'))


def compute_line_recommendation(line: LineMathInput, params: OrderingMathParams) -> LineMathResult:
    _validate_params(params)
    if line.unit_pack_size < 1:
        raise ValueError('Pack size must be at least 1')
    if line.min_order_qty < 0:
        raise ValueError('Min order quantity cannot be negative')

    trimmed = line.history_daily_units[-params.history_lookback_days :]
    # Use full lookback window so sparse sales do not over-inflate demand.
    total_units = sum(trimmed, Decimal('0'))
    days = max(len(trimmed), 1)
    avg_daily = total_units / Decimal(days)
    avg_weekly = (avg_daily * Decimal('7')).quantize(Decimal('0.0001'))

    forecast = forecast_demand(DemandEvidence(tuple(trimmed),line.current_on_hand,line.in_transit_qty,
        reorder_weeks=params.reorder_weeks,target_weeks=params.stock_up_weeks,multiple=line.unit_pack_size,
        manual_reorder=line.manual_level if line.par_source==ParLevelSource.MANUAL else None,
        manual_target=line.manual_par if line.par_source==ParLevelSource.MANUAL else None))
    raw, rounded = forecast.raw_need, forecast.quantity
    suggested_reorder=forecast.suggested_reorder or 0
    suggested_stock_up=forecast.suggested_target or 0
    effective_reorder=forecast.reorder_level or 0; effective_stock_up=forecast.target_level or 0

    confidence_score = _compute_confidence(trimmed, params.history_lookback_days)
    confidence_state = (
        PurchaseOrderConfidenceState.NORMAL
        if confidence_score >= line.confidence_threshold
        else PurchaseOrderConfidenceState.LOW
    )

    return LineMathResult(
        sku=line.sku,
        avg_weekly_units=avg_weekly,
        suggested_reorder_level=suggested_reorder,
        suggested_stock_up_level=suggested_stock_up,
        effective_reorder_level=effective_reorder,
        effective_stock_up_level=effective_stock_up,
        raw_recommended_qty=raw,
        rounded_recommended_qty=rounded,
        confidence_score=confidence_score,
        confidence_state=confidence_state,
        par_source=line.par_source,
    )


# One authoritative Purchasing V3 engine. Legacy entry points below adapt their
# evidence to this contract; reports may compute descriptive metrics separately.
ENGINE_VERSION = 'purchasing-v3-p2-20261017'


@dataclass(frozen=True)
class DemandEvidence:
    sales: tuple[Decimal, ...]
    current_inventory: Decimal | None
    incoming: int = 0
    sales_complete: bool = True
    availability: tuple[bool | None, ...] = ()
    lifecycle: str = 'ACTIVE'
    product_age_days: int | None = None
    lead_time_days: int | None = None
    reliable_stock_age_days: int | None = None
    reorder_weeks: Decimal = Decimal(5)
    target_weeks: Decimal = Decimal(10)
    multiple: int = 1
    manual_reorder: int | None = None
    manual_target: int | None = None


@dataclass(frozen=True)
class DemandForecast:
    daily_velocity: Decimal | None
    quantity: int | None
    raw_need: int | None
    inventory_position: Decimal | None
    reorder_level: int | None
    target_level: int | None
    trend: str
    confidence: str
    reasons: tuple[str, ...]
    spike_days: int = 0
    stockout_days: int = 0
    evidence_days: int = 0
    version: str = ENGINE_VERSION
    suggested_reorder: int | None = None
    suggested_target: int | None = None


def purchase_multiple(quantity: int, multiple: int = 1) -> int:
    if type(multiple) is not int or not 1 <= multiple <= 10000:
        raise ValueError('Purchasing multiple must be a whole number between 1 and 10000')
    if type(quantity) is not int or not 0 <= quantity <= 1000000:
        raise ValueError('Purchasing quantity must be a whole number between 0 and 1000000')
    return ((quantity + multiple - 1) // multiple) * multiple if quantity else 0


def forecast_demand(evidence: DemandEvidence) -> DemandForecast:
    """Robust completed-day velocity, sustained trends, bounded stockout uplift.

    No inventory is inferred from receipts, financing or historical stock deltas.
    Availability is optional historical *demand* evidence, never current stock.
    """
    from statistics import median
    from app.services.purchasing_evidence_service import quantity
    reorder, target = Decimal(str(evidence.reorder_weeks)), Decimal(str(evidence.target_weeks))
    if not reorder.is_finite() or not target.is_finite() or not 0 < reorder < target <= 52:
        raise ValueError('Choose 0 < reorder weeks < target weeks <= 52')
    purchase_multiple(0, evidence.multiple)
    position = inventory_position(evidence.current_inventory, evidence.incoming)
    reasons = []
    if position is None: reasons.append('INVENTORY_OR_INCOMING_UNKNOWN')
    if not evidence.sales_complete: reasons.append('SALES_COVERAGE_INCOMPLETE')
    if evidence.lifecycle != 'ACTIVE': reasons.append('LIFECYCLE_BLOCKED')
    points = [quantity(x) for x in evidence.sales]
    if any(x is None or x < 0 for x in points): reasons.append('INVALID_SALES')
    if not points: reasons.append('NO_HISTORY')
    if reasons:
        return DemandForecast(None,None,None,position,None,None,'UNKNOWN','LOW',tuple(reasons),evidence_days=len(points))
    # Never use pre-launch zeros as evidence of demand for a new variation.
    if evidence.product_age_days is not None:
        points = points[-max(0,evidence.product_age_days):] if evidence.product_age_days > 0 else []
    if not points:
        return DemandForecast(None,None,None,position,None,None,'NEW','LOW',('NO_COMPLETED_PRODUCT_HISTORY',))
    avail = list(evidence.availability[-len(points):]) if evidence.availability else [None]*len(points)
    if len(avail) != len(points):
        avail = [None]*len(points)
    positive = [x for x in points if x > 0]
    cap = max(Decimal(str(median(positive)))*4, Decimal(1)) if len(positive)>=7 and len(points)>=28 else None
    clipped = [min(x,cap) if cap is not None else x for x in points]
    spikes = sum(a!=b for a,b in zip(points,clipped))
    stockouts = sum(x is False for x in avail)
    def velocity(start, end=None):
        values=clipped[start:end]; available=avail[start:end]
        eligible=[v for v,a in zip(values,available) if a is not False]
        if not eligible: return None
        # Exclude confirmed unavailable days, bound the uplift to 2x the
        # observed completed-day velocity so sparse history cannot explode.
        observed=sum(values,Decimal(0))/len(values)
        return min(sum(eligible,Decimal(0))/len(eligible),observed*2) if stockouts else sum(values,Decimal(0))/len(values)
    if len(points)<14 and not positive and evidence.manual_target is None:
        return DemandForecast(None,None,None,position,None,None,'NEW','LOW',('NO_ESTABLISHED_DEMAND',),evidence_days=len(points))
    long=velocity(-min(56,len(points)))
    if long is None or (not positive and evidence.current_inventory <= 0 and not any(a is True for a in avail) and evidence.manual_target is None):
        return DemandForecast(None,None,None,position,None,None,'UNKNOWN','LOW',('DEMAND_CENSORED_OR_NEW',),spikes,stockouts,len(points))
    daily=long;trend='STABLE'
    if len(points)<28:
        daily=long*Decimal(len(points))/Decimal(len(points)+7)
        trend='NEW';reasons.append('CONSERVATIVE_LIMITED_HISTORY')
    elif len(points)>=42:
        previous=velocity(-42,-14);week1=velocity(-14,-7);week2=velocity(-7)
        if previous is not None and week1 is not None and week2 is not None:
            recent=(week1+week2)/2
            if previous>0 and min(week1,week2)>previous*Decimal('1.15'):
                trend='GROWING';daily=min(recent*Decimal('.65')+long*Decimal('.35'),long*Decimal('1.6'))
            elif previous>0 and max(week1,week2)<previous*Decimal('.85'):
                trend='DECLINING';daily=recent*Decimal('.75')+long*Decimal('.25')
    if len(points)>=28 and sum(points[-28:])==0 and not any(a is False for a in avail[-28:]):
        daily=Decimal(0);trend='SLOW';reasons.append('NO_RECENT_OBSERVED_SALES')
    if evidence.reliable_stock_age_days is not None and evidence.reliable_stock_age_days>=180 and trend in {'SLOW','DECLINING'}:
        daily*=Decimal('.75');reasons.append('VERIFIED_AGED_SLOW_INVENTORY')
    if spikes:reasons.append('ISOLATED_SPIKES_CAPPED')
    if stockouts:reasons.append('CONFIRMED_STOCKOUT_ADJUSTMENT')
    if evidence.lead_time_days is not None and not 0<=evidence.lead_time_days<=365:
        raise ValueError('Lead time must be between 0 and 365 days')
    reorder_days=max((reorder*7).quantize(Decimal('.000001')),Decimal(evidence.lead_time_days or 0))
    target_days=max((target*7).quantize(Decimal('.000001')),reorder_days)
    level=int((daily*reorder_days).to_integral_value(rounding=ROUND_CEILING))
    ceiling=int((daily*target_days).to_integral_value(rounding=ROUND_CEILING))
    suggested_level,suggested_ceiling=level,ceiling
    if evidence.manual_reorder is not None:level=max(0,int(evidence.manual_reorder))
    if evidence.manual_target is not None:ceiling=max(level,int(evidence.manual_target))
    need=int(max(Decimal(ceiling)-position,Decimal(0)).to_integral_value(rounding=ROUND_CEILING)) if position<=level else 0
    confidence='HIGH' if len(points)>=42 and len(positive)>=14 and stockouts<len(points)//2 else 'MEDIUM' if len(points)>=14 else 'LOW'
    return DemandForecast(daily.quantize(Decimal('.0001')),purchase_multiple(need,evidence.multiple),need,position,
        level,ceiling,trend,confidence,tuple(reasons),spikes,stockouts,len(points),
        suggested_reorder=suggested_level,suggested_target=suggested_ceiling)


def allocate_purchase(total: int, weights: dict[int, int | Decimal]) -> dict[int, int]:
    """Deterministic largest-remainder allocation; never changes a finalized PO."""
    if type(total) is not int or not 0<=total<=1000000:raise ValueError('Invalid allocation total')
    if not weights:
        if total:raise ValueError('No verified destination stores')
        return {}
    weights={int(k):max(Decimal(str(v)),Decimal(0)) for k,v in weights.items()}
    divisor=sum(weights.values(),Decimal(0))
    if divisor==0:return {key:total if key==min(weights) else 0 for key in sorted(weights)}
    exact={key:Decimal(total)*value/divisor for key,value in weights.items()}
    result={key:int(value) for key,value in exact.items()}
    for key in sorted(result,key=lambda k:(-(exact[k]-result[k]),k))[:total-sum(result.values())]:result[key]+=1
    return result


def qualified_availability(days, sales, deltas, current_inventory, current_day_delta, *, reliable):
    """Backcast only availability evidence; live inventory remains authoritative."""
    result=[None]*len(days)
    if not reliable or current_inventory is None or current_day_delta is None:return tuple(result)
    closing=current_inventory-current_day_delta
    for index in range(len(days)-1,-1,-1):
        opening=closing-deltas.get(days[index],Decimal(0))
        result[index]=False if opening<=0 and closing<=0 and sales.get(days[index],Decimal(0))==0 else True if opening>0 and closing>0 else None
        closing=opening
    return tuple(result)
