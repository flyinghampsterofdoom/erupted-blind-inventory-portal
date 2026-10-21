from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.models import (
    OrderingProductLifecycle,
    ParLevel,
    ParLevelSource,
    PurchaseOrder,
    PurchaseOrderStoreAllocation,
    PurchaseOrderLine,
    PurchaseOrderStatus,
    Store,
    VendorSkuConfig,
)
from app.services.ordering_service import resolve_effective_math_params
from app.services.purchasing_inventory_service import load_incoming_supply
from app.services.purchase_order_math_service import (
    LineMathInput,
    LineMathResult,
    MathOverrides,
    compute_line_recommendation,
    resolve_math_params,
)


@dataclass(frozen=True)
class GenerationLine:
    vendor_id: int
    store_id: int
    sku: str
    result: LineMathResult


HistoryLoader = Callable[[int, int, str, int], list[Decimal]]
OnHandLoader = Callable[[int, str], Decimal]


def list_selected_vendor_skus(
    db: Session,
    *,
    vendor_ids: list[int],
    include_non_default_vendor_skus: bool = False,
) -> dict[int, list[VendorSkuConfig]]:
    if not vendor_ids:
        return {}
    query = (
        select(VendorSkuConfig)
        .where(
            VendorSkuConfig.vendor_id.in_(vendor_ids),
            VendorSkuConfig.active.is_(True),
            ~select(OrderingProductLifecycle.square_variation_id).where(
                OrderingProductLifecycle.square_variation_id == VendorSkuConfig.square_variation_id,
                OrderingProductLifecycle.status.in_(["NO_FUTURE_REORDER", "ARCHIVED"]),
            ).exists(),
        )
        .order_by(VendorSkuConfig.vendor_id.asc(), VendorSkuConfig.sku.asc())
    )
    if not include_non_default_vendor_skus:
        query = query.where(VendorSkuConfig.is_default_vendor.is_(True))
    rows = db.execute(query).scalars().all()
    by_vendor: dict[int, list[VendorSkuConfig]] = {}
    for row in rows:
        by_vendor.setdefault(row.vendor_id, []).append(row)
    return by_vendor


def _active_store_ids(db: Session) -> list[int]:
    return [row[0] for row in db.execute(select(Store.id).where(Store.active.is_(True)).order_by(Store.id.asc())).all()]


def _open_in_transit_by_vendor_store_sku(db: Session, *, vendor_ids: list[int]) -> dict[tuple[int, int, str], int]:
    output = {}
    incoming = load_incoming_supply(db)
    identities = {}
    if incoming:
        mappings = db.execute(select(VendorSkuConfig.sku, VendorSkuConfig.square_variation_id).where(
            VendorSkuConfig.active.is_(True), VendorSkuConfig.sku.in_({row.sku for row in incoming}))).all()
        for sku, variation_id in mappings:
            identities.setdefault(sku, set()).add(str(variation_id or ''))
    for row in incoming:
        if identities.get(row.sku) and identities[row.sku] != {row.variation_id}:
            raise ValueError(f'Ambiguous incoming SKU {row.sku}; variation identities must be reconciled.')
        if row.quantity is None:
            raise ValueError(f'Incoming inventory UNKNOWN for PO {row.purchase_order_id}: ' + ', '.join(row.reasons))
        key = (row.vendor_id, row.store_id, row.sku)
        output[key] = output.get(key, 0) + row.quantity
    return output


def _par_levels_by_vendor_store_sku(db: Session, *, vendor_ids: list[int]) -> dict[tuple[int, int | None, str], ParLevel]:
    if not vendor_ids:
        return {}
    rows = db.execute(select(ParLevel).where(ParLevel.vendor_id.in_(vendor_ids))).scalars().all()
    return {
        (int(row.vendor_id), (int(row.store_id) if row.store_id is not None else None), row.sku): row
        for row in rows
        if row.vendor_id is not None
    }


def generate_vendor_scoped_recommendations(
    db: Session,
    *,
    vendor_ids: list[int],
    history_loader: HistoryLoader,
    on_hand_loader: OnHandLoader,
    overrides: MathOverrides | None = None,
    include_zero_qty: bool = False,
    include_non_default_vendor_skus: bool = False,
) -> list[GenerationLine]:
    """
    Generate ordering recommendations only for selected vendors and their mapped SKUs.
    This intentionally avoids running full-catalog/full-inventory calculations.
    """
    if not vendor_ids:
        return []

    from app.config import settings
    if settings.purchasing_v3_phase1_enabled or settings.purchasing_v3_phase2_enabled:
        from app.services.purchasing_inventory_service import purchasing_store_locations
        store_ids = list(purchasing_store_locations(db)[0])
    else:
        store_ids = _active_store_ids(db)
    if not store_ids:
        return []
    by_vendor = list_selected_vendor_skus(
        db,
        vendor_ids=vendor_ids,
        include_non_default_vendor_skus=include_non_default_vendor_skus,
    )
    # Load all historical PO vendors. A current default may have changed while an
    # older vendor's PO is still inbound; that supply must still prevent a
    # duplicate future order for the SKU.
    in_transit = _open_in_transit_by_vendor_store_sku(db, vendor_ids=[])
    in_transit_by_store_sku: dict[tuple[int, str], int] = {}
    for (_historical_vendor_id, store_id, sku), quantity in in_transit.items():
        key = (store_id, sku)
        in_transit_by_store_sku[key] = in_transit_by_store_sku.get(key, 0) + quantity
    # Square-backed adapters retain observation proof; recheck it after incoming.
    adapter = getattr(on_hand_loader, '__self__', None)
    if callable(getattr(adapter, 'reconcile_receipts', None)):
        adapter.reconcile_receipts(db)
    par_levels = _par_levels_by_vendor_store_sku(db, vendor_ids=vendor_ids)
    results: list[GenerationLine] = []

    for vendor_id in vendor_ids:
        sku_rows = by_vendor.get(vendor_id, [])
        if not sku_rows:
            continue
        vendor_defaults = resolve_effective_math_params(db, vendor_id=vendor_id)
        params = resolve_math_params(vendor_defaults, overrides)

        for sku_row in sku_rows:
            sku = sku_row.sku
            for store_id in store_ids:
                par = par_levels.get((vendor_id, store_id, sku)) or par_levels.get((vendor_id, None, sku))
                history = history_loader(vendor_id, store_id, sku, params.history_lookback_days)
                try:
                    on_hand = on_hand_loader(store_id, sku)
                except ValueError as exc:
                    if 'Inventory UNKNOWN' not in str(exc):
                        raise
                    # Preserve recommendations for observed pairs; never create a
                    # recommendation by treating the missing pair as zero.
                    continue
                in_transit_qty = in_transit_by_store_sku.get((store_id, sku), 0)

                line_input = LineMathInput(
                    sku=sku,
                    current_on_hand=on_hand,
                    in_transit_qty=in_transit_qty,
                    history_daily_units=history,
                    unit_pack_size=sku_row.pack_size,
                    min_order_qty=sku_row.min_order_qty,
                    manual_level=par.manual_par_level if par else None,
                    manual_par=par.manual_stock_up_level if par else None,
                    par_source=par.par_source if par else ParLevelSource.DYNAMIC,
                )
                result = compute_line_recommendation(line_input, params)
                if result.rounded_recommended_qty is None:
                    continue
                if not include_zero_qty and result.rounded_recommended_qty <= 0:
                    continue
                results.append(GenerationLine(vendor_id=vendor_id, store_id=store_id, sku=sku, result=result))

    return results
