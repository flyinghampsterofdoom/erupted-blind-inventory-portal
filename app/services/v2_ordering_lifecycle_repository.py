from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from math import ceil

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AuditLog,
    OrderingProductLifecycle,
    Principal,
    Store,
    TouchscreenSquareVariationCache,
    TouchscreenStoreInventoryCache,
    Vendor,
    VendorSkuConfig,
)


ACTIVE = 'ACTIVE'
NO_FUTURE_REORDER = 'NO_FUTURE_REORDER'
ARCHIVED = 'ARCHIVED'
LIFECYCLE_ORDER = {ACTIVE: 0, NO_FUTURE_REORDER: 1, ARCHIVED: 2}
INVENTORY_FILTERS = {'ANY', 'POSITIVE', 'ZERO', 'UNKNOWN'}
MAPPING_FILTERS = {'ANY', 'MAPPED', 'UNMAPPED'}
SORT_FIELDS = {'product', 'sku', 'vendor', 'lifecycle', 'changed_at', 'changed_by'}
AUDIT_ACTION = 'V2:ordering_lifecycle:lifecycle_status_changed'


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
    inventory_total: Decimal | None = None
    relevant_store_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class LifecycleWorkspaceFilters:
    product_search: str = ''
    sku_search: str = ''
    vendor: str = ''
    lifecycle: str = ''
    store: str = ''
    inventory: str = 'ANY'
    mapping: str = 'ANY'


@dataclass(frozen=True)
class LifecycleWorkspacePage:
    rows: tuple[LifecycleProductRow, ...]
    total_count: int
    page_number: int
    page_size: int
    total_pages: int
    range_start: int
    range_end: int
    status_counts: dict[str, int]
    vendor_options: tuple[str, ...]
    store_options: tuple[tuple[int, str], ...]
    query_count: int


def _clean_ids(variation_ids: list[str] | tuple[str, ...] | set[str]) -> tuple[str, ...]:
    return tuple(sorted({str(value).strip() for value in variation_ids if str(value).strip()}))


def _normalized(value: str | None) -> str:
    return ' '.join(str(value or '').casefold().split())


def _product_name(item_name: str | None, variation_name: str | None, fallback: str) -> str:
    values = [str(value).strip() for value in (item_name, variation_name) if str(value or '').strip()]
    return ' — '.join(values) if values else fallback


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


def _catalog_rows(db: Session) -> tuple[tuple[LifecycleProductRow, ...], tuple[tuple[int, str], ...], int]:
    """Build the local lifecycle catalog with a fixed six-query budget and no Square access."""
    query_count = 0
    lifecycle_rows = db.execute(select(OrderingProductLifecycle)).scalars().all()
    query_count += 1

    lifecycle_by_id = {row.square_variation_id: row for row in lifecycle_rows}

    mapping_rows = db.execute(
        select(VendorSkuConfig, Vendor, TouchscreenSquareVariationCache)
        .join(Vendor, Vendor.id == VendorSkuConfig.vendor_id)
        .outerjoin(
            TouchscreenSquareVariationCache,
            TouchscreenSquareVariationCache.square_variation_id == VendorSkuConfig.square_variation_id,
        )
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
        {str(mapping.square_variation_id or '') for mapping, _vendor, _cache in mapping_rows}
        | set(lifecycle_by_id)
    )
    inventory_by_id: dict[str, list[TouchscreenStoreInventoryCache]] = {}
    if variation_ids:
        inventory_rows = db.execute(
            select(TouchscreenStoreInventoryCache).where(
                TouchscreenStoreInventoryCache.square_variation_id.in_(variation_ids)
            )
        ).scalars().all()
    else:
        inventory_rows = []
    query_count += 1
    for inventory in inventory_rows:
        inventory_by_id.setdefault(inventory.square_variation_id, []).append(inventory)

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
    stores = tuple(
        (int(store.id), str(store.name))
        for store in db.execute(select(Store).where(Store.active.is_(True)).order_by(Store.name, Store.id)).scalars().all()
    )
    query_count += 1

    products: dict[str, LifecycleProductRow] = {}
    for mapping, vendor, cache in mapping_rows:
        variation_id = str(mapping.square_variation_id or '').strip()
        if not variation_id or variation_id in products:
            continue
        lifecycle = lifecycle_by_id.get(variation_id)
        sku = str(mapping.sku or '').strip() or (lifecycle.sku_snapshot if lifecycle else None) or 'SKU unavailable'
        fallback_name = (lifecycle.product_name_snapshot if lifecycle else None) or sku
        inventory = inventory_by_id.get(variation_id, [])
        audit = latest_audit.get(variation_id)
        products[variation_id] = LifecycleProductRow(
            square_variation_id=variation_id,
            sku=sku,
            product_name=_product_name(
                cache.item_name if cache else None,
                cache.variation_name if cache else None,
                fallback_name,
            ),
            vendor_name=str(vendor.name),
            status=lifecycle.status if lifecycle else ACTIVE,
            row_version=lifecycle.row_version if lifecycle else 0,
            status_note=lifecycle.status_note if lifecycle else None,
            changed_at=(audit.created_at if audit else lifecycle.updated_at if lifecycle else None),
            changed_by=(actors.get(int(audit.actor_principal_id)) if audit and audit.actor_principal_id else None),
            pre_archive_status=lifecycle.pre_archive_status if lifecycle else None,
            mapped=True,
            inventory_total=(sum((row.available_quantity for row in inventory), Decimal('0')) if inventory else None),
            relevant_store_ids=tuple(sorted({int(row.store_id) for row in inventory if row.is_location_present})),
        )

    for variation_id, lifecycle in lifecycle_by_id.items():
        if variation_id in products:
            continue
        inventory = inventory_by_id.get(variation_id, [])
        audit = latest_audit.get(variation_id)
        sku = lifecycle.sku_snapshot or 'SKU unavailable'
        products[variation_id] = LifecycleProductRow(
            square_variation_id=variation_id,
            sku=sku,
            product_name=lifecycle.product_name_snapshot or lifecycle.sku_snapshot or 'Product name unavailable',
            vendor_name='Unknown vendor',
            status=lifecycle.status,
            row_version=lifecycle.row_version,
            status_note=lifecycle.status_note,
            changed_at=audit.created_at if audit else lifecycle.updated_at,
            changed_by=(actors.get(int(audit.actor_principal_id)) if audit and audit.actor_principal_id else None),
            pre_archive_status=lifecycle.pre_archive_status,
            mapped=False,
            inventory_total=(sum((row.available_quantity for row in inventory), Decimal('0')) if inventory else None),
            relevant_store_ids=tuple(sorted({int(row.store_id) for row in inventory if row.is_location_present})),
        )
    return tuple(products.values()), stores, query_count


def list_lifecycle_products(db: Session, *, archived: bool) -> tuple[LifecycleProductRow, ...]:
    """Return the complete local mutation-validation source without Square or per-product SQL."""
    rows, _stores, _query_count = _catalog_rows(db)
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
    rows, stores, query_count = _catalog_rows(db)
    status_counts = {status: 0 for status in (ACTIVE, NO_FUTURE_REORDER, ARCHIVED)}
    for row in rows:
        status_counts[row.status] = status_counts.get(row.status, 0) + 1

    scoped = [row for row in rows if (row.status == ARCHIVED) is archived]
    vendor_options = tuple(sorted({row.vendor_name for row in scoped}, key=_normalized))
    product_search = _normalized(filters.product_search)
    sku_search = _normalized(filters.sku_search)
    vendor = _normalized(filters.vendor)
    lifecycle = filters.lifecycle.strip().upper()
    store = filters.store.strip().lower()
    inventory = filters.inventory.strip().upper() or 'ANY'
    mapping = filters.mapping.strip().upper() or 'ANY'

    if inventory not in INVENTORY_FILTERS or mapping not in MAPPING_FILTERS:
        raise ValueError('Unsupported lifecycle workspace filter')
    if lifecycle and lifecycle not in LIFECYCLE_ORDER:
        raise ValueError('Unsupported lifecycle status filter')

    filtered: list[LifecycleProductRow] = []
    for row in scoped:
        if product_search and product_search not in _normalized(row.product_name):
            continue
        if sku_search and sku_search not in _normalized(row.sku):
            continue
        if vendor and vendor != _normalized(row.vendor_name):
            continue
        if lifecycle and lifecycle != row.status:
            continue
        if store:
            if store == 'unknown':
                if row.relevant_store_ids:
                    continue
            else:
                try:
                    store_id = int(store)
                except ValueError as exc:
                    raise ValueError('Unsupported store relevance filter') from exc
                if store_id not in row.relevant_store_ids:
                    continue
        if inventory == 'POSITIVE' and not (row.inventory_total is not None and row.inventory_total > 0):
            continue
        if inventory == 'ZERO' and not (row.inventory_total is not None and row.inventory_total <= 0):
            continue
        if inventory == 'UNKNOWN' and row.inventory_total is not None:
            continue
        if mapping == 'MAPPED' and not row.mapped:
            continue
        if mapping == 'UNMAPPED' and row.mapped:
            continue
        filtered.append(row)

    reverse = direction == 'desc'
    if sort not in SORT_FIELDS or direction not in {'asc', 'desc'}:
        raise ValueError('Unsupported lifecycle workspace sort')

    def sort_key(row: LifecycleProductRow):
        values = {
            'product': _normalized(row.product_name),
            'sku': _normalized(row.sku),
            'vendor': _normalized(row.vendor_name),
            'lifecycle': LIFECYCLE_ORDER.get(row.status, 99),
            'changed_at': row.changed_at.timestamp() if row.changed_at else float('-inf'),
            'changed_by': _normalized(row.changed_by or ''),
        }
        return values[sort]

    # The stable variation-ID tie-breaker is always ascending, including descending primary sorts.
    filtered.sort(key=lambda row: row.square_variation_id)
    filtered.sort(key=sort_key, reverse=reverse)
    total_count = len(filtered)
    total_pages = max(1, ceil(total_count / page_size))
    resolved_page = min(page_number, total_pages)
    start = (resolved_page - 1) * page_size
    page_rows = tuple(filtered[start : start + page_size])
    return LifecycleWorkspacePage(
        rows=page_rows,
        total_count=total_count,
        page_number=resolved_page,
        page_size=page_size,
        total_pages=total_pages,
        range_start=start + 1 if total_count else 0,
        range_end=start + len(page_rows),
        status_counts=status_counts,
        vendor_options=vendor_options,
        store_options=stores,
        query_count=query_count,
    )
