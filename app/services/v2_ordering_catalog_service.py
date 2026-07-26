from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    OrderingCatalogIdentity,
    OrderingCatalogRefreshState,
    OrderingProductLifecycle,
    Vendor,
    VendorSkuConfig,
)
from app.services.v2_ordering_square_gateway import SquareOrderingReadGateway
from app.v2.audit import V2AuditEvent, write_v2_audit_event


COMPLETE = 'COMPLETE'
PARTIAL = 'PARTIAL'
FAILED = 'FAILED'


@dataclass(frozen=True)
class OrderingCatalogRefreshResult:
    outcome: str
    expected_mapped_count: int
    covered_mapped_count: int
    missing_mapped_count: int
    returned_variation_count: int
    square_request_count: int
    square_page_count: int
    attempted_at: datetime
    last_successful_at: datetime | None
    error: str = ''


def _mapped_variation_ids(db: Session) -> tuple[str, ...]:
    values = db.execute(
        select(VendorSkuConfig.square_variation_id)
        .join(Vendor, Vendor.id == VendorSkuConfig.vendor_id)
        .where(
            VendorSkuConfig.active.is_(True),
            VendorSkuConfig.is_default_vendor.is_(True),
            Vendor.active.is_(True),
            VendorSkuConfig.square_variation_id.is_not(None),
        )
    ).scalars().all()
    return tuple(sorted({str(value).strip() for value in values if str(value or '').strip()}))


def _requested_variation_ids(db: Session, mapped_ids: tuple[str, ...]) -> tuple[str, ...]:
    lifecycle_ids = db.execute(select(OrderingProductLifecycle.square_variation_id)).scalars().all()
    return tuple(sorted(set(mapped_ids) | {str(value).strip() for value in lifecycle_ids if str(value).strip()}))


def _refresh_state(db: Session) -> OrderingCatalogRefreshState:
    state = db.get(OrderingCatalogRefreshState, 1)
    if state is None:
        state = OrderingCatalogRefreshState(id=1)
        db.add(state)
    return state


def _coverage_count(db: Session, mapped_ids: tuple[str, ...]) -> int:
    if not mapped_ids:
        return 0
    rows = db.execute(
        select(OrderingCatalogIdentity.square_variation_id, OrderingCatalogIdentity.product_name).where(
            OrderingCatalogIdentity.square_variation_id.in_(mapped_ids)
        )
    ).all()
    return sum(1 for row in rows if str(row.product_name or '').strip())


def _display_name(item_name: str, variation_name: str) -> str | None:
    values = [value.strip() for value in (item_name, variation_name) if value.strip()]
    return ' — '.join(values)[:500] if values else None


def refresh_ordering_catalog_identity(
    db: Session,
    *,
    actor_principal_id: int,
    ip: str | None,
    gateway: SquareOrderingReadGateway | None = None,
    attempted_at: datetime | None = None,
) -> OrderingCatalogRefreshResult:
    """Refresh Ordering-owned identity metadata without touching lifecycle or touchscreen data."""
    now = attempted_at or datetime.now(tz=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    mapped_ids = _mapped_variation_ids(db)
    requested_ids = _requested_variation_ids(db, mapped_ids)
    state = _refresh_state(db)
    previous_success = state.last_successful_at

    catalog_gateway = gateway or SquareOrderingReadGateway()
    try:
        catalog = catalog_gateway.fetch_catalog_identity(list(requested_ids))
    except Exception as exc:
        error = f'Square catalog read failed ({type(exc).__name__}).'
        failed_metrics = (
            catalog_gateway.current_metrics()
            if hasattr(catalog_gateway, 'current_metrics')
            else None
        )
        failed_requests = failed_metrics.request_count if failed_metrics else 0
        failed_pages = (
            dict(failed_metrics.endpoint_request_counts).get('/v2/catalog/search-catalog-items', 0)
            if failed_metrics
            else 0
        )
        covered = _coverage_count(db, mapped_ids)
        state.last_result = FAILED
        state.expected_mapped_count = len(mapped_ids)
        state.covered_mapped_count = covered
        state.missing_mapped_count = max(0, len(mapped_ids) - covered)
        state.last_attempted_at = now
        state.last_error = error
        state.last_refreshed_by_principal_id = actor_principal_id
        write_v2_audit_event(
            db,
            event=V2AuditEvent(
                actor_principal_id=actor_principal_id,
                action='catalog_refresh_failed',
                domain='ordering_catalog',
                entity_type='ordering_catalog_snapshot',
                entity_id='singleton',
                timestamp=now,
                external_outcome={
                    'result': FAILED,
                    'error': error,
                    'square_request_count': failed_requests,
                },
                metadata={
                    'expected_mapped_count': len(mapped_ids),
                    'covered_mapped_count': covered,
                    'missing_mapped_count': max(0, len(mapped_ids) - covered),
                    'square_page_count': failed_pages,
                },
            ),
            ip=ip,
        )
        db.flush()
        return OrderingCatalogRefreshResult(
            FAILED, len(mapped_ids), covered, max(0, len(mapped_ids) - covered),
            0, failed_requests, failed_pages, now, previous_success, error,
        )

    existing = {
        row.square_variation_id: row
        for row in db.execute(
            select(OrderingCatalogIdentity).where(
                OrderingCatalogIdentity.square_variation_id.in_(tuple(catalog.products))
            )
        ).scalars().all()
    } if catalog.products else {}
    for variation_id, product in catalog.products.items():
        row = existing.get(variation_id)
        if row is None:
            row = OrderingCatalogIdentity(square_variation_id=variation_id, last_seen_at=now)
            db.add(row)
        row.square_item_id = product.item_id or row.square_item_id
        row.sku = product.sku[:255] if product.sku else row.sku
        row.item_name = product.item_name or row.item_name
        row.variation_name = product.variation_name or row.variation_name
        row.product_name = _display_name(product.item_name, product.variation_name) or row.product_name
        row.square_is_deleted = product.confirmed_discontinued
        row.square_updated_at = product.updated_at or row.square_updated_at
        row.last_seen_at = now
    db.flush()

    covered = _coverage_count(db, mapped_ids)
    returned_mapped = len(set(mapped_ids) & set(catalog.products))
    outcome = COMPLETE if returned_mapped == len(mapped_ids) and covered == len(mapped_ids) else PARTIAL
    state.last_result = outcome
    state.expected_mapped_count = len(mapped_ids)
    state.covered_mapped_count = covered
    state.missing_mapped_count = max(0, len(mapped_ids) - covered)
    state.last_attempted_at = now
    state.last_error = None if outcome == COMPLETE else 'Square catalog did not return every mapped variation.'
    state.last_refreshed_by_principal_id = actor_principal_id
    if outcome == COMPLETE:
        state.last_successful_at = now
    square_pages = dict(catalog.metrics.endpoint_request_counts).get('/v2/catalog/search-catalog-items', 0)
    write_v2_audit_event(
        db,
        event=V2AuditEvent(
            actor_principal_id=actor_principal_id,
            action='catalog_refresh_completed' if outcome == COMPLETE else 'catalog_refresh_partial',
            domain='ordering_catalog',
            entity_type='ordering_catalog_snapshot',
            entity_id='singleton',
            timestamp=now,
            external_outcome={'result': outcome, 'square_request_count': catalog.metrics.request_count},
            metadata={
                'expected_mapped_count': len(mapped_ids),
                'returned_mapped_count': returned_mapped,
                'covered_mapped_count': covered,
                'missing_mapped_count': max(0, len(mapped_ids) - covered),
                'square_page_count': square_pages,
            },
        ),
        ip=ip,
    )
    db.flush()
    return OrderingCatalogRefreshResult(
        outcome,
        len(mapped_ids),
        covered,
        max(0, len(mapped_ids) - covered),
        len(catalog.products),
        catalog.metrics.request_count,
        square_pages,
        now,
        state.last_successful_at,
        state.last_error or '',
    )
