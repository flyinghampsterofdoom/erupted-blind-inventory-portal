from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.models import ExchangeReturnForm, Store


def _ensure_store(db: Session, store_id: int) -> None:
    exists = db.execute(select(Store.id).where(Store.id == store_id, Store.active.is_(True))).scalar_one_or_none()
    if not exists:
        raise ValueError('Store not found')


def create_exchange_return_form(
    db: Session,
    *,
    store_id: int,
    principal_id: int,
    employee_name: str,
    original_purchase_date: date,
    generated_at: datetime,
    original_ticket_number: str,
    exchange_ticket_number: str,
    items_text: str,
    reason_text: str,
    refund_given: bool,
    refund_approved_by: str,
) -> ExchangeReturnForm:
    _ensure_store(db, store_id)

    if not employee_name.strip():
        raise ValueError('Employee name is required')
    if not original_ticket_number.strip():
        raise ValueError('Original ticket number is required')
    if not exchange_ticket_number.strip():
        raise ValueError('Exchange ticket number is required')
    if not items_text.strip():
        raise ValueError('Item(s) is required')
    if not reason_text.strip():
        raise ValueError('Reason is required')
    if not refund_approved_by.strip():
        raise ValueError('Refund approval name is required')

    form = ExchangeReturnForm(
        store_id=store_id,
        employee_name=employee_name.strip(),
        original_purchase_date=original_purchase_date,
        generated_at=generated_at,
        original_ticket_number=original_ticket_number.strip(),
        exchange_ticket_number=exchange_ticket_number.strip(),
        items_text=items_text.strip(),
        reason_text=reason_text.strip(),
        refund_given=bool(refund_given),
        refund_approved_by=refund_approved_by.strip(),
        created_by_principal_id=principal_id,
    )
    db.add(form)
    db.flush()
    return form


def list_forms(
    db: Session,
    *,
    store_id: int | None,
    from_date: date | None,
    to_date: date | None,
) -> list[dict]:
    conditions = []
    if store_id:
        conditions.append(ExchangeReturnForm.store_id == store_id)
    if from_date:
        conditions.append(ExchangeReturnForm.generated_at >= datetime.combine(from_date, time.min, tzinfo=timezone.utc))
    if to_date:
        conditions.append(
            ExchangeReturnForm.generated_at < datetime.combine(to_date + timedelta(days=1), time.min, tzinfo=timezone.utc)
        )

    query = (
        select(
            ExchangeReturnForm.id,
            ExchangeReturnForm.generated_at,
            ExchangeReturnForm.employee_name,
            ExchangeReturnForm.refund_given,
            ExchangeReturnForm.refund_approved_by,
            Store.name.label('store_name'),
        )
        .join(Store, Store.id == ExchangeReturnForm.store_id)
        .order_by(ExchangeReturnForm.generated_at.desc())
    )
    if conditions:
        query = query.where(and_(*conditions))

    return [
        {
            'id': row.id,
            'generated_at': row.generated_at,
            'employee_name': row.employee_name,
            'refund_given': row.refund_given,
            'refund_approved_by': row.refund_approved_by,
            'store_name': row.store_name,
        }
        for row in db.execute(query).all()
    ]


def get_form_detail(db: Session, *, form_id: int) -> dict:
    row = db.execute(
        select(ExchangeReturnForm, Store.name)
        .join(Store, Store.id == ExchangeReturnForm.store_id)
        .where(ExchangeReturnForm.id == form_id)
    ).one_or_none()
    if not row:
        raise ValueError('Exchange/Return form not found')

    form, store_name = row
    return {
        'id': form.id,
        'store_name': store_name,
        'generated_at': form.generated_at,
        'employee_name': form.employee_name,
        'original_purchase_date': form.original_purchase_date,
        'original_ticket_number': form.original_ticket_number,
        'exchange_ticket_number': form.exchange_ticket_number,
        'items_text': form.items_text,
        'reason_text': form.reason_text,
        'refund_given': form.refund_given,
        'refund_approved_by': form.refund_approved_by,
    }
