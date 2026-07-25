from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from app.services.v2_ordering_policy_service import DataSourceEvidence


ZERO = Decimal('0')


@dataclass(frozen=True)
class DailyQuantity:
    day: date
    quantity: Decimal


@dataclass(frozen=True)
class IncomingSupply:
    purchase_order_id: int
    quantity: int
    ordered_at: datetime | None


@dataclass(frozen=True)
class RawRecommendationCandidate:
    store_id: int
    store_name: str
    vendor_id: int
    vendor_name: str
    sku: str
    variation_id: str
    item_name: str
    variation_name: str
    as_of: datetime
    current_on_hand: Decimal | int | str
    inventory_valid: bool
    daily_sales: tuple[DailyQuantity, ...]
    daily_inventory_deltas: tuple[DailyQuantity, ...]
    sources: tuple[DataSourceEvidence, ...]
    reorder_weeks: int
    stock_up_weeks: int
    manual_level: int | None = None
    manual_target: int | None = None
    manual_locked: bool = False
    par_is_manual: bool = False
    incoming_supply: tuple[IncomingSupply, ...] = ()
    non_sellable_quantity: Decimal | int | str | None = None
    non_sellable_resolved: bool = False
    product_created_at: datetime | None = None
    confirmed_discontinued: bool = False
    supporting_warnings: tuple[str, ...] = ()
    lifecycle_status: str = 'ACTIVE'


@dataclass(frozen=True)
class NormalizedRecommendationInput:
    store_id: int
    store_name: str
    vendor_id: int
    vendor_name: str
    sku: str
    variation_id: str
    item_name: str
    variation_name: str
    as_of: datetime
    current_on_hand: Decimal
    inventory_valid: bool
    daily_sales: tuple[DailyQuantity, ...]
    daily_inventory_deltas: tuple[DailyQuantity, ...]
    sources: tuple[DataSourceEvidence, ...]
    reorder_weeks: int
    stock_up_weeks: int
    manual_level: int | None
    manual_target: int | None
    manual_locked: bool
    par_is_manual: bool
    incoming_supply: tuple[IncomingSupply, ...]
    non_sellable_quantity: Decimal
    non_sellable_resolved: bool
    product_created_at: datetime | None
    confirmed_discontinued: bool
    supporting_warnings: tuple[str, ...]
    lifecycle_status: str = 'ACTIVE'


def _decimal(value: Decimal | int | str | None) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except Exception:
        return ZERO


def normalize_candidate(candidate: RawRecommendationCandidate) -> NormalizedRecommendationInput:
    if candidate.reorder_weeks <= 0:
        raise ValueError('Reorder weeks must be greater than zero')
    if candidate.stock_up_weeks <= candidate.reorder_weeks:
        raise ValueError('Stock-up weeks must be greater than reorder weeks')
    if not candidate.sku.strip():
        raise ValueError('SKU is required')

    sales_by_day: dict[date, Decimal] = {}
    for row in candidate.daily_sales:
        sales_by_day[row.day] = sales_by_day.get(row.day, ZERO) + max(_decimal(row.quantity), ZERO)
    deltas_by_day: dict[date, Decimal] = {}
    for row in candidate.daily_inventory_deltas:
        deltas_by_day[row.day] = deltas_by_day.get(row.day, ZERO) + _decimal(row.quantity)

    incoming = tuple(
        row
        for row in sorted(candidate.incoming_supply, key=lambda value: (value.purchase_order_id, value.quantity))
        if row.quantity > 0
    )
    non_sellable = max(_decimal(candidate.non_sellable_quantity), ZERO) if candidate.non_sellable_resolved else ZERO
    return NormalizedRecommendationInput(
        store_id=int(candidate.store_id),
        store_name=str(candidate.store_name),
        vendor_id=int(candidate.vendor_id),
        vendor_name=str(candidate.vendor_name),
        sku=candidate.sku.strip(),
        variation_id=candidate.variation_id.strip(),
        item_name=candidate.item_name.strip() or candidate.sku.strip(),
        variation_name=candidate.variation_name.strip(),
        as_of=candidate.as_of,
        current_on_hand=max(_decimal(candidate.current_on_hand), ZERO),
        inventory_valid=bool(candidate.inventory_valid),
        daily_sales=tuple(DailyQuantity(day, sales_by_day[day]) for day in sorted(sales_by_day)),
        daily_inventory_deltas=tuple(DailyQuantity(day, deltas_by_day[day]) for day in sorted(deltas_by_day)),
        sources=tuple(sorted(candidate.sources, key=lambda value: value.source)),
        reorder_weeks=int(candidate.reorder_weeks),
        stock_up_weeks=int(candidate.stock_up_weeks),
        manual_level=max(int(candidate.manual_level), 0) if candidate.manual_level is not None else None,
        manual_target=max(int(candidate.manual_target), 0) if candidate.manual_target is not None else None,
        manual_locked=bool(candidate.manual_locked),
        par_is_manual=bool(candidate.par_is_manual),
        incoming_supply=incoming,
        non_sellable_quantity=non_sellable,
        non_sellable_resolved=bool(candidate.non_sellable_resolved),
        product_created_at=candidate.product_created_at,
        confirmed_discontinued=bool(candidate.confirmed_discontinued),
        supporting_warnings=tuple(dict.fromkeys(candidate.supporting_warnings)),
        lifecycle_status=candidate.lifecycle_status,
    )
