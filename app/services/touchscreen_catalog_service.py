from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    DigitalSignageMediaAsset,
    TouchscreenFlavor,
    TouchscreenFlavorCategory,
    TouchscreenFlavorCategoryLink,
    TouchscreenFlavorMedia,
    TouchscreenFlavorRecommendation,
    TouchscreenFlavorSkuLink,
    TouchscreenFlavorStoreOverride,
    TouchscreenSquareVariationCache,
    TouchscreenStoreInventoryCache,
    TouchscreenSyncRun,
)


logger = logging.getLogger(__name__)


class StaleTouchscreenCatalog(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def require_fresh_cache(db: Session) -> TouchscreenSyncRun:
    run = db.execute(select(TouchscreenSyncRun).where(
        TouchscreenSyncRun.status == 'SUCCEEDED', TouchscreenSyncRun.is_complete.is_(True),
        TouchscreenSyncRun.freshness_at.is_not(None),
    ).order_by(TouchscreenSyncRun.freshness_at.desc(), TouchscreenSyncRun.id.desc())).scalars().first()
    cutoff = _now() - timedelta(minutes=max(1, settings.touchscreen_cache_max_age_minutes))
    freshness = run.freshness_at if run else None
    if freshness is not None and freshness.tzinfo is None:
        freshness = freshness.replace(tzinfo=timezone.utc)
    if run is None or freshness is None or freshness < cutoff:
        logger.error('Touchscreen catalog unavailable because the last complete cache is missing or stale.')
        raise StaleTouchscreenCatalog('Catalog temporarily unavailable. Please ask a staff member for assistance.')
    return run


def _eligible_rows(db: Session, *, store_id: int, format_filter: str = 'both') -> list[tuple]:
    run = require_fresh_cache(db)
    query = (
        select(
            TouchscreenFlavor, TouchscreenFlavorSkuLink, TouchscreenStoreInventoryCache,
            TouchscreenFlavorStoreOverride,
        )
        .join(TouchscreenFlavorSkuLink, TouchscreenFlavorSkuLink.touchscreen_flavor_id == TouchscreenFlavor.id)
        .join(TouchscreenSquareVariationCache, TouchscreenSquareVariationCache.square_variation_id == TouchscreenFlavorSkuLink.square_variation_id)
        .join(TouchscreenStoreInventoryCache, (
            (TouchscreenStoreInventoryCache.square_variation_id == TouchscreenFlavorSkuLink.square_variation_id)
            & (TouchscreenStoreInventoryCache.store_id == store_id)
        ))
        .outerjoin(TouchscreenFlavorStoreOverride, (
            (TouchscreenFlavorStoreOverride.touchscreen_flavor_id == TouchscreenFlavor.id)
            & (TouchscreenFlavorStoreOverride.store_id == store_id)
        ))
        .where(
            TouchscreenFlavor.deleted_at.is_(None), TouchscreenFlavor.is_active.is_(True),
            TouchscreenFlavor.is_published.is_(True), TouchscreenFlavor.is_touchscreen_visible.is_(True),
            TouchscreenFlavorSkuLink.is_active.is_(True), TouchscreenSquareVariationCache.is_active.is_(True),
            TouchscreenSquareVariationCache.is_sellable.is_(True),
            TouchscreenStoreInventoryCache.is_location_present.is_(True),
            TouchscreenStoreInventoryCache.successful_run_id == run.id,
        )
    )
    clean_format = str(format_filter or 'both').lower()
    if clean_format in {'salt', 'freebase'}:
        query = query.where(TouchscreenFlavorSkuLink.format == clean_format.upper())
    rows = db.execute(query).all()
    out = []
    global_threshold = Decimal(settings.touchscreen_default_inventory_threshold)
    for flavor, mapping, inventory, override in rows:
        if override is not None and override.is_hidden:
            continue
        threshold = Decimal(override.inventory_display_threshold) if override and override.inventory_display_threshold is not None else global_threshold
        if inventory.available_quantity <= threshold:
            continue
        out.append((flavor, mapping, inventory, override))
    return out


def _category_maps(db: Session, flavor_ids: set[int]) -> tuple[dict[int, list[dict]], dict[int, set[int]]]:
    labels: dict[int, list[dict]] = {item_id: [] for item_id in flavor_ids}
    ids: dict[int, set[int]] = {item_id: set() for item_id in flavor_ids}
    if not flavor_ids:
        return labels, ids
    rows = db.execute(select(TouchscreenFlavorCategoryLink, TouchscreenFlavorCategory).join(
        TouchscreenFlavorCategory, TouchscreenFlavorCategory.id == TouchscreenFlavorCategoryLink.category_id
    ).where(
        TouchscreenFlavorCategoryLink.touchscreen_flavor_id.in_(flavor_ids),
        TouchscreenFlavorCategory.is_active.is_(True),
    )).all()
    for link, category in rows:
        labels[link.touchscreen_flavor_id].append({'id': category.id, 'name': str(category.name), 'type': category.category_type})
        ids[link.touchscreen_flavor_id].add(category.id)
    return labels, ids


def _media_map(db: Session, flavor_ids: set[int]) -> dict[int, dict | None]:
    result = {item_id: None for item_id in flavor_ids}
    if not flavor_ids:
        return result
    rows = db.execute(select(TouchscreenFlavorMedia, DigitalSignageMediaAsset).join(
        DigitalSignageMediaAsset, DigitalSignageMediaAsset.id == TouchscreenFlavorMedia.media_asset_id
    ).where(
        TouchscreenFlavorMedia.touchscreen_flavor_id.in_(flavor_ids), TouchscreenFlavorMedia.role == 'PRIMARY',
        DigitalSignageMediaAsset.archived_at.is_(None),
    ).order_by(TouchscreenFlavorMedia.sort_order)).all()
    for link, asset in rows:
        result.setdefault(link.touchscreen_flavor_id, {'url': f'/touchscreen/media/{asset.public_token}', 'alt': link.alt_text or ''})
        if result[link.touchscreen_flavor_id] is None:
            result[link.touchscreen_flavor_id] = {'url': f'/touchscreen/media/{asset.public_token}', 'alt': link.alt_text or ''}
    return result


def catalog_for_store(
    db: Session, *, store_id: int, format_filter: str = 'both', broad_category_ids: set[int] | None = None,
    fruit_category_ids: set[int] | None = None,
) -> list[dict]:
    rows = _eligible_rows(db, store_id=store_id, format_filter=format_filter)
    by_id: dict[int, dict] = {}
    for flavor, mapping, _inventory, _override in rows:
        item = by_id.setdefault(flavor.id, {'model': flavor, 'formats': set(), 'cooling': set()})
        item['formats'].add(mapping.format.lower())
        item['cooling'].add(mapping.cooling_type.lower())
    flavor_ids = set(by_id)
    category_labels, category_ids = _category_maps(db, flavor_ids)
    media = _media_map(db, flavor_ids)
    broad = set(broad_category_ids or ())
    fruits = set(fruit_category_ids or ())
    results: list[dict] = []
    for flavor_id, aggregate in by_id.items():
        labels = category_labels[flavor_id]
        broad_ids = {row['id'] for row in labels if row['type'] == 'BROAD'}
        fruit_ids = {row['id'] for row in labels if row['type'] == 'FRUIT'}
        if broad and not broad_ids.intersection(broad):
            continue
        if fruits and not fruit_ids.intersection(fruits):
            continue
        flavor = aggregate['model']
        results.append({
            'id': flavor.id, 'brand_name': flavor.brand_name, 'display_name': flavor.display_name,
            'short_description': flavor.short_description,
            'image': media.get(flavor_id), 'available_formats': sorted(aggregate['formats']),
            'available_cooling_types': sorted(aggregate['cooling']), 'category_labels': labels,
            'display_order': flavor.display_order,
        })
    results.sort(key=lambda row: (row['display_order'], row['brand_name'].lower(), row['display_name'].lower()))
    for row in results:
        row.pop('display_order', None)
    return results


def flavor_detail_for_store(db: Session, *, store_id: int, flavor_id: int, format_filter: str = 'both') -> dict | None:
    matches = catalog_for_store(db, store_id=store_id, format_filter=format_filter)
    current = next((row for row in matches if row['id'] == flavor_id), None)
    if current is None:
        return None
    flavor = db.get(TouchscreenFlavor, flavor_id)
    current['long_description'] = flavor.long_description or '' if flavor else ''
    recommendation_ids = list(db.execute(select(TouchscreenFlavorRecommendation.recommended_flavor_id).where(
        TouchscreenFlavorRecommendation.source_flavor_id == flavor_id,
        TouchscreenFlavorRecommendation.is_active.is_(True),
    ).order_by(TouchscreenFlavorRecommendation.sort_order)).scalars())
    match_map = {row['id']: row for row in matches if row['id'] != flavor_id}
    current['recommendations'] = [match_map[item_id] for item_id in recommendation_ids if item_id in match_map]
    return current
