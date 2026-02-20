from __future__ import annotations

import json
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import Campaign, Store
from app.services.snapshot_provider import CountItemInput


class SquareSnapshotProvider:
    def __init__(self) -> None:
        if not settings.square_access_token:
            raise ValueError('SQUARE_ACCESS_TOKEN is required when SNAPSHOT_PROVIDER=square')
        if not settings.square_read_only:
            raise ValueError('Square provider is read-only only. Set SQUARE_READ_ONLY=true.')

        self.base_url = settings.square_api_base_url.rstrip('/')
        self.headers = {
            'Authorization': f'Bearer {settings.square_access_token}',
            'Content-Type': 'application/json',
        }
        if settings.square_api_version:
            self.headers['Square-Version'] = settings.square_api_version

    def _post(self, path: str, payload: dict) -> dict:
        data = json.dumps(payload).encode('utf-8')
        req = Request(
            url=f'{self.base_url}{path}',
            data=data,
            headers=self.headers,
            method='POST',
        )
        try:
            with urlopen(req, timeout=settings.square_timeout_seconds) as response:
                parsed = json.loads(response.read().decode('utf-8'))
        except HTTPError as exc:
            body = exc.read().decode('utf-8', errors='ignore') if exc.fp else ''
            raise ValueError(f'Square API error {exc.code} on {path}: {body}') from exc
        except URLError as exc:
            raise ValueError(f'Square API network error on {path}: {exc.reason}') from exc

        if parsed.get('errors'):
            raise ValueError(f'Square API returned errors on {path}: {parsed["errors"]}')
        return parsed

    def _get_store(self, *, store_id: int) -> Store:
        with SessionLocal() as db:
            store = db.execute(select(Store).where(Store.id == store_id)).scalar_one_or_none()
            if not store:
                raise ValueError('Store not found')
            return store

    def _get_store_and_campaign(self, *, store_id: int, campaign_id: int) -> tuple[Store, Campaign]:
        with SessionLocal() as db:
            store = db.execute(select(Store).where(Store.id == store_id)).scalar_one_or_none()
            if not store:
                raise ValueError('Store not found')
            campaign = db.execute(select(Campaign).where(Campaign.id == campaign_id)).scalar_one_or_none()
            if not campaign:
                raise ValueError('Campaign not found')
            return store, campaign

    def _campaign_filters(self, campaign: Campaign) -> tuple[str | None, str | None]:
        category = campaign.category_filter.strip().lower() if campaign.category_filter else None
        if category and category.endswith(' rotation'):
            category = category.removesuffix(' rotation').strip()
        brand = campaign.brand_filter.strip().lower() if campaign.brand_filter else None
        return category, brand

    def _fetch_categories_by_id(self) -> dict[str, str]:
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
            response = self._post('/v2/catalog/search', payload)
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

    def list_count_items(self, *, store_id: int, campaign_id: int) -> list[CountItemInput]:
        _store, campaign = self._get_store_and_campaign(store_id=store_id, campaign_id=campaign_id)
        category_filter, brand_filter = self._campaign_filters(campaign)
        categories_by_id = self._fetch_categories_by_id()

        cursor: str | None = None
        items: list[dict] = []
        while True:
            payload: dict = {'limit': 100}
            if cursor:
                payload['cursor'] = cursor
            response = self._post('/v2/catalog/search-catalog-items', payload)
            items.extend(response.get('items', []))
            cursor = response.get('cursor')
            if not cursor:
                break

        results: list[CountItemInput] = []
        seen: set[str] = set()

        for item in items:
            item_data = item.get('item_data', {})
            item_name = item_data.get('name') or item.get('name') or ''
            reporting_category_id = (item_data.get('reporting_category') or {}).get('id')
            reporting_category_name = (categories_by_id.get(reporting_category_id) or '').strip()
            searchable_text = f'{item_name} {reporting_category_name}'.lower()

            if category_filter and reporting_category_name.lower() != category_filter:
                continue
            if brand_filter and brand_filter not in searchable_text:
                continue

            for variation in item_data.get('variations', []):
                variation_id = variation.get('id')
                if not variation_id or variation_id in seen:
                    continue
                variation_data = variation.get('item_variation_data', {})
                results.append(
                    CountItemInput(
                        variation_id=variation_id,
                        sku=variation_data.get('sku'),
                        item_name=item_name,
                        variation_name=variation_data.get('name') or 'Default',
                        source_catalog_version='square-live-read',
                    )
                )
                seen.add(variation_id)

        if not results:
            raise ValueError('Square catalog returned no countable variations for this campaign filter')
        return results

    def fetch_current_on_hand(self, *, store_id: int, variation_ids: list[str]) -> dict[str, Decimal]:
        store = self._get_store(store_id=store_id)
        if not store.square_location_id:
            raise ValueError('Store is missing square_location_id')

        values: dict[str, Decimal] = {variation_id: Decimal('0') for variation_id in variation_ids}
        if not variation_ids:
            return values

        batch_size = 100
        for i in range(0, len(variation_ids), batch_size):
            chunk = variation_ids[i : i + batch_size]
            cursor: str | None = None
            while True:
                payload: dict = {
                    'catalog_object_ids': chunk,
                    'location_ids': [store.square_location_id],
                    'states': ['IN_STOCK'],
                    'limit': 100,
                }
                if cursor:
                    payload['cursor'] = cursor

                response = self._post('/v2/inventory/batch-retrieve-counts', payload)
                for count in response.get('counts', []):
                    object_id = count.get('catalog_object_id')
                    qty = Decimal(count.get('quantity', '0'))
                    if object_id in values:
                        values[object_id] = qty

                cursor = response.get('cursor')
                if not cursor:
                    break

        return values
