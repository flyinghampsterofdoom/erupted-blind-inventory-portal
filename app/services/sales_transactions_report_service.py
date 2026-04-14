from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.config import settings


def _to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _money_from_cents(raw_amount: object) -> Decimal:
    try:
        return (Decimal(str(raw_amount)) / Decimal('100')).quantize(Decimal('0.01'))
    except Exception:
        return Decimal('0.00')


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


class _SquareClient:
    def __init__(self) -> None:
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

    def get(self, path: str, *, cursor: str | None = None) -> dict:
        url = f'{self.base_url}{path}'
        if cursor:
            joiner = '&' if '?' in url else '?'
            url = f'{url}{joiner}cursor={cursor}'
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
