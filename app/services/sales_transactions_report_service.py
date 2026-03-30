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
            rows.append(SalesReportLocation(id=location_id, name=location_name))
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
