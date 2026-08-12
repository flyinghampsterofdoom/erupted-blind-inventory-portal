from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    ConsignmentSaleFact,
    ConsignmentSalesSyncState,
    OrderingProductLifecycle,
    ReportingSavedView,
    Store,
)
from app.services.inventory_velocity_report_service import fetch_current_inventory

ZERO = Decimal(0)
TERM_SEPARATOR = re.compile(r'[,;\n\r]+')
REPORT_TYPES = {'sales_analysis', 'stock_value'}
SALES_GROUPINGS = {'product', 'variation', 'store', 'day', 'week', 'month', 'vendor'}
STOCK_GROUPINGS = {'product', 'variation', 'store', 'vendor'}
DATE_MODES = {'custom', 'last_7_days', 'last_30_days', 'this_month', 'last_month', 'choose_when_run'}


@dataclass(frozen=True)
class SearchableProduct:
    product_name: str
    variation_name: str = ''
    sku: str = ''
    variation_id: str = ''

    @property
    def searchable_text(self) -> str:
        return ' '.join((self.product_name, self.variation_name, self.sku, self.variation_id)).casefold()

    @property
    def identity(self) -> str:
        return self.variation_id or self.sku or f'{self.product_name}\x1f{self.variation_name}'


@dataclass
class MetricBucket:
    units_sold: Decimal = ZERO
    gross_sales: Decimal = ZERO
    discounts: Decimal = ZERO
    net_sales: Decimal = ZERO
    cogs: Decimal = ZERO
    missing_cost_count: int = 0
    sale_count: int = 0

    def add_sale(self, sale: ConsignmentSaleFact) -> None:
        self.units_sold += Decimal(str(sale.quantity_sold or 0))
        self.gross_sales += Decimal(str(sale.gross_sales_amount or 0))
        self.discounts += Decimal(str(sale.discount_amount or 0))
        self.net_sales += (
            Decimal(str(sale.gross_sales_amount or 0))
            - Decimal(str(sale.discount_amount or 0))
        )
        self.sale_count += 1
        if sale.extended_cogs_snapshot is None:
            self.missing_cost_count += 1
        else:
            self.cogs += Decimal(str(sale.extended_cogs_snapshot))


@dataclass(frozen=True)
class ReportResult:
    report_type: str
    columns: tuple[tuple[str, str], ...]
    rows: tuple[dict, ...]
    matched_product_count: int
    sale_count: int
    warnings: tuple[str, ...] = ()
    excluded_products: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReportDefinition:
    key: str
    label: str
    required_inputs: tuple[str, ...]
    available_filters: tuple[str, ...]
    date_mode: str
    grouping_options: tuple[str, ...]
    metrics: tuple[str, ...]
    result_columns: tuple[str, ...]
    permission: str
    export_options: tuple[str, ...] = ()


REPORT_DEFINITIONS = {
    'sales_analysis': ReportDefinition(
        key='sales_analysis', label='Sales Analysis',
        required_inputs=('start_date', 'end_date'),
        available_filters=('stores', 'product_search', 'exclusions'),
        date_mode='range', grouping_options=tuple(sorted(SALES_GROUPINGS)),
        metrics=(
            'units_sold', 'gross_sales', 'discounts', 'net_sales',
            'cogs', 'gross_profit', 'gross_margin',
        ),
        result_columns=(
            'group', 'units_sold', 'gross_sales', 'discounts', 'net_sales',
            'cogs', 'gross_profit', 'gross_margin',
        ),
        permission='reports.workbench.view',
    ),
    'stock_value': ReportDefinition(
        key='stock_value', label='Stock Value', required_inputs=(),
        available_filters=(
            'stores', 'product_search', 'exclusions', 'vendor', 'lifecycle',
        ),
        date_mode='current_only', grouping_options=tuple(sorted(STOCK_GROUPINGS)),
        metrics=('quantity_on_hand', 'unit_cost', 'inventory_value'),
        result_columns=(
            'group', 'product', 'sku', 'store', 'vendor', 'lifecycle',
            'quantity_on_hand', 'unit_cost', 'inventory_value',
        ),
        permission='reports.workbench.view',
    ),
}


def parse_search_terms(raw_values: str | Iterable[str]) -> list[str]:
    values = [raw_values] if isinstance(raw_values, str) else list(raw_values)
    terms: list[str] = []
    seen: set[str] = set()
    for value in values:
        for raw_term in TERM_SEPARATOR.split(str(value or '')):
            term = raw_term.strip()
            key = term.casefold()
            if term and key not in seen:
                seen.add(key)
                terms.append(term)
    return terms


def product_matches(
    product: SearchableProduct,
    *,
    include_terms: Iterable[str] = (),
    exclude_terms: Iterable[str] = (),
    match_mode: str = 'any',
) -> bool:
    text = product.searchable_text
    includes = [term.casefold() for term in include_terms if str(term).strip()]
    excludes = [term.casefold() for term in exclude_terms if str(term).strip()]
    included = not includes or (
        all(term in text for term in includes)
        if match_mode == 'all'
        else any(term in text for term in includes)
    )
    return included and not any(term in text for term in excludes)


def _sales_group_key(sale: ConsignmentSaleFact, store_name: str, grouping: str) -> tuple[str, str]:
    product = str(sale.product_name_snapshot or 'Unknown product')
    variation = str(sale.variation_name_snapshot or 'Default')
    if grouping == 'product':
        return product, product
    if grouping == 'variation':
        key = str(sale.square_variation_id or sale.sku_snapshot or f'{product}:{variation}')
        return key, f'{product} — {variation}'
    if grouping == 'store':
        return str(sale.store_id or sale.square_location_id), store_name
    if grouping == 'day':
        return sale.business_date.isoformat(), sale.business_date.strftime('%b %-d, %Y')
    if grouping == 'week':
        week = sale.business_date - timedelta(days=sale.business_date.weekday())
        return week.isoformat(), f'Week of {week.strftime("%b %-d, %Y")}'
    if grouping == 'month':
        month = sale.business_date.replace(day=1)
        return month.isoformat(), month.strftime('%B %Y')
    vendor = str(sale.vendor_name_snapshot or 'Unassigned')
    return vendor.casefold(), vendor


def _metric_row(label: str, bucket: MetricBucket) -> dict:
    cogs = None if bucket.missing_cost_count else bucket.cogs
    gross_profit = None if cogs is None else bucket.net_sales - cogs
    margin = None if gross_profit is None or bucket.net_sales == 0 else gross_profit / bucket.net_sales
    return {
        'group': label, 'units_sold': bucket.units_sold, 'gross_sales': bucket.gross_sales,
        'discounts': bucket.discounts, 'net_sales': bucket.net_sales, 'cogs': cogs,
        'gross_profit': gross_profit, 'gross_margin': margin,
        'sale_count': bucket.sale_count, 'missing_cost_count': bucket.missing_cost_count,
    }


def run_sales_analysis(
    db: Session,
    *,
    start_date: date,
    end_date: date,
    store_ids: Iterable[int] = (),
    include_terms: Iterable[str] = (),
    exclude_terms: Iterable[str] = (),
    match_mode: str = 'any',
    grouping: str = 'product',
    sort: str = 'net_sales_desc',
) -> ReportResult:
    if end_date < start_date:
        raise ValueError('End date must be on or after start date.')
    if grouping not in SALES_GROUPINGS:
        raise ValueError('Unsupported Sales Analysis grouping.')
    if match_mode not in {'any', 'all'}:
        raise ValueError('Search match mode must be any or all.')
    selected_stores = sorted({int(value) for value in store_ids})
    query = select(ConsignmentSaleFact).where(
        ConsignmentSaleFact.business_date >= start_date,
        ConsignmentSaleFact.business_date <= end_date,
    )
    if selected_stores:
        query = query.where(ConsignmentSaleFact.store_id.in_(selected_stores))
    sales = db.scalars(query.order_by(ConsignmentSaleFact.business_date, ConsignmentSaleFact.id)).all()
    store_names = {
        int(row.id): str(row.name)
        for row in db.execute(select(Store.id, Store.name)).all()
    }
    include = parse_search_terms(list(include_terms))
    exclude = parse_search_terms(list(exclude_terms))
    buckets: dict[str, tuple[str, MetricBucket]] = {}
    matched_products: set[str] = set()
    excluded_products: set[str] = set()
    matched_sales = 0
    for sale in sales:
        product = SearchableProduct(
            product_name=str(sale.product_name_snapshot or ''),
            variation_name=str(sale.variation_name_snapshot or ''),
            sku=str(sale.sku_snapshot or ''),
            variation_id=str(sale.square_variation_id or ''),
        )
        if not product_matches(product, include_terms=include, match_mode=match_mode):
            continue
        if any(term.casefold() in product.searchable_text for term in exclude):
            excluded_products.add(' — '.join(filter(None, (product.product_name, product.variation_name))))
            continue
        matched_products.add(
            str(sale.square_product_id or sale.product_name_snapshot or product.identity).casefold()
        )
        matched_sales += 1
        key, label = _sales_group_key(
            sale,
            store_names.get(int(sale.store_id), str(sale.square_location_id))
            if sale.store_id else str(sale.square_location_id),
            grouping,
        )
        if key not in buckets:
            buckets[key] = (label, MetricBucket())
        buckets[key][1].add_sale(sale)
    rows = [_metric_row(label, bucket) for label, bucket in buckets.values()]
    sorters = {
        'net_sales_desc': lambda row: (-row['net_sales'], row['group'].casefold()),
        'units_desc': lambda row: (-row['units_sold'], row['group'].casefold()),
        'gross_profit_desc': lambda row: (
            row['gross_profit'] is None, -(row['gross_profit'] or ZERO), row['group'].casefold(),
        ),
        'name_asc': lambda row: row['group'].casefold(),
    }
    rows.sort(key=sorters.get(sort, sorters['net_sales_desc']))
    missing = sum(int(row['missing_cost_count']) for row in rows)
    warnings: tuple[str, ...] = ()
    if missing:
        warnings += (
            f'{missing} matched sale line(s) have no authoritative cost snapshot; '
            'COGS, gross profit, and margin are shown as unknown for affected groups.',
        )
    sync_state = db.get(ConsignmentSalesSyncState, 1)
    required_through = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=timezone.utc)
    synchronized_through = sync_state.last_successful_through_at if sync_state else None
    if synchronized_through is not None and synchronized_through.tzinfo is None:
        synchronized_through = synchronized_through.replace(tzinfo=timezone.utc)
    if (
        sync_state is None or sync_state.last_result != 'COMPLETE'
        or synchronized_through is None or synchronized_through < required_through
    ):
        warnings += (
            'The local Square sales source is not confirmed complete through the selected end date; '
            'results may be partial until sales synchronization completes.',
        )
    return ReportResult(
        report_type='sales_analysis',
        columns=(
            ('group', grouping.title()), ('units_sold', 'Units sold'),
            ('gross_sales', 'Gross sales'), ('discounts', 'Discounts'),
            ('net_sales', 'Net sales'), ('cogs', 'COGS'),
            ('gross_profit', 'Gross profit'), ('gross_margin', 'Gross margin'),
        ),
        rows=tuple(rows), matched_product_count=len(matched_products), sale_count=matched_sales,
        warnings=warnings, excluded_products=tuple(sorted(excluded_products, key=str.casefold)),
    )


def run_stock_value(
    db: Session,
    *,
    store_ids: Iterable[int] = (),
    include_terms: Iterable[str] = (),
    exclude_terms: Iterable[str] = (),
    match_mode: str = 'any',
    grouping: str = 'variation',
    vendor: str = '',
    lifecycle: str = '',
    sort: str = 'inventory_value_desc',
) -> ReportResult:
    if grouping not in STOCK_GROUPINGS:
        raise ValueError('Unsupported Stock Value grouping.')
    selected_stores = {int(value) for value in store_ids}
    inventory, stores, _ = fetch_current_inventory(db)
    store_names = dict(stores)
    lifecycle_by_variation = _lifecycle_by_variation(db)
    include = parse_search_terms(list(include_terms))
    exclude = parse_search_terms(list(exclude_terms))
    buckets: dict[str, dict] = {}
    matched_products: set[str] = set()
    excluded_products: set[str] = set()
    for item in inventory.values():
        product = SearchableProduct(item.product_name, '', item.sku, item.variation_id)
        if not product_matches(product, include_terms=include, match_mode=match_mode):
            continue
        if any(term.casefold() in product.searchable_text for term in exclude):
            excluded_products.add(product.product_name)
            continue
        item_lifecycle = lifecycle_by_variation.get(item.variation_id, 'ACTIVE')
        if vendor and item.vendor.casefold() != vendor.casefold():
            continue
        if lifecycle and item_lifecycle != lifecycle:
            continue
        matched_products.add(product.identity)
        for store_id, quantity in item.by_store.items():
            if selected_stores and store_id not in selected_stores:
                continue
            if grouping == 'product':
                key, label = item.product_name.casefold(), item.product_name
            elif grouping == 'variation':
                key, label = item.variation_id, item.product_name
            elif grouping == 'store':
                key, label = str(store_id), store_names.get(store_id, str(store_id))
            else:
                key, label = item.vendor.casefold(), item.vendor
            row = buckets.setdefault(key, {
                'group': label, 'product': item.product_name, 'variation': item.variation_id,
                'sku': item.sku, 'store': store_names.get(store_id, str(store_id)),
                'vendor': item.vendor, 'lifecycle': item_lifecycle, 'quantity_on_hand': ZERO,
                'inventory_value': ZERO, 'missing_cost_count': 0, 'known_cost_quantity': ZERO,
            })
            row['quantity_on_hand'] += quantity
            if item.unit_cost is None:
                if quantity != 0:
                    row['missing_cost_count'] += 1
            else:
                row['inventory_value'] += quantity * item.unit_cost
                row['known_cost_quantity'] += quantity
    rows: list[dict] = []
    for row in buckets.values():
        row['unit_cost'] = (
            row['inventory_value'] / row['known_cost_quantity']
            if not row['missing_cost_count'] and row['known_cost_quantity'] != 0 else None
        )
        if row['missing_cost_count']:
            row['inventory_value'] = None
        rows.append(row)
    sorters = {
        'inventory_value_desc': lambda row: (
            row['inventory_value'] is None,
            -(row['inventory_value'] or ZERO), row['group'].casefold(),
        ),
        'quantity_desc': lambda row: (-row['quantity_on_hand'], row['group'].casefold()),
        'name_asc': lambda row: row['group'].casefold(),
    }
    rows.sort(key=sorters.get(sort, sorters['inventory_value_desc']))
    missing = sum(int(row['missing_cost_count']) for row in rows)
    warnings = (
        (f'{missing} inventory position(s) have no authoritative cost basis; '
         'their unit cost and inventory value are shown as unknown.',)
        if missing else ()
    )
    return ReportResult(
        report_type='stock_value',
        columns=(
            ('group', grouping.title()), ('product', 'Product'), ('sku', 'SKU'),
            ('store', 'Store'), ('vendor', 'Vendor'), ('lifecycle', 'Lifecycle'),
            ('quantity_on_hand', 'Quantity on hand'),
            ('unit_cost', 'Unit cost / cost basis'), ('inventory_value', 'Inventory value'),
        ),
        rows=tuple(rows), matched_product_count=len(matched_products), sale_count=0,
        warnings=warnings, excluded_products=tuple(sorted(excluded_products, key=str.casefold)),
    )


def _lifecycle_by_variation(db: Session) -> dict[str, str]:
    return {
        str(row.square_variation_id): str(row.status)
        for row in db.scalars(select(OrderingProductLifecycle)).all()
    }


def list_saved_views(db: Session, *, principal_id: int) -> list[ReportingSavedView]:
    return list(db.scalars(select(ReportingSavedView).where(
        ReportingSavedView.principal_id == principal_id
    ).order_by(ReportingSavedView.name, ReportingSavedView.id)).all())


def get_saved_view(db: Session, *, principal_id: int, view_id: int) -> ReportingSavedView:
    row = db.scalar(select(ReportingSavedView).where(
        ReportingSavedView.id == view_id,
        ReportingSavedView.principal_id == principal_id,
    ))
    if row is None:
        raise LookupError('Saved View not found.')
    return row


def save_view(
    db: Session, *, principal_id: int, name: str, report_type: str,
    configuration: dict, view_id: int | None = None,
) -> ReportingSavedView:
    clean_name = str(name or '').strip()
    if not clean_name or len(clean_name) > 120:
        raise ValueError('Saved View name is required and must be 120 characters or fewer.')
    if report_type not in REPORT_TYPES:
        raise ValueError('Unknown report type.')
    duplicate = db.scalar(select(ReportingSavedView.id).where(
        ReportingSavedView.principal_id == principal_id,
        ReportingSavedView.name == clean_name,
        ReportingSavedView.id != (view_id or 0),
    ))
    if duplicate is not None:
        raise ValueError('You already have a Saved View with that name.')
    row = (
        get_saved_view(db, principal_id=principal_id, view_id=view_id)
        if view_id else ReportingSavedView(
            principal_id=principal_id, name=clean_name,
            report_type=report_type, configuration={},
        )
    )
    row.name = clean_name
    row.report_type = report_type
    row.configuration = dict(configuration)
    db.add(row)
    db.flush()
    return row


def delete_saved_view(db: Session, *, principal_id: int, view_id: int) -> None:
    row = get_saved_view(db, principal_id=principal_id, view_id=view_id)
    db.delete(row)
    db.flush()


def resolve_relative_dates(mode: str, *, today: date) -> tuple[date, date] | None:
    if mode in {'custom', 'choose_when_run'}:
        return None
    if mode == 'last_7_days':
        return today - timedelta(days=6), today
    if mode == 'last_30_days':
        return today - timedelta(days=29), today
    if mode == 'this_month':
        return today.replace(day=1), today
    if mode == 'last_month':
        end = today.replace(day=1) - timedelta(days=1)
        return end.replace(day=1), end
    raise ValueError('Unknown relative date mode.')
