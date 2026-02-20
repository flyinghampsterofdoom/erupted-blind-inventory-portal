from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import Campaign


STOPWORDS = {
    'the',
    'and',
    'for',
    'with',
    'from',
    'off',
    'kit',
    'pod',
    'coil',
    'tank',
    'device',
    'battery',
}


@dataclass
class SquareClient:
    base_url: str
    headers: dict[str, str]
    timeout_seconds: int

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
            raise RuntimeError(f'Square API error {exc.code}: {body}') from exc
        except URLError as exc:
            raise RuntimeError(f'Square API network error: {exc.reason}') from exc

        if data.get('errors'):
            raise RuntimeError(f"Square API returned errors: {data['errors']}")
        return data


TOKEN_RE = re.compile(r"[A-Za-z0-9']+")


def _tokenize(text: str) -> list[str]:
    return TOKEN_RE.findall(text)


def _brand_candidate(name: str) -> str | None:
    tokens = _tokenize(name)
    if not tokens:
        return None

    first = tokens[0]
    if first.isdigit():
        return None

    low = first.lower()
    if low in STOPWORDS:
        return None

    # Keep canonical capitalization from source token.
    return first


def fetch_catalog_items(client: SquareClient) -> list[dict]:
    items: list[dict] = []
    cursor: str | None = None
    while True:
        payload: dict = {'limit': 100}
        if cursor:
            payload['cursor'] = cursor
        response = client.post('/v2/catalog/search-catalog-items', payload)
        items.extend(response.get('items', []))
        cursor = response.get('cursor')
        if not cursor:
            break
    return items


def fetch_categories(client: SquareClient) -> dict[str, str]:
    categories_by_id: dict[str, str] = {}
    cursor: str | None = None
    while True:
        payload: dict = {
            'object_types': ['CATEGORY'],
            'include_deleted_objects': False,
            'limit': 100,
        }
        if cursor:
            payload['cursor'] = cursor
        response = client.post('/v2/catalog/search', payload)
        for obj in response.get('objects', []):
            if obj.get('type') != 'CATEGORY':
                continue
            object_id = obj.get('id')
            name = (obj.get('category_data', {}) or {}).get('name')
            if object_id and name:
                categories_by_id[object_id] = name.strip()
        cursor = response.get('cursor')
        if not cursor:
            break
    return categories_by_id


def build_candidates(items: list[dict], categories_by_id: dict[str, str], min_items: int) -> list[tuple[str, int]]:
    counts = Counter()

    for item in items:
        item_data = item.get('item_data', {})
        reporting_category = item_data.get('reporting_category') or {}
        reporting_category_id = reporting_category.get('id')
        category_name = (categories_by_id.get(reporting_category_id) or '').strip()
        if not category_name:
            continue

        counts[category_name] += 1

    return sorted([(name, count) for name, count in counts.items() if count >= min_items], key=lambda x: (-x[1], x[0]))


def sync_campaigns(min_items: int, deactivate_missing: bool) -> tuple[int, int, int]:
    if not settings.square_access_token:
        raise RuntimeError('SQUARE_ACCESS_TOKEN is required')

    headers = {
        'Authorization': f'Bearer {settings.square_access_token}',
        'Content-Type': 'application/json',
    }
    if settings.square_api_version:
        headers['Square-Version'] = settings.square_api_version

    client = SquareClient(
        base_url=settings.square_api_base_url.rstrip('/'),
        headers=headers,
        timeout_seconds=settings.square_timeout_seconds,
    )

    items = fetch_catalog_items(client)
    categories_by_id = fetch_categories(client)
    candidates = build_candidates(items, categories_by_id=categories_by_id, min_items=min_items)
    candidate_names = {name for name, _ in candidates}

    created = 0
    updated = 0
    deactivated = 0

    with SessionLocal() as db:
        existing = db.execute(select(Campaign)).scalars().all()
        by_filter = {c.category_filter: c for c in existing if c.category_filter}

        for name, _count in candidates:
            campaign = by_filter.get(name)
            if campaign:
                changed = False
                if not campaign.active:
                    campaign.active = True
                    changed = True
                if campaign.label != name:
                    campaign.label = name
                    changed = True
                if changed:
                    updated += 1
            else:
                db.add(
                    Campaign(
                        label=name,
                        category_filter=name,
                        brand_filter=None,
                        active=True,
                    )
                )
                created += 1

        if deactivate_missing:
            for campaign in existing:
                if campaign.category_filter and campaign.active and campaign.category_filter not in candidate_names:
                    campaign.active = False
                    deactivated += 1

        db.commit()

    return created, updated, deactivated


def main() -> None:
    parser = argparse.ArgumentParser(description='Sync campaign filters from Square catalog (read-only).')
    parser.add_argument('--min-items', type=int, default=3, help='Minimum number of items required to create campaign candidate.')
    parser.add_argument('--deactivate-missing', action='store_true', help='Deactivate existing campaigns not found in current Square candidates.')
    args = parser.parse_args()

    created, updated, deactivated = sync_campaigns(args.min_items, args.deactivate_missing)
    print(f'Square campaign sync complete: created={created}, updated={updated}, deactivated={deactivated}')


if __name__ == '__main__':
    main()
