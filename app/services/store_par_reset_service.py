from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import delete, select
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
    StoreParDeliveryLine,
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


def _ensure_change_box_inventory_rows(db: Session, *, store_id: int) -> None:
    existing_rows = db.execute(
        select(ChangeBoxInventoryLine).where(ChangeBoxInventoryLine.store_id == store_id)
    ).scalars().all()
    by_code = {row.denomination_code: row for row in existing_rows}
    for denom in DENOMINATIONS:
        if denom['code'] in by_code:
            continue
        db.add(
            ChangeBoxInventoryLine(
                store_id=store_id,
                denomination_code=denom['code'],
                denomination_label=denom['label'],
                unit_value=denom['unit_value'],
                quantity=0,
            )
        )
    db.flush()


def _queue_lines_by_store(db: Session, *, store_id: int) -> dict[tuple[str, str], StoreParDeliveryLine]:
    try:
        rows = db.execute(
            select(StoreParDeliveryLine).where(StoreParDeliveryLine.store_id == store_id)
        ).scalars().all()
    except (ProgrammingError, OperationalError, DBAPIError):
        return {}
    return {(row.item_type, row.item_key): row for row in rows}


def get_store_par_reset_data(db: Session, *, store_id: int | None) -> dict:
    stores = _stores(db)
    selected = _selected_store(db, store_id=store_id)
    if not selected:
        return {'stores': [], 'selected_store_id': None, 'change_box_rows': [], 'non_sellable_rows': []}

    _ensure_change_box_inventory_rows(db, store_id=selected.id)
    inventory_rows = db.execute(
        select(ChangeBoxInventoryLine).where(ChangeBoxInventoryLine.store_id == selected.id)
    ).scalars().all()
    inventory_by_code = {row.denomination_code: row for row in inventory_rows}
    queue_by_key = _queue_lines_by_store(db, store_id=selected.id)

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
        needed_qty = max(par_qty - current_qty, 0)
        needed_amount = (denom['unit_value'] * Decimal(needed_qty)).quantize(Decimal('0.01'))
        total_needed_amount += needed_amount
        staged_row = queue_by_key.get(('CHANGE_BOX', denom['code']))
        staged_qty = int(Decimal(str(staged_row.quantity)).to_integral_value()) if staged_row else 0
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
                'staged_qty': staged_qty,
                'staged_selected': staged_qty > 0,
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
        needed_qty = (par_qty - current_qty).quantize(Decimal('0.001'))
        if needed_qty < 0:
            needed_qty = Decimal('0.000')
        staged_row = queue_by_key.get(('NON_SELLABLE', str(item.id)))
        staged_qty = (
            Decimal(str(staged_row.quantity)).quantize(Decimal('0.001'))
            if staged_row
            else Decimal('0.000')
        )
        non_sellable_rows.append(
            {
                'item_id': item.id,
                'item_name': item.name,
                'current_qty': current_qty,
                'level_qty': level_qty,
                'par_qty': par_qty,
                'needed_qty': needed_qty,
                'needs_restock': needed_qty > 0,
                'staged_qty': staged_qty,
                'staged_selected': staged_qty > 0,
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


def stage_store_par_delivery_lines(
    db: Session,
    *,
    store_id: int,
    principal_id: int,
    change_box_by_code: dict[str, int],
    non_sellable_by_item_id: dict[int, Decimal],
) -> dict:
    store = db.execute(select(Store).where(Store.id == store_id, Store.active.is_(True))).scalar_one_or_none()
    if not store:
        raise ValueError('Store not found')

    queue_by_key = _queue_lines_by_store(db, store_id=store_id)
    denom_by_code = {item['code']: item for item in DENOMINATIONS}
    non_sellable_items = db.execute(
        select(NonSellableItem).where(NonSellableItem.active.is_(True))
    ).scalars().all()
    ns_item_by_id = {int(item.id): item for item in non_sellable_items}
    staged_count = 0

    for code, qty in change_box_by_code.items():
        if qty <= 0:
            continue
        denom = denom_by_code.get(code)
        if denom is None:
            continue
        staged_count += 1
        key = ('CHANGE_BOX', code)
        existing = queue_by_key.get(key)
        if existing is None:
            db.add(
                StoreParDeliveryLine(
                    store_id=store_id,
                    item_type='CHANGE_BOX',
                    item_key=code,
                    item_label=denom['label'],
                    unit_value=denom['unit_value'],
                    quantity=Decimal(qty).quantize(Decimal('0.001')),
                    created_by_principal_id=principal_id,
                )
            )
        else:
            existing.item_label = denom['label']
            existing.unit_value = denom['unit_value']
            existing.quantity = (Decimal(str(existing.quantity)) + Decimal(qty)).quantize(Decimal('0.001'))

    for item_id, qty_raw in non_sellable_by_item_id.items():
        qty = Decimal(str(qty_raw)).quantize(Decimal('0.001'))
        if qty <= 0:
            continue
        item = ns_item_by_id.get(item_id)
        if item is None:
            continue
        staged_count += 1
        key = ('NON_SELLABLE', str(item_id))
        existing = queue_by_key.get(key)
        if existing is None:
            db.add(
                StoreParDeliveryLine(
                    store_id=store_id,
                    item_type='NON_SELLABLE',
                    item_key=str(item_id),
                    item_label=item.name,
                    unit_value=Decimal('0.00'),
                    quantity=qty,
                    created_by_principal_id=principal_id,
                )
            )
        else:
            existing.item_label = item.name
            existing.quantity = (Decimal(str(existing.quantity)) + qty).quantize(Decimal('0.001'))

    try:
        db.flush()
    except (ProgrammingError, OperationalError, DBAPIError) as exc:
        raise ValueError('Store par delivery tables are not initialized. Run schema update first.') from exc
    return {'store_id': store_id, 'staged_line_count': staged_count}


def get_store_par_delivery_data(db: Session, *, store_id: int | None) -> dict:
    stores = _stores(db)
    selected = _selected_store(db, store_id=store_id)
    if not selected:
        return {'stores': [], 'selected_store_id': None, 'rows': []}

    try:
        rows = db.execute(
            select(StoreParDeliveryLine)
            .where(StoreParDeliveryLine.store_id == selected.id)
            .order_by(StoreParDeliveryLine.item_type.asc(), StoreParDeliveryLine.item_label.asc())
        ).scalars().all()
    except (ProgrammingError, OperationalError, DBAPIError) as exc:
        raise ValueError('Store par delivery tables are not initialized. Run schema update first.') from exc
    out_rows: list[dict] = []
    total_change_amount = Decimal('0.00')
    for row in rows:
        qty = Decimal(str(row.quantity)).quantize(Decimal('0.001'))
        if row.item_type == 'CHANGE_BOX':
            qty_display = int(qty.to_integral_value())
            line_amount = (Decimal(str(row.unit_value)) * Decimal(qty_display)).quantize(Decimal('0.01'))
            total_change_amount += line_amount
            out_rows.append(
                {
                    'item_type': row.item_type,
                    'item_label': row.item_label,
                    'quantity_display': qty_display,
                    'line_amount': line_amount,
                }
            )
            continue
        out_rows.append(
            {
                'item_type': row.item_type,
                'item_label': row.item_label,
                'quantity_display': qty,
                'line_amount': None,
            }
        )

    return {
        'stores': stores,
        'selected_store_id': selected.id,
        'selected_store_name': selected.name,
        'rows': out_rows,
        'total_change_amount': total_change_amount.quantize(Decimal('0.01')),
    }


def deliver_store_par_queue(db: Session, *, store_id: int, principal_id: int) -> dict:
    store = db.execute(select(Store).where(Store.id == store_id, Store.active.is_(True))).scalar_one_or_none()
    if not store:
        raise ValueError('Store not found')

    try:
        queue_rows = db.execute(
            select(StoreParDeliveryLine).where(StoreParDeliveryLine.store_id == store_id)
        ).scalars().all()
    except (ProgrammingError, OperationalError, DBAPIError) as exc:
        raise ValueError('Store par delivery tables are not initialized. Run schema update first.') from exc
    if not queue_rows:
        raise ValueError('No queued delivery lines for this store')

    _ensure_change_box_inventory_rows(db, store_id=store_id)
    inventory_rows = db.execute(
        select(ChangeBoxInventoryLine).where(ChangeBoxInventoryLine.store_id == store_id)
    ).scalars().all()
    inventory_by_code = {row.denomination_code: row for row in inventory_rows}

    non_sellable_items = db.execute(
        select(NonSellableItem).where(NonSellableItem.active.is_(True)).order_by(NonSellableItem.name.asc())
    ).scalars().all()
    non_sellable_item_ids = {int(item.id) for item in non_sellable_items}
    latest_take_id = _latest_submitted_stock_take_id(db, store_id=store_id)
    latest_qty_by_item_id: dict[int, Decimal] = {}
    if latest_take_id is not None:
        latest_rows = db.execute(
            select(NonSellableStockTakeLine).where(NonSellableStockTakeLine.stock_take_id == latest_take_id)
        ).scalars().all()
        latest_qty_by_item_id = {int(row.item_id): Decimal(str(row.quantity)).quantize(Decimal('0.001')) for row in latest_rows}

    ns_delta_by_item_id: dict[int, Decimal] = {}
    change_box_lines_delivered = 0
    non_sellable_lines_delivered = 0
    for queue_row in queue_rows:
        qty = Decimal(str(queue_row.quantity)).quantize(Decimal('0.001'))
        if qty <= 0:
            continue
        if queue_row.item_type == 'CHANGE_BOX':
            code = queue_row.item_key
            inventory = inventory_by_code.get(code)
            if inventory is None:
                continue
            add_qty = int(qty.to_integral_value())
            if add_qty <= 0:
                continue
            inventory.quantity = int(inventory.quantity) + add_qty
            inventory.updated_by_principal_id = principal_id
            change_box_lines_delivered += 1
            continue
        if queue_row.item_type == 'NON_SELLABLE':
            if not queue_row.item_key.isdigit():
                continue
            item_id = int(queue_row.item_key)
            if item_id not in non_sellable_item_ids:
                continue
            ns_delta_by_item_id[item_id] = ns_delta_by_item_id.get(item_id, Decimal('0.000')) + qty
            non_sellable_lines_delivered += 1

    if ns_delta_by_item_id:
        take = NonSellableStockTake(
            store_id=store_id,
            employee_name='Store Par Delivery',
            status=NonSellableStockTakeStatus.SUBMITTED,
            created_by_principal_id=principal_id,
            submitted_by_principal_id=principal_id,
            submitted_at=_now(),
        )
        db.add(take)
        db.flush()
        for item in non_sellable_items:
            existing_qty = latest_qty_by_item_id.get(int(item.id), Decimal('0.000'))
            delta_qty = ns_delta_by_item_id.get(int(item.id), Decimal('0.000'))
            next_qty = (existing_qty + delta_qty).quantize(Decimal('0.001'))
            if next_qty < 0:
                next_qty = Decimal('0.000')
            db.add(
                NonSellableStockTakeLine(
                    stock_take_id=take.id,
                    item_id=item.id,
                    item_name=item.name,
                    quantity=next_qty,
                )
            )

    db.execute(delete(StoreParDeliveryLine).where(StoreParDeliveryLine.store_id == store_id))
    db.flush()
    return {
        'delivered_store_id': store_id,
        'change_box_lines_delivered': change_box_lines_delivered,
        'non_sellable_lines_delivered': non_sellable_lines_delivered,
    }


def clear_store_par_queue(db: Session, *, store_id: int) -> dict:
    store = db.execute(select(Store).where(Store.id == store_id, Store.active.is_(True))).scalar_one_or_none()
    if not store:
        raise ValueError('Store not found')
    try:
        cleared_count = len(
            db.execute(select(StoreParDeliveryLine.id).where(StoreParDeliveryLine.store_id == store_id)).all()
        )
        db.execute(delete(StoreParDeliveryLine).where(StoreParDeliveryLine.store_id == store_id))
    except (ProgrammingError, OperationalError, DBAPIError) as exc:
        raise ValueError('Store par delivery tables are not initialized. Run schema update first.') from exc
    db.flush()
    return {'cleared_store_id': store_id, 'cleared_line_count': cleared_count}
