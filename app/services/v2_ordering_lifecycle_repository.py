from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from math import ceil

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AuditLog,
    OrderingCatalogIdentity,
    OrderingCatalogRefreshState,
    OrderingCurrentInventory,
    OrderingInventoryRefreshRun,
    OrderingProductLifecycle,
    Principal,
    Store,
    Vendor,
    VendorSkuConfig,
)
from app.services.v2_ordering_inventory_repository import CRITICAL, FRESH, STALE, effective_freshness


ACTIVE = 'ACTIVE'
NO_FUTURE_REORDER = 'NO_FUTURE_REORDER'
ARCHIVED = 'ARCHIVED'
LIFECYCLE_ORDER = {ACTIVE: 0, NO_FUTURE_REORDER: 1, ARCHIVED: 2}
MAPPING_FILTERS = {'ANY', 'MAPPED', 'UNMAPPED'}
NAME_FILTERS = {'ANY', 'KNOWN', 'UNKNOWN'}
INVENTORY_FILTERS = {'ANY', 'POSITIVE', 'ZERO', 'UNKNOWN', 'STALE'}
SORT_FIELDS = {'product', 'sku', 'vendor', 'inventory', 'lifecycle', 'changed_at', 'changed_by'}
AUDIT_ACTION = 'V2:ordering_lifecycle:lifecycle_status_changed'
UNKNOWN_PRODUCT_NAME = 'Product name unavailable'


@dataclass(frozen=True)
class LifecycleState:
    square_variation_id: str
    status: str = ACTIVE
    row_version: int = 0
    pre_archive_status: str | None = None
    sku_snapshot: str | None = None
    product_name_snapshot: str | None = None
    status_note: str | None = None


@dataclass(frozen=True)
class LifecycleStoreInventory:
    store_id: int
    store_name: str
    on_hand: Decimal | None
    state: str
    refreshed_at: datetime | None = None
    source_calculated_at: datetime | None = None
    age_label: str = ''
    reason: str = ''

    @property
    def quantity_display(self) -> str:
        if self.on_hand is None:
            return 'Unknown'
        return _quantity_text(self.on_hand)


@dataclass(frozen=True)
class LifecycleProductRow:
    square_variation_id: str
    sku: str
    product_name: str
    vendor_name: str
    status: str
    row_version: int
    status_note: str | None
    changed_at: datetime | None = None
    changed_by: str | None = None
    pre_archive_status: str | None = None
    mapped: bool = True
    product_name_available: bool = True
    inventory_source_available: bool = False
    current_inventory_total: Decimal | None = None
    last_known_inventory_total: Decimal | None = None
    inventory_by_store: tuple[LifecycleStoreInventory, ...] = ()
    inventory_state: str = 'UNAVAILABLE'
    inventory_as_of: datetime | None = None
    inventory_coverage: str = 'No inventory refresh completed'
    inventory_blocking_stores: tuple[str, ...] = ()
    inventory_unavailable_reason: str = (
        'Inventory unavailable. Ordering does not currently own a reliable local per-store inventory source.'
    )

    @property
    def current_inventory_display(self) -> str:
        return _quantity_text(self.current_inventory_total) if self.current_inventory_total is not None else ''

    @property
    def last_known_inventory_display(self) -> str:
        return _quantity_text(self.last_known_inventory_total) if self.last_known_inventory_total is not None else ''


@dataclass(frozen=True)
class LifecycleWorkspaceFilters:
    product_search: str = ''
    sku_search: str = ''
    vendor: str = ''
    lifecycle: str = ''
    mapping: str = 'ANY'
    name_state: str = 'ANY'
    inventory: str = 'ANY'


@dataclass(frozen=True)
class CatalogCoverage:
    expected_mapped_count: int
    covered_mapped_count: int
    missing_mapped_count: int
    last_result: str
    last_attempted_at: datetime | None
    last_successful_at: datetime | None
    last_error: str | None


@dataclass(frozen=True)
class LifecycleWorkspacePage:
    rows: tuple[LifecycleProductRow, ...]
    total_count: int
    unfiltered_count: int
    page_number: int
    page_size: int
    total_pages: int
    range_start: int
    range_end: int
    status_counts: dict[str, int]
    vendor_options: tuple[str, ...]
    coverage: CatalogCoverage
    inventory_refresh: OrderingInventoryRefreshRun | None
    query_count: int


def _clean_ids(variation_ids: list[str] | tuple[str, ...] | set[str]) -> tuple[str, ...]:
    return tuple(sorted({str(value).strip() for value in variation_ids if str(value).strip()}))


def _normalized(value: str | None) -> str:
    return ' '.join(str(value or '').casefold().split())


def _quantity_text(value: Decimal) -> str:
    if value == value.to_integral_value():
        return str(int(value))
    return format(value.normalize(), 'f')


def _age_label(refreshed_at: datetime, *, now: datetime) -> str:
    refreshed = refreshed_at if refreshed_at.tzinfo else refreshed_at.replace(tzinfo=timezone.utc)
    current = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    hours = max(0, int((current.astimezone(timezone.utc) - refreshed.astimezone(timezone.utc)).total_seconds() // 3600))
    if hours < 1:
        return 'less than 1 hour old'
    return f'{hours} hour{"" if hours == 1 else "s"} old'


def state_from_model(row: OrderingProductLifecycle) -> LifecycleState:
    return LifecycleState(
        square_variation_id=row.square_variation_id,
        status=row.status,
        row_version=row.row_version,
        pre_archive_status=row.pre_archive_status,
        sku_snapshot=row.sku_snapshot,
        product_name_snapshot=row.product_name_snapshot,
        status_note=row.status_note,
    )


def load_lifecycle_states(
    db: Session,
    variation_ids: list[str] | tuple[str, ...] | set[str],
) -> dict[str, LifecycleState]:
    """Load all persisted overrides in one query; omitted IDs resolve to ACTIVE."""
    clean = _clean_ids(variation_ids)
    stored = (
        {
            row.square_variation_id: state_from_model(row)
            for row in db.execute(
                select(OrderingProductLifecycle).where(OrderingProductLifecycle.square_variation_id.in_(clean))
            ).scalars().all()
        }
        if clean
        else {}
    )
    return {variation_id: stored.get(variation_id, LifecycleState(variation_id)) for variation_id in clean}


def lock_lifecycle_rows(db: Session, variation_ids: tuple[str, ...]) -> dict[str, OrderingProductLifecycle]:
    clean = _clean_ids(variation_ids)
    if not clean:
        return {}
    rows = db.execute(
        select(OrderingProductLifecycle)
        .where(OrderingProductLifecycle.square_variation_id.in_(clean))
        .with_for_update()
    ).scalars()
    return {row.square_variation_id: row for row in rows}


def add_lifecycle_row(db: Session, row: OrderingProductLifecycle) -> None:
    db.add(row)


def _catalog_rows(
    db: Session,
) -> tuple[tuple[LifecycleProductRow, ...], CatalogCoverage, int, OrderingInventoryRefreshRun | None]:
    """Build the Ordering-owned lifecycle catalog in nine bounded local queries with no Square access."""
    query_count = 0
    lifecycle_rows = db.execute(select(OrderingProductLifecycle)).scalars().all()
    query_count += 1
    lifecycle_by_id = {row.square_variation_id: row for row in lifecycle_rows}

    mapping_rows = db.execute(
        select(VendorSkuConfig, Vendor)
        .join(Vendor, Vendor.id == VendorSkuConfig.vendor_id)
        .where(
            VendorSkuConfig.active.is_(True),
            VendorSkuConfig.is_default_vendor.is_(True),
            Vendor.active.is_(True),
            VendorSkuConfig.square_variation_id.is_not(None),
        )
        .order_by(Vendor.name, VendorSkuConfig.sku, VendorSkuConfig.id)
    ).all()
    query_count += 1

    variation_ids = _clean_ids(
        {str(mapping.square_variation_id or '') for mapping, _vendor in mapping_rows}
        | set(lifecycle_by_id)
    )
    identities = {
        row.square_variation_id: row
        for row in (
            db.execute(
                select(OrderingCatalogIdentity).where(OrderingCatalogIdentity.square_variation_id.in_(variation_ids))
            ).scalars().all()
            if variation_ids
            else ()
        )
    }
    query_count += 1

    audit_rows = db.execute(select(AuditLog).where(AuditLog.action == AUDIT_ACTION)).scalars().all()
    query_count += 1
    latest_audit: dict[str, AuditLog] = {}
    for audit in audit_rows:
        variation_id = str((audit.meta or {}).get('entity_id') or '').strip()
        if not variation_id:
            continue
        existing = latest_audit.get(variation_id)
        if existing is None or (audit.created_at, audit.id) > (existing.created_at, existing.id):
            latest_audit[variation_id] = audit

    actor_ids = {int(audit.actor_principal_id) for audit in latest_audit.values() if audit.actor_principal_id}
    actors = {
        int(principal.id): str(principal.username)
        for principal in db.execute(select(Principal).where(Principal.id.in_(actor_ids))).scalars().all()
    }
    query_count += 1
    refresh_state = db.get(OrderingCatalogRefreshState, 1)
    query_count += 1

    products: dict[str, LifecycleProductRow] = {}
    mapped_ids: set[str] = set()
    for mapping, vendor in mapping_rows:
        variation_id = str(mapping.square_variation_id or '').strip()
        if not variation_id or variation_id in products:
            continue
        mapped_ids.add(variation_id)
        lifecycle = lifecycle_by_id.get(variation_id)
        identity = identities.get(variation_id)
        product_name = str(identity.product_name or '').strip() if identity else ''
        sku = (
            str(mapping.sku or '').strip()
            or (str(identity.sku or '').strip() if identity else '')
            or (str(lifecycle.sku_snapshot or '').strip() if lifecycle else '')
            or 'SKU unavailable'
        )
        audit = latest_audit.get(variation_id)
        products[variation_id] = LifecycleProductRow(
            square_variation_id=variation_id,
            sku=sku,
            product_name=product_name or UNKNOWN_PRODUCT_NAME,
            vendor_name=str(vendor.name),
            status=lifecycle.status if lifecycle else ACTIVE,
            row_version=lifecycle.row_version if lifecycle else 0,
            status_note=lifecycle.status_note if lifecycle else None,
            changed_at=(audit.created_at if audit else lifecycle.updated_at if lifecycle else None),
            changed_by=(actors.get(int(audit.actor_principal_id)) if audit and audit.actor_principal_id else None),
            pre_archive_status=lifecycle.pre_archive_status if lifecycle else None,
            mapped=True,
            product_name_available=bool(product_name),
        )

    for variation_id, lifecycle in lifecycle_by_id.items():
        if variation_id in products:
            continue
        identity = identities.get(variation_id)
        product_name = str(identity.product_name or '').strip() if identity else ''
        audit = latest_audit.get(variation_id)
        products[variation_id] = LifecycleProductRow(
            square_variation_id=variation_id,
            sku=(str(identity.sku or '').strip() if identity else '') or lifecycle.sku_snapshot or 'SKU unavailable',
            product_name=product_name or UNKNOWN_PRODUCT_NAME,
            vendor_name='Unknown vendor',
            status=lifecycle.status,
            row_version=lifecycle.row_version,
            status_note=lifecycle.status_note,
            changed_at=audit.created_at if audit else lifecycle.updated_at,
            changed_by=(actors.get(int(audit.actor_principal_id)) if audit and audit.actor_principal_id else None),
            pre_archive_status=lifecycle.pre_archive_status,
            mapped=False,
            product_name_available=bool(product_name),
        )

    covered = sum(1 for variation_id in mapped_ids if products[variation_id].product_name_available)
    coverage = CatalogCoverage(
        expected_mapped_count=len(mapped_ids),
        covered_mapped_count=covered,
        missing_mapped_count=max(0, len(mapped_ids) - covered),
        last_result=refresh_state.last_result if refresh_state else 'NEVER',
        last_attempted_at=refresh_state.last_attempted_at if refresh_state else None,
        last_successful_at=refresh_state.last_successful_at if refresh_state else None,
        last_error=refresh_state.last_error if refresh_state else None,
    )

    active_stores = tuple(
        db.execute(
            select(Store.id, Store.name, Store.square_location_id)
            .where(Store.active.is_(True))
            .order_by(Store.name, Store.id)
        ).all()
    )
    query_count += 1
    inventory_run = db.execute(
        select(OrderingInventoryRefreshRun)
        .order_by(OrderingInventoryRefreshRun.completed_at.desc(), OrderingInventoryRefreshRun.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    query_count += 1
    inventory_rows = db.execute(
        select(OrderingCurrentInventory).where(
            OrderingCurrentInventory.square_variation_id.in_(variation_ids),
            OrderingCurrentInventory.store_id.in_(tuple(int(store.id) for store in active_stores)),
        )
    ).scalars().all()
    query_count += 1
    inventory_by_key = {(row.square_variation_id, int(row.store_id)): row for row in inventory_rows}
    now = datetime.now(tz=timezone.utc)
    for variation_id, product in tuple(products.items()):
        if not product.mapped:
            products[variation_id] = replace(
                product,
                inventory_unavailable_reason=(
                    'Inventory unavailable because this lifecycle record is not in the current mapped population.'
                ),
            )
            continue
        details: list[LifecycleStoreInventory] = []
        blockers: list[str] = []
        fresh_total = Decimal('0')
        fresh_count = 0
        stale_count = 0
        last_known_total = Decimal('0')
        last_known_count = 0
        for store in active_stores:
            stored = inventory_by_key.get((variation_id, int(store.id)))
            store_name = str(store.name)
            current_location = str(store.square_location_id).strip() if store.square_location_id else ''
            state = 'UNAVAILABLE' if inventory_run is None else CRITICAL
            reason = ''
            quantity = Decimal(stored.counted_quantity) if stored is not None else None
            if stored is not None and current_location and stored.square_location_id == current_location:
                last_known_total += Decimal(stored.counted_quantity)
                last_known_count += 1
            if inventory_run is None:
                reason = 'No Ordering inventory refresh has completed.'
            elif inventory_run.result == 'FAILED':
                reason = 'The latest Ordering inventory refresh failed.'
            elif not current_location:
                reason = 'This active store has no Square location identity.'
            elif stored is None:
                reason = 'Square omitted this required store and product pair.'
            elif stored.square_location_id != current_location:
                reason = 'The stored Square location no longer matches this store.'
            elif int(stored.refresh_run_id) != int(inventory_run.id):
                reason = 'The latest refresh did not return this required store and product pair.'
            else:
                state = effective_freshness(stored.refreshed_at, now=now)
                if state == FRESH:
                    fresh_total += Decimal(stored.counted_quantity)
                    fresh_count += 1
                elif state == STALE:
                    stale_count += 1
                    reason = 'The last successful retrieval is more than 24 hours old.'
                else:
                    reason = 'The last successful retrieval is more than 72 hours old.'
            if state != FRESH:
                blockers.append(store_name)
            details.append(
                LifecycleStoreInventory(
                    store_id=int(store.id),
                    store_name=store_name,
                    on_hand=quantity,
                    state=state,
                    refreshed_at=stored.refreshed_at if stored is not None else None,
                    source_calculated_at=stored.source_calculated_at if stored is not None else None,
                    age_label=_age_label(stored.refreshed_at, now=now) if stored is not None else '',
                    reason=reason,
                )
            )
        all_fresh = bool(active_stores) and fresh_count == len(active_stores)
        if all_fresh:
            aggregate_state = FRESH
        elif inventory_run is None:
            aggregate_state = 'UNAVAILABLE'
        elif stale_count and stale_count + fresh_count == len(active_stores):
            aggregate_state = STALE
        else:
            aggregate_state = 'UNKNOWN'
        products[variation_id] = replace(
            product,
            inventory_source_available=inventory_run is not None,
            current_inventory_total=fresh_total if all_fresh else None,
            last_known_inventory_total=(
                last_known_total if active_stores and last_known_count == len(active_stores) else None
            ),
            inventory_by_store=tuple(details),
            inventory_state=aggregate_state,
            inventory_as_of=inventory_run.completed_at if inventory_run else None,
            inventory_coverage=f'{fresh_count} of {len(active_stores)} active stores Fresh',
            inventory_blocking_stores=tuple(blockers),
            inventory_unavailable_reason=(
                'Inventory unavailable. Run the owner-only Ordering inventory refresh.'
                if inventory_run is None
                else 'A trusted company total requires Fresh evidence for every active store.'
            ),
        )

    return tuple(products.values()), coverage, query_count, inventory_run


def list_lifecycle_products(db: Session, *, archived: bool) -> tuple[LifecycleProductRow, ...]:
    """Return the complete local mutation-validation source without Square or per-product SQL."""
    rows, _coverage, _query_count, _inventory_run = _catalog_rows(db)
    selected = [row for row in rows if (row.status == ARCHIVED) is archived]
    return tuple(sorted(selected, key=lambda row: (_normalized(row.product_name), row.square_variation_id)))


def query_lifecycle_workspace(
    db: Session,
    *,
    archived: bool,
    filters: LifecycleWorkspaceFilters,
    sort: str,
    direction: str,
    page_number: int,
    page_size: int,
) -> LifecycleWorkspacePage:
    rows, coverage, query_count, inventory_run = _catalog_rows(db)
    status_counts = {status: 0 for status in (ACTIVE, NO_FUTURE_REORDER, ARCHIVED)}
    for row in rows:
        status_counts[row.status] = status_counts.get(row.status, 0) + 1

    scoped = [row for row in rows if (row.status == ARCHIVED) is archived]
    unfiltered_count = len(scoped)
    vendor_options = tuple(sorted({row.vendor_name for row in scoped}, key=_normalized))
    product_search = _normalized(filters.product_search)
    sku_search = _normalized(filters.sku_search)
    vendor = _normalized(filters.vendor)
    lifecycle = filters.lifecycle.strip().upper()
    mapping = filters.mapping.strip().upper() or 'ANY'
    name_state = filters.name_state.strip().upper() or 'ANY'
    inventory_filter = filters.inventory.strip().upper() or 'ANY'

    if mapping not in MAPPING_FILTERS or name_state not in NAME_FILTERS or inventory_filter not in INVENTORY_FILTERS:
        raise ValueError('Unsupported lifecycle workspace filter')
    if lifecycle and lifecycle not in LIFECYCLE_ORDER:
        raise ValueError('Unsupported lifecycle status filter')
    if sort not in SORT_FIELDS or direction not in {'asc', 'desc'}:
        raise ValueError('Unsupported lifecycle workspace sort')

    filtered: list[LifecycleProductRow] = []
    for row in scoped:
        if product_search and (not row.product_name_available or product_search not in _normalized(row.product_name)):
            continue
        if sku_search and sku_search not in _normalized(row.sku):
            continue
        if vendor and vendor != _normalized(row.vendor_name):
            continue
        if lifecycle and lifecycle != row.status:
            continue
        if mapping == 'MAPPED' and not row.mapped:
            continue
        if mapping == 'UNMAPPED' and row.mapped:
            continue
        if name_state == 'KNOWN' and not row.product_name_available:
            continue
        if name_state == 'UNKNOWN' and row.product_name_available:
            continue
        if inventory_filter == 'POSITIVE' and not (
            row.inventory_state == FRESH and row.current_inventory_total is not None and row.current_inventory_total > 0
        ):
            continue
        if inventory_filter == 'ZERO' and not (
            row.inventory_state == FRESH and row.current_inventory_total == 0
        ):
            continue
        if inventory_filter == 'UNKNOWN' and row.current_inventory_total is not None:
            continue
        if inventory_filter == 'STALE' and row.inventory_state != STALE:
            continue
        filtered.append(row)

    def sort_key(row: LifecycleProductRow):
        values = {
            'product': (not row.product_name_available, _normalized(row.product_name)),
            'sku': _normalized(row.sku),
            'vendor': _normalized(row.vendor_name),
            'lifecycle': LIFECYCLE_ORDER.get(row.status, 99),
            'changed_at': row.changed_at.timestamp() if row.changed_at else float('-inf'),
            'changed_by': _normalized(row.changed_by or ''),
        }
        return values[sort]

    filtered.sort(key=lambda row: row.square_variation_id)
    if sort == 'inventory':
        known_inventory = [row for row in filtered if row.current_inventory_total is not None]
        unavailable_inventory = [row for row in filtered if row.current_inventory_total is None]
        known_inventory.sort(
            key=lambda row: Decimal(row.current_inventory_total or 0),
            reverse=direction == 'desc',
        )
        state_order = {STALE: 0, 'UNKNOWN': 1, 'UNAVAILABLE': 2}
        unavailable_inventory.sort(
            key=lambda row: (state_order.get(row.inventory_state, 9), row.square_variation_id)
        )
        filtered = known_inventory + unavailable_inventory
    else:
        filtered.sort(key=sort_key, reverse=direction == 'desc')
    total_count = len(filtered)
    total_pages = max(1, ceil(total_count / page_size))
    resolved_page = min(page_number, total_pages)
    start = (resolved_page - 1) * page_size
    page_rows = tuple(filtered[start : start + page_size])
    return LifecycleWorkspacePage(
        rows=page_rows,
        total_count=total_count,
        unfiltered_count=unfiltered_count,
        page_number=resolved_page,
        page_size=page_size,
        total_pages=total_pages,
        range_start=start + 1 if total_count else 0,
        range_end=start + len(page_rows),
        status_counts=status_counts,
        vendor_options=vendor_options,
        coverage=coverage,
        inventory_refresh=inventory_run,
        query_count=query_count,
    )
