from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OrderingProductLifecycle, Vendor, VendorSkuConfig


ACTIVE = 'ACTIVE'
NO_FUTURE_REORDER = 'NO_FUTURE_REORDER'
ARCHIVED = 'ARCHIVED'


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


def _clean_ids(variation_ids: list[str] | tuple[str, ...] | set[str]) -> tuple[str, ...]:
    return tuple(sorted({str(value).strip() for value in variation_ids if str(value).strip()}))


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


def list_lifecycle_products(db: Session, *, archived: bool) -> tuple[LifecycleProductRow, ...]:
    """Return a bounded-management source without Square calls or product-level SQL."""
    if archived:
        lifecycle_rows = db.execute(
            select(OrderingProductLifecycle)
            .where(OrderingProductLifecycle.status == ARCHIVED)
            .order_by(OrderingProductLifecycle.product_name_snapshot, OrderingProductLifecycle.square_variation_id)
        ).scalars().all()
        return tuple(
            LifecycleProductRow(
                square_variation_id=row.square_variation_id,
                sku=row.sku_snapshot or 'SKU unavailable',
                product_name=row.product_name_snapshot or row.sku_snapshot or row.square_variation_id,
                vendor_name='Archived product',
                status=row.status,
                row_version=row.row_version,
                status_note=row.status_note,
            )
            for row in lifecycle_rows
        )

    rows = db.execute(
        select(VendorSkuConfig, Vendor, OrderingProductLifecycle)
        .join(Vendor, Vendor.id == VendorSkuConfig.vendor_id)
        .outerjoin(
            OrderingProductLifecycle,
            OrderingProductLifecycle.square_variation_id == VendorSkuConfig.square_variation_id,
        )
        .where(
            VendorSkuConfig.active.is_(True),
            VendorSkuConfig.is_default_vendor.is_(True),
            Vendor.active.is_(True),
            VendorSkuConfig.square_variation_id.is_not(None),
        )
        .order_by(Vendor.name, VendorSkuConfig.sku, VendorSkuConfig.id)
    ).all()
    products: dict[str, LifecycleProductRow] = {}
    for mapping, vendor, lifecycle in rows:
        variation_id = str(mapping.square_variation_id or '').strip()
        if not variation_id or (lifecycle is not None and lifecycle.status == ARCHIVED):
            continue
        products.setdefault(
            variation_id,
            LifecycleProductRow(
                square_variation_id=variation_id,
                sku=str(mapping.sku),
                product_name=(lifecycle.product_name_snapshot if lifecycle else None) or str(mapping.sku),
                vendor_name=str(vendor.name),
                status=lifecycle.status if lifecycle else ACTIVE,
                row_version=lifecycle.row_version if lifecycle else 0,
                status_note=lifecycle.status_note if lifecycle else None,
            ),
        )
    return tuple(products.values())
