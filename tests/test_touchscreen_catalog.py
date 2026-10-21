from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import BigInteger, create_engine, select
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.models import (
    Store,
    DigitalSignageMediaAsset,
    TouchscreenDevice,
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
from app.security.touchscreen_devices import hash_device_token, load_touchscreen_device
from app.services.touchscreen_catalog_service import StaleTouchscreenCatalog, catalog_for_store, flavor_detail_for_store
from app.services.touchscreen_inventory_sync_service import synchronize_touchscreen_cache


@compiles(CITEXT, 'sqlite')
def _compile_citext(_type, _compiler, **_kw):
    return 'TEXT'


@compiles(BigInteger, 'sqlite')
def _compile_bigint(_type, _compiler, **_kw):
    return 'INTEGER'


TABLES = (
    Store.__table__, TouchscreenSyncRun.__table__, TouchscreenSquareVariationCache.__table__,
    TouchscreenStoreInventoryCache.__table__, TouchscreenDevice.__table__, TouchscreenFlavor.__table__,
    TouchscreenFlavorCategory.__table__, TouchscreenFlavorCategoryLink.__table__,
    TouchscreenFlavorSkuLink.__table__, TouchscreenFlavorStoreOverride.__table__,
    TouchscreenFlavorRecommendation.__table__, DigitalSignageMediaAsset.__table__, TouchscreenFlavorMedia.__table__,
)


@pytest.fixture
def catalog_db(monkeypatch):
    engine = create_engine('sqlite://')
    for table in TABLES:
        table.create(engine, checkfirst=True)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(settings, 'touchscreen_cache_max_age_minutes', 30)
    monkeypatch.setattr(settings, 'touchscreen_default_inventory_threshold', 0)
    with Session() as db:
        db.add_all([Store(id=1, name='Downtown', square_location_id='LOC1'), Store(id=2, name='Uptown', square_location_id='LOC2')])
        run = TouchscreenSyncRun(id=1, status='SUCCEEDED', started_at=datetime.now(timezone.utc), completed_at=datetime.now(timezone.utc), freshness_at=datetime.now(timezone.utc), variation_count=4, inventory_record_count=8, is_complete=True)
        db.add(run)
        for index, variation in enumerate(('SALT-A', 'FREE-A', 'SALT-B', 'FREE-B'), 1):
            db.add(TouchscreenSquareVariationCache(square_variation_id=variation, sku=variation, item_name=f'Item {index}', variation_name='Default', is_active=True, is_sellable=True, successful_run_id=1))
        flavors = [
            TouchscreenFlavor(id=1, brand_name='Cloud', display_name='Watermelon Kiwi', slug='cloud-watermelon-kiwi', short_description='Bright melon and kiwi.', is_active=True, is_published=True, is_touchscreen_visible=True, display_order=1, created_by_principal_id=1, updated_by_principal_id=1),
            TouchscreenFlavor(id=2, brand_name='Cloud', display_name='Apple Ice', slug='cloud-apple-ice', short_description='Crisp apple.', is_active=True, is_published=True, is_touchscreen_visible=True, display_order=2, created_by_principal_id=1, updated_by_principal_id=1),
            TouchscreenFlavor(id=3, brand_name='Cloud', display_name='Draft Grape', slug='cloud-draft-grape', short_description='Hidden draft.', is_active=True, is_published=False, is_touchscreen_visible=True, display_order=3, created_by_principal_id=1, updated_by_principal_id=1),
        ]
        db.add_all(flavors)
        db.add_all([
            TouchscreenFlavorSkuLink(id=1, touchscreen_flavor_id=1, square_variation_id='SALT-A', format='SALT', cooling_type='NON_ICED', is_active=True, created_by_principal_id=1),
            TouchscreenFlavorSkuLink(id=2, touchscreen_flavor_id=1, square_variation_id='FREE-A', format='FREEBASE', cooling_type='NON_ICED', is_active=True, created_by_principal_id=1),
            TouchscreenFlavorSkuLink(id=3, touchscreen_flavor_id=2, square_variation_id='SALT-B', format='SALT', cooling_type='ICED', is_active=True, created_by_principal_id=1),
            TouchscreenFlavorSkuLink(id=4, touchscreen_flavor_id=3, square_variation_id='FREE-B', format='FREEBASE', cooling_type='NON_ICED', is_active=True, created_by_principal_id=1),
        ])
        fruity = TouchscreenFlavorCategory(id=1, name='Fruity', slug='fruity', category_type='BROAD', display_order=0)
        apple = TouchscreenFlavorCategory(id=2, name='Apple', slug='apple', category_type='FRUIT', display_order=0)
        kiwi = TouchscreenFlavorCategory(id=3, name='Kiwi', slug='kiwi', category_type='FRUIT', display_order=1)
        watermelon = TouchscreenFlavorCategory(id=4, name='Watermelon', slug='watermelon', category_type='FRUIT', display_order=2)
        db.add_all([fruity, apple, kiwi, watermelon])
        db.add_all([
            TouchscreenFlavorCategoryLink(touchscreen_flavor_id=1, category_id=1),
            TouchscreenFlavorCategoryLink(touchscreen_flavor_id=1, category_id=3),
            TouchscreenFlavorCategoryLink(touchscreen_flavor_id=1, category_id=4),
            TouchscreenFlavorCategoryLink(touchscreen_flavor_id=2, category_id=1),
            TouchscreenFlavorCategoryLink(touchscreen_flavor_id=2, category_id=2),
        ])
        quantities = {(1, 'SALT-A'): 6, (1, 'FREE-A'): 2, (1, 'SALT-B'): 0, (1, 'FREE-B'): 8, (2, 'SALT-A'): 0, (2, 'FREE-A'): 0, (2, 'SALT-B'): 9, (2, 'FREE-B'): 2}
        for (store_id, variation), quantity in quantities.items():
            db.add(TouchscreenStoreInventoryCache(store_id=store_id, square_variation_id=variation, available_quantity=Decimal(quantity), successful_run_id=1, freshness_at=run.freshness_at))
        db.add_all([
            TouchscreenFlavorRecommendation(id=1, source_flavor_id=1, recommended_flavor_id=2, sort_order=0, relationship_type='SIMILAR', is_active=True, created_by_principal_id=1),
            TouchscreenFlavorRecommendation(id=2, source_flavor_id=2, recommended_flavor_id=3, sort_order=0, relationship_type='SIMILAR', is_active=True, created_by_principal_id=1),
        ])
        db.commit()
    try:
        yield Session
    finally:
        engine.dispose()


def test_inventory_store_and_format_gating(catalog_db):
    with catalog_db() as db:
        assert [row['display_name'] for row in catalog_for_store(db, store_id=1)] == ['Watermelon Kiwi']
        assert catalog_for_store(db, store_id=1, format_filter='salt')[0]['available_formats'] == ['salt']
        assert catalog_for_store(db, store_id=1, format_filter='freebase')[0]['available_formats'] == ['freebase']
        assert [row['display_name'] for row in catalog_for_store(db, store_id=2)] == ['Apple Ice']


def test_positive_quantity_without_store_presence_is_excluded(catalog_db):
    with catalog_db() as db:
        row = db.get(TouchscreenStoreInventoryCache, (2, 'SALT-A'))
        row.available_quantity = Decimal(5)
        row.is_location_present = False
        db.commit()
        assert all(item['id'] != 1 for item in catalog_for_store(db, store_id=2))


def test_category_or_matching_and_combined_filters(catalog_db):
    with catalog_db() as db:
        assert [row['id'] for row in catalog_for_store(db, store_id=1, broad_category_ids={1})] == [1]
        assert [row['id'] for row in catalog_for_store(db, store_id=1, fruit_category_ids={2, 3})] == [1]
        assert catalog_for_store(db, store_id=1, format_filter='salt', broad_category_ids={1}, fruit_category_ids={3, 4})[0]['id'] == 1


def test_store_hide_and_threshold_overrides(catalog_db):
    with catalog_db() as db:
        db.add(TouchscreenFlavorStoreOverride(id=1, store_id=1, touchscreen_flavor_id=1, is_hidden=False, inventory_display_threshold=6, created_by_principal_id=1)); db.commit()
        assert catalog_for_store(db, store_id=1, format_filter='salt') == []
        override = db.get(TouchscreenFlavorStoreOverride, 1); override.inventory_display_threshold = 0; override.is_hidden = True; db.commit()
        assert catalog_for_store(db, store_id=1) == []


def test_recommendations_directional_stock_and_publication_gated(catalog_db):
    with catalog_db() as db:
        # Store 1 recommendation is out of stock, so the section is empty.
        detail = flavor_detail_for_store(db, store_id=1, flavor_id=1)
        assert detail['recommendations'] == []
        # Store 2 only Apple Ice is in stock; the reverse relationship was never created.
        reverse = flavor_detail_for_store(db, store_id=2, flavor_id=2)
        assert reverse['recommendations'] == []


def test_stale_cache_fails_closed_and_fresh_restores(catalog_db):
    with catalog_db() as db:
        run = db.get(TouchscreenSyncRun, 1); run.freshness_at = datetime.now(timezone.utc) - timedelta(hours=2); db.commit()
        with pytest.raises(StaleTouchscreenCatalog): catalog_for_store(db, store_id=1)
        run.freshness_at = datetime.now(timezone.utc); db.commit()
        assert catalog_for_store(db, store_id=1)


def test_device_token_is_hashed_revocable_and_store_bound(catalog_db):
    with catalog_db() as db:
        token = 'one-time-secret'
        db.add(TouchscreenDevice(id=1, store_id=1, name='Counter', token_hash=hash_device_token(token), status='ACTIVE', orientation='AUTO', created_by_principal_id=1)); db.commit()
        assert load_touchscreen_device(db, token).store_id == 1
        from fastapi import HTTPException
        with pytest.raises(HTTPException): load_touchscreen_device(db, 'wrong')
        device = db.get(TouchscreenDevice, 1); device.status = 'REVOKED'; device.revoked_at = datetime.now(timezone.utc); db.commit()
        with pytest.raises(HTTPException): load_touchscreen_device(db, token)


def test_failed_empty_or_partial_sync_preserves_previous_cache(catalog_db):
    with catalog_db() as db:
        previous = db.scalar(select(TouchscreenStoreInventoryCache.available_quantity).where(TouchscreenStoreInventoryCache.store_id == 1, TouchscreenStoreInventoryCache.square_variation_id == 'SALT-A'))
        run = synchronize_touchscreen_cache(db, catalog_fetcher=lambda: {}, inventory_fetcher=lambda *_: {})
        assert run.status == 'FAILED'
        assert db.scalar(select(TouchscreenStoreInventoryCache.available_quantity).where(TouchscreenStoreInventoryCache.store_id == 1, TouchscreenStoreInventoryCache.square_variation_id == 'SALT-A')) == previous
        valid_catalog = {'ONLY': {'square_variation_id': 'ONLY', 'sku': 'O', 'item_name': 'Only', 'variation_name': 'Default', 'is_active': True, 'is_sellable': True}}
        run = synchronize_touchscreen_cache(db, catalog_fetcher=lambda: valid_catalog, inventory_fetcher=lambda *_: {(1, 'ONLY'): Decimal(1)})
        assert run.status == 'FAILED' and 'partial' in run.error_summary.lower()
        assert db.scalar(select(TouchscreenStoreInventoryCache.available_quantity).where(TouchscreenStoreInventoryCache.store_id == 1, TouchscreenStoreInventoryCache.square_variation_id == 'SALT-A')) == previous
