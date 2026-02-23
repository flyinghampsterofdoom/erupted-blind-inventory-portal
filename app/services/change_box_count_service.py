from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.models import (
    ChangeBoxCount,
    ChangeBoxCountLine,
    ChangeBoxCountStatus,
    ChangeBoxInventoryLine,
    ChangeBoxInventorySetting,
    Store,
)

DENOMINATIONS: list[dict] = [
    {'code': 'PENNY', 'label': 'Penny', 'unit_value': Decimal('0.01'), 'position': 1},
    {'code': 'NICKEL', 'label': 'Nickel', 'unit_value': Decimal('0.05'), 'position': 2},
    {'code': 'DIME', 'label': 'Dime', 'unit_value': Decimal('0.10'), 'position': 3},
    {'code': 'QUARTER', 'label': 'Quarter', 'unit_value': Decimal('0.25'), 'position': 4},
    {'code': 'HALF_DOLLAR', 'label': 'Fifty Cent Piece', 'unit_value': Decimal('0.50'), 'position': 5},
    {'code': 'ONE_DOLLAR', 'label': 'One Dollar', 'unit_value': Decimal('1.00'), 'position': 6},
    {'code': 'TWO_DOLLAR', 'label': 'Two Dollars', 'unit_value': Decimal('2.00'), 'position': 7},
    {'code': 'FIVE_DOLLAR', 'label': 'Five Dollars', 'unit_value': Decimal('5.00'), 'position': 8},
    {'code': 'TEN_DOLLAR', 'label': 'Ten Dollars', 'unit_value': Decimal('10.00'), 'position': 9},
    {'code': 'TWENTY_DOLLAR', 'label': 'Twenty Dollars', 'unit_value': Decimal('20.00'), 'position': 10},
    {'code': 'FIFTY_DOLLAR', 'label': 'Fifty Dollars', 'unit_value': Decimal('50.00'), 'position': 11},
    {'code': 'HUNDRED_DOLLAR', 'label': 'One Hundred Dollars', 'unit_value': Decimal('100.00'), 'position': 12},
]

DENOM_BY_CODE = {item['code']: item for item in DENOMINATIONS}


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _ensure_store(db: Session, store_id: int) -> None:
    exists = db.execute(select(Store.id).where(Store.id == store_id, Store.active.is_(True))).scalar_one_or_none()
    if not exists:
        raise ValueError('Store not found')


def _create_empty_lines(db: Session, *, count_id: int) -> None:
    db.add_all(
        [
            ChangeBoxCountLine(
                count_id=count_id,
                denomination_code=item['code'],
                denomination_label=item['label'],
                position=item['position'],
                unit_value=item['unit_value'],
                quantity=0,
                line_amount=Decimal('0.00'),
            )
            for item in DENOMINATIONS
        ]
    )


def _ensure_inventory_rows(db: Session, *, store_id: int) -> list[ChangeBoxInventoryLine]:
    _ensure_store(db, store_id)
    existing = db.execute(
        select(ChangeBoxInventoryLine).where(ChangeBoxInventoryLine.store_id == store_id)
    ).scalars().all()
    by_code = {row.denomination_code: row for row in existing}
    for item in DENOMINATIONS:
        if item['code'] in by_code:
            continue
        db.add(
            ChangeBoxInventoryLine(
                store_id=store_id,
                denomination_code=item['code'],
                denomination_label=item['label'],
                unit_value=item['unit_value'],
                quantity=0,
            )
        )
    setting = db.execute(
        select(ChangeBoxInventorySetting).where(ChangeBoxInventorySetting.store_id == store_id)
    ).scalar_one_or_none()
    if not setting:
        db.add(ChangeBoxInventorySetting(store_id=store_id, target_amount=Decimal('0.00')))
    db.flush()
    return db.execute(
        select(ChangeBoxInventoryLine).where(ChangeBoxInventoryLine.store_id == store_id)
    ).scalars().all()


def get_or_create_draft_count(db: Session, *, store_id: int, principal_id: int) -> tuple[ChangeBoxCount, bool]:
    _ensure_store(db, store_id)
    draft = db.execute(
        select(ChangeBoxCount)
        .where(
            ChangeBoxCount.store_id == store_id,
            ChangeBoxCount.status == ChangeBoxCountStatus.DRAFT,
        )
        .order_by(ChangeBoxCount.created_at.desc())
    ).scalars().first()
    if draft:
        return draft, False

    count = ChangeBoxCount(
        store_id=store_id,
        employee_name='',
        status=ChangeBoxCountStatus.DRAFT,
        created_by_principal_id=principal_id,
        total_amount=Decimal('0.00'),
    )
    db.add(count)
    db.flush()
    _create_empty_lines(db, count_id=count.id)
    db.flush()
    return count, True


def get_store_draft_count(db: Session, *, store_id: int, count_id: int) -> ChangeBoxCount:
    count = db.execute(select(ChangeBoxCount).where(ChangeBoxCount.id == count_id)).scalar_one_or_none()
    if not count:
        raise ValueError('Change box count not found')
    if count.store_id != store_id:
        raise PermissionError('Not allowed to access this change box count')
    if count.status != ChangeBoxCountStatus.DRAFT:
        raise PermissionError('Submitted change box counts are viewable by lead/admin only')
    return count


def list_count_lines(db: Session, *, count_id: int) -> list[dict]:
    rows = db.execute(
        select(ChangeBoxCountLine)
        .where(ChangeBoxCountLine.count_id == count_id)
        .order_by(ChangeBoxCountLine.position.asc())
    ).scalars().all()
    return [
        {
            'denomination_code': row.denomination_code,
            'denomination_label': row.denomination_label,
            'unit_value': row.unit_value,
            'quantity': row.quantity,
            'line_amount': row.line_amount,
        }
        for row in rows
    ]


def save_or_submit_count(
    db: Session,
    *,
    count: ChangeBoxCount,
    employee_name: str,
    quantities_by_code: dict[str, int],
    submit: bool,
    submitted_by_principal_id: int,
) -> ChangeBoxCount:
    if count.status == ChangeBoxCountStatus.SUBMITTED:
        raise ValueError('Change box count is already submitted')

    clean_name = employee_name.strip()
    if not clean_name:
        raise ValueError('Name is required')

    lines = db.execute(select(ChangeBoxCountLine).where(ChangeBoxCountLine.count_id == count.id)).scalars().all()
    lines_by_code = {line.denomination_code: line for line in lines}

    total = Decimal('0.00')
    for code, meta in DENOM_BY_CODE.items():
        qty = quantities_by_code.get(code, 0)
        if qty < 0:
            raise ValueError(f'Quantity cannot be negative for {meta["label"]}')

        line = lines_by_code.get(code)
        if not line:
            continue
        line.quantity = qty
        line.line_amount = (line.unit_value * Decimal(qty)).quantize(Decimal('0.01'))
        total += line.line_amount

    count.employee_name = clean_name
    count.total_amount = total.quantize(Decimal('0.01'))
    count.updated_at = _now()

    if submit:
        count.status = ChangeBoxCountStatus.SUBMITTED
        count.submitted_at = _now()
        count.submitted_by_principal_id = submitted_by_principal_id
        inventory_rows = _ensure_inventory_rows(db, store_id=count.store_id)
        inventory_by_code = {row.denomination_code: row for row in inventory_rows}
        for code, meta in DENOM_BY_CODE.items():
            row = inventory_by_code.get(code)
            if not row:
                continue
            qty = quantities_by_code.get(code, 0)
            row.denomination_label = meta['label']
            row.unit_value = meta['unit_value']
            row.quantity = qty
            row.updated_by_principal_id = submitted_by_principal_id
            row.updated_at = _now()

    db.flush()
    return count


def list_counts_for_audit(
    db: Session,
    *,
    store_id: int | None,
) -> list[dict]:
    query = (
        select(ChangeBoxCount.id, ChangeBoxCount.employee_name, ChangeBoxCount.status, ChangeBoxCount.total_amount, ChangeBoxCount.created_at, ChangeBoxCount.submitted_at, Store.name.label('store_name'))
        .join(Store, Store.id == ChangeBoxCount.store_id)
        .order_by(ChangeBoxCount.created_at.desc())
    )
    if store_id:
        query = query.where(ChangeBoxCount.store_id == store_id)

    return [
        {
            'id': row.id,
            'employee_name': row.employee_name,
            'status': row.status.value if hasattr(row.status, 'value') else str(row.status),
            'total_amount': row.total_amount,
            'created_at': row.created_at,
            'submitted_at': row.submitted_at,
            'store_name': row.store_name,
        }
        for row in db.execute(query).all()
    ]


def get_count_detail(db: Session, *, count_id: int) -> dict:
    row = db.execute(
        select(ChangeBoxCount, Store.name)
        .join(Store, Store.id == ChangeBoxCount.store_id)
        .where(ChangeBoxCount.id == count_id)
    ).one_or_none()
    if not row:
        raise ValueError('Change box count not found')

    count, store_name = row
    lines = list_count_lines(db, count_id=count.id)
    return {
        'id': count.id,
        'store_name': store_name,
        'employee_name': count.employee_name,
        'status': count.status.value,
        'total_amount': count.total_amount,
        'created_at': count.created_at,
        'submitted_at': count.submitted_at,
        'lines': lines,
    }
