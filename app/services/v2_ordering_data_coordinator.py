from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import perf_counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.services.purchasing_inventory_service import purchasing_store_locations, load_incoming_supply
from app.models import (
    OrderingMathSetting,
    ParLevel,
    ParLevelSource,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderStatus,
    PurchaseOrderStoreAllocation,
    Store,
    Vendor,
    VendorOrderingSetting,
    VendorSkuConfig,
)
from app.services.v2_ordering_normalization_service import (
    IncomingSupply,
    RawRecommendationCandidate,
    normalize_candidate,
)
from app.services.v2_ordering_policy_service import DataSourceEvidence
from app.services.v2_ordering_recommendation_service import RecommendationResult, calculate_recommendation
from app.services.v2_ordering_square_gateway import SquareOrderingReadGateway
from app.services.v2_ordering_lifecycle_repository import (
    ACTIVE,
    ARCHIVED,
    NO_FUTURE_REORDER,
    load_lifecycle_states,
)


@dataclass(frozen=True)
class OrderingDashboardMetrics:
    active_variation_count: int = 0
    archived_variation_count: int = 0
    no_future_reorder_count: int = 0
    lifecycle_lookup_seconds: float = 0.0
    local_database_seconds: float = 0.0
    square_seconds: float = 0.0
    calculation_seconds: float = 0.0
    square_request_count: int = 0
    inventory_count_variation_ids_submitted: int = 0
    inventory_change_variation_ids_submitted: int = 0
    inventory_change_page_count: int = 0
    inventory_changes_returned: int = 0
    recommendation_count: int = 0


@dataclass(frozen=True)
class OrderingDashboardData:
    as_of: datetime
    recommendations: tuple[RecommendationResult, ...]
    metrics: OrderingDashboardMetrics = field(default_factory=OrderingDashboardMetrics)
    inventory_mapping_issues: tuple[str, ...] = ()


def _math_settings(db: Session) -> tuple[int, int, dict[int, tuple[int, int]]]:
    base = db.execute(select(OrderingMathSetting).where(OrderingMathSetting.id == 1)).scalar_one_or_none()
    default_reorder = int(base.default_reorder_weeks) if base else settings.ordering_reorder_weeks_default
    default_stock_up = int(base.default_stock_up_weeks) if base else settings.ordering_stock_up_weeks_default
    overrides = {
        int(row.vendor_id): (int(row.reorder_weeks), int(row.stock_up_weeks))
        for row in db.execute(select(VendorOrderingSetting)).scalars().all()
    }
    return default_reorder, default_stock_up, overrides


def build_ordering_dashboard(
    db: Session,
    *,
    store_ids: tuple[int, ...],
    gateway: SquareOrderingReadGateway | None = None,
    as_of: datetime | None = None,
) -> OrderingDashboardData:
    """Read V1-owned facts without mutation and calculate Phase 1 recommendations."""
    database_started = perf_counter()
    now = as_of or datetime.now(tz=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if not store_ids:
        return OrderingDashboardData(now, ())

    display_store_ids = store_ids
    mapping_issues = ()
    if settings.purchasing_v3_phase1_enabled or settings.purchasing_v3_phase2_enabled:
        locations, mapping_issues = purchasing_store_locations(db)
        store_ids = tuple(locations)
    store_rows = db.execute(
        select(Store.id, Store.name, Store.square_location_id)
        .where(Store.id.in_(store_ids), Store.active.is_(True))
        .order_by(Store.id.asc())
    ).all()
    stores = {int(row.id): (str(row.name), str(row.square_location_id or '')) for row in store_rows}
    location_by_store = {store_id: value[1] for store_id, value in stores.items() if value[1]}

    mapping_rows = db.execute(
        select(VendorSkuConfig, Vendor)
        .join(Vendor, Vendor.id == VendorSkuConfig.vendor_id)
        .where(
            VendorSkuConfig.active.is_(True),
            VendorSkuConfig.is_default_vendor.is_(True),
            Vendor.active.is_(True),
        )
        .order_by(Vendor.name.asc(), VendorSkuConfig.sku.asc(), VendorSkuConfig.id.asc())
    ).all()
    if not mapping_rows:
        return OrderingDashboardData(now, ())

    all_variation_ids = [str(mapping.square_variation_id or '').strip() for mapping, _vendor in mapping_rows]
    lifecycle_started = perf_counter()
    lifecycle = load_lifecycle_states(db, all_variation_ids)
    lifecycle_lookup_seconds = perf_counter() - lifecycle_started
    archived_ids = {variation_id for variation_id, state in lifecycle.items() if state.status == ARCHIVED}
    no_future_reorder_ids = {
        variation_id for variation_id, state in lifecycle.items() if state.status == NO_FUTURE_REORDER
    }
    mapping_rows = [
        (mapping, vendor)
        for mapping, vendor in mapping_rows
        if str(mapping.square_variation_id or '').strip() not in archived_ids
    ]
    if not mapping_rows:
        return OrderingDashboardData(
            now,
            (),
            OrderingDashboardMetrics(
                archived_variation_count=len(archived_ids),
                lifecycle_lookup_seconds=lifecycle_lookup_seconds,
                local_database_seconds=perf_counter() - database_started,
            ),
        )

    vendor_ids = sorted({int(mapping.vendor_id) for mapping, _vendor in mapping_rows})
    par_rows = db.execute(select(ParLevel).where(ParLevel.vendor_id.in_(vendor_ids))).scalars().all()
    pars = {
        (int(row.vendor_id), int(row.store_id) if row.store_id is not None else None, str(row.sku)): row
        for row in par_rows
        if row.vendor_id is not None
    }
    default_reorder, default_stock_up, overrides = _math_settings(db)

    incoming_rows = load_incoming_supply(db, store_ids=store_ids)
    incoming_by_store_variation = {}
    uncertain_incoming = set()
    for row in incoming_rows:
        key = (row.store_id, row.variation_id)
        if row.quantity is None:
            uncertain_incoming.add(key)
        elif row.quantity:
            incoming_by_store_variation.setdefault(key, []).append(
                IncomingSupply(row.purchase_order_id, row.quantity, row.ordered_at))

    variation_ids = [str(mapping.square_variation_id or '') for mapping, _vendor in mapping_rows]
    local_database_seconds = perf_counter() - database_started
    square_started = perf_counter()
    square = (gateway or SquareOrderingReadGateway()).fetch(
        location_by_store=location_by_store,
        variation_ids=variation_ids,
        as_of=now,
        include_today=True,
        parallel_history=True,
    )
    square_seconds = perf_counter() - square_started
    calculation_started = perf_counter()
    results: list[RecommendationResult] = []
    for mapping, vendor in mapping_rows:
        vendor_id = int(mapping.vendor_id)
        sku = str(mapping.sku)
        variation_id = str(mapping.square_variation_id or '').strip()
        product = square.products.get(variation_id)
        reorder_weeks, stock_up_weeks = overrides.get(vendor_id, (default_reorder, default_stock_up))
        for store_id in display_store_ids:
            if store_id not in stores:
                continue
            remote = square.by_store_variation.get((store_id, variation_id))
            if remote is None:
                sources = (
                    DataSourceEvidence('inventory', None, available=False, detail='Square location or variation unavailable'),
                    DataSourceEvidence('sales', None, available=False, detail='Square location or variation unavailable'),
                    DataSourceEvidence('stockout_history', None, available=False, detail='Square location or variation unavailable'),
                )
                daily_sales = ()
                daily_deltas = ()
                current_on_hand = None
                inventory_valid = False
                warnings = ('SQUARE_VARIATION_OR_LOCATION_UNAVAILABLE',)
            else:
                sources = remote.required_sources
                daily_sales = remote.daily_sales
                daily_deltas = remote.daily_inventory_deltas
                current_on_hand = remote.current_on_hand
                inventory_valid = remote.inventory_valid
                warnings = remote.warnings
            warnings = tuple(warnings) + mapping_issues

            if (store_id, variation_id) in uncertain_incoming:
                sources = sources + (DataSourceEvidence('incoming', None, available=False, complete=False,
                                                       detail='Incoming commitment is unknown'),)
            par = pars.get((vendor_id, store_id, sku)) or pars.get((vendor_id, None, sku))
            candidate = RawRecommendationCandidate(
                store_id=store_id,
                store_name=stores[store_id][0],
                vendor_id=vendor_id,
                vendor_name=str(vendor.name),
                sku=sku,
                variation_id=variation_id,
                item_name=product.item_name if product else sku,
                variation_name=product.variation_name if product else '',
                as_of=now,
                current_on_hand=current_on_hand,
                inventory_valid=inventory_valid,
                daily_sales=daily_sales,
                daily_inventory_deltas=daily_deltas,
                current_day_inventory_delta=getattr(remote,"current_day_inventory_delta",None),
                sources=sources,
                reorder_weeks=reorder_weeks,
                stock_up_weeks=stock_up_weeks,
                manual_level=par.manual_par_level if par else None,
                manual_target=par.manual_stock_up_level if par else None,
                manual_locked=bool(par.locked_manual) if par else False,
                par_is_manual=bool(par and par.par_source == ParLevelSource.MANUAL),
                # The mapping is the current default. Include open historical POs
                # for the same store/SKU even when they retain an older vendor.
                incoming_supply=tuple(incoming_by_store_variation.get((store_id, variation_id), ())),
                non_sellable_quantity=None,
                non_sellable_resolved=False,
                product_created_at=(product.created_at if product else None),
                confirmed_discontinued=bool(product and product.confirmed_discontinued),
                supporting_warnings=warnings,
                lifecycle_status=lifecycle.get(variation_id).status if variation_id in lifecycle else ACTIVE,
            )
            results.append(calculate_recommendation(normalize_candidate(candidate)))
    results.sort(key=lambda row: (row.store_name.lower(), row.vendor_name.lower(), row.item_name.lower(), row.sku))
    clean_eligible_ids = {value.strip() for value in variation_ids if value.strip()}
    metrics = OrderingDashboardMetrics(
        active_variation_count=len(clean_eligible_ids - no_future_reorder_ids),
        archived_variation_count=len(archived_ids),
        no_future_reorder_count=len(clean_eligible_ids & no_future_reorder_ids),
        lifecycle_lookup_seconds=lifecycle_lookup_seconds,
        local_database_seconds=local_database_seconds,
        square_seconds=square_seconds,
        calculation_seconds=perf_counter() - calculation_started,
        square_request_count=square.metrics.request_count,
        inventory_count_variation_ids_submitted=square.metrics.inventory_count_variation_ids_submitted,
        inventory_change_variation_ids_submitted=square.metrics.inventory_change_variation_ids_submitted,
        inventory_change_page_count=square.metrics.inventory_change_page_count,
        inventory_changes_returned=square.metrics.inventory_changes_returned,
        recommendation_count=len(results),
    )
    return OrderingDashboardData(now, tuple(results), metrics, mapping_issues)
