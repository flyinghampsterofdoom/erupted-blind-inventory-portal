from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    ChangeBoxAuditLine,
    ChangeBoxAuditSubmission,
    ChangeBoxInventoryLine,
    ChangeBoxInventorySetting,
    ChangeFormLine,
    ChangeFormSubmission,
    Store,
)

# Base denominations tracked in live change box inventory.
DENOMS: list[dict] = [
    {'code': 'PENNY', 'label': 'Pennies', 'unit_value': Decimal('0.01')},
    {'code': 'NICKEL', 'label': 'Nickels', 'unit_value': Decimal('0.05')},
    {'code': 'DIME', 'label': 'Dimes', 'unit_value': Decimal('0.10')},
    {'code': 'QUARTER', 'label': 'Quarters', 'unit_value': Decimal('0.25')},
    {'code': 'ONE_DOLLAR', 'label': 'One Dollar', 'unit_value': Decimal('1.00')},
    {'code': 'FIVE_DOLLAR', 'label': 'Five Dollars', 'unit_value': Decimal('5.00')},
    {'code': 'TEN_DOLLAR', 'label': 'Ten Dollars', 'unit_value': Decimal('10.00')},
    {'code': 'TWENTY_DOLLAR', 'label': 'Twenty Dollars', 'unit_value': Decimal('20.00')},
    {'code': 'FIFTY_DOLLAR', 'label': 'Fifty Dollars', 'unit_value': Decimal('50.00')},
    {'code': 'HUNDRED_DOLLAR', 'label': 'One Hundred Dollars', 'unit_value': Decimal('100.00')},
]
DENOM_BY_CODE = {d['code']: d for d in DENOMS}

BILLS_REPLACED_CODES = ['ONE_DOLLAR', 'FIVE_DOLLAR', 'TEN_DOLLAR', 'TWENTY_DOLLAR', 'FIFTY_DOLLAR', 'HUNDRED_DOLLAR']
CHANGE_MADE_BILL_CODES = ['ONE_DOLLAR', 'FIVE_DOLLAR', 'TEN_DOLLAR', 'TWENTY_DOLLAR', 'FIFTY_DOLLAR', 'HUNDRED_DOLLAR']
ROLL_TO_COIN = {
    'PENNY': 50,
    'NICKEL': 40,
    'DIME': 50,
    'QUARTER': 40,
}


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _ensure_store(db: Session, store_id: int) -> None:
    exists = db.execute(select(Store.id).where(Store.id == store_id, Store.active.is_(True))).scalar_one_or_none()
    if not exists:
        raise ValueError('Store not found')


def _ensure_inventory_rows(db: Session, *, store_id: int) -> list[ChangeBoxInventoryLine]:
    _ensure_store(db, store_id)
    existing = db.execute(
        select(ChangeBoxInventoryLine).where(ChangeBoxInventoryLine.store_id == store_id)
    ).scalars().all()
    by_code = {row.denomination_code: row for row in existing}

    for denom in DENOMS:
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
    return db.execute(
        select(ChangeBoxInventoryLine)
        .where(ChangeBoxInventoryLine.store_id == store_id)
        .order_by(ChangeBoxInventoryLine.denomination_label.asc())
    ).scalars().all()


def get_inventory_state(db: Session, *, store_id: int) -> dict:
    rows = _ensure_inventory_rows(db, store_id=store_id)
    setting = db.execute(
        select(ChangeBoxInventorySetting).where(ChangeBoxInventorySetting.store_id == store_id)
    ).scalar_one_or_none()
    if not setting:
        setting = ChangeBoxInventorySetting(store_id=store_id, target_amount=Decimal('0.00'))
        db.add(setting)
        db.flush()

    total = Decimal('0.00')
    lines = []
    for row in rows:
        amount = (row.unit_value * Decimal(row.quantity)).quantize(Decimal('0.01'))
        total += amount
        lines.append(
            {
                'denomination_code': row.denomination_code,
                'denomination_label': row.denomination_label,
                'unit_value': row.unit_value,
                'quantity': row.quantity,
                'line_amount': amount,
            }
        )
    return {
        'target_amount': setting.target_amount,
        'total_amount': total.quantize(Decimal('0.01')),
        'lines': lines,
    }


def _parse_int_map(source: dict[str, str], keys: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for key in keys:
        raw = str(source.get(key, '')).strip()
        if raw == '':
            out[key] = 0
            continue
        value = int(raw)
        if value < 0:
            raise ValueError('Quantities cannot be negative')
        out[key] = value
    return out


def submit_change_form(
    db: Session,
    *,
    store_id: int,
    principal_id: int,
    employee_name: str,
    signature_full_name: str,
    bills_replaced: dict[str, str],
    change_made_rolls: dict[str, str],
    change_made_bills: dict[str, str],
    generated_at: datetime | None = None,
) -> ChangeFormSubmission:
    clean_employee_name = employee_name.strip()
    clean_signature = signature_full_name.strip()
    if not clean_employee_name:
        raise ValueError('Name is required')
    if not clean_signature:
        raise ValueError('Full Name signature is required')

    _ensure_store(db, store_id)
    inventory_rows = _ensure_inventory_rows(db, store_id=store_id)
    inv_by_code = {row.denomination_code: row for row in inventory_rows}

    replaced_counts = _parse_int_map(bills_replaced, BILLS_REPLACED_CODES)
    removed_roll_counts = _parse_int_map(change_made_rolls, list(ROLL_TO_COIN.keys()))
    removed_bill_counts = _parse_int_map(change_made_bills, CHANGE_MADE_BILL_CODES)

    # Precheck no negative inventory after updates.
    deltas = {code: 0 for code in DENOM_BY_CODE.keys()}
    for code, qty in replaced_counts.items():
        deltas[code] += qty
    for code, rolls in removed_roll_counts.items():
        deltas[code] -= rolls * ROLL_TO_COIN[code]
    for code, qty in removed_bill_counts.items():
        deltas[code] -= qty

    for code, delta in deltas.items():
        row = inv_by_code.get(code)
        if not row:
            continue
        new_qty = row.quantity + delta
        if new_qty < 0:
            raise ValueError(f'Insufficient inventory for {row.denomination_label}')

    form = ChangeFormSubmission(
        store_id=store_id,
        employee_name=clean_employee_name,
        signature_full_name=clean_signature,
        created_by_principal_id=principal_id,
        generated_at=generated_at or _now(),
    )
    db.add(form)
    db.flush()

    # Persist explicit entered values for audit.
    for code, qty in replaced_counts.items():
        db.add(
            ChangeFormLine(
                submission_id=form.id,
                section='BILLS_REPLACED',
                denomination_code=code,
                denomination_label=DENOM_BY_CODE[code]['label'],
                quantity=qty,
                unit_value=DENOM_BY_CODE[code]['unit_value'],
                line_amount=(DENOM_BY_CODE[code]['unit_value'] * Decimal(qty)).quantize(Decimal('0.01')),
            )
        )

    for code, rolls in removed_roll_counts.items():
        quantity = rolls * ROLL_TO_COIN[code]
        db.add(
            ChangeFormLine(
                submission_id=form.id,
                section='CHANGE_MADE_ROLLS',
                denomination_code=code,
                denomination_label=f"{DENOM_BY_CODE[code]['label']} (Rolls)",
                quantity=quantity,
                unit_value=DENOM_BY_CODE[code]['unit_value'],
                line_amount=(DENOM_BY_CODE[code]['unit_value'] * Decimal(quantity)).quantize(Decimal('0.01')),
            )
        )

    for code, qty in removed_bill_counts.items():
        db.add(
            ChangeFormLine(
                submission_id=form.id,
                section='CHANGE_MADE_BILLS',
                denomination_code=code,
                denomination_label=DENOM_BY_CODE[code]['label'],
                quantity=qty,
                unit_value=DENOM_BY_CODE[code]['unit_value'],
                line_amount=(DENOM_BY_CODE[code]['unit_value'] * Decimal(qty)).quantize(Decimal('0.01')),
            )
        )

    # Apply live inventory update.
    for code, delta in deltas.items():
        row = inv_by_code.get(code)
        if not row:
            continue
        row.quantity = row.quantity + delta
        row.updated_by_principal_id = principal_id
        row.updated_at = _now()

    db.flush()
    return form


def list_change_forms(db: Session, *, store_id: int | None = None) -> list[dict]:
    query = (
        select(ChangeFormSubmission.id, ChangeFormSubmission.generated_at, ChangeFormSubmission.employee_name, ChangeFormSubmission.store_id, Store.name.label('store_name'))
        .join(Store, Store.id == ChangeFormSubmission.store_id)
        .order_by(ChangeFormSubmission.generated_at.desc())
    )
    if store_id:
        query = query.where(ChangeFormSubmission.store_id == store_id)
    return [
        {
            'id': row.id,
            'generated_at': row.generated_at,
            'employee_name': row.employee_name,
            'store_id': row.store_id,
            'store_name': row.store_name,
        }
        for row in db.execute(query).all()
    ]


def get_change_form_detail(db: Session, *, submission_id: int) -> dict:
    row = db.execute(
        select(ChangeFormSubmission, Store.name)
        .join(Store, Store.id == ChangeFormSubmission.store_id)
        .where(ChangeFormSubmission.id == submission_id)
    ).one_or_none()
    if not row:
        raise ValueError('Change form not found')

    form, store_name = row
    lines = db.execute(
        select(ChangeFormLine)
        .where(ChangeFormLine.submission_id == submission_id)
        .order_by(ChangeFormLine.id.asc())
    ).scalars().all()

    return {
        'id': form.id,
        'store_name': store_name,
        'generated_at': form.generated_at,
        'employee_name': form.employee_name,
        'signature_full_name': form.signature_full_name,
        'lines': [
            {
                'section': line.section,
                'denomination_label': line.denomination_label,
                'quantity': line.quantity,
                'unit_value': line.unit_value,
                'line_amount': line.line_amount,
            }
            for line in lines
        ],
    }


def submit_inventory_audit(
    db: Session,
    *,
    store_id: int,
    principal_id: int,
    auditor_name: str,
    target_amount: Decimal,
    quantities_by_code: dict[str, int],
) -> ChangeBoxAuditSubmission:
    clean_auditor = auditor_name.strip()
    if not clean_auditor:
        raise ValueError('Auditor name is required')
    if target_amount < 0:
        raise ValueError('Target amount cannot be negative')

    inventory_rows = _ensure_inventory_rows(db, store_id=store_id)
    inv_by_code = {row.denomination_code: row for row in inventory_rows}

    setting = db.execute(select(ChangeBoxInventorySetting).where(ChangeBoxInventorySetting.store_id == store_id)).scalar_one_or_none()
    if not setting:
        setting = ChangeBoxInventorySetting(store_id=store_id, target_amount=target_amount)
        db.add(setting)
    setting.target_amount = target_amount

    audit = ChangeBoxAuditSubmission(
        store_id=store_id,
        auditor_name=clean_auditor,
        target_amount=target_amount,
        created_by_principal_id=principal_id,
    )
    db.add(audit)
    db.flush()

    for code, meta in DENOM_BY_CODE.items():
        qty = max(0, int(quantities_by_code.get(code, 0)))
        row = inv_by_code.get(code)
        if row:
            row.quantity = qty
            row.updated_by_principal_id = principal_id
            row.updated_at = _now()
        db.add(
            ChangeBoxAuditLine(
                audit_submission_id=audit.id,
                denomination_code=code,
                denomination_label=meta['label'],
                unit_value=meta['unit_value'],
                quantity=qty,
                line_amount=(meta['unit_value'] * Decimal(qty)).quantize(Decimal('0.01')),
            )
        )

    db.flush()
    return audit
