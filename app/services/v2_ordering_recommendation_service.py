from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta, timezone
from decimal import Decimal, ROUND_CEILING

from app.services.v2_ordering_normalization_service import NormalizedRecommendationInput
from app.services.v2_ordering_policy_service import (
    ConfidenceEvidence,
    DataFreshness,
    DataSourceEvidence,
    FreshnessAssessment,
    RecommendationActionability,
    RecommendationConfidence,
    assess_confidence,
    assess_freshness,
)


ZERO = Decimal('0')
POLICY_VERSION = 'ordering-phase1-2026-07-25'
APPLIED_POLICIES = tuple(f'P1-POL-{number:03d}' for number in range(1, 13)) + ('P1-POL-016',)


@dataclass(frozen=True)
class WindowMetrics:
    days: int
    observed_units: Decimal
    eligible_days: int
    stockout_days: int
    observed_daily_velocity: Decimal
    adjusted_daily_velocity: Decimal
    adjusted_units: Decimal


@dataclass(frozen=True)
class RecommendationWarning:
    code: str
    message: str


@dataclass(frozen=True)
class RecommendationResult:
    store_id: int
    store_name: str
    vendor_id: int
    vendor_name: str
    sku: str
    variation_id: str
    item_name: str
    variation_name: str
    lifecycle_status: str
    policy_version: str
    applied_policies: tuple[str, ...]
    freshness: DataFreshness
    actionability: RecommendationActionability
    confidence: RecommendationConfidence
    confidence_reasons: tuple[str, ...]
    source_evidence: tuple[DataSourceEvidence, ...]
    primary: WindowMetrics
    comparison_7: WindowMetrics
    comparison_56: WindowMetrics
    current_on_hand: Decimal
    non_sellable_applied: Decimal
    sellable_on_hand: Decimal
    incoming_supply: int
    incoming_purchase_order_ids: tuple[int, ...]
    suggested_reorder_level: int | None
    suggested_target_level: int | None
    effective_reorder_level: int | None
    effective_target_level: int | None
    calculated_quantity: int | None
    displayed_quantity: int | None
    warnings: tuple[RecommendationWarning, ...]
    blocking_reasons: tuple[str, ...]
    explanation_inputs: tuple[tuple[str, str], ...]


def _ceil(value: Decimal) -> int:
    if value <= 0:
        return 0
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def _utc_date(value) -> date:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc).date()
    return value.astimezone(timezone.utc).date()


def _history_available_days(line: NormalizedRecommendationInput, maximum: int) -> int:
    if line.product_created_at is None:
        return maximum
    completed_end = _utc_date(line.as_of) - timedelta(days=1)
    created = _utc_date(line.product_created_at)
    return max(0, min(maximum, (completed_end - created).days + 1))


def _window_metrics(line: NormalizedRecommendationInput, days: int) -> WindowMetrics:
    end_day = _utc_date(line.as_of) - timedelta(days=1)
    start_day = end_day - timedelta(days=days - 1)
    sales = {row.day: row.quantity for row in line.daily_sales}
    deltas = {row.day: row.quantity for row in line.daily_inventory_deltas}
    available_days = _history_available_days(line, days)
    first_available = end_day - timedelta(days=max(available_days - 1, 0)) if available_days else end_day + timedelta(days=1)

    quantity = line.current_on_hand
    stockout_days = 0
    observed = ZERO
    counted_days = 0
    for offset in range(days):
        day = end_day - timedelta(days=offset)
        if day < start_day:
            break
        if day >= first_available:
            counted_days += 1
            observed += sales.get(day, ZERO)
            if quantity <= 0:
                stockout_days += 1
        quantity -= deltas.get(day, ZERO)

    eligible_days = max(counted_days - stockout_days, 0)
    observed_velocity = observed / Decimal(max(counted_days, 1))
    adjusted_velocity = observed / Decimal(eligible_days) if eligible_days else ZERO
    return WindowMetrics(
        days=days,
        observed_units=observed.quantize(Decimal('0.001')),
        eligible_days=eligible_days,
        stockout_days=stockout_days,
        observed_daily_velocity=observed_velocity.quantize(Decimal('0.0001')),
        adjusted_daily_velocity=adjusted_velocity.quantize(Decimal('0.0001')),
        adjusted_units=(adjusted_velocity * Decimal(days)).quantize(Decimal('0.001')),
    )


def _warning(code: str, message: str) -> RecommendationWarning:
    return RecommendationWarning(code, message)


def calculate_recommendation(line: NormalizedRecommendationInput) -> RecommendationResult:
    freshness: FreshnessAssessment = assess_freshness(line.sources, as_of=line.as_of)
    primary = _window_metrics(line, 28)
    comparison_7 = _window_metrics(line, 7)
    comparison_56 = _window_metrics(line, 56)
    warnings: list[RecommendationWarning] = []
    blocking: list[str] = list(freshness.reason_codes if freshness.status == DataFreshness.CRITICAL else ())

    if freshness.status == DataFreshness.STALE:
        warnings.append(_warning('STALE_DATA', 'Required data is over 24 hours old; this result is informational only.'))
    if freshness.status == DataFreshness.CRITICAL:
        warnings.append(_warning('CRITICAL_DATA', 'Required data is unavailable or over 72 hours old.'))
    if primary.stockout_days:
        warnings.append(_warning('STOCKOUT_ADJUSTED', f'{primary.stockout_days} confirmed stockout days were excluded.'))

    incoming_qty = sum(row.quantity for row in line.incoming_supply)
    incoming_ids = tuple(row.purchase_order_id for row in line.incoming_supply)
    now = line.as_of if line.as_of.tzinfo is not None else line.as_of.replace(tzinfo=timezone.utc)
    aged_supply = [
        row.purchase_order_id
        for row in line.incoming_supply
        if row.ordered_at is not None
        and (now.astimezone(timezone.utc) - (
            row.ordered_at.astimezone(timezone.utc)
            if row.ordered_at.tzinfo is not None
            else row.ordered_at.replace(tzinfo=timezone.utc)
        )).days > 30
    ]
    if aged_supply:
        warnings.append(_warning('AGED_IN_TRANSIT_SUPPLY', 'Incoming supply includes a V1 order older than 30 days.'))

    sellable = max(line.current_on_hand - line.non_sellable_quantity, ZERO)
    if line.non_sellable_resolved and line.non_sellable_quantity > 0:
        warnings.append(_warning('NON_SELLABLE_SUBTRACTED', 'Fresh product-resolved non-sellable quantity was subtracted.'))
    warnings.extend(_warning(code, code.replace('_', ' ').title()) for code in line.supporting_warnings)

    available_history = _history_available_days(line, 28)
    new_product = available_history < 14
    sparse_history = primary.eligible_days < 14
    product_age_unknown = line.product_created_at is None
    zero_sales_insufficient = (
        primary.observed_units == 0
        and (
            freshness.status != DataFreshness.FRESH
            or primary.eligible_days < 14
            or new_product
            or product_age_unknown
        )
    )
    if new_product:
        warnings.append(_warning('NEW_PRODUCT', 'Fewer than 14 days of product history are available.'))
    if product_age_unknown:
        warnings.append(_warning('PRODUCT_AGE_UNAVAILABLE', 'Product age is unavailable; established-product zero demand cannot be confirmed.'))
    if zero_sales_insufficient:
        warnings.append(_warning('ZERO_SALES_INSUFFICIENT_EVIDENCE', 'Zero demand is not accepted without fresh complete in-stock evidence.'))

    null_par_inference = line.manual_level is None or line.manual_target is None
    manual_assumption = new_product and line.manual_target is not None
    if null_par_inference:
        warnings.append(_warning('NULL_PAR_DEMAND_INFERENCE', 'One or more par inputs were derived from approved demand evidence.'))
    if line.manual_locked:
        warnings.append(_warning('MANUAL_INPUT_LOCKED', 'The named manual par input is locked; it is not an exclusion.'))

    no_future_reorder = line.lifecycle_status == 'NO_FUTURE_REORDER'
    suggested_reorder: int | None = None
    suggested_target: int | None = None
    effective_reorder: int | None = None
    effective_target: int | None = None
    calculated: int | None
    if no_future_reorder:
        calculated = None
        blocking.append('NO_FUTURE_REORDER')
        warnings.append(_warning('NO_FUTURE_REORDER', 'No Future Reorder lifecycle policy blocks purchasing.'))
        if line.confirmed_discontinued:
            blocking.append('CONFIRMED_DISCONTINUED')
            warnings.append(_warning('CONFIRMED_DISCONTINUED', 'Confirmed discontinued product evidence is also present.'))
    else:
        suggested_reorder = _ceil(primary.adjusted_daily_velocity * Decimal(7 * line.reorder_weeks))
        suggested_target = _ceil(primary.adjusted_daily_velocity * Decimal(7 * line.stock_up_weeks))
        effective_reorder = suggested_reorder
        effective_target = max(suggested_target, effective_reorder)
        if line.par_is_manual:
            if line.manual_level is not None:
                effective_reorder = line.manual_level
            if line.manual_target is not None:
                effective_target = line.manual_target
            effective_target = max(effective_target, effective_reorder)
        current_total = _ceil(sellable) + incoming_qty
        if line.confirmed_discontinued:
            calculated = None
            blocking.append('CONFIRMED_DISCONTINUED')
            warnings.append(_warning('CONFIRMED_DISCONTINUED', 'Confirmed discontinued product; actionable quantity is suppressed.'))
        elif new_product and line.manual_target is None:
            calculated = None
            blocking.append('INSUFFICIENT_NEW_PRODUCT_HISTORY')
        elif zero_sales_insufficient and line.manual_target is None:
            calculated = None
            blocking.append('ZERO_SALES_INSUFFICIENT_EVIDENCE')
        elif not line.inventory_valid:
            calculated = None
            blocking.append('INVALID_INVENTORY')
        else:
            calculated = max(effective_target - current_total, 0) if current_total <= effective_reorder else 0

    actionability = freshness.actionability
    if blocking:
        actionability = RecommendationActionability.BLOCKED
    displayed = calculated if actionability != RecommendationActionability.BLOCKED else None

    incomplete_supporting = any(
        not source.available or not source.complete or source.observed_at is None for source in line.sources
    )
    confidence = assess_confidence(
        ConfidenceEvidence(
            new_product=new_product,
            sparse_history=sparse_history,
            incomplete_supporting_data=incomplete_supporting,
            invalid_inventory=not line.inventory_valid,
            manual_assumption=manual_assumption,
            reliability_warning=bool(line.supporting_warnings or aged_supply or zero_sales_insufficient or product_age_unknown),
            stale_or_critical=freshness.status != DataFreshness.FRESH,
            minor_warning=False,
            null_par_inference=null_par_inference,
            stockout_adjusted=primary.stockout_days > 0,
            limited_history=14 <= primary.eligible_days < 28,
        )
    )

    explanation = (
        ('as_of', line.as_of.isoformat()),
        ('primary_window_days', '28'),
        ('observed_units_28d', str(primary.observed_units)),
        ('eligible_days_28d', str(primary.eligible_days)),
        ('stockout_days_28d', str(primary.stockout_days)),
        ('adjusted_daily_velocity', str(primary.adjusted_daily_velocity)),
        ('reorder_weeks', str(line.reorder_weeks)),
        ('stock_up_weeks', str(line.stock_up_weeks)),
        ('sellable_on_hand', str(sellable)),
        ('incoming_supply', str(incoming_qty)),
        ('lifecycle_status', line.lifecycle_status),
        ('effective_reorder_level', '' if effective_reorder is None else str(effective_reorder)),
        ('effective_target_level', '' if effective_target is None else str(effective_target)),
        ('calculated_quantity', '' if calculated is None else str(calculated)),
    )
    return RecommendationResult(
        store_id=line.store_id,
        store_name=line.store_name,
        vendor_id=line.vendor_id,
        vendor_name=line.vendor_name,
        sku=line.sku,
        variation_id=line.variation_id,
        item_name=line.item_name,
        variation_name=line.variation_name,
        lifecycle_status=line.lifecycle_status,
        policy_version=POLICY_VERSION,
        applied_policies=APPLIED_POLICIES + (('P2-POL-001',) if no_future_reorder else ()),
        freshness=freshness.status,
        actionability=actionability,
        confidence=confidence.level,
        confidence_reasons=confidence.reason_codes,
        source_evidence=line.sources,
        primary=primary,
        comparison_7=comparison_7,
        comparison_56=comparison_56,
        current_on_hand=line.current_on_hand,
        non_sellable_applied=line.non_sellable_quantity,
        sellable_on_hand=sellable,
        incoming_supply=incoming_qty,
        incoming_purchase_order_ids=incoming_ids,
        suggested_reorder_level=suggested_reorder,
        suggested_target_level=suggested_target,
        effective_reorder_level=effective_reorder,
        effective_target_level=effective_target,
        calculated_quantity=calculated,
        displayed_quantity=displayed,
        warnings=tuple(dict.fromkeys(warnings)),
        blocking_reasons=tuple(dict.fromkeys(blocking)),
        explanation_inputs=explanation,
    )
