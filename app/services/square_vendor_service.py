from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Vendor


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _square_post(path: str, payload: dict) -> dict:
    if not settings.square_access_token:
        raise RuntimeError('SQUARE_ACCESS_TOKEN is required')

    headers = {
        'Authorization': f'Bearer {settings.square_access_token}',
        'Content-Type': 'application/json',
    }
    if settings.square_api_version:
        headers['Square-Version'] = settings.square_api_version

    req = Request(
        url=f"{settings.square_api_base_url.rstrip('/')}{path}",
        data=json.dumps(payload).encode('utf-8'),
        headers=headers,
        method='POST',
    )
    try:
        with urlopen(req, timeout=settings.square_timeout_seconds) as response:
            parsed = json.loads(response.read().decode('utf-8'))
    except HTTPError as exc:
        body = exc.read().decode('utf-8', errors='ignore') if exc.fp else ''
        raise RuntimeError(f'Square API error {exc.code}: {body}') from exc
    except URLError as exc:
        raise RuntimeError(f'Square API network error: {exc.reason}') from exc

    if parsed.get('errors'):
        raise RuntimeError(f"Square API returned errors: {parsed['errors']}")
    return parsed

def _fetch_square_vendors() -> list[dict]:
    vendors: list[dict] = []
    cursor: str | None = None
    while True:
        payload: dict = {
            'query': {
                'filter': {
                    # SearchVendors requires non-empty filter.
                    'status': ['ACTIVE', 'INACTIVE'],
                }
            },
            'limit': 100,
        }
        if cursor:
            payload['cursor'] = cursor
        response = _square_post(
            '/v2/vendors/search',
            payload,
        )
        vendors.extend(response.get('vendors', []))
        cursor = response.get('cursor')
        if not cursor:
            break
    return vendors


def sync_vendors_from_square(db: Session) -> tuple[int, int, int]:
    square_vendors = _fetch_square_vendors()
    by_square_id = {
        row.square_vendor_id: row
        for row in db.execute(select(Vendor)).scalars().all()
    }

    created = 0
    updated = 0
    deactivated = 0
    seen: set[str] = set()

    for sv in square_vendors:
        square_vendor_id = (sv.get('id') or '').strip()
        if not square_vendor_id:
            continue
        seen.add(square_vendor_id)
        name = (sv.get('name') or '').strip() or square_vendor_id
        status = str(sv.get('status') or '').upper()
        is_active = status != 'INACTIVE'

        existing = by_square_id.get(square_vendor_id)
        if existing is None:
            db.add(
                Vendor(
                    square_vendor_id=square_vendor_id,
                    name=name,
                    active=is_active,
                    last_synced_at=_now(),
                )
            )
            created += 1
            continue

        changed = False
        if existing.name != name:
            existing.name = name
            changed = True
        if existing.active != is_active:
            existing.active = is_active
            changed = True
        existing.last_synced_at = _now()
        if changed:
            updated += 1

    for existing in by_square_id.values():
        if existing.square_vendor_id in seen:
            continue
        if existing.active:
            existing.active = False
            existing.last_synced_at = _now()
            deactivated += 1

    db.flush()
    return created, updated, deactivated
