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
class VelocityInventory:
    variation_id: str
    sku: str
    product_name: str
    category: str
    vendor: str
    unit_cost: Decimal | None
    discontinued: bool
    by_store: dict[int, Decimal]


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
        select(VendorSkuConfig.square_variation_id, VendorSkuConfig.unit_cost, Vendor.name)
        .join(Vendor, Vendor.id == VendorSkuConfig.vendor_id)
        .where(VendorSkuConfig.active.is_(True), Vendor.active.is_(True))
        .order_by(VendorSkuConfig.is_default_vendor.desc(), VendorSkuConfig.updated_at.desc())
    ).all()
    vendor_by_variation: dict[str, tuple[str, Decimal | None]] = {}
    for row in vendor_rows:
        vid = str(row.square_variation_id or '').strip()
        if vid and vid not in vendor_by_variation:
            cost = Decimal(str(row.unit_cost)) if row.unit_cost is not None and Decimal(str(row.unit_cost)) > 0 else None
            vendor_by_variation[vid] = (str(row.name or 'Unassigned'), cost)
    result: dict[str, VelocityInventory] = {}
    for vid, meta in catalog.items():
        vendor, configured_cost = vendor_by_variation.get(vid, ('Unassigned', None))
        result[vid] = VelocityInventory(
            variation_id=vid, sku=meta.sku or vid, product_name=' '.join(x for x in (meta.item_name, meta.variation_name) if x).strip(),
            category='Uncategorized', vendor=vendor, unit_cost=configured_cost or meta.first_vendor_unit_cost,
            discontinued=False, by_store={sid: stock.get((sid, vid), ZERO) for sid, _ in store_list},
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


def calculate_velocity_metrics(sales: list[VelocitySale], inventory: dict[str, VelocityInventory], *, days: int, end_date: date, store_names: dict[int, str], store_by_location: dict[str, int], target_days: int = 30) -> list[VelocityRow]:
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
        previous_units = previous[vid]
        daily = units / Decimal(days)
        on_hand = sum(item.by_store.values(), ZERO)
        supply = on_hand / daily if daily > 0 else None
        gross_profit = revenue[vid] - units * item.unit_cost if item.unit_cost is not None else None
        margin = gross_profit / revenue[vid] if gross_profit is not None and revenue[vid] != 0 else None
        trend = (units - previous_units) / previous_units * 100 if previous_units != 0 else None
        trend_label = 'New' if previous_units == 0 and units > 0 else (f'{trend:+.1f}%' if trend is not None else '0.0%')
        breakdown = '; '.join(f'{store_names.get(sid, sid)}: {current[(vid, sid)]:g} sold / {qty:g} on hand' for sid, qty in item.by_store.items())
        reorder = max(ZERO, (Decimal(target_days) * daily - on_hand).to_integral_value(rounding=ROUND_CEILING))
        rows.append(VelocityRow(0, vid, item.sku, item.product_name, item.category, item.vendor, units, revenue[vid], gross_profit, margin, daily, on_hand, on_hand * item.unit_cost if item.unit_cost is not None else None, supply, last_sold.get(vid), breakdown, calculate_inventory_health(on_hand, supply, units), reorder, trend, trend_label, previous_units, item.discontinued))
    rows.sort(key=lambda row: (-row.units_sold, row.sku.lower()))
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


def build_inventory_velocity_report(db: Session, *, days: int = 30, end_date: date | None = None, store_id: int | None = None) -> InventoryVelocityReport:
    end_date = end_date or date.today()
    inventory, stores, store_by_location = fetch_current_inventory(db, store_id=store_id)
    sales = fetch_sales_data(db, start_date=end_date - timedelta(days=days * 2 - 1), end_date=end_date, store_id=store_id)
    store_names = dict(stores)
    rows = calculate_velocity_metrics(sales, inventory, days=days, end_date=end_date, store_names=store_names, store_by_location=store_by_location)
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


def render_export_report(rows: list[VelocityRow]) -> list[list[str]]:
    header = ['Rank', 'SKU', 'Product name', 'Category', 'Vendor', 'Units sold', 'Sales revenue', 'Gross profit dollars', 'Gross margin percent', 'Average units sold per day', 'Current inventory quantity', 'Inventory value at cost', 'Days of supply remaining', 'Last sold date', 'Store/location breakdown', 'Inventory health flag', 'Recommended reorder quantity', 'Trend versus previous matching period']
    output = [header]
    for row in rows:
        output.append([str(row.rank), row.sku, row.product_name, row.category, row.vendor, str(row.units_sold), f'{row.sales_revenue:.2f}', f'{row.gross_profit_dollars:.2f}' if row.gross_profit_dollars is not None else '', f'{row.gross_margin_percent * 100:.2f}%' if row.gross_margin_percent is not None else '', f'{row.average_units_sold_per_day:.3f}', str(row.current_inventory_quantity), f'{row.inventory_value_at_cost:.2f}' if row.inventory_value_at_cost is not None else '', f'{row.days_of_supply_remaining:.2f}' if row.days_of_supply_remaining is not None else '', row.last_sold_date.isoformat() if row.last_sold_date else '', row.store_location_breakdown, row.inventory_health_flag, str(row.recommended_reorder_quantity), row.trend_label])
    return output
