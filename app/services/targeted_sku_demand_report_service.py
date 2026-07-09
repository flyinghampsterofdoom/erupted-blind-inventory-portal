from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_CEILING

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Store, Vendor, VendorSkuConfig
from app.services.inventory_velocity_report_service import (
    ZERO,
    StoreDemandSplit,
    VelocityInventory,
    calculate_stockout_adjustments,
    fetch_inventory_stock_events,
    fetch_sales_data,
    format_quantity_compact,
)
from app.services.square_ordering_data_service import (
    CatalogVariationMeta,
    fetch_catalog_variation_maps,
    fetch_on_hand_by_store_variation,
)


TARGETED_SKU_TIME_WINDOWS = (15, 30, 45, 60, 90, 120, 180)


@dataclass(frozen=True)
class TargetedSkuSearchOption:
    variation_id: str
    sku: str
    product_name: str
    variation_name: str
    vendor: str
    vendor_id: int | None


@dataclass(frozen=True)
class TargetedSkuDemandRow:
    variation_id: str
    sku: str
    product_name: str
    variation_name: str
    vendor: str
    vendor_id: int | None
    units_sold: Decimal
    average_units_sold_per_day: Decimal
    target_days: int
    target_inventory_quantity: Decimal
    current_inventory_quantity: Decimal
    recommended_purchase_quantity: Decimal
    estimated_purchase_cost: Decimal | None
    days_of_supply_remaining: Decimal | None
    store_location_breakdown: str
    store_splits: list[StoreDemandSplit] | None = None
    store_specific_need_masked: bool = False
    adjusted_units_sold: Decimal = ZERO
    estimated_lost_units: Decimal = ZERO
    zero_stock_days: int = 0


@dataclass(frozen=True)
class TargetedSkuDemandReport:
    lookback_days: int
    target_days: int
    end_date: date
    rows: list[TargetedSkuDemandRow]
    stores: list[tuple[int, str]]
    total_purchase_quantity: Decimal
    total_estimated_purchase_cost: Decimal
    missing_cost_sku_count: int


def _vendor_info_by_variation(db: Session) -> dict[str, tuple[int | None, str, Decimal | None]]:
    rows = db.execute(
        select(VendorSkuConfig.square_variation_id, VendorSkuConfig.unit_cost, Vendor.name, Vendor.id)
        .join(Vendor, Vendor.id == VendorSkuConfig.vendor_id)
        .where(VendorSkuConfig.active.is_(True), Vendor.active.is_(True))
        .order_by(VendorSkuConfig.is_default_vendor.desc(), VendorSkuConfig.updated_at.desc())
    ).all()
    by_variation: dict[str, tuple[int | None, str, Decimal | None]] = {}
    for row in rows:
        variation_id = str(row.square_variation_id or '').strip()
        if not variation_id or variation_id in by_variation:
            continue
        cost = Decimal(str(row.unit_cost)) if row.unit_cost is not None and Decimal(str(row.unit_cost)) > 0 else None
        by_variation[variation_id] = (int(row.id), str(row.name or 'Unassigned'), cost)
    return by_variation


def _search_score(meta: CatalogVariationMeta, query: str) -> int:
    terms = [term for term in query.lower().split() if term]
    if not terms:
        return 0
    searchable = ' '.join([meta.sku, meta.item_name, meta.variation_name]).lower()
    if not all(term in searchable for term in terms):
        return 0
    score = sum(1 for term in terms if term in searchable)
    if query.lower() in searchable:
        score += len(terms) + 2
    if meta.sku.lower() == query.lower():
        score += 10
    return score


def search_targeted_sku_options(db: Session, *, query: str, limit: int | None = None) -> list[TargetedSkuSearchOption]:
    clean_query = str(query or '').strip()
    if not clean_query:
        return []
    catalog, _ = fetch_catalog_variation_maps()
    vendor_info = _vendor_info_by_variation(db)
    scored: list[tuple[int, CatalogVariationMeta]] = []
    for meta in catalog.values():
        score = _search_score(meta, clean_query)
        if score > 0:
            scored.append((score, meta))
    scored.sort(key=lambda item: (-item[0], item[1].item_name.lower(), item[1].variation_name.lower(), item[1].sku.lower()))
    options: list[TargetedSkuSearchOption] = []
    selected = scored if limit is None else scored[:limit]
    for _score, meta in selected:
        vendor_id, vendor, _cost = vendor_info.get(meta.variation_id, (None, 'Unassigned', None))
        options.append(
            TargetedSkuSearchOption(
                variation_id=meta.variation_id,
                sku=meta.sku or meta.variation_id,
                product_name=meta.item_name,
                variation_name=meta.variation_name,
                vendor=vendor,
                vendor_id=vendor_id,
            )
        )
    return options


def build_targeted_sku_demand_report(
    db: Session,
    *,
    variation_ids: list[str],
    lookback_days: int = 30,
    target_days: int | None = None,
    store_id: int | None = None,
    end_date: date | None = None,
) -> TargetedSkuDemandReport:
    clean_variation_ids = sorted({str(value).strip() for value in variation_ids if str(value or '').strip()})
    if not clean_variation_ids:
        raise ValueError('Select at least one SKU or variation')
    if lookback_days < 1:
        raise ValueError('Lookback days must be at least 1')
    report_end_date = end_date or date.today()
    target_days = target_days if target_days is not None else lookback_days
    if target_days < 1:
        raise ValueError('Target days must be at least 1')

    catalog, _ = fetch_catalog_variation_maps()
    selected = [catalog[variation_id] for variation_id in clean_variation_ids if variation_id in catalog]
    if not selected:
        raise ValueError('Selected variations were not found in the Square catalog')

    stores_query = select(Store.id, Store.name, Store.square_location_id).where(Store.active.is_(True), Store.square_location_id.is_not(None)).order_by(Store.name)
    if store_id is not None:
        stores_query = stores_query.where(Store.id == store_id)
    store_rows = db.execute(stores_query).all()
    stores = [(int(row.id), str(row.name)) for row in store_rows]
    store_ids = [store_id for store_id, _name in stores]
    store_names = dict(stores)
    store_by_location = {str(row.square_location_id): int(row.id) for row in store_rows if row.square_location_id}

    selected_variation_ids = [meta.variation_id for meta in selected]
    sales: dict[str, Decimal] = {variation_id: ZERO for variation_id in selected_variation_ids}
    sales_by_store_variation: dict[tuple[int, str], Decimal] = {}
    if stores:
        sales_rows = fetch_sales_data(db, start_date=report_end_date - timedelta(days=lookback_days - 1), end_date=report_end_date, store_id=store_id)
        selected_set = set(selected_variation_ids)
        for sale in sales_rows:
            sid = store_by_location.get(sale.location_id)
            if sid is not None and sale.variation_id in selected_set:
                key = (sid, sale.variation_id)
                sales_by_store_variation[key] = sales_by_store_variation.get(key, ZERO) + sale.units
                sales[sale.variation_id] = sales.get(sale.variation_id, ZERO) + sale.units
    on_hand = fetch_on_hand_by_store_variation(db, variation_ids=selected_variation_ids, store_ids=store_ids)
    vendor_info = _vendor_info_by_variation(db)
    velocity_inventory = {
        meta.variation_id: VelocityInventory(
            meta.variation_id,
            meta.sku or meta.variation_id,
            meta.item_name,
            'Uncategorized',
            vendor_info.get(meta.variation_id, (None, 'Unassigned', None))[1],
            vendor_info.get(meta.variation_id, (None, 'Unassigned', None))[2] or meta.first_vendor_unit_cost,
            False,
            {sid: on_hand.get((sid, meta.variation_id), ZERO) for sid in store_ids},
            vendor_info.get(meta.variation_id, (None, 'Unassigned', None))[0],
        )
        for meta in selected
    }
    inventory_events = fetch_inventory_stock_events(
        db,
        variation_ids=selected_variation_ids,
        start_date=report_end_date - timedelta(days=lookback_days - 1),
        end_date=report_end_date,
        store_id=store_id,
    )
    stockout_adjustments = calculate_stockout_adjustments(
        sales_rows if stores else [],
        velocity_inventory,
        inventory_events,
        days=lookback_days,
        end_date=report_end_date,
        store_by_location=store_by_location,
    )

    rows: list[TargetedSkuDemandRow] = []
    total_purchase_quantity = ZERO
    total_estimated_cost = ZERO
    missing_cost_skus: set[str] = set()
    for meta in selected:
        units_sold = sales.get(meta.variation_id, ZERO)
        adjusted_units_sold = sum(
            (
                stockout_adjustments[(meta.variation_id, sid)].adjusted_units_sold
                if (meta.variation_id, sid) in stockout_adjustments
                else sales_by_store_variation.get((sid, meta.variation_id), ZERO)
                for sid in store_ids
            ),
            ZERO,
        )
        estimated_lost_units = max(ZERO, adjusted_units_sold - units_sold).quantize(Decimal('0.001'))
        zero_stock_days = sum(
            (
                stockout_adjustments.get((meta.variation_id, sid)).zero_stock_days
                if (meta.variation_id, sid) in stockout_adjustments
                else 0
                for sid in store_ids
            ),
            0,
        )
        daily = adjusted_units_sold / Decimal(lookback_days)
        current_inventory = sum((on_hand.get((sid, meta.variation_id), ZERO) for sid in store_ids), ZERO)
        target_inventory = (daily * Decimal(target_days)).to_integral_value(rounding=ROUND_CEILING)
        aggregate_purchase_qty = max(ZERO, target_inventory - current_inventory).to_integral_value(rounding=ROUND_CEILING)
        store_splits: list[StoreDemandSplit] = []
        for sid in store_ids:
            store_units = sales_by_store_variation.get((sid, meta.variation_id), ZERO)
            adjustment = stockout_adjustments.get((meta.variation_id, sid))
            adjusted_store_units = adjustment.adjusted_units_sold if adjustment else store_units
            store_daily = adjusted_store_units / Decimal(lookback_days)
            store_on_hand = on_hand.get((sid, meta.variation_id), ZERO)
            store_target = (store_daily * Decimal(target_days)).to_integral_value(rounding=ROUND_CEILING)
            store_purchase = max(ZERO, store_target - store_on_hand).to_integral_value(rounding=ROUND_CEILING)
            store_supply = store_on_hand / store_daily if store_daily > 0 else None
            store_splits.append(
                StoreDemandSplit(
                    store_id=sid,
                    store_name=str(store_names.get(sid, sid)),
                    units_sold=store_units,
                    average_units_sold_per_day=store_daily,
                    target_inventory_quantity=store_target,
                    current_inventory_quantity=store_on_hand,
                    recommended_purchase_quantity=store_purchase,
                    days_of_supply_remaining=store_supply,
                    adjusted_units_sold=adjusted_store_units,
                    estimated_lost_units=adjustment.estimated_lost_units if adjustment else ZERO,
                    zero_stock_days=adjustment.zero_stock_days if adjustment else 0,
                )
            )
        purchase_qty = sum((split.recommended_purchase_quantity for split in store_splits), ZERO)
        if purchase_qty <= 0:
            purchase_qty = aggregate_purchase_qty
        days_supply = current_inventory / daily if daily > 0 else None
        vendor_id, vendor, configured_cost = vendor_info.get(meta.variation_id, (None, 'Unassigned', None))
        unit_cost = configured_cost or meta.first_vendor_unit_cost
        estimated_cost = purchase_qty * unit_cost if unit_cost is not None else None
        if estimated_cost is not None:
            total_estimated_cost += estimated_cost
        elif purchase_qty > 0:
            missing_cost_skus.add(meta.sku or meta.variation_id)
        total_purchase_quantity += purchase_qty
        breakdown = '; '.join(
            f'{split.store_name}: {split.units_sold:g} sold / {split.current_inventory_quantity:g} on hand / {split.recommended_purchase_quantity:g} need'
            + (f' / {split.zero_stock_days} zero days / {format_quantity_compact(split.estimated_lost_units)} est. lost' if split.zero_stock_days > 0 else '')
            for split in store_splits
        )
        rows.append(
            TargetedSkuDemandRow(
                variation_id=meta.variation_id,
                sku=meta.sku or meta.variation_id,
                product_name=meta.item_name,
                variation_name=meta.variation_name,
                vendor=vendor,
                vendor_id=vendor_id,
                units_sold=units_sold,
                average_units_sold_per_day=daily,
                target_days=target_days,
                target_inventory_quantity=target_inventory,
                current_inventory_quantity=current_inventory,
                recommended_purchase_quantity=purchase_qty,
                estimated_purchase_cost=estimated_cost,
                days_of_supply_remaining=days_supply,
                store_location_breakdown=breakdown,
                store_splits=store_splits,
                store_specific_need_masked=aggregate_purchase_qty < purchase_qty,
                adjusted_units_sold=adjusted_units_sold,
                estimated_lost_units=estimated_lost_units,
                zero_stock_days=zero_stock_days,
            )
        )
    rows.sort(key=lambda row: (-row.recommended_purchase_quantity, row.product_name.lower(), row.variation_name.lower(), row.sku.lower()))
    return TargetedSkuDemandReport(
        lookback_days=lookback_days,
        target_days=target_days,
        end_date=report_end_date,
        rows=rows,
        stores=stores,
        total_purchase_quantity=total_purchase_quantity,
        total_estimated_purchase_cost=total_estimated_cost,
        missing_cost_sku_count=len(missing_cost_skus),
    )


def render_targeted_sku_demand_export(report: TargetedSkuDemandReport) -> list[list[str]]:
    output = [
        ['Targeted SKU Demand Report'],
        ['Lookback days', str(report.lookback_days)],
        ['Target days', str(report.target_days)],
        ['Total purchase quantity', str(report.total_purchase_quantity)],
        ['Total estimated purchase cost', f'{report.total_estimated_purchase_cost:.2f}'],
        ['SKUs missing cost', str(report.missing_cost_sku_count)],
        [],
        [
            'SKU',
            'Product name',
            'Variation',
            'Vendor',
            'Units sold',
            'Stockout-adjusted units',
            'Estimated lost sales',
            'Zero-stock days',
            'Average units sold per day',
            'Target days',
            'Target inventory quantity',
            'Current inventory quantity',
            'Recommended purchase quantity',
            'Estimated purchase cost',
            'Days of supply remaining',
            'Store/location breakdown',
            'Store-specific issue',
            'Variation ID',
        ],
    ]
    for row in report.rows:
        output.append(
            [
                row.sku,
                row.product_name,
                row.variation_name,
                row.vendor,
                str(row.units_sold),
                f'{row.adjusted_units_sold:g}',
                f'{row.estimated_lost_units:g}',
                str(row.zero_stock_days),
                f'{row.average_units_sold_per_day:.3f}',
                str(row.target_days),
                str(row.target_inventory_quantity),
                str(row.current_inventory_quantity),
                str(row.recommended_purchase_quantity),
                f'{row.estimated_purchase_cost:.2f}' if row.estimated_purchase_cost is not None else '',
                f'{row.days_of_supply_remaining:.2f}' if row.days_of_supply_remaining is not None else '',
                row.store_location_breakdown,
                'Store need masked by other stores' if row.store_specific_need_masked else '',
                row.variation_id,
            ]
        )
    return output
