from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from app.models import (
    ChangeBoxInventoryLine,
    ChangeBoxParLevel,
    NonSellableItem,
    NonSellableParLevel,
    NonSellableStockTake,
    NonSellableStockTakeLine,
    NonSellableStockTakeStatus,
    Store,
)
from app.services.change_box_count_service import DENOMINATIONS


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _stores(db: Session) -> list[Store]:
    return db.execute(select(Store).where(Store.active.is_(True)).order_by(Store.name.asc())).scalars().all()


def _selected_store(db: Session, *, store_id: int | None) -> Store | None:
    stores = _stores(db)
    if not stores:
        return None
    if store_id is None:
        return stores[0]
    selected = next((store for store in stores if store.id == store_id), None)
    return selected if selected else stores[0]


def _latest_submitted_stock_take_id(db: Session, *, store_id: int) -> int | None:
    return db.execute(
        select(NonSellableStockTake.id)
        .where(
            NonSellableStockTake.store_id == store_id,
            NonSellableStockTake.status == NonSellableStockTakeStatus.SUBMITTED,
        )
        .order_by(NonSellableStockTake.submitted_at.desc(), NonSellableStockTake.id.desc())
    ).scalars().first()


def get_store_par_reset_data(db: Session, *, store_id: int | None) -> dict:
    stores = _stores(db)
    selected = _selected_store(db, store_id=store_id)
    if not selected:
        return {'stores': [], 'selected_store_id': None, 'change_box_rows': [], 'non_sellable_rows': []}

    inventory_rows = db.execute(
        select(ChangeBoxInventoryLine).where(ChangeBoxInventoryLine.store_id == selected.id)
    ).scalars().all()
    inventory_by_code = {row.denomination_code: row for row in inventory_rows}

    try:
        par_rows = db.execute(
            select(ChangeBoxParLevel).where(ChangeBoxParLevel.store_id == selected.id)
        ).scalars().all()
        par_by_code = {row.denomination_code: row for row in par_rows}
    except (ProgrammingError, OperationalError, DBAPIError):
        par_by_code = {}

    change_box_rows: list[dict] = []
    total_needed_amount = Decimal('0.00')
    for denom in DENOMINATIONS:
        current_qty = int(inventory_by_code.get(denom['code']).quantity) if denom['code'] in inventory_by_code else 0
        custom_par = par_by_code.get(denom['code'])
        level_qty = int(custom_par.level_quantity) if custom_par else current_qty
        par_qty = int(custom_par.par_quantity) if custom_par else current_qty
        needed_qty = max(par_qty - current_qty, 0) if current_qty <= level_qty else 0
        needed_amount = (denom['unit_value'] * Decimal(needed_qty)).quantize(Decimal('0.01'))
        total_needed_amount += needed_amount
        change_box_rows.append(
            {
                'code': denom['code'],
                'label': denom['label'],
                'unit_value': denom['unit_value'],
                'current_qty': current_qty,
                'level_qty': level_qty,
                'par_qty': par_qty,
                'needed_qty': needed_qty,
                'needed_amount': needed_amount,
                'needs_restock': needed_qty > 0,
            }
        )

    latest_take_id = _latest_submitted_stock_take_id(db, store_id=selected.id)
    current_non_sellable_by_item_id: dict[int, Decimal] = {}
    if latest_take_id is not None:
        for row in db.execute(
            select(NonSellableStockTakeLine).where(NonSellableStockTakeLine.stock_take_id == latest_take_id)
        ).scalars().all():
            current_non_sellable_by_item_id[row.item_id] = Decimal(str(row.quantity))

    active_items = db.execute(
        select(NonSellableItem).where(NonSellableItem.active.is_(True)).order_by(NonSellableItem.name.asc())
    ).scalars().all()
    try:
        ns_par_rows = db.execute(
            select(NonSellableParLevel).where(NonSellableParLevel.store_id == selected.id)
        ).scalars().all()
        ns_par_by_item_id = {row.item_id: row for row in ns_par_rows}
    except (ProgrammingError, OperationalError, DBAPIError):
        ns_par_by_item_id = {}

    non_sellable_rows: list[dict] = []
    for item in active_items:
        current_qty = current_non_sellable_by_item_id.get(item.id, Decimal('0.000')).quantize(Decimal('0.001'))
        custom_par = ns_par_by_item_id.get(item.id)
        level_qty = (
            Decimal(str(custom_par.level_quantity)).quantize(Decimal('0.001'))
            if custom_par
            else current_qty
        )
        par_qty = (
            Decimal(str(custom_par.par_quantity)).quantize(Decimal('0.001'))
            if custom_par
            else current_qty
        )
        needed_qty = (par_qty - current_qty).quantize(Decimal('0.001')) if current_qty <= level_qty else Decimal('0.000')
        if needed_qty < 0:
            needed_qty = Decimal('0.000')
        non_sellable_rows.append(
            {
                'item_id': item.id,
                'item_name': item.name,
                'current_qty': current_qty,
                'level_qty': level_qty,
                'par_qty': par_qty,
                'needed_qty': needed_qty,
                'needs_restock': needed_qty > 0,
            }
        )

    return {
        'stores': stores,
        'selected_store_id': selected.id,
        'selected_store_name': selected.name,
        'change_box_rows': change_box_rows,
        'non_sellable_rows': non_sellable_rows,
        'total_change_box_needed_amount': total_needed_amount.quantize(Decimal('0.01')),
        'latest_non_sellable_take_id': latest_take_id,
    }


def save_store_par_levels(
    db: Session,
    *,
    store_id: int,
    principal_id: int,
    change_box_par_by_code: dict[str, int],
    change_box_level_by_code: dict[str, int],
    non_sellable_par_by_item_id: dict[int, Decimal],
    non_sellable_level_by_item_id: dict[int, Decimal],
) -> dict:
    store = db.execute(select(Store).where(Store.id == store_id, Store.active.is_(True))).scalar_one_or_none()
    if not store:
        raise ValueError('Store not found')

    now = _now()
    try:
        existing_cb = {
            row.denomination_code: row
            for row in db.execute(select(ChangeBoxParLevel).where(ChangeBoxParLevel.store_id == store_id)).scalars().all()
        }
    except (ProgrammingError, OperationalError, DBAPIError) as exc:
        raise ValueError('Store par tables are not initialized. Run schema update first.') from exc
    for denom in DENOMINATIONS:
        code = denom['code']
        level_qty = int(change_box_level_by_code.get(code, 0))
        par_qty = int(change_box_par_by_code.get(code, 0))
        if level_qty < 0:
            raise ValueError(f'Change box level cannot be negative for {denom["label"]}')
        if par_qty < 0:
            raise ValueError(f'Change box par cannot be negative for {denom["label"]}')
        if par_qty < level_qty:
            raise ValueError(f'Change box par must be greater than or equal to level for {denom["label"]}')
        row = existing_cb.get(code)
        if row is None:
            row = ChangeBoxParLevel(
                store_id=store_id,
                denomination_code=code,
                level_quantity=level_qty,
                par_quantity=par_qty,
                updated_by_principal_id=principal_id,
                created_at=now,
                updated_at=now,
            )
            db.add(row)
            existing_cb[code] = row
        else:
            row.level_quantity = level_qty
            row.par_quantity = par_qty
            row.updated_by_principal_id = principal_id
            row.updated_at = now

    try:
        existing_ns = {
            row.item_id: row
            for row in db.execute(select(NonSellableParLevel).where(NonSellableParLevel.store_id == store_id)).scalars().all()
        }
    except (ProgrammingError, OperationalError, DBAPIError) as exc:
        raise ValueError('Store par tables are not initialized. Run schema update first.') from exc
    item_ids = sorted(set(non_sellable_par_by_item_id.keys()) | set(non_sellable_level_by_item_id.keys()))
    for item_id in item_ids:
        level_qty_raw = non_sellable_level_by_item_id.get(item_id, Decimal('0.000'))
        par_qty_raw = non_sellable_par_by_item_id.get(item_id, Decimal('0.000'))
        try:
            level_qty = Decimal(str(level_qty_raw)).quantize(Decimal('0.001'))
        except (InvalidOperation, TypeError) as exc:
            raise ValueError('Invalid non-sellable level quantity') from exc
        try:
            par_qty = Decimal(str(par_qty_raw)).quantize(Decimal('0.001'))
        except (InvalidOperation, TypeError) as exc:
            raise ValueError('Invalid non-sellable par quantity') from exc
        if level_qty < 0:
            raise ValueError('Non-sellable level quantities cannot be negative')
        if par_qty < 0:
            raise ValueError('Non-sellable par quantities cannot be negative')
        if par_qty < level_qty:
            raise ValueError('Non-sellable par must be greater than or equal to level')
        row = existing_ns.get(item_id)
        if row is None:
            db.add(
                NonSellableParLevel(
                    store_id=store_id,
                    item_id=item_id,
                    level_quantity=level_qty,
                    par_quantity=par_qty,
                    updated_by_principal_id=principal_id,
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            row.level_quantity = level_qty
            row.par_quantity = par_qty
            row.updated_by_principal_id = principal_id
            row.updated_at = now

    db.flush()
    return {
        'store_id': store_id,
        'change_box_rows_saved': len(DENOMINATIONS),
        'non_sellable_rows_saved': len(non_sellable_par_by_item_id),
    }
