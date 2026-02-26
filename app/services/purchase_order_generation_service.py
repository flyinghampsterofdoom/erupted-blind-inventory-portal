from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import replace
from decimal import Decimal

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.models import (
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


def list_selected_vendor_skus(db: Session, *, vendor_ids: list[int]) -> dict[int, list[VendorSkuConfig]]:
    if not vendor_ids:
        return {}
    rows = db.execute(
        select(VendorSkuConfig)
        .where(
            VendorSkuConfig.vendor_id.in_(vendor_ids),
            VendorSkuConfig.active.is_(True),
            VendorSkuConfig.is_default_vendor.is_(True),
        )
        .order_by(VendorSkuConfig.vendor_id.asc(), VendorSkuConfig.sku.asc())
    ).scalars().all()
    by_vendor: dict[int, list[VendorSkuConfig]] = {}
    for row in rows:
        by_vendor.setdefault(row.vendor_id, []).append(row)
    return by_vendor


def _active_store_ids(db: Session) -> list[int]:
    return [row[0] for row in db.execute(select(Store.id).where(Store.active.is_(True)).order_by(Store.id.asc())).all()]


def _open_in_transit_query(vendor_ids: list[int]) -> Select:
    return (
        select(
            PurchaseOrder.vendor_id,
            PurchaseOrderStoreAllocation.store_id,
            PurchaseOrderLine.sku,
            func.coalesce(func.sum(PurchaseOrderStoreAllocation.allocated_qty), 0).label('open_in_transit_qty'),
        )
        .join(PurchaseOrderLine, PurchaseOrderLine.purchase_order_id == PurchaseOrder.id)
        .join(PurchaseOrderStoreAllocation, PurchaseOrderStoreAllocation.purchase_order_line_id == PurchaseOrderLine.id)
        .where(
            PurchaseOrder.vendor_id.in_(vendor_ids),
            PurchaseOrder.status.in_(
                [PurchaseOrderStatus.IN_TRANSIT, PurchaseOrderStatus.RECEIVED_SPLIT_PENDING]
            ),
            PurchaseOrderLine.removed.is_(False),
            PurchaseOrderLine.sku.is_not(None),
        )
        .group_by(PurchaseOrder.vendor_id, PurchaseOrderStoreAllocation.store_id, PurchaseOrderLine.sku)
    )


def _open_in_transit_by_vendor_store_sku(db: Session, *, vendor_ids: list[int]) -> dict[tuple[int, int, str], int]:
    if not vendor_ids:
        return {}
    rows = db.execute(_open_in_transit_query(vendor_ids)).all()
    return {
        (int(row.vendor_id), int(row.store_id), str(row.sku)): int(row.open_in_transit_qty or 0)
        for row in rows
    }


def _par_levels_by_vendor_sku(db: Session, *, vendor_ids: list[int]) -> dict[tuple[int, str], ParLevel]:
    if not vendor_ids:
        return {}
    rows = db.execute(select(ParLevel).where(ParLevel.vendor_id.in_(vendor_ids))).scalars().all()
    return {(int(row.vendor_id), row.sku): row for row in rows if row.vendor_id is not None}


def generate_vendor_scoped_recommendations(
    db: Session,
    *,
    vendor_ids: list[int],
    history_loader: HistoryLoader,
    on_hand_loader: OnHandLoader,
    overrides: MathOverrides | None = None,
    include_zero_qty: bool = False,
) -> list[GenerationLine]:
    """
    Generate ordering recommendations only for selected vendors and their mapped SKUs.
    This intentionally avoids running full-catalog/full-inventory calculations.
    """
    if not vendor_ids:
        return []

    store_ids = _active_store_ids(db)
    if not store_ids:
        return []
    by_vendor = list_selected_vendor_skus(db, vendor_ids=vendor_ids)
    in_transit = _open_in_transit_by_vendor_store_sku(db, vendor_ids=vendor_ids)
    par_levels = _par_levels_by_vendor_sku(db, vendor_ids=vendor_ids)
    results: list[GenerationLine] = []

    for vendor_id in vendor_ids:
        sku_rows = by_vendor.get(vendor_id, [])
        if not sku_rows:
            continue
        vendor_defaults = resolve_effective_math_params(db, vendor_id=vendor_id)
        params = resolve_math_params(vendor_defaults, overrides)

        for sku_row in sku_rows:
            sku = sku_row.sku
            par = par_levels.get((vendor_id, sku))
            for store_id in store_ids:
                history = history_loader(vendor_id, store_id, sku, params.history_lookback_days)
                on_hand = on_hand_loader(store_id, sku)
                in_transit_qty = in_transit.get((vendor_id, store_id, sku), 0)

                line_input = LineMathInput(
                    sku=sku,
                    current_on_hand=on_hand,
                    in_transit_qty=in_transit_qty,
                    history_daily_units=history,
                    unit_pack_size=sku_row.pack_size,
                    min_order_qty=sku_row.min_order_qty,
                    manual_par_level=par.manual_par_level if par else None,
                    par_source=par.par_source if par else ParLevelSource.MANUAL,
                )
                result = compute_line_recommendation(line_input, params)
                if on_hand <= 0 and result.rounded_recommended_qty < 1:
                    # Ensure true zero-stock items are visible in ordering output.
                    result = replace(
                        result,
                        raw_recommended_qty=max(result.raw_recommended_qty, 1),
                        rounded_recommended_qty=1,
                    )
                if not include_zero_qty and result.rounded_recommended_qty <= 0:
                    continue
                results.append(GenerationLine(vendor_id=vendor_id, store_id=store_id, sku=sku, result=result))

    return results
