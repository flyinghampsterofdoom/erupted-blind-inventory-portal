from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from time import perf_counter

from app.services.square_ordering_data_service import _square_post
from app.services.v2_ordering_normalization_service import DailyQuantity
from app.services.v2_ordering_policy_service import DataSourceEvidence


READ_ENDPOINTS = frozenset(
    {
        '/v2/catalog/search-catalog-items',
        '/v2/inventory/counts/batch-retrieve',
        '/v2/inventory/batch-retrieve-counts',
        '/v2/orders/search',
        '/v2/inventory/changes/batch-retrieve',
    }
)
ZERO = Decimal('0')


@dataclass(frozen=True)
class SquareProductMetadata:
    variation_id: str
    sku: str
    item_name: str
    variation_name: str
    created_at: datetime | None
    confirmed_discontinued: bool
    item_id: str = ''
    updated_at: datetime | None = None


@dataclass(frozen=True)
class SquareStoreSkuData:
    store_id: int
    variation_id: str
    current_on_hand: Decimal
    inventory_valid: bool
    daily_sales: tuple[DailyQuantity, ...]
    daily_inventory_deltas: tuple[DailyQuantity, ...]
    required_sources: tuple[DataSourceEvidence, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class SquareOrderingReadMetrics:
    request_count: int = 0
    inventory_count_variation_ids_submitted: int = 0
    inventory_change_variation_ids_submitted: int = 0
    inventory_change_page_count: int = 0
    inventory_changes_returned: int = 0
    endpoint_request_counts: tuple[tuple[str, int], ...] = ()
    endpoint_elapsed_seconds: tuple[tuple[str, float], ...] = ()


@dataclass(frozen=True)
class SquareOrderingReadResult:
    products: dict[str, SquareProductMetadata]
    by_store_variation: dict[tuple[int, str], SquareStoreSkuData]
    metrics: SquareOrderingReadMetrics = field(default_factory=SquareOrderingReadMetrics)


@dataclass(frozen=True)
class SquareCatalogReadResult:
    products: dict[str, SquareProductMetadata]
    metrics: SquareOrderingReadMetrics = field(default_factory=SquareOrderingReadMetrics)


@dataclass(frozen=True)
class SquareInventoryCount:
    location_id: str
    variation_id: str
    quantity: Decimal
    calculated_at: datetime | None


@dataclass(frozen=True)
class SquareInventoryCountReadResult:
    counts: dict[tuple[str, str], SquareInventoryCount]
    metrics: SquareOrderingReadMetrics = field(default_factory=SquareOrderingReadMetrics)


PostCallable = Callable[[str, dict], dict]


def _parse_datetime(raw: object) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _decimal(raw: object) -> Decimal:
    try:
        return Decimal(str(raw or 0))
    except Exception:
        return ZERO


def _optional_decimal(raw: object) -> Decimal | None:
    if raw is None or str(raw).strip() == '':
        return None
    try:
        return Decimal(str(raw))
    except Exception:
        return None


class SquareOrderingReadGateway:
    """Narrow read-only Square boundary for Phase 1 Ordering."""

    def __init__(self, post: PostCallable | None = None):
        self._post = post or _square_post
        self._request_counts: dict[str, int] = {}
        self._request_seconds: dict[str, float] = {}
        self._inventory_changes_returned = 0

    def _read_post(self, path: str, payload: dict) -> dict:
        if path not in READ_ENDPOINTS:
            raise ValueError(f'V2 Ordering Square endpoint is not read-only: {path}')
        started = perf_counter()
        try:
            return self._post(path, payload)
        finally:
            self._request_counts[path] = self._request_counts.get(path, 0) + 1
            self._request_seconds[path] = self._request_seconds.get(path, 0.0) + (perf_counter() - started)

    def current_metrics(self) -> SquareOrderingReadMetrics:
        """Return request metrics accumulated so far, including a failed final request."""
        return SquareOrderingReadMetrics(
            request_count=sum(self._request_counts.values()),
            endpoint_request_counts=tuple(sorted(self._request_counts.items())),
            endpoint_elapsed_seconds=tuple(sorted(self._request_seconds.items())),
        )

    def _catalog(self, variation_ids: set[str]) -> dict[str, SquareProductMetadata]:
        products: dict[str, SquareProductMetadata] = {}
        cursor: str | None = None
        while True:
            payload: dict = {'limit': 100}
            if cursor:
                payload['cursor'] = cursor
            response = self._read_post('/v2/catalog/search-catalog-items', payload)
            for item in response.get('items', []) or []:
                item_data = item.get('item_data') or {}
                item_name = str(item_data.get('name') or '').strip()
                item_created = _parse_datetime(item.get('created_at'))
                item_deleted = bool(item.get('is_deleted'))
                for variation in item_data.get('variations', []) or []:
                    variation_id = str(variation.get('id') or '').strip()
                    if variation_id not in variation_ids:
                        continue
                    variation_data = variation.get('item_variation_data') or {}
                    products[variation_id] = SquareProductMetadata(
                        variation_id=variation_id,
                        sku=str(variation_data.get('sku') or '').strip(),
                        item_name=item_name,
                        variation_name=str(variation_data.get('name') or '').strip(),
                        created_at=_parse_datetime(variation.get('created_at')) or item_created,
                        confirmed_discontinued=item_deleted or bool(variation.get('is_deleted')),
                        item_id=str(item.get('id') or '').strip(),
                        updated_at=(
                            _parse_datetime(variation.get('updated_at'))
                            or _parse_datetime(item.get('updated_at'))
                        ),
                    )
            cursor = str(response.get('cursor') or '').strip() or None
            if not cursor:
                return products

    def fetch_product_metadata(self, variation_ids: list[str]) -> dict[str, SquareProductMetadata]:
        """Compatibility wrapper for a bulk Ordering catalog identity read."""
        return self.fetch_catalog_identity(variation_ids).products

    def fetch_catalog_identity(self, variation_ids: list[str]) -> SquareCatalogReadResult:
        """Bulk-read Ordering-owned catalog identity with pagination metrics."""
        self._request_counts = {}
        self._request_seconds = {}
        clean = {value.strip() for value in variation_ids if value.strip()}
        products = self._catalog(clean) if clean else {}
        metrics = self.current_metrics()
        return SquareCatalogReadResult(products=products, metrics=metrics)

    def fetch_current_inventory_counts(
        self,
        *,
        location_ids: list[str],
        variation_ids: list[str],
    ) -> SquareInventoryCountReadResult:
        """Return only explicit Square count pairs; an omitted pair is never synthesized as zero."""
        self._request_counts = {}
        self._request_seconds = {}
        clean_locations = sorted({str(value).strip() for value in location_ids if str(value).strip()})
        clean_variations = sorted({str(value).strip() for value in variation_ids if str(value).strip()})
        counts: dict[tuple[str, str], SquareInventoryCount] = {}
        if not clean_locations or not clean_variations:
            return SquareInventoryCountReadResult(counts=counts, metrics=self.current_metrics())

        for offset in range(0, len(clean_variations), 1000):
            chunk = clean_variations[offset : offset + 1000]
            cursor: str | None = None
            while True:
                payload: dict = {
                    'catalog_object_ids': chunk,
                    'location_ids': clean_locations,
                    'states': ['IN_STOCK'],
                    'limit': 1000,
                }
                if cursor:
                    payload['cursor'] = cursor
                response = self._read_post('/v2/inventory/counts/batch-retrieve', payload)
                for raw in response.get('counts', []) or []:
                    if str(raw.get('state') or '').strip().upper() != 'IN_STOCK':
                        continue
                    location_id = str(raw.get('location_id') or '').strip()
                    variation_id = str(raw.get('catalog_object_id') or '').strip()
                    quantity = _optional_decimal(raw.get('quantity'))
                    if not location_id or not variation_id or quantity is None:
                        continue
                    candidate = SquareInventoryCount(
                        location_id=location_id,
                        variation_id=variation_id,
                        quantity=quantity,
                        calculated_at=_parse_datetime(raw.get('calculated_at')),
                    )
                    key = (location_id, variation_id)
                    existing = counts.get(key)
                    if existing is None or (
                        candidate.calculated_at is not None
                        and (existing.calculated_at is None or candidate.calculated_at > existing.calculated_at)
                    ):
                        counts[key] = candidate
                cursor = str(response.get('cursor') or '').strip() or None
                if not cursor:
                    break
        return SquareInventoryCountReadResult(counts=counts, metrics=self.current_metrics())

    def _inventory_counts(
        self,
        location_ids: list[str],
        variation_ids: list[str],
        *,
        fetched_at: datetime,
    ) -> dict[tuple[str, str], tuple[Decimal, datetime, bool]]:
        counts: dict[tuple[str, str], tuple[Decimal, datetime, bool]] = {}
        for offset in range(0, len(variation_ids), 100):
            chunk = variation_ids[offset : offset + 100]
            cursor: str | None = None
            while True:
                payload: dict = {
                    'catalog_object_ids': chunk,
                    'location_ids': location_ids,
                    'states': ['IN_STOCK'],
                    'limit': 100,
                }
                if cursor:
                    payload['cursor'] = cursor
                response = self._read_post('/v2/inventory/batch-retrieve-counts', payload)
                for row in response.get('counts', []) or []:
                    location_id = str(row.get('location_id') or '').strip()
                    variation_id = str(row.get('catalog_object_id') or '').strip()
                    if not location_id or not variation_id:
                        continue
                    observed_at = _parse_datetime(row.get('calculated_at')) or fetched_at
                    counts[(location_id, variation_id)] = (_decimal(row.get('quantity')), observed_at, True)
                cursor = str(response.get('cursor') or '').strip() or None
                if not cursor:
                    break
        return counts

    def _daily_sales(
        self,
        location_ids: list[str],
        variation_ids: set[str],
        *,
        as_of: datetime,
    ) -> dict[tuple[str, str, date], Decimal]:
        end_day = as_of.astimezone(timezone.utc).date() - timedelta(days=1)
        start_day = end_day - timedelta(days=55)
        start_at = datetime.combine(start_day, datetime.min.time(), tzinfo=timezone.utc)
        end_at = datetime.combine(end_day + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
        cursor: str | None = None
        sales: dict[tuple[str, str, date], Decimal] = {}
        while True:
            payload: dict = {
                'location_ids': location_ids,
                'query': {
                    'filter': {
                        'date_time_filter': {
                            'closed_at': {
                                'start_at': start_at.isoformat().replace('+00:00', 'Z'),
                                'end_at': end_at.isoformat().replace('+00:00', 'Z'),
                            }
                        },
                        'state_filter': {'states': ['COMPLETED']},
                    }
                },
                'limit': 500,
            }
            if cursor:
                payload['cursor'] = cursor
            response = self._read_post('/v2/orders/search', payload)
            for order in response.get('orders', []) or []:
                location_id = str(order.get('location_id') or '').strip()
                sold_at = _parse_datetime(order.get('closed_at') or order.get('created_at'))
                if not location_id or sold_at is None:
                    continue
                for line in order.get('line_items', []) or []:
                    variation_id = str(line.get('catalog_object_id') or '').strip()
                    if variation_id not in variation_ids:
                        continue
                    key = (location_id, variation_id, sold_at.date())
                    sales[key] = sales.get(key, ZERO) + _decimal(line.get('quantity'))
            cursor = str(response.get('cursor') or '').strip() or None
            if not cursor:
                return sales

    def _inventory_deltas(
        self,
        location_ids: list[str],
        variation_ids: list[str],
        *,
        as_of: datetime,
    ) -> dict[tuple[str, str, date], Decimal]:
        end_day = as_of.astimezone(timezone.utc).date() - timedelta(days=1)
        start_day = end_day - timedelta(days=55)
        start_at = datetime.combine(start_day, datetime.min.time(), tzinfo=timezone.utc)
        end_at = datetime.combine(end_day + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
        deltas: dict[tuple[str, str, date], Decimal] = {}
        for offset in range(0, len(variation_ids), 500):
            chunk = variation_ids[offset : offset + 500]
            cursor: str | None = None
            while True:
                payload: dict = {
                    'catalog_object_ids': chunk,
                    'location_ids': location_ids,
                    'types': ['ADJUSTMENT'],
                    'states': ['IN_STOCK', 'SOLD'],
                    'updated_after': start_at.isoformat().replace('+00:00', 'Z'),
                    'updated_before': end_at.isoformat().replace('+00:00', 'Z'),
                    'limit': 1000,
                }
                if cursor:
                    payload['cursor'] = cursor
                response = self._read_post('/v2/inventory/changes/batch-retrieve', payload)
                changes = response.get('changes', []) or []
                self._inventory_changes_returned += len(changes)
                for change in changes:
                    if str(change.get('type') or '').upper() != 'ADJUSTMENT':
                        continue
                    adjustment = change.get('adjustment') or {}
                    location_id = str(adjustment.get('location_id') or '').strip()
                    variation_id = str(adjustment.get('catalog_object_id') or '').strip()
                    occurred_at = _parse_datetime(adjustment.get('occurred_at') or change.get('created_at'))
                    if not location_id or not variation_id or occurred_at is None:
                        continue
                    quantity = _decimal(adjustment.get('quantity'))
                    delta = ZERO
                    if str(adjustment.get('to_state') or '').upper() == 'IN_STOCK':
                        delta += quantity
                    if str(adjustment.get('from_state') or '').upper() == 'IN_STOCK':
                        delta -= quantity
                    key = (location_id, variation_id, occurred_at.date())
                    deltas[key] = deltas.get(key, ZERO) + delta
                cursor = str(response.get('cursor') or '').strip() or None
                if not cursor:
                    break
        return deltas

    def fetch(
        self,
        *,
        location_by_store: dict[int, str],
        variation_ids: list[str],
        as_of: datetime,
    ) -> SquareOrderingReadResult:
        self._request_counts = {}
        self._request_seconds = {}
        self._inventory_changes_returned = 0
        clean_variations = sorted({value.strip() for value in variation_ids if value.strip()})
        location_ids = sorted({value for value in location_by_store.values() if value})
        fetched_at = as_of.astimezone(timezone.utc) if as_of.tzinfo else as_of.replace(tzinfo=timezone.utc)
        products: dict[str, SquareProductMetadata] = {}
        operation_errors: dict[str, str] = {}

        try:
            products = self._catalog(set(clean_variations))
        except Exception as exc:
            operation_errors['catalog'] = str(exc)
        try:
            counts = self._inventory_counts(location_ids, clean_variations, fetched_at=fetched_at)
        except Exception as exc:
            counts = {}
            operation_errors['inventory'] = str(exc)
        try:
            sales = self._daily_sales(location_ids, set(clean_variations), as_of=fetched_at)
        except Exception as exc:
            sales = {}
            operation_errors['sales'] = str(exc)
        try:
            deltas = self._inventory_deltas(location_ids, clean_variations, as_of=fetched_at)
        except Exception as exc:
            deltas = {}
            operation_errors['stockout_history'] = str(exc)

        by_key: dict[tuple[int, str], SquareStoreSkuData] = {}
        end_day = fetched_at.date() - timedelta(days=1)
        days = tuple(end_day - timedelta(days=offset) for offset in range(55, -1, -1))
        for store_id, location_id in sorted(location_by_store.items()):
            for variation_id in clean_variations:
                warnings: list[str] = []
                if variation_id not in products:
                    warnings.append('CATALOG_METADATA_UNAVAILABLE')
                count = counts.get((location_id, variation_id))
                if 'inventory' in operation_errors:
                    inventory = ZERO
                    inventory_valid = False
                    inventory_source = DataSourceEvidence('inventory', None, available=False, detail=operation_errors['inventory'])
                elif count is None:
                    inventory = ZERO
                    inventory_valid = True
                    inventory_source = DataSourceEvidence('inventory', fetched_at)
                    warnings.append('INVENTORY_COUNT_MISSING_ASSUMED_ZERO')
                else:
                    inventory, observed_at, inventory_valid = count
                    inventory_source = DataSourceEvidence('inventory', observed_at)
                sales_source = DataSourceEvidence(
                    'sales',
                    None if 'sales' in operation_errors else fetched_at,
                    available='sales' not in operation_errors,
                    detail=operation_errors.get('sales', ''),
                )
                stockout_source = DataSourceEvidence(
                    'stockout_history',
                    None if 'stockout_history' in operation_errors else fetched_at,
                    available='stockout_history' not in operation_errors,
                    detail=operation_errors.get('stockout_history', ''),
                )
                by_key[(store_id, variation_id)] = SquareStoreSkuData(
                    store_id=store_id,
                    variation_id=variation_id,
                    current_on_hand=inventory,
                    inventory_valid=inventory_valid,
                    daily_sales=tuple(
                        DailyQuantity(day, sales.get((location_id, variation_id, day), ZERO)) for day in days
                    ),
                    daily_inventory_deltas=tuple(
                        DailyQuantity(day, deltas.get((location_id, variation_id, day), ZERO)) for day in days
                    ),
                    required_sources=(inventory_source, sales_source, stockout_source),
                    warnings=tuple(warnings),
                )
        metrics = SquareOrderingReadMetrics(
            request_count=sum(self._request_counts.values()),
            inventory_count_variation_ids_submitted=len(clean_variations),
            inventory_change_variation_ids_submitted=len(clean_variations),
            inventory_change_page_count=self._request_counts.get('/v2/inventory/changes/batch-retrieve', 0),
            inventory_changes_returned=self._inventory_changes_returned,
            endpoint_request_counts=tuple(sorted(self._request_counts.items())),
            endpoint_elapsed_seconds=tuple(sorted(self._request_seconds.items())),
        )
        return SquareOrderingReadResult(products=products, by_store_variation=by_key, metrics=metrics)
