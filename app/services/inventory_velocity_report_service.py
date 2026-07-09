from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Store, Vendor, VendorSkuConfig
from app.services.sales_transactions_report_service import _SquareClient, _parse_iso_datetime, _to_iso
from app.services.square_ordering_data_service import fetch_catalog_variation_maps, fetch_on_hand_by_store_variation


TIME_WINDOWS = (15, 30, 45, 60, 90)
ZERO = Decimal('0')


@dataclass(frozen=True)
class VelocitySale:
    sold_on: date
    variation_id: str
    location_id: str
    units: Decimal
    revenue: Decimal


@dataclass(frozen=True)
class InventoryStockEvent:
    occurred_on: date
    variation_id: str
    location_id: str
    quantity_delta: Decimal


@dataclass(frozen=True)
class StockoutDemandAdjustment:
    zero_stock_days: int
    in_stock_days: int
    observed_units_sold: Decimal
    adjusted_units_sold: Decimal
    estimated_lost_units: Decimal


@dataclass(frozen=True)
class VelocityInventory:
    variation_id: str
    sku: str
    product_name: str
    category: str
    vendor: str
    unit_cost: Decimal | None
    discontinued: bool
    by_store: dict[int, Decimal]
    vendor_id: int | None = None


@dataclass(frozen=True)
class VelocityRow:
    rank: int
    variation_id: str
    sku: str
    product_name: str
    category: str
    vendor: str
    units_sold: Decimal
    sales_revenue: Decimal
    gross_profit_dollars: Decimal | None
    gross_margin_percent: Decimal | None
    average_units_sold_per_day: Decimal
    current_inventory_quantity: Decimal
    inventory_value_at_cost: Decimal | None
    days_of_supply_remaining: Decimal | None
    last_sold_date: date | None
    store_location_breakdown: str
    inventory_health_flag: str
    recommended_reorder_quantity: Decimal
    trend_percent: Decimal | None
    trend_label: str
    previous_units_sold: Decimal
    discontinued: bool
    vendor_id: int | None = None
    store_splits: list['StoreDemandSplit'] | None = None
    adjusted_units_sold: Decimal = ZERO
    estimated_lost_units: Decimal = ZERO
    zero_stock_days: int = 0
    unit_cost: Decimal | None = None


@dataclass(frozen=True)
class StoreDemandSplit:
    store_id: int
    store_name: str
    units_sold: Decimal
    average_units_sold_per_day: Decimal
    target_inventory_quantity: Decimal
    current_inventory_quantity: Decimal
    recommended_purchase_quantity: Decimal
    days_of_supply_remaining: Decimal | None
    adjusted_units_sold: Decimal = ZERO
    estimated_lost_units: Decimal = ZERO
    zero_stock_days: int = 0


@dataclass(frozen=True)
class TransferOpportunity:
    sku: str
    product_name: str
    store_needing_inventory: str
    store_with_excess_inventory: str
    units_sold_at_needing_store: Decimal
    current_inventory_at_needing_store: Decimal
    current_inventory_at_excess_store: Decimal
    suggested_transfer_quantity: Decimal


@dataclass(frozen=True)
class InventoryVelocityReport:
    days: int
    end_date: date
    rows: list[VelocityRow]
    transfers: list[TransferOpportunity]
    sections: dict[str, list]
    stores: list[tuple[int, str]]
    categories: list[str]
    vendors: list[str]


@dataclass(frozen=True)
class StockCoveragePurchaseRow:
    rank: int
    sku: str
    product_name: str
    category: str
    vendor: str
    units_sold: Decimal
    average_units_sold_per_day: Decimal
    target_months: Decimal
    target_days: Decimal
    target_inventory_quantity: Decimal
    current_inventory_quantity: Decimal
    recommended_purchase_quantity: Decimal
    estimated_purchase_cost: Decimal | None
    days_of_supply_remaining: Decimal | None
    store_location_breakdown: str
    vendor_id: int | None = None
    store_splits: list[StoreDemandSplit] | None = None
    store_specific_need_masked: bool = False
    adjusted_units_sold: Decimal = ZERO
    estimated_lost_units: Decimal = ZERO
    zero_stock_days: int = 0


@dataclass(frozen=True)
class StockCoverageVendorSummary:
    vendor: str
    sku_count: int
    purchase_quantity: Decimal
    estimated_purchase_cost: Decimal
    missing_cost_sku_count: int
    vendor_id: int | None = None


@dataclass(frozen=True)
class StockCoveragePurchaseReport:
    days: int
    end_date: date
    target_months: Decimal
    top_n: int
    rows: list[StockCoveragePurchaseRow]
    stores: list[tuple[int, str]]
    vendor_summaries: list[StockCoverageVendorSummary]
    total_estimated_purchase_cost: Decimal
    total_purchase_quantity: Decimal
    missing_cost_sku_count: int

    @property
    def target_days(self) -> Decimal:
        return self.target_months * Decimal('30')


def _money(raw: object) -> Decimal:
    try:
        return (Decimal(str(raw or 0)) / Decimal('100')).quantize(Decimal('0.01'))
    except Exception:
        return Decimal('0.00')


def fetch_sales_data(db: Session, *, start_date: date, end_date: date, store_id: int | None = None) -> list[VelocitySale]:
    query = select(Store.id, Store.square_location_id).where(Store.active.is_(True), Store.square_location_id.is_not(None))
    if store_id is not None:
        query = query.where(Store.id == store_id)
    location_rows = db.execute(query).all()
    location_ids = [str(row.square_location_id) for row in location_rows if row.square_location_id]
    if not location_ids:
        return []
    client = _SquareClient()
    cursor = None
    sales: list[VelocitySale] = []
    while True:
        payload: dict = {
            'location_ids': location_ids,
            'query': {'filter': {'date_time_filter': {'closed_at': {
                'start_at': _to_iso(datetime.combine(start_date, time.min, tzinfo=timezone.utc)),
                'end_at': _to_iso(datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=timezone.utc)),
            }}, 'state_filter': {'states': ['COMPLETED']}}},
            'limit': 500,
        }
        if cursor:
            payload['cursor'] = cursor
        response = client.post('/v2/orders/search', payload)
        for order in response.get('orders', []) or []:
            sold_at = _parse_iso_datetime(order.get('closed_at') or order.get('created_at'))
            if sold_at is None:
                continue
            location_id = str(order.get('location_id') or '')
            for line in order.get('line_items', []) or []:
                variation_id = str(line.get('catalog_object_id') or '').strip()
                if not variation_id:
                    continue
                try:
                    units = Decimal(str(line.get('quantity') or '0'))
                except Exception:
                    units = ZERO
                revenue = _money((line.get('gross_sales_money') or line.get('total_money') or {}).get('amount'))
                sales.append(VelocitySale(sold_at.date(), variation_id, location_id, units, revenue))
        cursor = response.get('cursor')
        if not cursor:
            break
    return sales


def _decimal_quantity(raw: object) -> Decimal:
    try:
        return Decimal(str(raw or '0'))
    except Exception:
        return ZERO


def format_quantity_compact(value: Decimal) -> str:
    if value == value.to_integral_value():
        return str(value.to_integral_value())
    return format(value.normalize(), 'f')


def _inventory_event_date(raw: object) -> date | None:
    parsed = _parse_iso_datetime(raw)
    return parsed.date() if parsed else None


def _inventory_change_events(change: dict) -> list[InventoryStockEvent]:
    change_type = str(change.get('type') or '').upper()
    if change_type == 'ADJUSTMENT':
        adjustment = change.get('adjustment') or {}
        occurred_on = _inventory_event_date(adjustment.get('occurred_at') or change.get('created_at'))
        variation_id = str(adjustment.get('catalog_object_id') or '').strip()
        location_id = str(adjustment.get('location_id') or '').strip()
        quantity = _decimal_quantity(adjustment.get('quantity'))
        if not occurred_on or not variation_id or not location_id or quantity == 0:
            return []
        delta = ZERO
        if str(adjustment.get('to_state') or '').upper() == 'IN_STOCK':
            delta += quantity
        if str(adjustment.get('from_state') or '').upper() == 'IN_STOCK':
            delta -= quantity
        return [InventoryStockEvent(occurred_on, variation_id, location_id, delta)] if delta != 0 else []
    if change_type == 'TRANSFER':
        transfer = change.get('transfer') or {}
        occurred_on = _inventory_event_date(transfer.get('occurred_at') or change.get('created_at'))
        variation_id = str(transfer.get('catalog_object_id') or '').strip()
        quantity = _decimal_quantity(transfer.get('quantity'))
        if not occurred_on or not variation_id or quantity == 0:
            return []
        events: list[InventoryStockEvent] = []
        from_location_id = str(transfer.get('from_location_id') or '').strip()
        to_location_id = str(transfer.get('to_location_id') or '').strip()
        if from_location_id:
            events.append(InventoryStockEvent(occurred_on, variation_id, from_location_id, -quantity))
        if to_location_id:
            events.append(InventoryStockEvent(occurred_on, variation_id, to_location_id, quantity))
        return events
    return []


def fetch_inventory_stock_events(
    db: Session,
    *,
    variation_ids: list[str],
    start_date: date,
    end_date: date,
    store_id: int | None = None,
) -> list[InventoryStockEvent]:
    clean_variation_ids = sorted({str(value).strip() for value in variation_ids if str(value or '').strip()})
    if not clean_variation_ids:
        return []
    query = select(Store.id, Store.square_location_id).where(Store.active.is_(True), Store.square_location_id.is_not(None))
    if store_id is not None:
        query = query.where(Store.id == store_id)
    location_ids = sorted({str(row.square_location_id) for row in db.execute(query).all() if row.square_location_id})
    if not location_ids:
        return []

    client = _SquareClient()
    events: list[InventoryStockEvent] = []
    start_at = _to_iso(datetime.combine(start_date, time.min, tzinfo=timezone.utc))
    end_at = _to_iso(datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=timezone.utc))
    for i in range(0, len(clean_variation_ids), 500):
        chunk = clean_variation_ids[i : i + 500]
        cursor = None
        while True:
            payload: dict = {
                'catalog_object_ids': chunk,
                'location_ids': location_ids,
                'types': ['ADJUSTMENT', 'TRANSFER'],
                'states': ['IN_STOCK', 'SOLD'],
                'updated_after': start_at,
                'updated_before': end_at,
                'limit': 1000,
            }
            if cursor:
                payload['cursor'] = cursor
            response = client.post('/v2/inventory/changes/batch-retrieve', payload)
            for change in response.get('changes', []) or []:
                events.extend(_inventory_change_events(change))
            cursor = response.get('cursor')
            if not cursor:
                break
    return events


def calculate_stockout_adjustments(
    sales: list[VelocitySale],
    inventory: dict[str, VelocityInventory],
    inventory_events: list[InventoryStockEvent],
    *,
    days: int,
    end_date: date,
    store_by_location: dict[str, int],
    max_adjustment_factor: Decimal = Decimal('3'),
) -> dict[tuple[str, int], StockoutDemandAdjustment]:
    start_date = end_date - timedelta(days=days - 1)
    sales_by_store_variation: dict[tuple[str, int], Decimal] = defaultdict(lambda: ZERO)
    for sale in sales:
        sid = store_by_location.get(sale.location_id)
        if sid is not None and start_date <= sale.sold_on <= end_date:
            sales_by_store_variation[(sale.variation_id, sid)] += sale.units

    events_by_key_day: dict[tuple[str, int, date], Decimal] = defaultdict(lambda: ZERO)
    for event in inventory_events:
        sid = store_by_location.get(event.location_id)
        if sid is not None and start_date <= event.occurred_on <= end_date:
            events_by_key_day[(event.variation_id, sid, event.occurred_on)] += event.quantity_delta

    adjustments: dict[tuple[str, int], StockoutDemandAdjustment] = {}
    for variation_id, item in inventory.items():
        for sid, current_on_hand in item.by_store.items():
            qty = current_on_hand
            zero_days = 0
            for offset in range(days):
                current_day = end_date - timedelta(days=offset)
                if qty <= 0:
                    zero_days += 1
                qty -= events_by_key_day[(variation_id, sid, current_day)]
            observed_units = sales_by_store_variation[(variation_id, sid)]
            in_stock_days = max(days - zero_days, 0)
            adjusted_units = observed_units
            if zero_days > 0 and observed_units > 0 and in_stock_days > 0:
                uncapped = (observed_units / Decimal(in_stock_days)) * Decimal(days)
                capped = observed_units * max_adjustment_factor
                adjusted_units = min(uncapped, capped).quantize(Decimal('0.001'))
            estimated_lost = max(ZERO, adjusted_units - observed_units).quantize(Decimal('0.001'))
            adjustments[(variation_id, sid)] = StockoutDemandAdjustment(
                zero_stock_days=zero_days,
                in_stock_days=in_stock_days,
                observed_units_sold=observed_units,
                adjusted_units_sold=adjusted_units,
                estimated_lost_units=estimated_lost,
            )
    return adjustments


def fetch_current_inventory(db: Session, *, store_id: int | None = None) -> tuple[dict[str, VelocityInventory], list[tuple[int, str]], dict[int, str]]:
    stores_query = select(Store.id, Store.name, Store.square_location_id).where(Store.active.is_(True), Store.square_location_id.is_not(None)).order_by(Store.name)
    if store_id is not None:
        stores_query = stores_query.where(Store.id == store_id)
    stores = db.execute(stores_query).all()
    store_list = [(int(row.id), str(row.name)) for row in stores]
    location_by_store = {int(row.id): str(row.square_location_id) for row in stores}
    store_by_location = {location: sid for sid, location in location_by_store.items()}
    catalog, _ = fetch_catalog_variation_maps()
    variation_ids = sorted(catalog)
    stock = fetch_on_hand_by_store_variation(db, variation_ids=variation_ids, store_ids=[sid for sid, _ in store_list])
    vendor_rows = db.execute(
        select(VendorSkuConfig.square_variation_id, VendorSkuConfig.unit_cost, Vendor.name, Vendor.id)
        .join(Vendor, Vendor.id == VendorSkuConfig.vendor_id)
        .where(VendorSkuConfig.active.is_(True), Vendor.active.is_(True))
        .order_by(VendorSkuConfig.is_default_vendor.desc(), VendorSkuConfig.updated_at.desc())
    ).all()
    vendor_by_variation: dict[str, tuple[int | None, str, Decimal | None]] = {}
    for row in vendor_rows:
        vid = str(row.square_variation_id or '').strip()
        if vid and vid not in vendor_by_variation:
            cost = Decimal(str(row.unit_cost)) if row.unit_cost is not None and Decimal(str(row.unit_cost)) > 0 else None
            vendor_by_variation[vid] = (int(row.id), str(row.name or 'Unassigned'), cost)
    result: dict[str, VelocityInventory] = {}
    for vid, meta in catalog.items():
        vendor_id, vendor, configured_cost = vendor_by_variation.get(vid, (None, 'Unassigned', None))
        result[vid] = VelocityInventory(
            variation_id=vid, sku=meta.sku or vid, product_name=' '.join(x for x in (meta.item_name, meta.variation_name) if x).strip(),
            category='Uncategorized', vendor=vendor, unit_cost=configured_cost or meta.first_vendor_unit_cost,
            discontinued=False, by_store={sid: stock.get((sid, vid), ZERO) for sid, _ in store_list}, vendor_id=vendor_id,
        )
    return result, store_list, store_by_location


def calculate_inventory_health(inventory: Decimal, supply: Decimal | None, units: Decimal) -> str:
    if inventory > 0 and units == 0:
        return 'Dead stock'
    if inventory <= 0:
        return 'Out of stock'
    if supply is not None and supply <= 3:
        return 'Critical'
    if supply is not None and supply <= 7:
        return 'Low'
    if supply is not None and supply <= 14:
        return 'Watch'
    return 'Healthy'


def calculate_velocity_metrics(
    sales: list[VelocitySale],
    inventory: dict[str, VelocityInventory],
    *,
    days: int,
    end_date: date,
    store_names: dict[int, str],
    store_by_location: dict[str, int],
    target_days: Decimal | int = 30,
    stockout_adjustments: dict[tuple[str, int], StockoutDemandAdjustment] | None = None,
) -> list[VelocityRow]:
    if days not in TIME_WINDOWS:
        raise ValueError(f'Time window must be one of {TIME_WINDOWS}')
    current_start = end_date - timedelta(days=days - 1)
    previous_start = current_start - timedelta(days=days)
    current: dict[tuple[str, int], Decimal] = defaultdict(lambda: ZERO)
    revenue: dict[str, Decimal] = defaultdict(lambda: ZERO)
    previous: dict[str, Decimal] = defaultdict(lambda: ZERO)
    last_sold: dict[str, date] = {}
    for sale in sales:
        sid = store_by_location.get(sale.location_id)
        if sid is None:
            continue
        if current_start <= sale.sold_on <= end_date:
            current[(sale.variation_id, sid)] += sale.units
            revenue[sale.variation_id] += sale.revenue
        elif previous_start <= sale.sold_on < current_start:
            previous[sale.variation_id] += sale.units
        if sale.units > 0 and (sale.variation_id not in last_sold or sale.sold_on > last_sold[sale.variation_id]):
            last_sold[sale.variation_id] = sale.sold_on
    rows: list[VelocityRow] = []
    for vid, item in inventory.items():
        units = sum((current[(vid, sid)] for sid in item.by_store), ZERO)
        adjusted_units = sum(
            (
                (stockout_adjustments or {}).get(
                    (vid, sid),
                    StockoutDemandAdjustment(0, days, current[(vid, sid)], current[(vid, sid)], ZERO),
                ).adjusted_units_sold
                for sid in item.by_store
            ),
            ZERO,
        )
        estimated_lost_units = max(ZERO, adjusted_units - units).quantize(Decimal('0.001'))
        zero_stock_days = sum(
            ((stockout_adjustments or {}).get((vid, sid), StockoutDemandAdjustment(0, days, ZERO, ZERO, ZERO)).zero_stock_days for sid in item.by_store),
            0,
        )
        previous_units = previous[vid]
        daily = adjusted_units / Decimal(days)
        on_hand = sum(item.by_store.values(), ZERO)
        supply = on_hand / daily if daily > 0 else None
        gross_profit = revenue[vid] - units * item.unit_cost if item.unit_cost is not None else None
        margin = gross_profit / revenue[vid] if gross_profit is not None and revenue[vid] != 0 else None
        trend = (units - previous_units) / previous_units * 100 if previous_units != 0 else None
        trend_label = 'New' if previous_units == 0 and units > 0 else (f'{trend:+.1f}%' if trend is not None else '0.0%')
        breakdown = '; '.join(f'{store_names.get(sid, sid)}: {current[(vid, sid)]:g} sold / {qty:g} on hand' for sid, qty in item.by_store.items())
        reorder = max(ZERO, (Decimal(target_days) * daily - on_hand).to_integral_value(rounding=ROUND_CEILING))
        store_splits: list[StoreDemandSplit] = []
        for sid, store_on_hand in item.by_store.items():
            store_units = current[(vid, sid)]
            adjustment = (stockout_adjustments or {}).get(
                (vid, sid),
                StockoutDemandAdjustment(0, days, store_units, store_units, ZERO),
            )
            store_daily = adjustment.adjusted_units_sold / Decimal(days)
            store_target = (Decimal(target_days) * store_daily).to_integral_value(rounding=ROUND_CEILING)
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
                    adjusted_units_sold=adjustment.adjusted_units_sold,
                    estimated_lost_units=adjustment.estimated_lost_units,
                    zero_stock_days=adjustment.zero_stock_days,
                )
            )
        rows.append(VelocityRow(0, vid, item.sku, item.product_name, item.category, item.vendor, units, revenue[vid], gross_profit, margin, daily, on_hand, on_hand * item.unit_cost if item.unit_cost is not None else None, supply, last_sold.get(vid), breakdown, calculate_inventory_health(on_hand, supply, units), reorder, trend, trend_label, previous_units, item.discontinued, item.vendor_id, store_splits, adjusted_units, estimated_lost_units, zero_stock_days, item.unit_cost))
    rows.sort(key=lambda row: (-row.adjusted_units_sold, -row.units_sold, row.sku.lower()))
    return [VelocityRow(rank=index, **{k: v for k, v in row.__dict__.items() if k != 'rank'}) for index, row in enumerate(rows, 1)]


def calculate_transfer_opportunities(rows: list[VelocityRow], sales: list[VelocitySale], inventory: dict[str, VelocityInventory], *, days: int, end_date: date, store_names: dict[int, str], store_by_location: dict[str, int]) -> list[TransferOpportunity]:
    start = end_date - timedelta(days=days - 1)
    sold: dict[tuple[str, int], Decimal] = defaultdict(lambda: ZERO)
    for sale in sales:
        sid = store_by_location.get(sale.location_id)
        if sid is not None and start <= sale.sold_on <= end_date:
            sold[(sale.variation_id, sid)] += sale.units
    output: list[TransferOpportunity] = []
    for vid, item in inventory.items():
        for needing, needing_stock in item.by_store.items():
            needing_daily = sold[(vid, needing)] / Decimal(days)
            needing_supply = needing_stock / needing_daily if needing_daily > 0 else None
            if needing_stock > 0 and (needing_supply is None or needing_supply > 7):
                continue
            desired = max(ZERO, (Decimal(14) * needing_daily - needing_stock).to_integral_value(rounding=ROUND_CEILING))
            for excess, excess_stock in item.by_store.items():
                if excess == needing or desired <= 0:
                    continue
                excess_daily = sold[(vid, excess)] / Decimal(days)
                excess_supply = excess_stock / excess_daily if excess_daily > 0 else (Decimal('Infinity') if excess_stock > 0 else ZERO)
                if excess_supply <= 30:
                    continue
                transferable = max(ZERO, (excess_stock - Decimal(21) * excess_daily).to_integral_value(rounding=ROUND_FLOOR))
                suggested = min(desired, transferable)
                if suggested > 0:
                    output.append(TransferOpportunity(item.sku, item.product_name, store_names.get(needing, str(needing)), store_names.get(excess, str(excess)), sold[(vid, needing)], needing_stock, excess_stock, suggested))
    return sorted(output, key=lambda row: (-row.suggested_transfer_quantity, row.sku))


def build_inventory_velocity_report(db: Session, *, days: int = 30, end_date: date | None = None, store_id: int | None = None, target_days: Decimal | int = 30) -> InventoryVelocityReport:
    end_date = end_date or date.today()
    inventory, stores, store_by_location = fetch_current_inventory(db, store_id=store_id)
    sales = fetch_sales_data(db, start_date=end_date - timedelta(days=days * 2 - 1), end_date=end_date, store_id=store_id)
    variation_ids = sorted(inventory)
    inventory_events = fetch_inventory_stock_events(
        db,
        variation_ids=variation_ids,
        start_date=end_date - timedelta(days=days - 1),
        end_date=end_date,
        store_id=store_id,
    )
    stockout_adjustments = calculate_stockout_adjustments(
        sales,
        inventory,
        inventory_events,
        days=days,
        end_date=end_date,
        store_by_location=store_by_location,
    )
    store_names = dict(stores)
    rows = calculate_velocity_metrics(sales, inventory, days=days, end_date=end_date, store_names=store_names, store_by_location=store_by_location, target_days=target_days, stockout_adjustments=stockout_adjustments)
    transfers = calculate_transfer_opportunities(rows, sales, inventory, days=days, end_date=end_date, store_names=store_names, store_by_location=store_by_location)
    active = [row for row in rows if row.units_sold > 0 and not row.discontinued]
    sections = {
        'top': active,
        'stockouts': [row for row in active if row.days_of_supply_remaining is not None and row.days_of_supply_remaining <= 7],
        'overloaded': [row for row in rows if row.days_of_supply_remaining is not None and row.days_of_supply_remaining > 90],
        'dead': [row for row in rows if row.inventory_health_flag == 'Dead stock'],
        'growing': sorted(active, key=lambda row: (row.previous_units_sold > 0, -(row.trend_percent or ZERO))),
        'declining': sorted([row for row in active if row.trend_percent is not None and row.trend_percent < 0], key=lambda row: row.trend_percent or ZERO),
        'transfers': transfers,
    }
    return InventoryVelocityReport(days, end_date, rows, transfers, sections, stores, sorted({row.category for row in rows}), sorted({row.vendor for row in rows}))


def build_stock_coverage_purchase_report(
    db: Session,
    *,
    days: int = 30,
    target_months: Decimal = Decimal('3'),
    top_n: int = 50,
    end_date: date | None = None,
    store_id: int | None = None,
) -> StockCoveragePurchaseReport:
    if target_months <= 0:
        raise ValueError('Target months must be greater than zero')
    if top_n <= 0:
        raise ValueError('Ranked SKU count must be greater than zero')
    target_days = target_months * Decimal('30')
    velocity = build_inventory_velocity_report(db, days=days, end_date=end_date, store_id=store_id, target_days=target_days)
    rows: list[StockCoveragePurchaseRow] = []
    ranked_rows = [row for row in velocity.rows if row.units_sold > 0 and not row.discontinued]
    for row in ranked_rows[:top_n]:
        target_inventory = (row.average_units_sold_per_day * target_days).to_integral_value(rounding=ROUND_CEILING)
        aggregate_purchase_quantity = max(ZERO, target_inventory - row.current_inventory_quantity).to_integral_value(rounding=ROUND_CEILING)
        purchase_quantity = sum((split.recommended_purchase_quantity for split in (row.store_splits or [])), ZERO)
        if purchase_quantity <= 0:
            purchase_quantity = aggregate_purchase_quantity
        unit_cost = row.unit_cost
        if unit_cost is None and row.inventory_value_at_cost is not None and row.current_inventory_quantity > 0:
            unit_cost = row.inventory_value_at_cost / row.current_inventory_quantity
        estimated_cost = purchase_quantity * unit_cost if unit_cost is not None else None
        store_need_breakdown = (
            '; '.join(
                f'{split.store_name}: {split.units_sold:g} sold / {split.current_inventory_quantity:g} on hand / {split.recommended_purchase_quantity:g} need'
                + (f' / {split.zero_stock_days} zero days / {format_quantity_compact(split.estimated_lost_units)} est. lost' if split.zero_stock_days > 0 else '')
                for split in (row.store_splits or [])
            )
            or row.store_location_breakdown
        )
        rows.append(
            StockCoveragePurchaseRow(
                rank=row.rank,
                sku=row.sku,
                product_name=row.product_name,
                category=row.category,
                vendor=row.vendor,
                units_sold=row.units_sold,
                average_units_sold_per_day=row.average_units_sold_per_day,
                target_months=target_months,
                target_days=target_days,
                target_inventory_quantity=target_inventory,
                current_inventory_quantity=row.current_inventory_quantity,
                recommended_purchase_quantity=purchase_quantity,
                estimated_purchase_cost=estimated_cost,
                days_of_supply_remaining=row.days_of_supply_remaining,
                store_location_breakdown=store_need_breakdown,
                vendor_id=row.vendor_id,
                store_splits=row.store_splits,
                store_specific_need_masked=aggregate_purchase_quantity < purchase_quantity,
                adjusted_units_sold=row.adjusted_units_sold,
                estimated_lost_units=row.estimated_lost_units,
                zero_stock_days=row.zero_stock_days,
            )
        )
    vendor_summaries, total_quantity, total_cost, missing_cost_count = summarize_stock_coverage_purchase_rows(rows)
    return StockCoveragePurchaseReport(
        velocity.days,
        velocity.end_date,
        target_months,
        top_n,
        rows,
        velocity.stores,
        vendor_summaries,
        total_cost,
        total_quantity,
        missing_cost_count,
    )


def summarize_stock_coverage_purchase_rows(
    rows: list[StockCoveragePurchaseRow],
) -> tuple[list[StockCoverageVendorSummary], Decimal, Decimal, int]:
    vendor_accumulator: dict[tuple[int | None, str], dict[str, Decimal | set[str]]] = defaultdict(lambda: {'skus': set(), 'quantity': ZERO, 'cost': ZERO, 'missing': set()})
    total_cost = ZERO
    total_quantity = ZERO
    missing_cost_skus: set[str] = set()
    for row in rows:
        if row.recommended_purchase_quantity <= 0:
            continue
        vendor_summary = vendor_accumulator[(row.vendor_id, row.vendor)]
        vendor_summary['skus'].add(row.sku)
        vendor_summary['quantity'] += row.recommended_purchase_quantity
        total_quantity += row.recommended_purchase_quantity
        if row.estimated_purchase_cost is None:
            if row.recommended_purchase_quantity > 0:
                vendor_summary['missing'].add(row.sku)
                missing_cost_skus.add(row.sku)
            continue
        vendor_summary['cost'] += row.estimated_purchase_cost
        total_cost += row.estimated_purchase_cost
    vendor_summaries = [
        StockCoverageVendorSummary(
            vendor=vendor,
            sku_count=len(summary['skus']),
            purchase_quantity=summary['quantity'],
            estimated_purchase_cost=summary['cost'],
            missing_cost_sku_count=len(summary['missing']),
            vendor_id=vendor_id,
        )
        for (vendor_id, vendor), summary in vendor_accumulator.items()
    ]
    vendor_summaries.sort(key=lambda row: (-row.estimated_purchase_cost, row.vendor.lower()))
    return vendor_summaries, total_quantity, total_cost, len(missing_cost_skus)


def render_export_report(rows: list[VelocityRow]) -> list[list[str]]:
    header = ['Rank', 'SKU', 'Product name', 'Category', 'Vendor', 'Units sold', 'Stockout-adjusted units', 'Estimated lost sales', 'Zero-stock days', 'Sales revenue', 'Gross profit dollars', 'Gross margin percent', 'Average units sold per day', 'Current inventory quantity', 'Inventory value at cost', 'Days of supply remaining', 'Last sold date', 'Store/location breakdown', 'Inventory health flag', 'Recommended reorder quantity', 'Trend versus previous matching period']
    output = [header]
    for row in rows:
        output.append([str(row.rank), row.sku, row.product_name, row.category, row.vendor, str(row.units_sold), f'{row.adjusted_units_sold:g}', f'{row.estimated_lost_units:g}', str(row.zero_stock_days), f'{row.sales_revenue:.2f}', f'{row.gross_profit_dollars:.2f}' if row.gross_profit_dollars is not None else '', f'{row.gross_margin_percent * 100:.2f}%' if row.gross_margin_percent is not None else '', f'{row.average_units_sold_per_day:.3f}', str(row.current_inventory_quantity), f'{row.inventory_value_at_cost:.2f}' if row.inventory_value_at_cost is not None else '', f'{row.days_of_supply_remaining:.2f}' if row.days_of_supply_remaining is not None else '', row.last_sold_date.isoformat() if row.last_sold_date else '', row.store_location_breakdown, row.inventory_health_flag, str(row.recommended_reorder_quantity), row.trend_label])
    return output


def render_stock_coverage_purchase_export(report: StockCoveragePurchaseReport) -> list[list[str]]:
    output = [
        ['Vendor Purchase Summary'],
        ['Vendor', 'SKU count', 'Recommended purchase quantity', 'Estimated purchase cost', 'SKUs missing cost'],
    ]
    for summary in report.vendor_summaries:
        output.append(
            [
                summary.vendor,
                str(summary.sku_count),
                str(summary.purchase_quantity),
                f'{summary.estimated_purchase_cost:.2f}',
                str(summary.missing_cost_sku_count),
            ]
        )
    output.append(['Total', '', str(report.total_purchase_quantity), f'{report.total_estimated_purchase_cost:.2f}', str(report.missing_cost_sku_count)])
    output.append([])
    header = [
        'Velocity rank',
        'SKU',
        'Product name',
        'Category',
        'Vendor',
        'Units sold in lookback',
        'Stockout-adjusted units',
        'Estimated lost sales',
        'Zero-stock days',
        'Average units sold per day',
        'Target months',
        'Target days',
        'Target inventory quantity',
        'Current inventory quantity',
        'Recommended purchase quantity',
        'Estimated purchase cost',
        'Days of supply remaining',
        'Store/location breakdown',
        'Store-specific issue',
    ]
    output.append(header)
    for row in report.rows:
        output.append(
            [
                str(row.rank),
                row.sku,
                row.product_name,
                row.category,
                row.vendor,
                str(row.units_sold),
                f'{row.adjusted_units_sold:g}',
                f'{row.estimated_lost_units:g}',
                str(row.zero_stock_days),
                f'{row.average_units_sold_per_day:.3f}',
                f'{row.target_months:g}',
                f'{row.target_days:g}',
                str(row.target_inventory_quantity),
                str(row.current_inventory_quantity),
                str(row.recommended_purchase_quantity),
                f'{row.estimated_purchase_cost:.2f}' if row.estimated_purchase_cost is not None else '',
                f'{row.days_of_supply_remaining:.2f}' if row.days_of_supply_remaining is not None else '',
                row.store_location_breakdown,
                'Store need masked by other stores' if row.store_specific_need_masked else '',
            ]
        )
    return output
