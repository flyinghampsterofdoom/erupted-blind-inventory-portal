from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.services.v2_ordering_data_coordinator import OrderingDashboardData
from app.services.v2_ordering_policy_service import (
    DataFreshness,
    RecommendationActionability,
    RecommendationConfidence,
)
from app.services.v2_ordering_recommendation_service import RecommendationResult


def _number(value: Decimal | int | None, places: int = 0) -> str:
    if value is None:
        return '—'
    number = Decimal(str(value))
    if places:
        return f'{number:.{places}f}'
    if number == number.to_integral_value():
        return str(number.to_integral_value())
    return format(number.normalize(), 'f')


def _tone(value: str) -> str:
    return {
        'FRESH': 'success',
        'STALE': 'warning',
        'CRITICAL': 'danger',
        'ACTIONABLE': 'success',
        'INFORMATIONAL': 'warning',
        'BLOCKED': 'danger',
        'HIGH': 'success',
        'MEDIUM': 'warning',
        'LOW': 'danger',
    }.get(value, 'info')


@dataclass(frozen=True)
class OrderingRecommendationView:
    store_name: str
    vendor_name: str
    sku: str
    product_name: str
    freshness: str
    freshness_label: str
    freshness_tone: str
    actionability: str
    actionability_tone: str
    confidence: str
    confidence_tone: str
    quantity: str
    calculated_quantity: str
    on_hand: str
    incoming: str
    velocity: str
    days_supply: str
    warning_messages: tuple[str, ...]
    blocking_reasons: tuple[str, ...]
    confidence_reasons: tuple[str, ...]
    applied_policies: tuple[str, ...]
    source_rows: tuple[dict[str, str], ...]
    calculation_rows: tuple[tuple[str, str], ...]
    explanation_inputs: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class OrderingDashboardView:
    as_of_label: str
    rows: tuple[OrderingRecommendationView, ...]
    actionable_count: int
    informational_count: int
    blocked_count: int
    stale_count: int


def recommendation_view(row: RecommendationResult) -> OrderingRecommendationView:
    product_name = ' — '.join(value for value in (row.item_name, row.variation_name) if value)
    quantity = _number(row.displayed_quantity)
    if row.actionability == RecommendationActionability.INFORMATIONAL and row.calculated_quantity is not None:
        quantity = f'{_number(row.calculated_quantity)} (informational)'
    freshness_label = 'STALE DATA' if row.freshness == DataFreshness.STALE else row.freshness.value
    days_supply = '—'
    if row.primary.adjusted_daily_velocity > 0:
        days_supply = _number(row.sellable_on_hand / row.primary.adjusted_daily_velocity, 1)
    source_rows = tuple(
        {
            'source': source.source,
            'observed_at': source.observed_at.isoformat() if source.observed_at else 'Unavailable',
            'available': 'Yes' if source.available else 'No',
            'complete': 'Yes' if source.complete else 'No',
            'detail': source.detail,
        }
        for source in row.source_evidence
    )
    calculation_rows = (
        ('Observed units (7 / 28 / 56)', f'{_number(row.comparison_7.observed_units)} / {_number(row.primary.observed_units)} / {_number(row.comparison_56.observed_units)}'),
        ('Eligible days (7 / 28 / 56)', f'{row.comparison_7.eligible_days} / {row.primary.eligible_days} / {row.comparison_56.eligible_days}'),
        ('Stockout days (7 / 28 / 56)', f'{row.comparison_7.stockout_days} / {row.primary.stockout_days} / {row.comparison_56.stockout_days}'),
        ('Observed daily velocity', _number(row.primary.observed_daily_velocity, 4)),
        ('Adjusted daily velocity', _number(row.primary.adjusted_daily_velocity, 4)),
        ('Current on hand', _number(row.current_on_hand)),
        ('Non-sellable applied', _number(row.non_sellable_applied)),
        ('Sellable on hand', _number(row.sellable_on_hand)),
        ('Incoming V1 supply', _number(row.incoming_supply)),
        ('Suggested reorder / target', f'{_number(row.suggested_reorder_level)} / {_number(row.suggested_target_level)}'),
        ('Effective reorder / target', f'{_number(row.effective_reorder_level)} / {_number(row.effective_target_level)}'),
        ('Calculated quantity', _number(row.calculated_quantity)),
    )
    return OrderingRecommendationView(
        store_name=row.store_name,
        vendor_name=row.vendor_name,
        sku=row.sku,
        product_name=product_name or row.sku,
        freshness=row.freshness.value,
        freshness_label=freshness_label,
        freshness_tone=_tone(row.freshness.value),
        actionability=row.actionability.value,
        actionability_tone=_tone(row.actionability.value),
        confidence=row.confidence.value,
        confidence_tone=_tone(row.confidence.value),
        quantity=quantity,
        calculated_quantity=_number(row.calculated_quantity),
        on_hand=_number(row.sellable_on_hand),
        incoming=_number(row.incoming_supply),
        velocity=_number(row.primary.adjusted_daily_velocity, 4),
        days_supply=days_supply,
        warning_messages=tuple(warning.message for warning in row.warnings),
        blocking_reasons=row.blocking_reasons,
        confidence_reasons=row.confidence_reasons,
        applied_policies=row.applied_policies,
        source_rows=source_rows,
        calculation_rows=calculation_rows,
        explanation_inputs=row.explanation_inputs,
    )


def dashboard_view(data: OrderingDashboardData) -> OrderingDashboardView:
    rows = tuple(recommendation_view(row) for row in data.recommendations)
    return OrderingDashboardView(
        as_of_label=data.as_of.isoformat(),
        rows=rows,
        actionable_count=sum(row.actionability == RecommendationActionability.ACTIONABLE.value for row in rows),
        informational_count=sum(row.actionability == RecommendationActionability.INFORMATIONAL.value for row in rows),
        blocked_count=sum(row.actionability == RecommendationActionability.BLOCKED.value for row in rows),
        stale_count=sum(row.freshness == DataFreshness.STALE.value for row in rows),
    )
