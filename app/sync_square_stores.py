from __future__ import annotations

import argparse
import json
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import Store


def _slugify(name: str) -> str:
    value = re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')
    return value or 'store'


def _square_get(path: str) -> dict:
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
        headers=headers,
        method='GET',
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


def sync_stores(*, deactivate_missing: bool = False) -> tuple[int, int, int]:
    payload = _square_get('/v2/locations')
    locations = payload.get('locations', [])
    square_ids = {loc.get('id') for loc in locations if loc.get('id')}

    created = 0
    updated = 0
    deactivated = 0

    with SessionLocal() as db:
        existing_by_square_id = {
            s.square_location_id: s
            for s in db.execute(select(Store).where(Store.square_location_id.is_not(None))).scalars().all()
        }

        for loc in locations:
            loc_id = loc.get('id')
            if not loc_id:
                continue
            loc_name = (loc.get('name') or '').strip() or f'Store {loc_id}'
            is_active = (loc.get('status') or '').upper() == 'ACTIVE'
            store = existing_by_square_id.get(loc_id)

            if not store:
                db.add(
                    Store(
                        name=loc_name,
                        square_location_id=loc_id,
                        active=is_active,
                    )
                )
                created += 1
                continue

            changed = False
            if store.name != loc_name:
                store.name = loc_name
                changed = True
            if store.active != is_active:
                store.active = is_active
                changed = True
            if changed:
                updated += 1

        if deactivate_missing:
            for store in db.execute(select(Store)).scalars().all():
                if store.square_location_id and store.square_location_id not in square_ids and store.active:
                    store.active = False
                    deactivated += 1

        db.commit()

    return created, updated, deactivated


def main() -> None:
    parser = argparse.ArgumentParser(description='Sync stores/locations from Square.')
    parser.add_argument(
        '--deactivate-missing',
        action='store_true',
        help='Deactivate stores that have a square_location_id no longer present in Square.',
    )
    args = parser.parse_args()

    created, updated, deactivated = sync_stores(deactivate_missing=args.deactivate_missing)
    print(f'Square store sync complete: created={created}, updated={updated}, deactivated={deactivated}')


if __name__ == '__main__':
    main()
