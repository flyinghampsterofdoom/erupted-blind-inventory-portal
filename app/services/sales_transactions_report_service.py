from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if TYPE_CHECKING:
    from sqlalchemy.orm import Session
    from app.models import Vendor


def _to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _money_from_cents(raw_amount: object) -> Decimal:
    try:
        return (Decimal(str(raw_amount)) / Decimal('100')).quantize(Decimal('0.01'))
    except Exception:
        return Decimal('0.00')


def _decimal_or_zero(raw_value: object) -> Decimal:
    try:
        return Decimal(str(raw_value))
    except Exception:
        return Decimal('0')


def _parse_iso_datetime(raw: object) -> datetime | None:
    text = str(raw or '').strip()
    if not text:
        return None
    if text.endswith('Z'):
        text = f'{text[:-1]}+00:00'
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


@dataclass(frozen=True)
class SalesReportLocation:
    id: str
    name: str
    status: str = ''
    created_at: datetime | None = None
    timezone_name: str = 'UTC'


@dataclass(frozen=True)
class SalesReportLineItem:
    name: str
    unit_price: Decimal


@dataclass(frozen=True)
class SalesReportTransactionRow:
    transaction_datetime: datetime | None
    transaction_date: date | None
    location_id: str
    location_name: str
    order_id: str
    line_items: list[SalesReportLineItem]
    discounts: Decimal
    tips: Decimal
    sales_tax: Decimal
    total_paid: Decimal
    subtotal_before_tax_and_tips: Decimal


@dataclass(frozen=True)
class SalesTransactionsReportResult:
    start_date: date
    end_date: date
    selected_location_ids: list[str]
    rows: list[SalesReportTransactionRow]


@dataclass(frozen=True)
class GrossSalesByStoreMonthRow:
    month_start: date
    month_label: str
    gross_sales_by_location: dict[str, Decimal]
    order_count_by_location: dict[str, int]
    total_gross_sales: Decimal
    total_order_count: int


@dataclass(frozen=True)
class GrossSalesByStoreReportResult:
    start_date: date
    end_date: date
    selected_location_ids: list[str]
    locations: list[SalesReportLocation]
    month_rows: list[GrossSalesByStoreMonthRow]
    totals_by_location: dict[str, Decimal]
    order_counts_by_location: dict[str, int]
    grand_total_gross_sales: Decimal
    grand_total_orders: int


@dataclass(frozen=True)
class EmployeeSalesReportRow:
    team_member_id: str
    employee_name: str
    location_names: list[str]
    transaction_count: int
    gross_sales: Decimal
    net_sales: Decimal
    discounts: Decimal
    tips: Decimal
    sales_tax: Decimal
    total_paid: Decimal
    average_gross_per_transaction: Decimal
    average_net_per_transaction: Decimal


@dataclass(frozen=True)
class EmployeeSalesReportResult:
    start_date: date
    end_date: date
    selected_location_ids: list[str]
    locations: list[SalesReportLocation]
    rows: list[EmployeeSalesReportRow]
    total_transaction_count: int
    total_gross_sales: Decimal
    total_net_sales: Decimal
    total_discounts: Decimal
    total_tips: Decimal
    total_sales_tax: Decimal
    total_paid: Decimal
    average_gross_per_transaction: Decimal
    average_net_per_transaction: Decimal
    unattributed_transaction_count: int


@dataclass(frozen=True)
class SalesByVendorReportRow:
    sku: str
    variation_id: str
    item_name: str
    variation_name: str
    units_sold: Decimal
    line_item_count: int
    order_count: int
    gross_sales: Decimal
    discounts: Decimal
    net_sales: Decimal
    average_net_per_unit: Decimal


@dataclass(frozen=True)
class SalesByVendorReportResult:
    start_date: date
    end_date: date
    vendor_id: int
    vendor_name: str
    selected_location_ids: list[str]
    locations: list[SalesReportLocation]
    mapped_variation_count: int
    rows: list[SalesByVendorReportRow]
    total_units_sold: Decimal
    total_line_item_count: int
    total_order_count: int
    total_gross_sales: Decimal
    total_discounts: Decimal
    total_net_sales: Decimal
    average_net_per_unit: Decimal


@dataclass(frozen=True)
class _VendorSkuMapping:
    sku: str
    variation_id: str


class _SquareClient:
    def __init__(self) -> None:
        from app.config import settings

        if not settings.square_access_token:
            raise RuntimeError('SQUARE_ACCESS_TOKEN is required')
        base_url = settings.square_api_base_url.rstrip('/')
        if base_url.endswith('/v2'):
            base_url = base_url[:-3]
        self.base_url = base_url
        self.timeout_seconds = settings.square_timeout_seconds
        self.headers = {
            'Authorization': f'Bearer {settings.square_access_token}',
            'Content-Type': 'application/json',
        }
        if settings.square_api_version:
            self.headers['Square-Version'] = settings.square_api_version

    def get(self, path: str, *, cursor: str | None = None, query: dict[str, object] | None = None) -> dict:
        url = f'{self.base_url}{path}'
        params = dict(query or {})
        if cursor:
            params['cursor'] = cursor
        if params:
            joiner = '&' if '?' in url else '?'
            url = f'{url}{joiner}{urlencode(params)}'
        req = Request(
            url=url,
            headers=self.headers,
            method='GET',
        )
        try:
            with urlopen(req, timeout=self.timeout_seconds) as response:
                data = json.loads(response.read().decode('utf-8'))
        except HTTPError as exc:
            body = exc.read().decode('utf-8', errors='ignore') if exc.fp else ''
            raise RuntimeError(f'Square API error {exc.code} for {path}: {body}') from exc
        except URLError as exc:
            raise RuntimeError(f'Square API network error for {path}: {exc.reason}') from exc

        if data.get('errors'):
            raise RuntimeError(f"Square API returned errors on {path}: {data['errors']}")
        return data

    def post(self, path: str, payload: dict) -> dict:
        req = Request(
            url=f'{self.base_url}{path}',
            data=json.dumps(payload).encode('utf-8'),
            headers=self.headers,
            method='POST',
        )
        try:
            with urlopen(req, timeout=self.timeout_seconds) as response:
                data = json.loads(response.read().decode('utf-8'))
        except HTTPError as exc:
            body = exc.read().decode('utf-8', errors='ignore') if exc.fp else ''
            raise RuntimeError(f'Square API error {exc.code} for {path}: {body}') from exc
        except URLError as exc:
            raise RuntimeError(f'Square API network error for {path}: {exc.reason}') from exc

        if data.get('errors'):
            raise RuntimeError(f"Square API returned errors on {path}: {data['errors']}")
        return data


def list_square_locations_for_reports() -> list[SalesReportLocation]:
    client = _SquareClient()
    rows: list[SalesReportLocation] = []
    cursor: str | None = None
    while True:
        response = client.get('/v2/locations', cursor=cursor)
        for location in response.get('locations', []) or []:
            location_id = str(location.get('id') or '').strip()
            if not location_id:
                continue
            location_name = str(location.get('name') or '').strip() or location_id
            rows.append(
                SalesReportLocation(
                    id=location_id,
                    name=location_name,
                    status=str(location.get('status') or '').strip().upper(),
                    created_at=_parse_iso_datetime(location.get('created_at')),
                    timezone_name=str(location.get('timezone') or '').strip() or 'UTC',
                )
            )
        cursor = response.get('cursor')
        if not cursor:
            break

    deduped: dict[str, SalesReportLocation] = {}
    for row in rows:
        deduped[row.id] = row
    return sorted(deduped.values(), key=lambda row: row.name.lower())


def build_sales_transactions_report(
    *,
    start_date: date,
    end_date: date,
    selected_location_ids: list[str] | None = None,
) -> SalesTransactionsReportResult:
    if end_date < start_date:
        raise ValueError('End date must be on or after start date')

    locations = list_square_locations_for_reports()
    if not locations:
        raise RuntimeError('No Square locations were found for this account')

    valid_location_ids = {location.id for location in locations}
    requested_location_ids = [str(value).strip() for value in (selected_location_ids or []) if str(value).strip()]
    if requested_location_ids:
        location_ids = [location_id for location_id in requested_location_ids if location_id in valid_location_ids]
        if not location_ids:
            raise ValueError('Selected location filter is invalid')
    else:
        location_ids = [location.id for location in locations]

    location_name_by_id = {location.id: location.name for location in locations}

    start_at = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
    end_at = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=timezone.utc)

    client = _SquareClient()
    rows: list[SalesReportTransactionRow] = []
    cursor: str | None = None
    while True:
        payload: dict[str, object] = {
            'location_ids': location_ids,
            'query': {
                'filter': {
                    'date_time_filter': {
                        'closed_at': {
                            'start_at': _to_iso(start_at),
                            'end_at': _to_iso(end_at),
                        }
                    },
                    'state_filter': {'states': ['COMPLETED']},
                },
                'sort': {
                    'sort_field': 'CLOSED_AT',
                    'sort_order': 'ASC',
                },
            },
            'limit': 500,
        }
        if cursor:
            payload['cursor'] = cursor

        response = client.post('/v2/orders/search', payload)
        for order in response.get('orders', []) or []:
            location_id = str(order.get('location_id') or '').strip()
            location_name = location_name_by_id.get(location_id) or location_id or 'Unknown Location'
            transaction_dt = _parse_iso_datetime(order.get('closed_at') or order.get('created_at'))
            line_items: list[SalesReportLineItem] = []
            for line in order.get('line_items', []) or []:
                line_name = str(line.get('name') or '').strip() or 'Unnamed Item'
                unit_price = _money_from_cents((line.get('base_price_money') or {}).get('amount'))
                line_items.append(SalesReportLineItem(name=line_name, unit_price=unit_price))

            total_paid = _money_from_cents((order.get('total_money') or {}).get('amount'))
            sales_tax = _money_from_cents((order.get('total_tax_money') or {}).get('amount'))
            tips = _money_from_cents((order.get('total_tip_money') or {}).get('amount'))
            subtotal_before_tax_and_tips = (total_paid - sales_tax - tips).quantize(Decimal('0.01'))

            rows.append(
                SalesReportTransactionRow(
                    transaction_datetime=transaction_dt,
                    transaction_date=transaction_dt.date() if transaction_dt else None,
                    location_id=location_id,
                    location_name=location_name,
                    order_id=str(order.get('id') or ''),
                    line_items=line_items,
                    discounts=_money_from_cents((order.get('total_discount_money') or {}).get('amount')),
                    tips=tips,
                    sales_tax=sales_tax,
                    total_paid=total_paid,
                    subtotal_before_tax_and_tips=subtotal_before_tax_and_tips,
                )
            )

        cursor = response.get('cursor')
        if not cursor:
            break

    return SalesTransactionsReportResult(
        start_date=start_date,
        end_date=end_date,
        selected_location_ids=location_ids,
        rows=rows,
    )


def _month_starts_between(start_date: date, end_date: date) -> list[date]:
    current = date(start_date.year, start_date.month, 1)
    final = date(end_date.year, end_date.month, 1)
    months: list[date] = []
    while current <= final:
        months.append(current)
        if current.month == 12:
            current = date(current.year + 1, 1, 1)
        else:
            current = date(current.year, current.month + 1, 1)
    return months


def build_gross_sales_by_store_report(
    *,
    start_date: date,
    end_date: date,
    selected_location_ids: list[str] | None = None,
) -> GrossSalesByStoreReportResult:
    if end_date < start_date:
        raise ValueError('End date must be on or after start date')

    all_locations = list_square_locations_for_reports()
    if not all_locations:
        raise RuntimeError('No Square locations were found for this account')

    valid_location_ids = {location.id for location in all_locations}
    requested_location_ids = [str(value).strip() for value in (selected_location_ids or []) if str(value).strip()]
    if requested_location_ids:
        fetch_location_ids = [location_id for location_id in requested_location_ids if location_id in valid_location_ids]
        if not fetch_location_ids:
            raise ValueError('Selected location filter is invalid')
    else:
        fetch_location_ids = [location.id for location in all_locations]

    location_by_id = {location.id: location for location in all_locations}
    start_at = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
    end_at = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=timezone.utc)

    gross_sales_by_cell: dict[tuple[date, str], Decimal] = {}
    order_count_by_cell: dict[tuple[date, str], int] = {}
    locations_with_orders: set[str] = set()

    client = _SquareClient()
    cursor: str | None = None
    while True:
        payload: dict[str, object] = {
            'location_ids': fetch_location_ids,
            'query': {
                'filter': {
                    'date_time_filter': {
                        'closed_at': {
                            'start_at': _to_iso(start_at),
                            'end_at': _to_iso(end_at),
                        }
                    },
                    'state_filter': {'states': ['COMPLETED']},
                },
                'sort': {
                    'sort_field': 'CLOSED_AT',
                    'sort_order': 'ASC',
                },
            },
            'limit': 500,
        }
        if cursor:
            payload['cursor'] = cursor

        response = client.post('/v2/orders/search', payload)
        for order in response.get('orders', []) or []:
            location_id = str(order.get('location_id') or '').strip()
            if location_id not in valid_location_ids:
                continue
            transaction_dt = _parse_iso_datetime(order.get('closed_at') or order.get('created_at'))
            if transaction_dt is None:
                continue
            transaction_date = transaction_dt.date()
            if transaction_date < start_date or transaction_date > end_date:
                continue

            month_start = date(transaction_date.year, transaction_date.month, 1)
            total_paid = _money_from_cents((order.get('total_money') or {}).get('amount'))
            sales_tax = _money_from_cents((order.get('total_tax_money') or {}).get('amount'))
            tips = _money_from_cents((order.get('total_tip_money') or {}).get('amount'))
            gross_sales = (total_paid - sales_tax - tips).quantize(Decimal('0.01'))

            key = (month_start, location_id)
            gross_sales_by_cell[key] = (gross_sales_by_cell.get(key) or Decimal('0.00')) + gross_sales
            order_count_by_cell[key] = (order_count_by_cell.get(key) or 0) + 1
            locations_with_orders.add(location_id)

        cursor = response.get('cursor')
        if not cursor:
            break

    if requested_location_ids:
        included_location_ids = fetch_location_ids
    else:
        included_location_ids = []
        for location in all_locations:
            is_active = location.status == 'ACTIVE'
            existed_by_end = location.created_at is None or location.created_at.date() <= end_date
            if location.id in locations_with_orders or (is_active and existed_by_end):
                included_location_ids.append(location.id)

    if not included_location_ids:
        raise RuntimeError('No Square locations matched this date range and location selection')

    included_locations = sorted(
        [location_by_id[location_id] for location_id in included_location_ids if location_id in location_by_id],
        key=lambda row: row.name.lower(),
    )

    months = _month_starts_between(start_date, end_date)
    month_rows: list[GrossSalesByStoreMonthRow] = []
    totals_by_location: dict[str, Decimal] = {location.id: Decimal('0.00') for location in included_locations}
    order_counts_by_location: dict[str, int] = {location.id: 0 for location in included_locations}
    grand_total_gross_sales = Decimal('0.00')
    grand_total_orders = 0

    for month_start in months:
        gross_sales_for_month: dict[str, Decimal] = {}
        order_counts_for_month: dict[str, int] = {}
        month_total = Decimal('0.00')
        month_order_total = 0
        for location in included_locations:
            key = (month_start, location.id)
            gross_sales_value = (gross_sales_by_cell.get(key) or Decimal('0.00')).quantize(Decimal('0.01'))
            order_count_value = int(order_count_by_cell.get(key) or 0)
            gross_sales_for_month[location.id] = gross_sales_value
            order_counts_for_month[location.id] = order_count_value
            month_total += gross_sales_value
            month_order_total += order_count_value
            totals_by_location[location.id] += gross_sales_value
            order_counts_by_location[location.id] += order_count_value

        month_total = month_total.quantize(Decimal('0.01'))
        grand_total_gross_sales += month_total
        grand_total_orders += month_order_total
        month_rows.append(
            GrossSalesByStoreMonthRow(
                month_start=month_start,
                month_label=month_start.strftime('%Y-%m'),
                gross_sales_by_location=gross_sales_for_month,
                order_count_by_location=order_counts_for_month,
                total_gross_sales=month_total,
                total_order_count=month_order_total,
            )
        )

    for location_id, total in list(totals_by_location.items()):
        totals_by_location[location_id] = total.quantize(Decimal('0.01'))

    return GrossSalesByStoreReportResult(
        start_date=start_date,
        end_date=end_date,
        selected_location_ids=[location.id for location in included_locations],
        locations=included_locations,
        month_rows=month_rows,
        totals_by_location=totals_by_location,
        order_counts_by_location=order_counts_by_location,
        grand_total_gross_sales=grand_total_gross_sales.quantize(Decimal('0.01')),
        grand_total_orders=grand_total_orders,
    )


def _vendor_report_mappings(db: Session, *, vendor_id: int) -> tuple[Vendor, dict[str, _VendorSkuMapping]]:
    from sqlalchemy import select

    from app.models import Vendor, VendorSkuConfig

    vendor = db.execute(
        select(Vendor).where(
            Vendor.id == vendor_id,
            Vendor.active.is_(True),
        )
    ).scalar_one_or_none()
    if vendor is None:
        raise ValueError('Vendor not found')

    rows = db.execute(
        select(
            VendorSkuConfig.sku,
            VendorSkuConfig.square_variation_id,
            VendorSkuConfig.is_default_vendor,
            VendorSkuConfig.updated_at,
            VendorSkuConfig.id,
        )
        .where(
            VendorSkuConfig.vendor_id == vendor_id,
            VendorSkuConfig.active.is_(True),
            VendorSkuConfig.square_variation_id.is_not(None),
        )
        .order_by(
            VendorSkuConfig.is_default_vendor.desc(),
            VendorSkuConfig.updated_at.desc(),
            VendorSkuConfig.id.desc(),
        )
    ).all()

    mappings: dict[str, _VendorSkuMapping] = {}
    for row in rows:
        variation_id = str(row.square_variation_id or '').strip()
        sku = str(row.sku or '').strip()
        if not variation_id or variation_id in mappings:
            continue
        mappings[variation_id] = _VendorSkuMapping(
            sku=sku or variation_id,
            variation_id=variation_id,
        )
    return vendor, mappings


def _average_money_per_unit(total: Decimal, units: Decimal) -> Decimal:
    if units <= 0:
        return Decimal('0.00')
    return (total / units).quantize(Decimal('0.01'))


def build_sales_by_vendor_report(
    db: Session,
    *,
    vendor_id: int,
    start_date: date,
    end_date: date,
    selected_location_ids: list[str] | None = None,
) -> SalesByVendorReportResult:
    if end_date < start_date:
        raise ValueError('End date must be on or after start date')

    vendor, mappings_by_variation_id = _vendor_report_mappings(db, vendor_id=vendor_id)
    if not mappings_by_variation_id:
        raise RuntimeError('No active Square variation mappings were found for this vendor')

    all_locations = list_square_locations_for_reports()
    if not all_locations:
        raise RuntimeError('No Square locations were found for this account')

    valid_location_ids = {location.id for location in all_locations}
    requested_location_ids = [str(value).strip() for value in (selected_location_ids or []) if str(value).strip()]
    if requested_location_ids:
        fetch_location_ids = [location_id for location_id in requested_location_ids if location_id in valid_location_ids]
        if not fetch_location_ids:
            raise ValueError('Selected location filter is invalid')
    else:
        fetch_location_ids = [location.id for location in all_locations]

    location_by_id = {location.id: location for location in all_locations}
    included_locations = sorted(
        [location_by_id[location_id] for location_id in fetch_location_ids if location_id in location_by_id],
        key=lambda row: row.name.lower(),
    )

    start_at = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
    end_at = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=timezone.utc)

    buckets: dict[str, dict[str, object]] = {}
    all_order_ids: set[str] = set()

    client = _SquareClient()
    for order in _iter_completed_orders(
        client,
        location_ids=fetch_location_ids,
        start_at=start_at,
        end_at=end_at,
    ):
        order_id = str(order.get('id') or '').strip()
        for line in order.get('line_items', []) or []:
            variation_id = str(line.get('catalog_object_id') or '').strip()
            mapping = mappings_by_variation_id.get(variation_id)
            if mapping is None:
                continue

            qty = _decimal_or_zero(line.get('quantity'))
            if qty <= 0:
                continue

            gross_sales = _money_from_cents((line.get('gross_sales_money') or {}).get('amount'))
            if gross_sales <= 0:
                base_unit_price = _money_from_cents((line.get('base_price_money') or {}).get('amount'))
                gross_sales = (qty * base_unit_price).quantize(Decimal('0.01'))

            discounts = _money_from_cents((line.get('total_discount_money') or {}).get('amount'))
            net_sales = (gross_sales - discounts).quantize(Decimal('0.01'))
            bucket = buckets.setdefault(
                variation_id,
                {
                    'sku': mapping.sku,
                    'item_name': str(line.get('name') or '').strip() or mapping.sku,
                    'variation_name': str(line.get('variation_name') or '').strip(),
                    'units_sold': Decimal('0'),
                    'line_item_count': 0,
                    'order_ids': set(),
                    'gross_sales': Decimal('0.00'),
                    'discounts': Decimal('0.00'),
                    'net_sales': Decimal('0.00'),
                },
            )
            if not str(bucket['item_name']).strip():
                bucket['item_name'] = str(line.get('name') or '').strip() or mapping.sku
            if not str(bucket['variation_name']).strip():
                bucket['variation_name'] = str(line.get('variation_name') or '').strip()
            bucket['units_sold'] += qty
            bucket['line_item_count'] += 1
            bucket['gross_sales'] += gross_sales
            bucket['discounts'] += discounts
            bucket['net_sales'] += net_sales
            if order_id:
                bucket['order_ids'].add(order_id)
                all_order_ids.add(order_id)

    rows: list[SalesByVendorReportRow] = []
    total_units_sold = Decimal('0')
    total_line_item_count = 0
    total_gross_sales = Decimal('0.00')
    total_discounts = Decimal('0.00')
    total_net_sales = Decimal('0.00')

    for variation_id, bucket in buckets.items():
        units_sold = Decimal(bucket['units_sold'])
        gross_sales = Decimal(bucket['gross_sales']).quantize(Decimal('0.01'))
        discounts = Decimal(bucket['discounts']).quantize(Decimal('0.01'))
        net_sales = Decimal(bucket['net_sales']).quantize(Decimal('0.01'))
        line_item_count = int(bucket['line_item_count'])
        order_count = len(set(bucket['order_ids']))
        rows.append(
            SalesByVendorReportRow(
                sku=str(bucket['sku']),
                variation_id=variation_id,
                item_name=str(bucket['item_name']),
                variation_name=str(bucket['variation_name']),
                units_sold=units_sold,
                line_item_count=line_item_count,
                order_count=order_count,
                gross_sales=gross_sales,
                discounts=discounts,
                net_sales=net_sales,
                average_net_per_unit=_average_money_per_unit(net_sales, units_sold),
            )
        )
        total_units_sold += units_sold
        total_line_item_count += line_item_count
        total_gross_sales += gross_sales
        total_discounts += discounts
        total_net_sales += net_sales

    rows.sort(key=lambda row: (-row.net_sales, row.item_name.lower(), row.variation_name.lower(), row.sku.lower()))

    total_gross_sales = total_gross_sales.quantize(Decimal('0.01'))
    total_discounts = total_discounts.quantize(Decimal('0.01'))
    total_net_sales = total_net_sales.quantize(Decimal('0.01'))

    return SalesByVendorReportResult(
        start_date=start_date,
        end_date=end_date,
        vendor_id=int(vendor.id),
        vendor_name=str(vendor.name),
        selected_location_ids=[location.id for location in included_locations],
        locations=included_locations,
        mapped_variation_count=len(mappings_by_variation_id),
        rows=rows,
        total_units_sold=total_units_sold,
        total_line_item_count=total_line_item_count,
        total_order_count=len(all_order_ids),
        total_gross_sales=total_gross_sales,
        total_discounts=total_discounts,
        total_net_sales=total_net_sales,
        average_net_per_unit=_average_money_per_unit(total_net_sales, total_units_sold),
    )


@dataclass(frozen=True)
class _OrderEmployeeSalesAmounts:
    transaction_datetime: datetime | None
    transaction_date: date | None
    location_id: str
    order_id: str
    gross_sales: Decimal
    net_sales: Decimal
    discounts: Decimal
    tips: Decimal
    sales_tax: Decimal
    total_paid: Decimal


@dataclass(frozen=True)
class _PaymentAttribution:
    order_id: str
    team_member_id: str
    amount_cents: int
    created_at: datetime | None


_UNATTRIBUTED_TEAM_MEMBER_ID = '__unattributed__'


def _zoneinfo_or_utc(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name or 'UTC')
    except ZoneInfoNotFoundError:
        return ZoneInfo('UTC')


def _local_date_range_to_utc(start_date: date, end_date: date, *, tz: ZoneInfo) -> tuple[datetime, datetime]:
    local_start = datetime.combine(start_date, time.min, tzinfo=tz)
    local_end = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=tz)
    return local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)


def _money_cents(raw_money: object) -> int:
    if not isinstance(raw_money, dict):
        return 0
    try:
        return int(raw_money.get('amount') or 0)
    except (TypeError, ValueError):
        return 0


def _money_from_order_money(order: dict, net_amounts: dict, net_key: str, order_key: str) -> Decimal:
    if isinstance(net_amounts.get(net_key), dict):
        return _money_from_cents((net_amounts.get(net_key) or {}).get('amount'))
    return _money_from_cents((order.get(order_key) or {}).get('amount'))


def _order_employee_sales_amounts(
    order: dict,
    *,
    timezone_by_location_id: dict[str, ZoneInfo],
    start_date: date,
    end_date: date,
) -> _OrderEmployeeSalesAmounts | None:
    order_id = str(order.get('id') or '').strip()
    location_id = str(order.get('location_id') or '').strip()
    if not order_id or not location_id:
        return None

    transaction_dt = _parse_iso_datetime(order.get('closed_at') or order.get('created_at'))
    if transaction_dt is None:
        return None

    location_tz = timezone_by_location_id.get(location_id) or ZoneInfo('UTC')
    transaction_date = transaction_dt.astimezone(location_tz).date()
    if transaction_date < start_date or transaction_date > end_date:
        return None

    net_amounts = order.get('net_amounts') if isinstance(order.get('net_amounts'), dict) else {}
    total_paid = _money_from_order_money(order, net_amounts, 'total_money', 'total_money')
    sales_tax = _money_from_order_money(order, net_amounts, 'tax_money', 'total_tax_money')
    tips = _money_from_order_money(order, net_amounts, 'tip_money', 'total_tip_money')
    service_charges = _money_from_order_money(
        order,
        net_amounts,
        'service_charge_money',
        'total_service_charge_money',
    )
    discounts = _money_from_order_money(order, net_amounts, 'discount_money', 'total_discount_money')
    net_sales = (total_paid - sales_tax - tips - service_charges).quantize(Decimal('0.01'))
    gross_sales = (net_sales + discounts).quantize(Decimal('0.01'))

    if gross_sales == Decimal('0.00'):
        line_gross = Decimal('0.00')
        for line in order.get('line_items', []) or []:
            line_gross += _money_from_cents((line.get('gross_sales_money') or {}).get('amount'))
        if line_gross:
            gross_sales = line_gross.quantize(Decimal('0.01'))

    return _OrderEmployeeSalesAmounts(
        transaction_datetime=transaction_dt,
        transaction_date=transaction_date,
        location_id=location_id,
        order_id=order_id,
        gross_sales=gross_sales,
        net_sales=net_sales,
        discounts=discounts,
        tips=tips,
        sales_tax=sales_tax,
        total_paid=total_paid,
    )


def _payment_amount_cents(payment: dict) -> int:
    return _money_cents(payment.get('total_money')) or _money_cents(payment.get('amount_money'))


def _employee_name_from_team_member(team_member: dict) -> str:
    given_name = str(team_member.get('given_name') or '').strip()
    family_name = str(team_member.get('family_name') or '').strip()
    full_name = ' '.join(part for part in [given_name, family_name] if part).strip()
    if full_name:
        return full_name
    return str(team_member.get('email_address') or team_member.get('id') or 'Unknown Employee').strip()


def _team_member_names(client: _SquareClient) -> dict[str, str]:
    out: dict[str, str] = {}
    cursor: str | None = None
    while True:
        payload: dict[str, object] = {'limit': 200}
        if cursor:
            payload['cursor'] = cursor
        response = client.post('/v2/team-members/search', payload)
        for team_member in response.get('team_members', []) or []:
            team_member_id = str(team_member.get('id') or '').strip()
            if not team_member_id:
                continue
            out[team_member_id] = _employee_name_from_team_member(team_member)
        cursor = response.get('cursor')
        if not cursor:
            break
    return out


def _iter_completed_orders(
    client: _SquareClient,
    *,
    location_ids: list[str],
    start_at: datetime,
    end_at: datetime,
) -> list[dict]:
    orders: list[dict] = []
    cursor: str | None = None
    while True:
        payload: dict[str, object] = {
            'location_ids': location_ids,
            'query': {
                'filter': {
                    'date_time_filter': {
                        'closed_at': {
                            'start_at': _to_iso(start_at),
                            'end_at': _to_iso(end_at),
                        }
                    },
                    'state_filter': {'states': ['COMPLETED']},
                },
                'sort': {
                    'sort_field': 'CLOSED_AT',
                    'sort_order': 'ASC',
                },
            },
            'limit': 500,
        }
        if cursor:
            payload['cursor'] = cursor
        response = client.post('/v2/orders/search', payload)
        orders.extend(response.get('orders', []) or [])
        cursor = response.get('cursor')
        if not cursor:
            break
    return orders


def _fetch_payment_attributions_for_location(
    client: _SquareClient,
    *,
    location_id: str,
    start_at: datetime,
    end_at: datetime,
    order_ids: set[str],
) -> dict[str, list[_PaymentAttribution]]:
    out: dict[str, list[_PaymentAttribution]] = {}
    cursor: str | None = None
    while True:
        query: dict[str, object] = {
            'location_id': location_id,
            'begin_time': _to_iso(start_at),
            'end_time': _to_iso(end_at),
            'sort_order': 'ASC',
            'limit': 100,
        }
        response = client.get('/v2/payments', query=query, cursor=cursor)
        for payment in response.get('payments', []) or []:
            if str(payment.get('status') or '').strip().upper() != 'COMPLETED':
                continue
            order_id = str(payment.get('order_id') or '').strip()
            if not order_id or order_id not in order_ids:
                continue
            team_member_id = str(payment.get('team_member_id') or payment.get('employee_id') or '').strip()
            out.setdefault(order_id, []).append(
                _PaymentAttribution(
                    order_id=order_id,
                    team_member_id=team_member_id,
                    amount_cents=_payment_amount_cents(payment),
                    created_at=_parse_iso_datetime(payment.get('created_at') or payment.get('updated_at')),
                )
            )
        cursor = response.get('cursor')
        if not cursor:
            break
    return out


def _primary_team_member_id(attributions: list[_PaymentAttribution]) -> str:
    with_team_member = [row for row in attributions if row.team_member_id]
    if not with_team_member:
        return _UNATTRIBUTED_TEAM_MEMBER_ID
    return sorted(
        with_team_member,
        key=lambda row: (
            -row.amount_cents,
            row.created_at or datetime.min.replace(tzinfo=timezone.utc),
            row.team_member_id,
        ),
    )[0].team_member_id


def _average_money(total: Decimal, count: int) -> Decimal:
    if count <= 0:
        return Decimal('0.00')
    return (total / Decimal(count)).quantize(Decimal('0.01'))


def build_employee_sales_report(
    *,
    start_date: date,
    end_date: date,
    selected_location_ids: list[str] | None = None,
) -> EmployeeSalesReportResult:
    if end_date < start_date:
        raise ValueError('End date must be on or after start date')

    all_locations = list_square_locations_for_reports()
    if not all_locations:
        raise RuntimeError('No Square locations were found for this account')

    valid_location_ids = {location.id for location in all_locations}
    requested_location_ids = [str(value).strip() for value in (selected_location_ids or []) if str(value).strip()]
    if requested_location_ids:
        fetch_location_ids = [location_id for location_id in requested_location_ids if location_id in valid_location_ids]
        if not fetch_location_ids:
            raise ValueError('Selected location filter is invalid')
    else:
        fetch_location_ids = [location.id for location in all_locations]

    location_by_id = {location.id: location for location in all_locations}
    timezone_by_location_id = {
        location.id: _zoneinfo_or_utc(location.timezone_name)
        for location in all_locations
    }
    location_ids_by_timezone: dict[str, list[str]] = {}
    for location_id in fetch_location_ids:
        location = location_by_id.get(location_id)
        timezone_name = location.timezone_name if location else 'UTC'
        location_ids_by_timezone.setdefault(timezone_name or 'UTC', []).append(location_id)

    client = _SquareClient()
    orders_by_id: dict[str, _OrderEmployeeSalesAmounts] = {}
    for timezone_name, location_ids in location_ids_by_timezone.items():
        tz = _zoneinfo_or_utc(timezone_name)
        start_at, end_at = _local_date_range_to_utc(start_date, end_date, tz=tz)
        for order in _iter_completed_orders(
            client,
            location_ids=location_ids,
            start_at=start_at,
            end_at=end_at,
        ):
            amounts = _order_employee_sales_amounts(
                order,
                timezone_by_location_id=timezone_by_location_id,
                start_date=start_date,
                end_date=end_date,
            )
            if amounts is not None:
                orders_by_id[amounts.order_id] = amounts

    payment_attributions_by_order_id: dict[str, list[_PaymentAttribution]] = {}
    order_ids_by_location: dict[str, set[str]] = {}
    for order in orders_by_id.values():
        order_ids_by_location.setdefault(order.location_id, set()).add(order.order_id)

    for location_id, order_ids in order_ids_by_location.items():
        location_tz = timezone_by_location_id.get(location_id) or ZoneInfo('UTC')
        start_at, end_at = _local_date_range_to_utc(start_date, end_date, tz=location_tz)
        location_attributions = _fetch_payment_attributions_for_location(
            client,
            location_id=location_id,
            start_at=start_at - timedelta(days=1),
            end_at=end_at + timedelta(days=1),
            order_ids=order_ids,
        )
        for order_id, attributions in location_attributions.items():
            payment_attributions_by_order_id.setdefault(order_id, []).extend(attributions)

    team_member_name_by_id = _team_member_names(client)

    buckets: dict[str, dict[str, object]] = {}
    unattributed_transaction_count = 0
    for order in orders_by_id.values():
        team_member_id = _primary_team_member_id(payment_attributions_by_order_id.get(order.order_id, []))
        if team_member_id == _UNATTRIBUTED_TEAM_MEMBER_ID:
            unattributed_transaction_count += 1
        bucket = buckets.setdefault(
            team_member_id,
            {
                'location_ids': set(),
                'transaction_count': 0,
                'gross_sales': Decimal('0.00'),
                'net_sales': Decimal('0.00'),
                'discounts': Decimal('0.00'),
                'tips': Decimal('0.00'),
                'sales_tax': Decimal('0.00'),
                'total_paid': Decimal('0.00'),
            },
        )
        bucket['location_ids'].add(order.location_id)
        bucket['transaction_count'] += 1
        bucket['gross_sales'] += order.gross_sales
        bucket['net_sales'] += order.net_sales
        bucket['discounts'] += order.discounts
        bucket['tips'] += order.tips
        bucket['sales_tax'] += order.sales_tax
        bucket['total_paid'] += order.total_paid

    rows: list[EmployeeSalesReportRow] = []
    totals = {
        'transaction_count': 0,
        'gross_sales': Decimal('0.00'),
        'net_sales': Decimal('0.00'),
        'discounts': Decimal('0.00'),
        'tips': Decimal('0.00'),
        'sales_tax': Decimal('0.00'),
        'total_paid': Decimal('0.00'),
    }
    for team_member_id, bucket in buckets.items():
        transaction_count = int(bucket['transaction_count'])
        gross_sales = Decimal(bucket['gross_sales']).quantize(Decimal('0.01'))
        net_sales = Decimal(bucket['net_sales']).quantize(Decimal('0.01'))
        discounts = Decimal(bucket['discounts']).quantize(Decimal('0.01'))
        tips = Decimal(bucket['tips']).quantize(Decimal('0.01'))
        sales_tax = Decimal(bucket['sales_tax']).quantize(Decimal('0.01'))
        total_paid = Decimal(bucket['total_paid']).quantize(Decimal('0.01'))
        location_ids = sorted(str(value) for value in bucket['location_ids'])
        location_names = [
            (location_by_id.get(location_id).name if location_by_id.get(location_id) else location_id)
            for location_id in location_ids
        ]
        employee_name = (
            'Unattributed'
            if team_member_id == _UNATTRIBUTED_TEAM_MEMBER_ID
            else team_member_name_by_id.get(team_member_id, team_member_id)
        )
        rows.append(
            EmployeeSalesReportRow(
                team_member_id=team_member_id,
                employee_name=employee_name,
                location_names=location_names,
                transaction_count=transaction_count,
                gross_sales=gross_sales,
                net_sales=net_sales,
                discounts=discounts,
                tips=tips,
                sales_tax=sales_tax,
                total_paid=total_paid,
                average_gross_per_transaction=_average_money(gross_sales, transaction_count),
                average_net_per_transaction=_average_money(net_sales, transaction_count),
            )
        )
        totals['transaction_count'] += transaction_count
        totals['gross_sales'] += gross_sales
        totals['net_sales'] += net_sales
        totals['discounts'] += discounts
        totals['tips'] += tips
        totals['sales_tax'] += sales_tax
        totals['total_paid'] += total_paid

    rows.sort(key=lambda row: (-row.net_sales, row.employee_name.lower()))
    included_locations = sorted(
        [location_by_id[location_id] for location_id in fetch_location_ids if location_id in location_by_id],
        key=lambda row: row.name.lower(),
    )

    total_transaction_count = int(totals['transaction_count'])
    total_gross_sales = Decimal(totals['gross_sales']).quantize(Decimal('0.01'))
    total_net_sales = Decimal(totals['net_sales']).quantize(Decimal('0.01'))

    return EmployeeSalesReportResult(
        start_date=start_date,
        end_date=end_date,
        selected_location_ids=[location.id for location in included_locations],
        locations=included_locations,
        rows=rows,
        total_transaction_count=total_transaction_count,
        total_gross_sales=total_gross_sales,
        total_net_sales=total_net_sales,
        total_discounts=Decimal(totals['discounts']).quantize(Decimal('0.01')),
        total_tips=Decimal(totals['tips']).quantize(Decimal('0.01')),
        total_sales_tax=Decimal(totals['sales_tax']).quantize(Decimal('0.01')),
        total_paid=Decimal(totals['total_paid']).quantize(Decimal('0.01')),
        average_gross_per_transaction=_average_money(total_gross_sales, total_transaction_count),
        average_net_per_transaction=_average_money(total_net_sales, total_transaction_count),
        unattributed_transaction_count=unattributed_transaction_count,
    )
