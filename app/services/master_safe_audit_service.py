from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    MasterSafeAuditLine,
    MasterSafeAuditSubmission,
    MasterSafeInventoryLine,
    MasterSafeInventorySetting,
)
from app.services.change_form_service import DENOMS, DENOM_BY_CODE


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


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


def get_inventory_state(db: Session) -> dict:
    rows = _ensure_inventory_rows(db)
    by_code = {row.denomination_code: row for row in rows}
    setting = db.execute(select(MasterSafeInventorySetting).where(MasterSafeInventorySetting.id == 1)).scalar_one()

    total = Decimal('0.00')
    lines = []
    for denom in DENOMS:
        row = by_code.get(denom['code'])
        if not row:
            continue
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
