from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Store
from app.services.square_ordering_data_service import fetch_catalog_variation_maps, fetch_on_hand_by_store_variation


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


@dataclass(frozen=True)
class StockValueStoreRow:
    store_id: int
    store_name: str
    total_units_on_hand: Decimal
    total_cost_value: Decimal
    total_retail_value: Decimal


@dataclass(frozen=True)
class StockValueTopItemRow:
    variation_id: str
    item_name: str
    variation_name: str
    on_hand_qty: Decimal
    unit_cost: Decimal | None
    unit_price: Decimal | None
    extended_cost_value: Decimal
    extended_retail_value: Decimal


@dataclass(frozen=True)
class StockValueOnHandResult:
    as_of_utc: datetime
    active_store_count: int
    tracked_variation_count: int
    in_stock_variation_count: int
    missing_cost_variation_count: int
    missing_price_variation_count: int
    total_units_on_hand: Decimal
    total_cost_value: Decimal
    total_retail_value: Decimal
    store_rows: list[StockValueStoreRow]
    top_item_rows: list[StockValueTopItemRow]


def build_stock_value_on_hand_report(
    db: Session,
    *,
    store_id: int | None = None,
    top_n_items: int = 200,
) -> StockValueOnHandResult:
    stores_query = (
        select(Store.id, Store.name)
        .where(
            Store.active.is_(True),
            Store.square_location_id.is_not(None),
        )
        .order_by(Store.name.asc())
    )
    if store_id is not None:
        stores_query = stores_query.where(Store.id == store_id)
    stores = db.execute(stores_query).all()
    if not stores:
        raise RuntimeError('No active stores with Square location mapping found for this filter.')

    selected_store_ids = [int(row.id) for row in stores]
    store_name_by_id = {int(row.id): str(row.name) for row in stores}

    catalog_by_variation_id, _ = fetch_catalog_variation_maps()
    variation_ids = sorted(catalog_by_variation_id.keys())
    if not variation_ids:
        raise RuntimeError('Square catalog returned no variations.')

    on_hand_by_store_variation = fetch_on_hand_by_store_variation(
        db,
        variation_ids=variation_ids,
        store_ids=selected_store_ids,
    )

    total_qty_by_variation: dict[str, Decimal] = {}
    store_totals: dict[int, dict[str, Decimal]] = {
        sid: {
            'units': Decimal('0'),
            'cost': Decimal('0.00'),
            'retail': Decimal('0.00'),
        }
        for sid in selected_store_ids
    }

    for (sid, variation_id), qty in on_hand_by_store_variation.items():
        qty_value = Decimal(str(qty))
        total_qty_by_variation[variation_id] = total_qty_by_variation.get(variation_id, Decimal('0')) + qty_value
        store_totals[sid]['units'] = store_totals[sid]['units'] + qty_value

        meta = catalog_by_variation_id.get(variation_id)
        if meta is None:
            continue
        unit_cost = meta.first_vendor_unit_cost
        unit_price = meta.unit_price
        if unit_cost is not None:
            store_totals[sid]['cost'] = store_totals[sid]['cost'] + (qty_value * unit_cost)
        if unit_price is not None:
            store_totals[sid]['retail'] = store_totals[sid]['retail'] + (qty_value * unit_price)

    total_units_on_hand = Decimal('0')
    total_cost_value = Decimal('0.00')
    total_retail_value = Decimal('0.00')
    missing_cost_variation_count = 0
    missing_price_variation_count = 0
    in_stock_variation_count = 0
    top_item_rows: list[StockValueTopItemRow] = []

    for variation_id, qty in total_qty_by_variation.items():
        if qty == 0:
            continue
        in_stock_variation_count += 1
        total_units_on_hand += qty

        meta = catalog_by_variation_id.get(variation_id)
        if meta is None:
            continue

        unit_cost = meta.first_vendor_unit_cost
        unit_price = meta.unit_price
        extended_cost = qty * unit_cost if unit_cost is not None else Decimal('0.00')
        extended_retail = qty * unit_price if unit_price is not None else Decimal('0.00')
        if unit_cost is None:
            missing_cost_variation_count += 1
        if unit_price is None:
            missing_price_variation_count += 1

        total_cost_value += extended_cost
        total_retail_value += extended_retail
        top_item_rows.append(
            StockValueTopItemRow(
                variation_id=variation_id,
                item_name=meta.item_name,
                variation_name=meta.variation_name,
                on_hand_qty=qty,
                unit_cost=unit_cost,
                unit_price=unit_price,
                extended_cost_value=extended_cost,
                extended_retail_value=extended_retail,
            )
        )

    top_item_rows.sort(key=lambda row: row.extended_retail_value, reverse=True)
    top_item_rows = top_item_rows[: max(1, top_n_items)]

    store_rows: list[StockValueStoreRow] = []
    for sid in selected_store_ids:
        totals = store_totals[sid]
        store_rows.append(
            StockValueStoreRow(
                store_id=sid,
                store_name=store_name_by_id.get(sid, str(sid)),
                total_units_on_hand=totals['units'],
                total_cost_value=totals['cost'],
                total_retail_value=totals['retail'],
            )
        )

    return StockValueOnHandResult(
        as_of_utc=_now(),
        active_store_count=len(selected_store_ids),
        tracked_variation_count=len(variation_ids),
        in_stock_variation_count=in_stock_variation_count,
        missing_cost_variation_count=missing_cost_variation_count,
        missing_price_variation_count=missing_price_variation_count,
        total_units_on_hand=total_units_on_hand,
        total_cost_value=total_cost_value.quantize(Decimal('0.01')),
        total_retail_value=total_retail_value.quantize(Decimal('0.01')),
        store_rows=store_rows,
        top_item_rows=top_item_rows,
    )
