from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    ChangeFormLine,
    ChangeFormSubmission,
    MasterSafeAuditLine,
    MasterSafeAuditSubmission,
    MasterSafeInventoryLine,
    MasterSafeInventorySetting,
    MasterSafeParLevel,
)
from app.services.change_form_service import DENOMS, DENOM_BY_CODE

DEFAULT_MASTER_SAFE_PAR_AMOUNTS: dict[str, Decimal] = {
    'PENNY': Decimal('13.00'),
    'NICKEL': Decimal('22.00'),
    'DIME': Decimal('65.00'),
    'QUARTER': Decimal('100.00'),
    'ONE_DOLLAR': Decimal('500.00'),
    'FIVE_DOLLAR': Decimal('700.00'),
}
CHANGE_MADE_SECTIONS = ('CHANGE_MADE_ROLLS', 'CHANGE_MADE_BILLS')


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _money(value: Decimal | int | str) -> Decimal:
    return Decimal(value).quantize(Decimal('0.01'))


def _amount_to_quantity(amount: Decimal, unit_value: Decimal) -> int:
    if unit_value <= 0:
        return 0
    return max(0, int((amount / unit_value).to_integral_value(rounding=ROUND_HALF_UP)))


def _quantity_to_amount(quantity: int, unit_value: Decimal) -> Decimal:
    return (Decimal(quantity) * unit_value).quantize(Decimal('0.01'))


def _ensure_inventory_rows(db: Session) -> list[MasterSafeInventoryLine]:
    existing = db.execute(select(MasterSafeInventoryLine)).scalars().all()
    by_code = {row.denomination_code: row for row in existing}
    for denom in DENOMS:
        if denom['code'] in by_code:
            continue
        db.add(
            MasterSafeInventoryLine(
                denomination_code=denom['code'],
                denomination_label=denom['label'],
                unit_value=denom['unit_value'],
                quantity=0,
            )
        )
    setting = db.execute(select(MasterSafeInventorySetting).where(MasterSafeInventorySetting.id == 1)).scalar_one_or_none()
    if not setting:
        db.add(MasterSafeInventorySetting(id=1, target_amount=Decimal('0.00')))
    db.flush()
    return db.execute(select(MasterSafeInventoryLine)).scalars().all()


def _ensure_par_level_rows(db: Session) -> list[MasterSafeParLevel]:
    existing = db.execute(select(MasterSafeParLevel)).scalars().all()
    by_code = {row.denomination_code: row for row in existing}
    for denom in DENOMS:
        if denom['code'] in by_code:
            continue
        db.add(
            MasterSafeParLevel(
                denomination_code=denom['code'],
                par_amount=DEFAULT_MASTER_SAFE_PAR_AMOUNTS.get(denom['code'], Decimal('0.00')),
            )
        )
    db.flush()
    return db.execute(select(MasterSafeParLevel)).scalars().all()


def get_inventory_state(db: Session) -> dict:
    rows = _ensure_inventory_rows(db)
    by_code = {row.denomination_code: row for row in rows}
    par_rows = _ensure_par_level_rows(db)
    par_by_code = {row.denomination_code: row for row in par_rows}
    setting = db.execute(select(MasterSafeInventorySetting).where(MasterSafeInventorySetting.id == 1)).scalar_one()

    total = Decimal('0.00')
    total_par = Decimal('0.00')
    total_reset_add = Decimal('0.00')
    total_reset_remove = Decimal('0.00')
    lines = []
    for denom in DENOMS:
        row = by_code.get(denom['code'])
        if not row:
            continue
        amount = (row.unit_value * Decimal(row.quantity)).quantize(Decimal('0.01'))
        par_amount = _money(par_by_code.get(denom['code']).par_amount if par_by_code.get(denom['code']) else Decimal('0.00'))
        par_quantity = _amount_to_quantity(par_amount, row.unit_value)
        reset_quantity_delta = par_quantity - row.quantity
        reset_amount_delta = _quantity_to_amount(reset_quantity_delta, row.unit_value)
        if reset_quantity_delta > 0:
            total_reset_add += reset_amount_delta
            reset_status = 'Add'
        elif reset_quantity_delta < 0:
            total_reset_remove += abs(reset_amount_delta)
            reset_status = 'Remove'
        else:
            reset_status = 'At Par'
        total += amount
        total_par += par_amount
        lines.append(
            {
                'denomination_code': row.denomination_code,
                'denomination_label': row.denomination_label,
                'unit_value': row.unit_value,
                'quantity': row.quantity,
                'line_amount': amount,
                'par_amount': par_amount,
                'par_quantity': par_quantity,
                'reset_quantity_delta': reset_quantity_delta,
                'reset_amount_delta': reset_amount_delta,
                'reset_status': reset_status,
            }
        )

    return {
        'target_amount': setting.target_amount,
        'total_amount': total.quantize(Decimal('0.01')),
        'total_par_amount': total_par.quantize(Decimal('0.01')),
        'total_reset_add_amount': total_reset_add.quantize(Decimal('0.01')),
        'total_reset_remove_amount': total_reset_remove.quantize(Decimal('0.01')),
        'lines': lines,
    }


def save_par_levels(
    db: Session,
    *,
    principal_id: int,
    par_amounts_by_code: dict[str, Decimal],
) -> dict:
    rows = _ensure_par_level_rows(db)
    by_code = {row.denomination_code: row for row in rows}
    updated_codes: list[str] = []

    for denom in DENOMS:
        code = denom['code']
        amount = _money(par_amounts_by_code.get(code, Decimal('0.00')))
        if amount < 0:
            raise ValueError(f'Par level cannot be negative for {denom["label"]}')
        row = by_code.get(code)
        if not row:
            row = MasterSafeParLevel(denomination_code=code, par_amount=amount)
            db.add(row)
            by_code[code] = row
        row.par_amount = amount
        row.updated_by_principal_id = principal_id
        row.updated_at = _now()
        updated_codes.append(code)

    db.flush()
    return {'denomination_codes': updated_codes, 'count': len(updated_codes)}


def submit_audit(
    db: Session,
    *,
    principal_id: int,
    auditor_name: str,
    target_amount: Decimal,
    quantities_by_code: dict[str, int],
) -> MasterSafeAuditSubmission:
    clean_auditor = auditor_name.strip()
    if not clean_auditor:
        raise ValueError('Auditor name is required')
    if target_amount < 0:
        raise ValueError('Target amount cannot be negative')

    rows = _ensure_inventory_rows(db)
    by_code = {row.denomination_code: row for row in rows}
    setting = db.execute(select(MasterSafeInventorySetting).where(MasterSafeInventorySetting.id == 1)).scalar_one()
    setting.target_amount = target_amount

    audit = MasterSafeAuditSubmission(
        auditor_name=clean_auditor,
        target_amount=target_amount,
        created_by_principal_id=principal_id,
    )
    db.add(audit)
    db.flush()

    for code, denom in DENOM_BY_CODE.items():
        qty = max(0, int(quantities_by_code.get(code, 0)))
        line = by_code.get(code)
        if line:
            line.quantity = qty
            line.updated_by_principal_id = principal_id
            line.updated_at = _now()
        db.add(
            MasterSafeAuditLine(
                audit_submission_id=audit.id,
                denomination_code=code,
                denomination_label=denom['label'],
                unit_value=denom['unit_value'],
                quantity=qty,
                line_amount=(denom['unit_value'] * Decimal(qty)).quantize(Decimal('0.01')),
            )
        )

    db.flush()
    return audit


def build_change_made_usage_report(
    db: Session,
    *,
    start_date: date,
    end_date: date,
    store_id: int | None = None,
) -> dict:
    if end_date < start_date:
        raise ValueError('End date must be on or after start date')

    start_dt = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
    end_dt = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=timezone.utc)
    days = (end_date - start_date).days + 1
    weeks = Decimal(days) / Decimal('7')

    par_rows = _ensure_par_level_rows(db)
    par_by_code = {row.denomination_code: _money(row.par_amount) for row in par_rows}

    query = (
        select(
            ChangeFormLine.denomination_code,
            func.coalesce(func.sum(ChangeFormLine.quantity), 0).label('quantity_used'),
            func.coalesce(func.sum(ChangeFormLine.line_amount), 0).label('amount_used'),
            func.count(ChangeFormLine.id).label('times_used'),
        )
        .join(ChangeFormSubmission, ChangeFormSubmission.id == ChangeFormLine.submission_id)
        .where(
            ChangeFormLine.section.in_(CHANGE_MADE_SECTIONS),
            ChangeFormLine.quantity > 0,
            ChangeFormSubmission.generated_at >= start_dt,
            ChangeFormSubmission.generated_at < end_dt,
        )
        .group_by(ChangeFormLine.denomination_code)
    )
    if store_id:
        query = query.where(ChangeFormSubmission.store_id == store_id)

    usage_by_code = {
        row.denomination_code: {
            'quantity_used': int(row.quantity_used or 0),
            'amount_used': _money(row.amount_used or Decimal('0.00')),
            'times_used': int(row.times_used or 0),
        }
        for row in db.execute(query).all()
    }

    rows = []
    for denom in DENOMS:
        code = denom['code']
        usage = usage_by_code.get(code, {'quantity_used': 0, 'amount_used': Decimal('0.00'), 'times_used': 0})
        amount_used = _money(usage['amount_used'])
        weekly_usage_amount = _money(amount_used / weeks) if weeks > 0 else Decimal('0.00')
        par_amount = par_by_code.get(code, Decimal('0.00'))
        coverage_weeks = None
        if weekly_usage_amount > 0:
            coverage_weeks = (par_amount / weekly_usage_amount).quantize(Decimal('0.1'))
        suggested_par_amount = _money(weekly_usage_amount * Decimal('2'))
        suggested_par_quantity = _amount_to_quantity(suggested_par_amount, denom['unit_value'])
        suggested_par_amount = _quantity_to_amount(suggested_par_quantity, denom['unit_value'])

        if amount_used == 0:
            status = 'No usage in period' if par_amount > 0 else 'No par needed'
        elif par_amount == 0:
            status = 'Set par'
        elif coverage_weeks is not None and coverage_weeks < Decimal('1.0'):
            status = 'Raise par'
        elif coverage_weeks is not None and coverage_weeks < Decimal('2.0'):
            status = 'Watch par'
        elif coverage_weeks is not None and coverage_weeks > Decimal('6.0'):
            status = 'Possible overstock'
        else:
            status = 'Par looks reasonable'

        rows.append(
            {
                'denomination_code': code,
                'denomination_label': denom['label'],
                'unit_value': denom['unit_value'],
                'quantity_used': usage['quantity_used'],
                'amount_used': amount_used,
                'times_used': usage['times_used'],
                'weekly_usage_amount': weekly_usage_amount,
                'par_amount': par_amount,
                'par_quantity': _amount_to_quantity(par_amount, denom['unit_value']),
                'coverage_weeks': coverage_weeks,
                'suggested_par_amount': suggested_par_amount,
                'suggested_par_quantity': suggested_par_quantity,
                'status': status,
            }
        )

    rows.sort(key=lambda row: (row['amount_used'], row['times_used'], row['par_amount']), reverse=True)
    for index, row in enumerate(rows, start=1):
        row['rank'] = index

    return {
        'start_date': start_date,
        'end_date': end_date,
        'store_id': store_id,
        'period_days': days,
        'total_amount_used': sum((row['amount_used'] for row in rows), Decimal('0.00')).quantize(Decimal('0.01')),
        'total_par_amount': sum((row['par_amount'] for row in rows), Decimal('0.00')).quantize(Decimal('0.01')),
        'rows': rows,
    }
