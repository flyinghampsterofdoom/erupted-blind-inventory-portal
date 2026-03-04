from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AdminStoreCount,
    AdminStoreCountLine,
    AdminStoreCountStatus,
    SquareSyncEvent,
    SquareSyncStatus,
    Store,
)
from app.services.square_ordering_data_service import _square_post, fetch_catalog_variation_maps, fetch_on_hand_by_store_variation

ADMIN_STORE_COUNT_SQUARE_SYNC_TYPE = 'ADMIN_STORE_COUNT_SET_ON_HAND'


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _format_square_quantity(value: Decimal) -> str:
    normalized = value.normalize()
    text = format(normalized, 'f')
    return text if '.' in text else f'{text}.000'


def list_active_store_rows(db: Session) -> list[dict]:
    rows = db.execute(
        select(Store.id, Store.name, Store.square_location_id)
        .where(Store.active.is_(True))
        .order_by(Store.name.asc())
    ).all()
    return [
        {
            'id': int(row.id),
            'name': str(row.name),
            'square_location_id': str(row.square_location_id or ''),
        }
        for row in rows
    ]


def _get_active_store(db: Session, *, store_id: int) -> Store:
    store = db.execute(select(Store).where(Store.id == store_id, Store.active.is_(True))).scalar_one_or_none()
    if not store:
        raise ValueError('Store not found')
    if not str(store.square_location_id or '').strip():
        raise ValueError('Store is missing square_location_id')
    return store


def get_or_create_draft_count(db: Session, *, store_id: int, principal_id: int) -> tuple[AdminStoreCount, bool]:
    _get_active_store(db, store_id=store_id)
    existing = db.execute(
        select(AdminStoreCount)
        .where(
            AdminStoreCount.store_id == store_id,
            AdminStoreCount.status == AdminStoreCountStatus.DRAFT,
        )
        .order_by(AdminStoreCount.updated_at.desc(), AdminStoreCount.created_at.desc(), AdminStoreCount.id.desc())
    ).scalars().first()
    if existing:
        return existing, False

    by_variation_id, _by_sku = fetch_catalog_variation_maps()
    variation_ids = sorted(by_variation_id.keys())
    if not variation_ids:
        raise RuntimeError('Square catalog returned no inventory variations')
    on_hand_by_store_variation = fetch_on_hand_by_store_variation(db, variation_ids=variation_ids, store_ids=[store_id])

    count = AdminStoreCount(
        store_id=store_id,
        employee_name='',
        status=AdminStoreCountStatus.DRAFT,
        expected_fetched_at=_now(),
        created_by_principal_id=principal_id,
    )
    db.add(count)
    db.flush()

    lines: list[AdminStoreCountLine] = []
    metas = sorted(
        by_variation_id.values(),
        key=lambda meta: ((meta.item_name or '').lower(), (meta.variation_name or '').lower(), (meta.sku or '').lower()),
    )
    for meta in metas:
        lines.append(
            AdminStoreCountLine(
                count_id=count.id,
                variation_id=meta.variation_id,
                sku=meta.sku or None,
                item_name=meta.item_name,
                variation_name=meta.variation_name,
                expected_on_hand=on_hand_by_store_variation.get((store_id, meta.variation_id), Decimal('0')),
                counted_qty=None,
            )
        )
    db.add_all(lines)
    db.flush()
    return count, True


def get_draft_count(db: Session, *, count_id: int) -> AdminStoreCount:
    count = db.execute(select(AdminStoreCount).where(AdminStoreCount.id == count_id)).scalar_one_or_none()
    if not count:
        raise ValueError('Store count not found')
    if count.status != AdminStoreCountStatus.DRAFT:
        raise ValueError('Store count has already been submitted')
    return count


def list_count_lines(db: Session, *, count_id: int) -> list[dict]:
    rows = db.execute(
        select(AdminStoreCountLine)
        .where(AdminStoreCountLine.count_id == count_id)
        .order_by(
            AdminStoreCountLine.item_name.asc(),
            AdminStoreCountLine.variation_name.asc(),
            AdminStoreCountLine.sku.asc(),
        )
    ).scalars().all()
    output: list[dict] = []
    for row in rows:
        counted = row.counted_qty
        variance = (counted - row.expected_on_hand) if counted is not None else None
        output.append(
            {
                'variation_id': row.variation_id,
                'sku': row.sku,
                'item_name': row.item_name,
                'variation_name': row.variation_name,
                'expected_on_hand': row.expected_on_hand,
                'counted_qty': row.counted_qty,
                'variance': variance,
            }
        )
    return output


def save_draft_count(
    db: Session,
    *,
    count: AdminStoreCount,
    employee_name: str,
    counted_by_variation_id: dict[str, Decimal | None],
    principal_id: int,
) -> AdminStoreCount:
    lines = db.execute(
        select(AdminStoreCountLine).where(AdminStoreCountLine.count_id == count.id)
    ).scalars().all()
    by_variation_id = {line.variation_id: line for line in lines}
    for variation_id, counted_qty in counted_by_variation_id.items():
        line = by_variation_id.get(variation_id)
        if not line:
            continue
        line.counted_qty = counted_qty
        line.updated_by_principal_id = principal_id

    count.employee_name = employee_name.strip()
    count.updated_at = _now()
    db.flush()
    return count


def submit_count(
    db: Session,
    *,
    count: AdminStoreCount,
    employee_name: str,
    counted_by_variation_id: dict[str, Decimal | None],
    principal_id: int,
) -> dict:
    save_draft_count(
        db,
        count=count,
        employee_name=employee_name,
        counted_by_variation_id=counted_by_variation_id,
        principal_id=principal_id,
    )
    if not count.employee_name.strip():
        raise ValueError('Name is required')

    store = _get_active_store(db, store_id=count.store_id)
    location_id = str(store.square_location_id or '').strip()
    now = _now()
    lines = db.execute(
        select(AdminStoreCountLine).where(AdminStoreCountLine.count_id == count.id)
    ).scalars().all()
    if not lines:
        raise ValueError('Store count has no lines to submit')

    missing = [line for line in lines if line.counted_qty is None]
    if missing:
        raise ValueError(f'All lines require a counted quantity before submit ({len(missing)} missing)')

    attempted = 0
    succeeded = 0
    failed = 0
    for line in lines:
        attempted += 1
        counted_qty = Decimal(str(line.counted_qty))
        idempotency_key = f'admin-store-count-sync-{uuid4().hex}'
        request_payload = {
            'admin_store_count_id': count.id,
            'store_id': count.store_id,
            'store_name': store.name,
            'location_id': location_id,
            'variation_id': line.variation_id,
            'sku': str(line.sku or ''),
            'item_name': line.item_name,
            'variation_name': line.variation_name,
            'counted_qty': str(counted_qty),
            'expected_on_hand': str(line.expected_on_hand),
            'variance': str(counted_qty - line.expected_on_hand),
            'source': 'management_store_count',
        }
        event = SquareSyncEvent(
            purchase_order_id=None,
            purchase_order_line_id=None,
            store_id=count.store_id,
            sync_type=ADMIN_STORE_COUNT_SQUARE_SYNC_TYPE,
            idempotency_key=idempotency_key,
            status=SquareSyncStatus.PENDING,
            request_payload=request_payload,
            response_payload=None,
            error_text=None,
            attempt_count=0,
            last_attempt_at=None,
        )
        db.add(event)
        db.flush()

        payload = {
            'idempotency_key': idempotency_key,
            'changes': [
                {
                    'type': 'PHYSICAL_COUNT',
                    'physical_count': {
                        'catalog_object_id': line.variation_id,
                        'location_id': location_id,
                        'state': 'IN_STOCK',
                        'quantity': _format_square_quantity(counted_qty),
                        'occurred_at': _to_iso(now),
                    },
                }
            ],
            'ignore_unchanged_counts': False,
        }
        try:
            response = _square_post('/v2/inventory/changes/batch-create', payload)
            event.status = SquareSyncStatus.SUCCESS
            event.response_payload = response
            event.error_text = None
            succeeded += 1
        except RuntimeError as exc:
            event.status = SquareSyncStatus.FAILED
            event.response_payload = None
            event.error_text = str(exc)
            failed += 1

        event.attempt_count = 1
        event.last_attempt_at = _now()
        db.flush()

    if failed:
        raise RuntimeError(f'Square sync incomplete ({succeeded} succeeded, {failed} failed)')

    count.status = AdminStoreCountStatus.SUBMITTED
    count.submitted_by_principal_id = principal_id
    count.submitted_at = _now()
    count.updated_at = _now()
    db.flush()
    return {
        'count_id': count.id,
        'store_id': count.store_id,
        'attempted': attempted,
        'succeeded': succeeded,
        'failed': failed,
    }
