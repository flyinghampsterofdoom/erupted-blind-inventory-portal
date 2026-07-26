from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import (
    OrderingCurrentInventory,
    OrderingInventoryRefreshRun,
    Store,
    Vendor,
    VendorSkuConfig,
)


FRESH = 'FRESH'
STALE = 'STALE'
CRITICAL = 'CRITICAL'
INVENTORY_REFRESH_LOCK_KEY = 730202607250009


@dataclass(frozen=True)
class InventoryStoreIdentity:
    store_id: int
    store_name: str
    square_location_id: str | None


@dataclass(frozen=True)
class InventoryExpectedScope:
    variation_ids: tuple[str, ...]
    stores: tuple[InventoryStoreIdentity, ...]

    @property
    def expected_pair_count(self) -> int:
        return len(self.variation_ids) * len(self.stores)


@dataclass(frozen=True)
class InventoryObservation:
    square_variation_id: str
    store_id: int
    square_location_id: str
    quantity: Decimal
    source_calculated_at: datetime | None


def effective_freshness(refreshed_at: datetime, *, now: datetime) -> str:
    refreshed = refreshed_at if refreshed_at.tzinfo else refreshed_at.replace(tzinfo=timezone.utc)
    current = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    age = max(timedelta(0), current.astimezone(timezone.utc) - refreshed.astimezone(timezone.utc))
    if age <= timedelta(hours=24):
        return FRESH
    if age <= timedelta(hours=72):
        return STALE
    return CRITICAL


def load_inventory_expected_scope(db: Session) -> InventoryExpectedScope:
    variation_values = db.execute(
        select(VendorSkuConfig.square_variation_id)
        .join(Vendor, Vendor.id == VendorSkuConfig.vendor_id)
        .where(
            VendorSkuConfig.active.is_(True),
            VendorSkuConfig.is_default_vendor.is_(True),
            Vendor.active.is_(True),
            VendorSkuConfig.square_variation_id.is_not(None),
        )
    ).scalars().all()
    variations = tuple(sorted({str(value).strip() for value in variation_values if str(value or '').strip()}))
    stores = tuple(
        InventoryStoreIdentity(
            store_id=int(row.id),
            store_name=str(row.name),
            square_location_id=str(row.square_location_id).strip() if row.square_location_id else None,
        )
        for row in db.execute(
            select(Store.id, Store.name, Store.square_location_id)
            .where(Store.active.is_(True))
            .order_by(Store.name, Store.id)
        ).all()
    )
    return InventoryExpectedScope(variations, stores)


def try_inventory_refresh_lock(db: Session) -> bool:
    return bool(
        db.execute(
            text('SELECT pg_try_advisory_xact_lock(:lock_key)'),
            {'lock_key': INVENTORY_REFRESH_LOCK_KEY},
        ).scalar_one()
    )


def latest_inventory_refresh_run(db: Session) -> OrderingInventoryRefreshRun | None:
    return db.execute(
        select(OrderingInventoryRefreshRun)
        .order_by(OrderingInventoryRefreshRun.completed_at.desc(), OrderingInventoryRefreshRun.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def load_current_inventory_rows(
    db: Session,
    *,
    variation_ids: tuple[str, ...],
    store_ids: tuple[int, ...],
) -> dict[tuple[str, int], OrderingCurrentInventory]:
    if not variation_ids or not store_ids:
        return {}
    rows = db.execute(
        select(OrderingCurrentInventory).where(
            OrderingCurrentInventory.square_variation_id.in_(variation_ids),
            OrderingCurrentInventory.store_id.in_(store_ids),
        )
    ).scalars().all()
    return {(row.square_variation_id, int(row.store_id)): row for row in rows}


def persist_inventory_refresh(
    db: Session,
    *,
    run: OrderingInventoryRefreshRun,
    observations: tuple[InventoryObservation, ...],
    refreshed_at: datetime,
) -> None:
    db.add(run)
    db.flush()
    if not observations:
        return
    variation_ids = tuple(sorted({row.square_variation_id for row in observations}))
    store_ids = tuple(sorted({row.store_id for row in observations}))
    existing = load_current_inventory_rows(db, variation_ids=variation_ids, store_ids=store_ids)
    for observation in observations:
        key = (observation.square_variation_id, observation.store_id)
        row = existing.get(key)
        if row is None:
            row = OrderingCurrentInventory(
                square_variation_id=observation.square_variation_id,
                store_id=observation.store_id,
                square_location_id=observation.square_location_id,
                counted_quantity=observation.quantity,
                source_calculated_at=observation.source_calculated_at,
                refreshed_at=refreshed_at,
                freshness_state=FRESH,
                refresh_run_id=run.id,
            )
            db.add(row)
            existing[key] = row
        else:
            row.square_location_id = observation.square_location_id
            row.counted_quantity = observation.quantity
            row.source_calculated_at = observation.source_calculated_at
            row.refreshed_at = refreshed_at
            row.freshness_state = FRESH
            row.refresh_run_id = run.id
    db.flush()
