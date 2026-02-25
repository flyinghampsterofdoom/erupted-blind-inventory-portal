from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import and_, delete, select
from sqlalchemy.orm import Session

from app.models import (
    NonSellableItem,
    NonSellableStockTake,
    NonSellableStockTakeLine,
    NonSellableStockTakeStatus,
    Store,
)

DEFAULT_ITEMS = ['Toilet Paper', 'Windex', 'Water Jugs']


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def ensure_default_items(db: Session) -> None:
    existing_names = {row[0] for row in db.execute(select(NonSellableItem.name)).all()}
    for name in DEFAULT_ITEMS:
        if name in existing_names:
            continue
        db.add(
            NonSellableItem(
                name=name,
                active=True,
            )
        )
    db.flush()


def list_items(db: Session, *, include_inactive: bool = False) -> list[NonSellableItem]:
    ensure_default_items(db)
    query = select(NonSellableItem).order_by(NonSellableItem.name.asc())
    if not include_inactive:
        query = query.where(NonSellableItem.active.is_(True))
    return db.execute(query).scalars().all()


def add_item(db: Session, *, name: str, created_by_principal_id: int) -> NonSellableItem:
    clean_name = name.strip()
    if not clean_name:
        raise ValueError('Item name is required')

    existing = db.execute(select(NonSellableItem).where(NonSellableItem.name == clean_name)).scalar_one_or_none()
    if existing:
        existing.active = True
        existing.created_by_principal_id = created_by_principal_id
        db.flush()
        return existing

    item = NonSellableItem(
        name=clean_name,
        active=True,
        created_by_principal_id=created_by_principal_id,
    )
    db.add(item)
    db.flush()
    return item


def deactivate_item(db: Session, *, item_id: int) -> NonSellableItem:
    item = db.execute(select(NonSellableItem).where(NonSellableItem.id == item_id)).scalar_one_or_none()
    if not item:
        raise ValueError('Item not found')
    item.active = False
    db.flush()
    return item


def _ensure_store(db: Session, store_id: int) -> None:
    exists = db.execute(select(Store.id).where(Store.id == store_id, Store.active.is_(True))).scalar_one_or_none()
    if not exists:
        raise ValueError('Store not found')


def _sync_draft_lines_to_active_items(db: Session, *, stock_take_id: int) -> None:
    active_items = list_items(db, include_inactive=False)
    active_item_ids = {item.id for item in active_items}
    current_item_ids = {
        row[0]
        for row in db.execute(
            select(NonSellableStockTakeLine.item_id).where(NonSellableStockTakeLine.stock_take_id == stock_take_id)
        ).all()
    }

    stale_item_ids = [item_id for item_id in current_item_ids if item_id not in active_item_ids]
    if stale_item_ids:
        db.execute(
            delete(NonSellableStockTakeLine).where(
                NonSellableStockTakeLine.stock_take_id == stock_take_id,
                NonSellableStockTakeLine.item_id.in_(stale_item_ids),
            )
        )

    for item in active_items:
        if item.id in current_item_ids:
            continue
        db.add(
            NonSellableStockTakeLine(
                stock_take_id=stock_take_id,
                item_id=item.id,
                item_name=item.name,
                quantity=0,
            )
        )
    db.flush()


def get_or_create_draft_stock_take(db: Session, *, store_id: int, principal_id: int) -> tuple[NonSellableStockTake, bool]:
    _ensure_store(db, store_id)
    ensure_default_items(db)

    draft = db.execute(
        select(NonSellableStockTake)
        .where(
            NonSellableStockTake.store_id == store_id,
            NonSellableStockTake.status == NonSellableStockTakeStatus.DRAFT,
        )
        .order_by(NonSellableStockTake.created_at.desc())
    ).scalars().first()
    if draft:
        _sync_draft_lines_to_active_items(db, stock_take_id=draft.id)
        return draft, False

    take = NonSellableStockTake(
        store_id=store_id,
        employee_name='',
        status=NonSellableStockTakeStatus.DRAFT,
        created_by_principal_id=principal_id,
    )
    db.add(take)
    db.flush()

    for item in list_items(db, include_inactive=False):
        db.add(
            NonSellableStockTakeLine(
                stock_take_id=take.id,
                item_id=item.id,
                item_name=item.name,
                quantity=0,
            )
        )
    db.flush()
    return take, True


def get_store_draft_stock_take(db: Session, *, store_id: int, stock_take_id: int) -> NonSellableStockTake:
    take = db.execute(select(NonSellableStockTake).where(NonSellableStockTake.id == stock_take_id)).scalar_one_or_none()
    if not take:
        raise ValueError('Non-sellable stock take not found')
    if take.store_id != store_id:
        raise PermissionError('Not allowed to access this stock take')
    if take.status != NonSellableStockTakeStatus.DRAFT:
        raise PermissionError('Submitted stock takes are viewable by lead/admin only')
    return take


def list_stock_take_lines(db: Session, *, stock_take_id: int) -> list[dict]:
    rows = db.execute(
        select(NonSellableStockTakeLine)
        .where(NonSellableStockTakeLine.stock_take_id == stock_take_id)
        .order_by(NonSellableStockTakeLine.item_name.asc())
    ).scalars().all()
    return [
        {
            'item_id': row.item_id,
            'item_name': row.item_name,
            'quantity': row.quantity,
        }
        for row in rows
    ]


def save_or_submit_stock_take(
    db: Session,
    *,
    stock_take: NonSellableStockTake,
    employee_name: str,
    quantities_by_item_id: dict[int, Decimal],
    submit: bool,
    submitted_by_principal_id: int,
) -> NonSellableStockTake:
    if stock_take.status == NonSellableStockTakeStatus.SUBMITTED:
        raise ValueError('Stock take is already submitted')

    clean_name = employee_name.strip()
    if not clean_name:
        raise ValueError('Name is required')

    lines = db.execute(select(NonSellableStockTakeLine).where(NonSellableStockTakeLine.stock_take_id == stock_take.id)).scalars().all()
    for line in lines:
        quantity = quantities_by_item_id.get(line.item_id, 0)
        if quantity < 0:
            raise ValueError(f'Quantity cannot be negative for {line.item_name}')
        line.quantity = Decimal(str(quantity))
        line.updated_at = _now()

    stock_take.employee_name = clean_name
    stock_take.updated_at = _now()
    if submit:
        stock_take.status = NonSellableStockTakeStatus.SUBMITTED
        stock_take.submitted_by_principal_id = submitted_by_principal_id
        stock_take.submitted_at = _now()

    db.flush()
    return stock_take


def list_stock_takes_for_audit(db: Session, *, store_id: int | None, include_draft: bool = True) -> list[dict]:
    query = (
        select(
            NonSellableStockTake.id,
            NonSellableStockTake.store_id,
            Store.name.label('store_name'),
            NonSellableStockTake.employee_name,
            NonSellableStockTake.status,
            NonSellableStockTake.created_at,
            NonSellableStockTake.submitted_at,
        )
        .join(Store, Store.id == NonSellableStockTake.store_id)
        .order_by(NonSellableStockTake.created_at.desc())
    )
    if store_id:
        query = query.where(NonSellableStockTake.store_id == store_id)
    if not include_draft:
        query = query.where(NonSellableStockTake.status == NonSellableStockTakeStatus.SUBMITTED)

    return [
        {
            'id': row.id,
            'store_id': row.store_id,
            'store_name': row.store_name,
            'employee_name': row.employee_name,
            'status': row.status.value if hasattr(row.status, 'value') else str(row.status),
            'created_at': row.created_at,
            'submitted_at': row.submitted_at,
        }
        for row in db.execute(query).all()
    ]


def get_stock_take_detail(db: Session, *, stock_take_id: int) -> dict:
    take_row = db.execute(
        select(NonSellableStockTake, Store.name)
        .join(Store, Store.id == NonSellableStockTake.store_id)
        .where(NonSellableStockTake.id == stock_take_id)
    ).one_or_none()
    if not take_row:
        raise ValueError('Non-sellable stock take not found')

    take, store_name = take_row
    lines = list_stock_take_lines(db, stock_take_id=take.id)
    return {
        'id': take.id,
        'store_name': store_name,
        'employee_name': take.employee_name,
        'status': take.status.value,
        'created_at': take.created_at,
        'submitted_at': take.submitted_at,
        'lines': lines,
    }


def unlock_stock_take(db: Session, *, stock_take_id: int) -> NonSellableStockTake:
    take = db.execute(select(NonSellableStockTake).where(NonSellableStockTake.id == stock_take_id)).scalar_one_or_none()
    if not take:
        raise ValueError('Non-sellable stock take not found')
    take.status = NonSellableStockTakeStatus.DRAFT
    take.submitted_at = None
    take.submitted_by_principal_id = None
    take.updated_at = _now()
    db.flush()
    return take
