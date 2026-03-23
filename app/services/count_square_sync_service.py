from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import CountSession, SessionStatus, SquareSyncEvent, SquareSyncStatus, Store
from app.services.session_service import get_management_variance_lines

COUNT_SESSION_SQUARE_SYNC_TYPE = 'COUNT_SESSION_SET_ON_HAND'
COUNT_SESSION_RECOUNT_SQUARE_SYNC_TYPE = 'COUNT_SESSION_SET_ON_HAND_RECOUNT_ONLY'


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _format_square_quantity(value: Decimal) -> str:
    normalized = value.normalize()
    text = format(normalized, 'f')
    return text if '.' in text else f'{text}.000'


def _normalize_square_base_url(raw_base_url: str) -> str:
    clean = str(raw_base_url or '').strip().rstrip('/')
    if not clean:
        return 'https://connect.squareup.com'
    parts = urlsplit(clean)
    if parts.scheme and parts.netloc:
        return f'{parts.scheme}://{parts.netloc}'
    if clean.startswith('connect.squareup.com'):
        return f'https://{clean.split("/", 1)[0]}'
    return clean


class _SquareClient:
    def __init__(self) -> None:
        if not settings.square_access_token:
            raise RuntimeError('SQUARE_ACCESS_TOKEN is required')
        self.base_url = _normalize_square_base_url(settings.square_api_base_url)
        self.timeout_seconds = settings.square_timeout_seconds
        self.headers = {
            'Authorization': f'Bearer {settings.square_access_token}',
            'Content-Type': 'application/json',
        }
        if settings.square_api_version:
            self.headers['Square-Version'] = settings.square_api_version

    def post(self, path: str, payload: dict) -> dict:
        req = Request(
            url=f'{self.base_url}{path}',
            data=json.dumps(payload).encode('utf-8'),
            headers=self.headers,
            method='POST',
        )
        try:
            with urlopen(req, timeout=self.timeout_seconds) as response:
                data = json.loads(response.read().decode('utf-8'))
        except HTTPError as exc:
            body = exc.read().decode('utf-8', errors='ignore') if exc.fp else ''
            raise RuntimeError(f'Square API error {exc.code} for {path}: {body}') from exc
        except URLError as exc:
            raise RuntimeError(f'Square API network error for {path}: {exc.reason}') from exc

        if data.get('errors'):
            raise RuntimeError(f"Square API returned errors on {path}: {data['errors']}")
        return data


def _push_session_variance_to_square(
    db: Session,
    *,
    session_id: int,
    recount_only: bool,
    sync_type: str,
    no_rows_error: str,
) -> dict:
    session_row = db.execute(select(CountSession).where(CountSession.id == session_id)).scalar_one_or_none()
    if session_row is None:
        raise ValueError('Session not found')
    if session_row.status != SessionStatus.SUBMITTED:
        raise ValueError('Only submitted sessions can be pushed to Square')

    store = db.execute(select(Store).where(Store.id == session_row.store_id)).scalar_one_or_none()
    if store is None:
        raise ValueError('Store not found')
    square_location_id = str(store.square_location_id or '').strip()
    if not square_location_id:
        raise ValueError('Store is missing square_location_id')

    variance_rows = get_management_variance_lines(db, session_id=session_id)
    rows_to_push = [row for row in variance_rows if Decimal(str(row.get('variance') or 0)) != 0]
    if recount_only:
        rows_to_push = [row for row in rows_to_push if str(row.get('section_type') or '').upper() == 'RECOUNT']
    if not rows_to_push:
        raise ValueError(no_rows_error)

    return _push_rows_to_square(
        db,
        session_id=session_id,
        store_id=session_row.store_id,
        store_name=store.name,
        square_location_id=square_location_id,
        rows_to_push=rows_to_push,
        sync_type=sync_type,
    )


def _push_rows_to_square(
    db: Session,
    *,
    session_id: int,
    store_id: int,
    store_name: str,
    square_location_id: str,
    rows_to_push: list[dict],
    sync_type: str,
) -> dict:
    client = _SquareClient()
    now = _now()
    attempted = 0
    succeeded = 0
    failed = 0
    results: list[dict] = []

    for row in rows_to_push:
        attempted += 1
        variation_id = str(row.get('variation_id') or '').strip()
        if not variation_id:
            failed += 1
            results.append(
                {
                    'variation_id': '',
                    'item_name': str(row.get('item_name') or ''),
                    'variation_name': str(row.get('variation_name') or ''),
                    'status': 'FAILED',
                    'error': 'Missing variation_id',
                }
            )
            continue

        counted_qty = Decimal(str(row.get('counted_qty') or '0'))
        idempotency_key = f'count-session-sync-{uuid4().hex}'
        payload = {
            'idempotency_key': idempotency_key,
            'changes': [
                {
                    'type': 'PHYSICAL_COUNT',
                    'physical_count': {
                        'catalog_object_id': variation_id,
                        'location_id': square_location_id,
                        'state': 'IN_STOCK',
                        'quantity': _format_square_quantity(counted_qty),
                        'occurred_at': _to_iso(now),
                    },
                }
            ],
            'ignore_unchanged_counts': False,
        }
        request_payload = {
            'session_id': session_id,
            'store_id': store_id,
            'store_name': store_name,
            'location_id': square_location_id,
            'variation_id': variation_id,
            'sku': str(row.get('sku') or ''),
            'item_name': str(row.get('item_name') or ''),
            'variation_name': str(row.get('variation_name') or ''),
            'counted_qty': str(counted_qty),
            'expected_on_hand': str(row.get('expected_on_hand') or '0'),
            'variance': str(row.get('variance') or '0'),
            'source': 'management_session_variance',
        }
        event = SquareSyncEvent(
            purchase_order_id=None,
            purchase_order_line_id=None,
            store_id=store_id,
            sync_type=sync_type,
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

        try:
            response = client.post('/v2/inventory/changes/batch-create', payload)
            event.status = SquareSyncStatus.SUCCESS
            event.response_payload = response
            event.error_text = None
            succeeded += 1
            results.append(
                {
                    'variation_id': variation_id,
                    'item_name': str(row.get('item_name') or ''),
                    'variation_name': str(row.get('variation_name') or ''),
                    'status': 'SUCCESS',
                    'error': None,
                }
            )
        except RuntimeError as exc:
            event.status = SquareSyncStatus.FAILED
            event.response_payload = None
            event.error_text = str(exc)
            failed += 1
            results.append(
                {
                    'variation_id': variation_id,
                    'item_name': str(row.get('item_name') or ''),
                    'variation_name': str(row.get('variation_name') or ''),
                    'status': 'FAILED',
                    'error': str(exc),
                }
            )

        event.attempt_count = 1
        event.last_attempt_at = _now()
        db.flush()

    return {
        'session_id': session_id,
        'store_id': store_id,
        'store_name': store_name,
        'location_id': square_location_id,
        'attempted': attempted,
        'succeeded': succeeded,
        'failed': failed,
        'results': results,
    }


def push_session_variance_to_square(
    db: Session,
    *,
    session_id: int,
) -> dict:
    return _push_session_variance_to_square(
        db,
        session_id=session_id,
        recount_only=False,
        sync_type=COUNT_SESSION_SQUARE_SYNC_TYPE,
        no_rows_error='No variance lines to push for this session',
    )


def push_session_recount_variance_to_square(
    db: Session,
    *,
    session_id: int,
) -> dict:
    return _push_session_variance_to_square(
        db,
        session_id=session_id,
        recount_only=True,
        sync_type=COUNT_SESSION_RECOUNT_SQUARE_SYNC_TYPE,
        no_rows_error='No recount variance lines to push for this session',
    )


def push_recount_closeout_rows_to_square(
    db: Session,
    *,
    session_id: int,
    rows: list[dict],
) -> dict:
    session_row = db.execute(select(CountSession).where(CountSession.id == session_id)).scalar_one_or_none()
    if session_row is None:
        raise ValueError('Session not found')
    if session_row.status != SessionStatus.SUBMITTED:
        raise ValueError('Only submitted sessions can be pushed to Square')
    store = db.execute(select(Store).where(Store.id == session_row.store_id)).scalar_one_or_none()
    if store is None:
        raise ValueError('Store not found')
    square_location_id = str(store.square_location_id or '').strip()
    if not square_location_id:
        raise ValueError('Store is missing square_location_id')

    rows_to_push = [row for row in rows if Decimal(str(row.get('variance') or 0)) != 0]
    if not rows_to_push:
        return {
            'session_id': session_id,
            'store_id': session_row.store_id,
            'store_name': store.name,
            'location_id': square_location_id,
            'attempted': 0,
            'succeeded': 0,
            'failed': 0,
            'results': [],
        }

    return _push_rows_to_square(
        db,
        session_id=session_id,
        store_id=session_row.store_id,
        store_name=store.name,
        square_location_id=square_location_id,
        rows_to_push=rows_to_push,
        sync_type='COUNT_SESSION_RECOUNT_AUTO_CLOSEOUT',
    )


def list_count_square_sync_report_rows(
    db: Session,
    *,
    store_id: int | None,
    from_date: date | None,
    to_date: date | None,
    session_id: int | None = None,
    recount_only: bool = False,
    limit: int = 500,
) -> list[dict]:
    sync_types = [COUNT_SESSION_RECOUNT_SQUARE_SYNC_TYPE] if recount_only else [
        COUNT_SESSION_SQUARE_SYNC_TYPE,
        COUNT_SESSION_RECOUNT_SQUARE_SYNC_TYPE,
    ]
    query = (
        select(
            SquareSyncEvent,
            Store.name.label('store_name'),
        )
        .outerjoin(Store, Store.id == SquareSyncEvent.store_id)
        .where(SquareSyncEvent.sync_type.in_(sync_types))
        .order_by(SquareSyncEvent.created_at.desc(), SquareSyncEvent.id.desc())
        .limit(max(1, min(limit, 2000)))
    )
    if store_id:
        query = query.where(SquareSyncEvent.store_id == store_id)
    if from_date:
        from_ts = datetime.combine(from_date, time.min, tzinfo=timezone.utc)
        query = query.where(SquareSyncEvent.created_at >= from_ts)
    if to_date:
        to_ts = datetime.combine(to_date + timedelta(days=1), time.min, tzinfo=timezone.utc)
        query = query.where(SquareSyncEvent.created_at < to_ts)

    rows = db.execute(query).all()
    out: list[dict] = []
    for event, store_name in rows:
        payload = event.request_payload or {}
        payload_session_id = payload.get('session_id')
        try:
            payload_session_id = int(payload_session_id) if payload_session_id is not None else None
        except (TypeError, ValueError):
            payload_session_id = None
        if session_id is not None and payload_session_id != session_id:
            continue
        out.append(
            {
                'event_id': event.id,
                'created_at': event.created_at,
                'store_id': event.store_id,
                'store_name': store_name or f'Store {event.store_id}' if event.store_id else '-',
                'location_id': str(payload.get('location_id') or ''),
                'session_id': payload_session_id,
                'variation_id': str(payload.get('variation_id') or ''),
                'sku': str(payload.get('sku') or ''),
                'item_name': str(payload.get('item_name') or ''),
                'variation_name': str(payload.get('variation_name') or ''),
                'counted_qty': str(payload.get('counted_qty') or '0'),
                'expected_on_hand': str(payload.get('expected_on_hand') or '0'),
                'variance': str(payload.get('variance') or '0'),
                'status': event.status.value if hasattr(event.status, 'value') else str(event.status),
                'sync_type': event.sync_type,
                'error_text': event.error_text,
            }
        )
    return out
