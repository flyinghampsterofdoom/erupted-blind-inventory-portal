from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
from math import sqrt

from app.models import ParLevelSource, PurchaseOrderConfidenceState


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
    manual_par_level: int | None = None
    par_source: ParLevelSource = ParLevelSource.MANUAL
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

    suggested_reorder = _non_negative_int(avg_weekly * Decimal(params.reorder_weeks))
    suggested_stock_up = _non_negative_int(avg_weekly * Decimal(params.stock_up_weeks))
    effective_reorder = suggested_reorder
    effective_stock_up = suggested_stock_up

    if line.par_source == ParLevelSource.MANUAL and line.manual_par_level is not None:
        manual = max(int(line.manual_par_level), 0)
        # Manual par is the reorder floor and can raise the stock-up target when needed.
        effective_reorder = manual
        effective_stock_up = max(suggested_stock_up, manual)

    current_total = _non_negative_int(line.current_on_hand) + max(int(line.in_transit_qty), 0)
    raw = max(effective_stock_up - current_total, 0)
    raw = max(raw, line.min_order_qty)
    rounded = _round_up_to_pack(raw, line.unit_pack_size)

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
