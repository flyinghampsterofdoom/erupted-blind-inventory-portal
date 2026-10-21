from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import (
    Store,
    TouchscreenSquareVariationCache,
    TouchscreenStoreInventoryCache,
    TouchscreenSyncRun,
)
from app.services.square_ordering_data_service import _square_post, fetch_on_hand_by_store_variation


CatalogFetcher = Callable[[], dict[str, dict]]
InventoryFetcher = Callable[[Session, list[str], list[int]], dict[tuple[int, str], Decimal]]


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _optional_datetime(value: object) -> datetime | None:
    if value in {None, ''}:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValueError('Square returned a malformed catalog timestamp.') from exc


def square_catalog_fetcher() -> dict[str, dict]:
    items: list[dict] = []
    cursor: str | None = None
    while True:
        payload: dict = {'limit': 100}
        if cursor:
            payload['cursor'] = cursor
        response = _square_post('/v2/catalog/search-catalog-items', payload)
        response_items = response.get('items')
        if not isinstance(response_items, list):
            raise ValueError('Square returned a malformed catalog response.')
        items.extend(response_items)
        cursor = response.get('cursor')
        if not cursor:
            break
    out: dict[str, dict] = {}
    for item in items:
        if not isinstance(item, dict):
            raise ValueError('Square returned a malformed catalog item.')
        item_data = item.get('item_data') or {}
        item_name = str(item_data.get('name') or item.get('name') or '').strip()
        item_active = not bool(item.get('is_deleted') or item_data.get('is_archived'))
        for variation in item_data.get('variations', []) or []:
            if not isinstance(variation, dict):
                raise ValueError('Square returned a malformed catalog variation.')
            variation_id = str(variation.get('id') or '').strip()
            vdata = variation.get('item_variation_data') or {}
            if not variation_id:
                continue
            out[variation_id] = {
                'square_variation_id': variation_id,
                'sku': str(vdata.get('sku') or '').strip() or None,
                'item_name': item_name or str(vdata.get('sku') or variation_id),
                'variation_name': str(vdata.get('name') or 'Default').strip() or 'Default',
                'is_active': item_active and not bool(variation.get('is_deleted')),
                'is_sellable': bool(vdata.get('sellable', True)),
                'source_updated_at': variation.get('updated_at') or item.get('updated_at'),
                'present_at_all_locations': bool(variation.get('present_at_all_locations', item.get('present_at_all_locations', False))),
                'present_at_location_ids': list(variation.get('present_at_location_ids') or item.get('present_at_location_ids') or []),
                'absent_at_location_ids': list(variation.get('absent_at_location_ids') or item.get('absent_at_location_ids') or []),
            }
    return out


def square_inventory_fetcher(
    db: Session, variation_ids: list[str], store_ids: list[int]
) -> dict[tuple[int, str], Decimal]:
    return fetch_on_hand_by_store_variation(db, variation_ids=variation_ids, store_ids=store_ids)


def _validate_catalog(payload: object) -> dict[str, dict]:
    if not isinstance(payload, dict) or not payload:
        raise ValueError('Square returned an unexpectedly empty catalog response.')
    validated: dict[str, dict] = {}
    for key, row in payload.items():
        if not isinstance(row, dict):
            raise ValueError('Square returned a malformed catalog response.')
        variation_id = str(row.get('square_variation_id') or key or '').strip()
        item_name = str(row.get('item_name') or '').strip()
        if not variation_id or not item_name:
            raise ValueError('Square returned an incomplete catalog variation.')
        validated[variation_id] = {
            'sku': str(row.get('sku') or '').strip() or None,
            'item_name': item_name,
            'variation_name': str(row.get('variation_name') or 'Default').strip() or 'Default',
            'is_active': bool(row.get('is_active', True)),
            'is_sellable': bool(row.get('is_sellable', True)),
            'source_updated_at': _optional_datetime(row.get('source_updated_at')),
            'present_at_all_locations': bool(row.get('present_at_all_locations', True)),
            'present_at_location_ids': {str(value) for value in row.get('present_at_location_ids', [])},
            'absent_at_location_ids': {str(value) for value in row.get('absent_at_location_ids', [])},
        }
    return validated


def _validate_inventory(
    payload: object, *, variation_ids: list[str], store_ids: list[int]
) -> dict[tuple[int, str], Decimal]:
    if not isinstance(payload, dict) or (variation_ids and store_ids and not payload):
        raise ValueError('Square returned an unexpectedly empty inventory response.')
    expected = {(store_id, variation_id) for store_id in store_ids for variation_id in variation_ids}
    if not expected.issubset(set(payload)):
        raise ValueError('Square returned a partial inventory response.')
    validated: dict[tuple[int, str], Decimal] = {}
    for key in expected:
        try:
            validated[key] = Decimal(str(payload[key]))
        except Exception as exc:
            raise ValueError('Square returned a malformed inventory quantity.') from exc
    return validated


def synchronize_touchscreen_cache(
    db: Session,
    *,
    principal_id: int | None = None,
    catalog_fetcher: CatalogFetcher = square_catalog_fetcher,
    inventory_fetcher: InventoryFetcher = square_inventory_fetcher,
) -> TouchscreenSyncRun:
    run = TouchscreenSyncRun(status='RUNNING', started_at=_now(), created_by_principal_id=principal_id)
    db.add(run)
    db.commit()
    try:
        catalog = _validate_catalog(catalog_fetcher())
        store_rows = db.execute(select(Store.id, Store.square_location_id).where(
            Store.active.is_(True), Store.square_location_id.is_not(None)
        )).all()
        store_ids = [int(row.id) for row in store_rows]
        location_by_store = {int(row.id): str(row.square_location_id) for row in store_rows}
        if not store_ids:
            raise ValueError('No active Square-enabled stores are configured.')
        variation_ids = sorted(catalog)
        inventory = _validate_inventory(
            inventory_fetcher(db, variation_ids, store_ids), variation_ids=variation_ids, store_ids=store_ids
        )
        freshness = _now()
        # External responses are fully validated before the atomic replacement begins.
        db.execute(delete(TouchscreenStoreInventoryCache))
        db.execute(delete(TouchscreenSquareVariationCache))
        db.add_all([
            TouchscreenSquareVariationCache(
                square_variation_id=variation_id, successful_run_id=run.id, cached_at=freshness,
                sku=row['sku'], item_name=row['item_name'], variation_name=row['variation_name'],
                is_active=row['is_active'], is_sellable=row['is_sellable'], source_updated_at=row['source_updated_at'],
            ) for variation_id, row in catalog.items()
        ])
        db.add_all([
            TouchscreenStoreInventoryCache(
                store_id=store_id, square_variation_id=variation_id, available_quantity=quantity,
                is_location_present=(
                    Decimal(quantity) > 0
                    or (
                        location_by_store[store_id] not in catalog[variation_id]['absent_at_location_ids']
                        and (
                            catalog[variation_id]['present_at_all_locations']
                            or location_by_store[store_id] in catalog[variation_id]['present_at_location_ids']
                        )
                    )
                ),
                successful_run_id=run.id, freshness_at=freshness,
            ) for (store_id, variation_id), quantity in inventory.items()
        ])
        run.status = 'SUCCEEDED'
        run.completed_at = freshness
        run.freshness_at = freshness
        run.variation_count = len(catalog)
        run.inventory_record_count = len(inventory)
        run.is_complete = True
        run.error_summary = None
        db.commit()
    except Exception as exc:
        db.rollback()
        run = db.get(TouchscreenSyncRun, run.id)
        run.status = 'FAILED'
        run.completed_at = _now()
        run.is_complete = False
        run.error_summary = str(exc)[:1000]
        db.commit()
    return run


def sync_health(db: Session) -> dict:
    last_attempt = db.execute(select(TouchscreenSyncRun).order_by(
        TouchscreenSyncRun.started_at.desc(), TouchscreenSyncRun.id.desc()
    )).scalars().first()
    last_success = db.execute(select(TouchscreenSyncRun).where(
        TouchscreenSyncRun.status == 'SUCCEEDED', TouchscreenSyncRun.is_complete.is_(True)
    ).order_by(TouchscreenSyncRun.completed_at.desc(), TouchscreenSyncRun.id.desc())).scalars().first()
    return {'last_attempt': last_attempt, 'last_success': last_success}
