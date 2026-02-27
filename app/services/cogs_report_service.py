from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Store, VendorSkuConfig
from app.sync_square_campaigns import fetch_catalog_items, fetch_categories


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
    except (InvalidOperation, TypeError, ValueError):
        return Decimal('0')


@dataclass(frozen=True)
class CatalogVariationRow:
    variation_id: str
    sku: str
    category_name: str


@dataclass(frozen=True)
class CogsCategoryRow:
    category_name: str
    units_sold: Decimal
    gross_revenue: Decimal
    cogs_amount: Decimal
    sku_count: int
    covered_sku_count: int
    missing_cost_sku_count: int


@dataclass(frozen=True)
class CogsReportResult:
    start_date: date
    end_date: date
    rows: list[CogsCategoryRow]
    total_units_sold: Decimal
    total_gross_revenue: Decimal
    total_cogs_amount: Decimal
    total_skus_sold: int
    total_skus_with_cost: int
    total_skus_missing_cost: int
    matched_line_items: int
    unmatched_line_items: int


class _SquareClient:
    def __init__(self) -> None:
        if not settings.square_access_token:
            raise RuntimeError('SQUARE_ACCESS_TOKEN is required')
        self.base_url = settings.square_api_base_url.rstrip('/')
        self.timeout_seconds = settings.square_timeout_seconds
        self.headers = {
            'Authorization': f'Bearer {settings.square_access_token}',
            'Content-Type': 'application/json',
        }
        if settings.square_api_version:
            self.headers['Square-Version'] = settings.square_api_version

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
            raise RuntimeError(f'Square API error {exc.code}: {body}') from exc
        except URLError as exc:
            raise RuntimeError(f'Square API network error: {exc.reason}') from exc

        if data.get('errors'):
            raise RuntimeError(f"Square API returned errors: {data['errors']}")
        return data


def _active_store_location_ids(db: Session) -> list[str]:
    rows = db.execute(
        select(Store.square_location_id).where(
            Store.active.is_(True),
            Store.square_location_id.is_not(None),
        )
    ).all()
    return sorted({str(row.square_location_id).strip() for row in rows if row.square_location_id})


def _catalog_variation_lookup(client: _SquareClient) -> dict[str, CatalogVariationRow]:
    categories_by_id = fetch_categories(client)
    items = fetch_catalog_items(client)
    by_variation_id: dict[str, CatalogVariationRow] = {}

    for item in items:
        item_data = item.get('item_data') or {}
        reporting_category = item_data.get('reporting_category') or {}
        category_id = str(reporting_category.get('id') or '').strip()
        category_name = (categories_by_id.get(category_id) or '').strip() or '(No Reporting Category)'

        for variation in item_data.get('variations', []) or []:
            variation_id = str(variation.get('id') or '').strip()
            if not variation_id:
                continue
            vdata = variation.get('item_variation_data') or {}
            sku = str(vdata.get('sku') or '').strip()
            by_variation_id[variation_id] = CatalogVariationRow(
                variation_id=variation_id,
                sku=sku,
                category_name=category_name,
            )

    return by_variation_id


def _current_cost_by_sku(db: Session) -> dict[str, Decimal]:
    rows = db.execute(
        select(
            VendorSkuConfig.sku,
            VendorSkuConfig.unit_cost,
            VendorSkuConfig.is_default_vendor,
            VendorSkuConfig.updated_at,
            VendorSkuConfig.id,
        )
        .where(VendorSkuConfig.active.is_(True))
        .order_by(
            VendorSkuConfig.is_default_vendor.desc(),
            VendorSkuConfig.updated_at.desc(),
            VendorSkuConfig.id.desc(),
        )
    ).all()

    out: dict[str, Decimal] = {}
    for row in rows:
        sku = str(row.sku or '').strip()
        if not sku or sku in out:
            continue
        out[sku] = _decimal_or_zero(row.unit_cost).quantize(Decimal('0.0001'))
    return out


def build_cogs_report(db: Session, *, start_date: date, end_date: date) -> CogsReportResult:
    if end_date < start_date:
        raise ValueError('End date must be on or after start date')

    location_ids = _active_store_location_ids(db)
    if not location_ids:
        raise RuntimeError('No active stores have square_location_id configured')

    client = _SquareClient()
    variation_lookup = _catalog_variation_lookup(client)
    current_cost_by_sku = _current_cost_by_sku(db)

    start_at = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
    end_at = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=timezone.utc)

    by_category: dict[str, dict[str, object]] = {}
    matched_line_items = 0
    unmatched_line_items = 0

    cursor: str | None = None
    while True:
        payload: dict = {
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
                }
            },
            'limit': 500,
        }
        if cursor:
            payload['cursor'] = cursor

        response = client.post('/v2/orders/search', payload)
        for order in response.get('orders', []) or []:
            for line in order.get('line_items', []) or []:
                variation_id = str(line.get('catalog_object_id') or '').strip()
                if not variation_id:
                    unmatched_line_items += 1
                    continue

                variation = variation_lookup.get(variation_id)
                if variation is None:
                    unmatched_line_items += 1
                    continue

                qty = _decimal_or_zero(line.get('quantity'))
                if qty <= 0:
                    continue

                matched_line_items += 1
                sku = variation.sku
                category_name = variation.category_name
                unit_cost = current_cost_by_sku.get(sku) if sku else None
                cogs_amount = (qty * unit_cost) if unit_cost is not None else Decimal('0.00')

                gross_sales = _money_from_cents((line.get('gross_sales_money') or {}).get('amount'))
                if gross_sales <= 0:
                    base_unit_price = _money_from_cents((line.get('base_price_money') or {}).get('amount'))
                    gross_sales = (qty * base_unit_price).quantize(Decimal('0.01'))

                bucket = by_category.setdefault(
                    category_name,
                    {
                        'units_sold': Decimal('0'),
                        'gross_revenue': Decimal('0.00'),
                        'cogs_amount': Decimal('0.00'),
                        'skus': set(),
                        'covered_skus': set(),
                        'missing_cost_skus': set(),
                    },
                )

                bucket['units_sold'] = bucket['units_sold'] + qty
                bucket['gross_revenue'] = bucket['gross_revenue'] + gross_sales
                bucket['cogs_amount'] = bucket['cogs_amount'] + cogs_amount

                if sku:
                    bucket['skus'].add(sku)
                    if unit_cost is None:
                        bucket['missing_cost_skus'].add(sku)
                    else:
                        bucket['covered_skus'].add(sku)

        cursor = response.get('cursor')
        if not cursor:
            break

    rows: list[CogsCategoryRow] = []
    total_units_sold = Decimal('0')
    total_gross_revenue = Decimal('0.00')
    total_cogs_amount = Decimal('0.00')
    all_skus: set[str] = set()
    all_covered_skus: set[str] = set()
    all_missing_cost_skus: set[str] = set()

    for category_name, bucket in by_category.items():
        units_sold = Decimal(str(bucket['units_sold']))
        gross_revenue = Decimal(str(bucket['gross_revenue'])).quantize(Decimal('0.01'))
        cogs_amount = Decimal(str(bucket['cogs_amount'])).quantize(Decimal('0.01'))
        skus = set(bucket['skus'])
        covered_skus = set(bucket['covered_skus'])
        missing_cost_skus = set(bucket['missing_cost_skus'])

        total_units_sold += units_sold
        total_gross_revenue += gross_revenue
        total_cogs_amount += cogs_amount

        all_skus.update(skus)
        all_covered_skus.update(covered_skus)
        all_missing_cost_skus.update(missing_cost_skus)

        rows.append(
            CogsCategoryRow(
                category_name=category_name,
                units_sold=units_sold,
                gross_revenue=gross_revenue,
                cogs_amount=cogs_amount,
                sku_count=len(skus),
                covered_sku_count=len(covered_skus),
                missing_cost_sku_count=len(missing_cost_skus),
            )
        )

    rows.sort(key=lambda row: (-row.cogs_amount, row.category_name.lower()))

    return CogsReportResult(
        start_date=start_date,
        end_date=end_date,
        rows=rows,
        total_units_sold=total_units_sold,
        total_gross_revenue=total_gross_revenue.quantize(Decimal('0.01')),
        total_cogs_amount=total_cogs_amount.quantize(Decimal('0.01')),
        total_skus_sold=len(all_skus),
        total_skus_with_cost=len(all_covered_skus),
        total_skus_missing_cost=len(all_missing_cost_skus),
        matched_line_items=matched_line_items,
        unmatched_line_items=unmatched_line_items,
    )
