from __future__ import annotations

import re
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.models import CustomerRequestItem, CustomerRequestLine, CustomerRequestSubmission, Store

SPLIT_RE = re.compile(r'[\n,]+')
WS_RE = re.compile(r'\s+')


def normalize_name(value: str) -> str:
    value = WS_RE.sub(' ', value.strip().lower())
    return value


def _ensure_store(db: Session, store_id: int) -> None:
    exists = db.execute(select(Store.id).where(Store.id == store_id, Store.active.is_(True))).scalar_one_or_none()
    if not exists:
        raise ValueError('Store not found')


def split_items(raw: str) -> list[str]:
    return [WS_RE.sub(' ', token.strip()) for token in SPLIT_RE.split(raw) if token.strip()]


def list_suggestions(db: Session, *, limit: int = 30) -> list[str]:
    rows = db.execute(
        select(CustomerRequestItem.name)
        .where(CustomerRequestItem.active.is_(True))
        .order_by(CustomerRequestItem.request_count.desc(), CustomerRequestItem.name.asc())
        .limit(limit)
    ).all()
    return [row[0] for row in rows]


def _get_or_create_item(db: Session, *, raw_name: str, principal_id: int) -> CustomerRequestItem:
    normalized = normalize_name(raw_name)
    if not normalized:
        raise ValueError('Item name cannot be empty')

    item = db.execute(select(CustomerRequestItem).where(CustomerRequestItem.normalized_name == normalized)).scalar_one_or_none()
    if item:
        if not item.active:
            item.active = True
        return item

    item = CustomerRequestItem(
        name=raw_name.strip(),
        normalized_name=normalized,
        request_count=0,
        active=True,
        created_by_principal_id=principal_id,
    )
    db.add(item)
    db.flush()
    return item


def create_submission(
    db: Session,
    *,
    store_id: int,
    principal_id: int,
    requested_items_raw: str,
    notes: str | None,
) -> CustomerRequestSubmission:
    _ensure_store(db, store_id)
    parsed_items = split_items(requested_items_raw)
    if not parsed_items:
        raise ValueError('Enter at least one requested item')

    counts = Counter(parsed_items)

    submission = CustomerRequestSubmission(
        store_id=store_id,
        notes=notes.strip() if notes and notes.strip() else None,
        created_by_principal_id=principal_id,
    )
    db.add(submission)
    db.flush()

    for raw_name, qty in counts.items():
        item = _get_or_create_item(db, raw_name=raw_name, principal_id=principal_id)
        item.request_count = max(0, item.request_count + qty)
        db.add(
            CustomerRequestLine(
                submission_id=submission.id,
                item_id=item.id,
                raw_name=raw_name,
                quantity=qty,
            )
        )

    db.flush()
    return submission


def list_submissions(
    db: Session,
    *,
    store_id: int | None,
    from_date: date | None,
    to_date: date | None,
) -> list[dict]:
    conditions = []
    if store_id:
        conditions.append(CustomerRequestSubmission.store_id == store_id)
    if from_date:
        conditions.append(
            CustomerRequestSubmission.created_at >= datetime.combine(from_date, time.min, tzinfo=timezone.utc)
        )
    if to_date:
        conditions.append(
            CustomerRequestSubmission.created_at
            < datetime.combine(to_date + timedelta(days=1), time.min, tzinfo=timezone.utc)
        )

    query = (
        select(
            CustomerRequestSubmission.id,
            CustomerRequestSubmission.store_id,
            Store.name.label('store_name'),
            CustomerRequestSubmission.notes,
            CustomerRequestSubmission.created_at,
        )
        .join(Store, Store.id == CustomerRequestSubmission.store_id)
        .order_by(CustomerRequestSubmission.created_at.desc())
    )
    if conditions:
        query = query.where(and_(*conditions))

    submissions = db.execute(query).all()

    ids = [row.id for row in submissions]
    lines_by_submission: dict[int, list[str]] = {sid: [] for sid in ids}
    if ids:
        line_rows = db.execute(
            select(CustomerRequestLine.submission_id, CustomerRequestLine.raw_name, CustomerRequestLine.quantity)
            .where(CustomerRequestLine.submission_id.in_(ids))
            .order_by(CustomerRequestLine.id.asc())
        ).all()
        for line in line_rows:
            label = f"{line.raw_name} ({line.quantity})" if line.quantity > 1 else line.raw_name
            lines_by_submission[line.submission_id].append(label)

    return [
        {
            'id': row.id,
            'store_id': row.store_id,
            'store_name': row.store_name,
            'notes': row.notes,
            'created_at': row.created_at,
            'items_summary': ', '.join(lines_by_submission.get(row.id, [])),
        }
        for row in submissions
    ]


def list_items_for_management(db: Session) -> list[CustomerRequestItem]:
    return db.execute(
        select(CustomerRequestItem)
        .order_by(CustomerRequestItem.request_count.desc(), CustomerRequestItem.name.asc())
    ).scalars().all()


def add_item(db: Session, *, name: str, principal_id: int) -> CustomerRequestItem:
    clean_name = name.strip()
    if not clean_name:
        raise ValueError('Item name is required')
    return _get_or_create_item(db, raw_name=clean_name, principal_id=principal_id)


def set_item_count(db: Session, *, item_id: int, request_count: int) -> CustomerRequestItem:
    if request_count < 0:
        raise ValueError('Request count cannot be negative')

    item = db.execute(select(CustomerRequestItem).where(CustomerRequestItem.id == item_id)).scalar_one_or_none()
    if not item:
        raise ValueError('Customer request item not found')

    item.request_count = request_count
    db.flush()
    return item
