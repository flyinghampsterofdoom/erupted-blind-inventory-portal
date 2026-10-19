from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from time import perf_counter
import re

from app.services.square_ordering_data_service import _square_post
from app.services.square_request_policy import enforce_square_request_policy
from app.services.purchasing_evidence_service import quantity as quantity_value, BUSINESS_TIMEZONE
from app.services.purchasing_classification_service import square_vendor_ids
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
    vendor_ids: tuple[str, ...] = ()
    category_ids: tuple[str, ...] = ()
    product_type: str = ''


@dataclass(frozen=True)
class SquareStoreSkuData:
    store_id: int
    variation_id: str
    current_on_hand: Decimal | None
    inventory_valid: bool
    daily_sales: tuple[DailyQuantity, ...]
    daily_inventory_deltas: tuple[DailyQuantity, ...]
    required_sources: tuple[DataSourceEvidence, ...]
    warnings: tuple[str, ...]
    source_calculated_at: datetime | None = None
    current_day_inventory_delta: Decimal | None = None


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
        # Python 3.10 fromisoformat rejects valid RFC 3339 fractional seconds
        # such as Square's .79Z. Reuse the installed ISO parser, never invent
        # an event time or treat a failed history read as an empty history.
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+(?:Z|[+-]\d{2}:\d{2})',str(raw)):
            return None
        from dateutil.parser import isoparse
        try:
            parsed = isoparse(str(raw))
        except (ValueError, TypeError, OverflowError):
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
        enforce_square_request_policy('POST', path, payload)
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

    def _catalog(self, variation_ids: set[str] | None) -> dict[str, SquareProductMetadata]:
        products: dict[str, SquareProductMetadata] = {}
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            payload: dict = {'limit': 100}
            if cursor:
                payload['cursor'] = cursor
            response = self._read_post('/v2/catalog/search-catalog-items', payload)
            if response.get('errors') or not isinstance(response.get('items', []), list):
                raise ValueError('Incomplete active catalog response')
            for item in response.get('items', []) or []:
                item_data = item.get('item_data') or {}
                item_name = str(item_data.get('name') or '').strip()
                item_created = _parse_datetime(item.get('created_at'))
                item_deleted = bool(item.get('is_deleted'))
                for variation in item_data.get('variations', []) or []:
                    variation_id = str(variation.get('id') or '').strip()
                    if variation_ids is not None and variation_id not in variation_ids:
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
                        vendor_ids=square_vendor_ids(variation_data),
                        category_ids=tuple(sorted({str(c['id']) for c in item_data.get('categories',[]) if c.get('id')} | {str(c) for c in [item_data.get('category_id'),(item_data.get('reporting_category') or {}).get('id')] if c})),
                        product_type=str(item_data.get('product_type') or ''),
                        updated_at=(
                            _parse_datetime(variation.get('updated_at'))
                            or _parse_datetime(item.get('updated_at'))
                        ),
                    )
            cursor = str(response.get('cursor') or '').strip() or None
            if cursor in seen_cursors:
                raise ValueError('Repeated Square pagination cursor.')
            if cursor:
                seen_cursors.add(cursor)
            if not cursor:
                return products

    def fetch_active_catalog(self) -> dict[str, SquareProductMetadata]:
        """Complete active variation identity, including products without a SKU."""
        return {key: product for key, product in self._catalog(None).items() if not product.confirmed_discontinued}

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
        reset_metrics: bool = True,
        preserve_partial: bool = False,
    ) -> SquareInventoryCountReadResult:
        """Return only explicit Square count pairs; an omitted pair is never synthesized as zero."""
        if reset_metrics:
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
            seen_cursors: set[str] = set()
            while True:
                payload: dict = {
                    'catalog_object_ids': chunk,
                    'location_ids': clean_locations,
                    'states': ['IN_STOCK'],
                    'limit': 1000,
                }
                if cursor:
                    payload['cursor'] = cursor
                try:
                    response = self._read_post('/v2/inventory/counts/batch-retrieve', payload)
                    if response.get('errors') or not isinstance(response.get('counts', []), list):
                        raise ValueError('Incomplete inventory response.')
                except (ValueError, RuntimeError, OSError):
                    if not preserve_partial:
                        raise
                    # Already observed pairs remain observations. Unreturned pairs
                    # are omitted and therefore UNKNOWN; subsequent batches continue.
                    break
                for raw in response.get('counts', []) or []:
                    if not isinstance(raw, dict):
                        continue
                    if str(raw.get('state') or '').strip().upper() != 'IN_STOCK':
                        continue
                    location_id = str(raw.get('location_id') or '').strip()
                    variation_id = str(raw.get('catalog_object_id') or '').strip()
                    quantity = quantity_value(raw.get('quantity'))
                    if location_id not in clean_locations or variation_id not in chunk or quantity is None:
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
                if cursor in seen_cursors:
                    if preserve_partial:
                        break
                    raise ValueError('Repeated inventory cursor.')
                if cursor:
                    seen_cursors.add(cursor)
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
        result = self.fetch_current_inventory_counts(location_ids=location_ids, variation_ids=variation_ids, reset_metrics=False, preserve_partial=True)
        return {key: (row.quantity, row.calculated_at, True)
                for key, row in result.counts.items()}

    def _daily_sales(
        self,
        location_ids: list[str],
        variation_ids: set[str],
        *,
        as_of: datetime,
        history_days: int = 56,
    ) -> dict[tuple[str, str, date], Decimal]:
        end_day = as_of.astimezone(BUSINESS_TIMEZONE).date() - timedelta(days=1)
        start_day = end_day - timedelta(days=history_days-1)
        start_at = datetime.combine(start_day, datetime.min.time(), tzinfo=BUSINESS_TIMEZONE)
        end_at = datetime.combine(end_day + timedelta(days=1), datetime.min.time(), tzinfo=BUSINESS_TIMEZONE)
        cursor: str | None = None
        sales: dict[tuple[str, str, date], Decimal] = {}
        seen_cursors: set[str] = set()
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
            if response.get('errors') or not isinstance(response.get('orders', []), list):
                raise ValueError('Incomplete sales response.')
            for order in response.get('orders', []) or []:
                location_id = str(order.get('location_id') or '').strip()
                sold_at = _parse_datetime(order.get('closed_at') or order.get('created_at'))
                if location_id not in location_ids or sold_at is None:
                    raise ValueError('Sales have unresolved location or timestamp.')
                for line in order.get('line_items', []) or []:
                    variation_id = str(line.get('catalog_object_id') or '').strip()
                    if variation_id not in variation_ids:
                        continue
                    key = (location_id, variation_id, sold_at.astimezone(BUSINESS_TIMEZONE).date())
                    sold_quantity = quantity_value(line.get('quantity'))
                    if sold_quantity is None or sold_quantity < 0:
                        raise ValueError('Sales contain invalid quantities.')
                    sales[key] = sales.get(key, ZERO) + sold_quantity
            cursor = str(response.get('cursor') or '').strip() or None
            if cursor in seen_cursors:
                raise ValueError('Repeated Square pagination cursor.')
            if cursor:
                seen_cursors.add(cursor)
            if not cursor:
                return sales

    def _inventory_deltas(
        self,
        location_ids: list[str],
        variation_ids: list[str],
        *,
        as_of: datetime,
        history_days: int = 56,
        include_today: bool = False,
    ) -> dict[tuple[str, str, date], Decimal]:
        end_day = as_of.astimezone(BUSINESS_TIMEZONE).date() - timedelta(days=1)
        start_day = end_day - timedelta(days=history_days-1)
        start_at = datetime.combine(start_day, datetime.min.time(), tzinfo=BUSINESS_TIMEZONE)
        end_at = datetime.combine(end_day + timedelta(days=1), datetime.min.time(), tzinfo=BUSINESS_TIMEZONE)
        if include_today:end_at=as_of
        deltas: dict[tuple[str, str, date], Decimal] = {}
        self._uncertain_history_pairs = set()
        for offset in range(0, len(variation_ids), 500):
            chunk = variation_ids[offset : offset + 500]
            cursor: str | None = None
            seen_cursors = set()
            while True:
                payload: dict = {
                    'catalog_object_ids': chunk,
                    'location_ids': location_ids,
                    'types': ['ADJUSTMENT', 'PHYSICAL_COUNT'],
                    'states': ['IN_STOCK', 'SOLD'],
                    'updated_after': start_at.isoformat().replace('+00:00', 'Z'),
                    'updated_before': end_at.isoformat().replace('+00:00', 'Z'),
                    'limit': 1000,
                }
                if cursor:
                    payload['cursor'] = cursor
                response = self._read_post('/v2/inventory/changes/batch-retrieve', payload)
                if response.get('errors') or not isinstance(response.get('changes',[]),list):
                    raise ValueError('Incomplete inventory history response.')
                changes = response.get('changes', []) or []
                self._inventory_changes_returned += len(changes)
                for change in changes:
                    if str(change.get('type') or '').upper() == 'PHYSICAL_COUNT':
                        count = change.get('physical_count') or {}
                        if not count.get('location_id') or not count.get('catalog_object_id'):raise ValueError('Unresolved physical-count history identity')
                        self._uncertain_history_pairs.add((str(count.get('location_id') or ''), str(count.get('catalog_object_id') or '')))
                        continue
                    if str(change.get('type') or '').upper() != 'ADJUSTMENT':
                        raise ValueError('Unsupported inventory history event')
                    adjustment = change.get('adjustment') or {}
                    location_id = str(adjustment.get('location_id') or '').strip()
                    variation_id = str(adjustment.get('catalog_object_id') or '').strip()
                    occurred_at = _parse_datetime(adjustment.get('occurred_at') or change.get('created_at'))
                    if not location_id or not variation_id or occurred_at is None:
                        raise ValueError('Unresolved adjustment identity or timestamp')
                    quantity = quantity_value(adjustment.get('quantity'))
                    if quantity is None or quantity<0:raise ValueError('Invalid inventory history quantity')
                    delta = ZERO
                    if str(adjustment.get('to_state') or '').upper() == 'IN_STOCK':
                        delta += quantity
                    if str(adjustment.get('from_state') or '').upper() == 'IN_STOCK':
                        delta -= quantity
                    key = (location_id, variation_id, occurred_at.astimezone(BUSINESS_TIMEZONE).date())
                    deltas[key] = deltas.get(key, ZERO) + delta
                cursor = str(response.get('cursor') or '').strip() or None
                if cursor in seen_cursors:
                    raise ValueError('Repeated inventory history cursor.')
                if cursor:
                    seen_cursors.add(cursor)
                if not cursor:
                    break
        return deltas

    def fetch(
        self,
        *,
        location_by_store: dict[int, str],
        variation_ids: list[str],
        as_of: datetime,
        history_days: int = 56,
        catalog: dict[str, SquareProductMetadata] | None = None,
        include_today: bool = False,
    ) -> SquareOrderingReadResult:
        if not 7 <= history_days <= 180:
            raise ValueError('History window must be between 7 and 180 days')
        self._request_counts = {}
        self._request_seconds = {}
        self._inventory_changes_returned = 0
        clean_variations = sorted({value.strip() for value in variation_ids if value.strip()})
        location_ids = sorted({value for value in location_by_store.values() if value})
        fetched_at = as_of.astimezone(timezone.utc) if as_of.tzinfo else as_of.replace(tzinfo=timezone.utc)
        products: dict[str, SquareProductMetadata] = {}
        operation_errors: dict[str, str] = {}

        try:
            products = catalog if catalog is not None else self._catalog(set(clean_variations))
        except Exception as exc:
            operation_errors['catalog'] = str(exc)
        try:
            counts = self._inventory_counts(location_ids, clean_variations, fetched_at=fetched_at)
        except Exception as exc:
            counts = {}
            operation_errors['inventory'] = str(exc)
        try:
            sales = self._daily_sales(location_ids, set(clean_variations), as_of=fetched_at, history_days=history_days)
        except Exception as exc:
            sales = {}
            operation_errors['sales'] = str(exc)
        try:
            deltas = self._inventory_deltas(location_ids, clean_variations, as_of=fetched_at, history_days=history_days, include_today=include_today)
        except Exception as exc:
            deltas = {}
            operation_errors['stockout_history'] = str(exc)

        by_key: dict[tuple[int, str], SquareStoreSkuData] = {}
        end_day = fetched_at.astimezone(BUSINESS_TIMEZONE).date() - timedelta(days=1)
        days = tuple(end_day - timedelta(days=offset) for offset in range(history_days-1, -1, -1))
        for store_id, location_id in sorted(location_by_store.items()):
            for variation_id in clean_variations:
                warnings: list[str] = []
                if variation_id not in products:
                    warnings.append('CATALOG_METADATA_UNAVAILABLE')
                count = counts.get((location_id, variation_id))
                if 'inventory' in operation_errors:
                    inventory = None
                    inventory_valid = False
                    inventory_source = DataSourceEvidence('inventory', None, available=False, detail=operation_errors['inventory'])
                elif count is None:
                    inventory = None
                    inventory_valid = False
                    inventory_source = DataSourceEvidence('inventory', None, available=False, complete=False)
                    warnings.append('INVENTORY_COUNT_MISSING')
                else:
                    inventory, observed_at, inventory_valid = count
                    inventory_valid = inventory_valid and (observed_at is None or observed_at <= fetched_at)
                    inventory_source = DataSourceEvidence('inventory', fetched_at, available=inventory_valid,
                        detail=f'Square calculated at {observed_at.isoformat() if observed_at else "UNKNOWN"}')
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
                    complete=(location_id, variation_id) not in getattr(self, '_uncertain_history_pairs', set()),
                    detail=operation_errors.get('stockout_history', '') or ('Physical-count history needs reconciliation' if (location_id, variation_id) in getattr(self, '_uncertain_history_pairs', set()) else ''),
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
                    source_calculated_at=count[1] if count else None,
                    current_day_inventory_delta=deltas.get((location_id,variation_id,fetched_at.astimezone(BUSINESS_TIMEZONE).date()),ZERO) if include_today and 'stockout_history' not in operation_errors and (location_id,variation_id) not in getattr(self,'_uncertain_history_pairs',set()) else None,
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
