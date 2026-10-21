from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import Principal, Role, require_capability
from app.config import settings
from app.db import get_db
from app.dependencies import get_client_ip
from app.models import (
    DigitalSignageMediaAsset,
    Store,
    TouchscreenDevice,
    TouchscreenFlavor,
    TouchscreenFlavorCategory,
    TouchscreenFlavorCategoryLink,
    TouchscreenFlavorMedia,
    TouchscreenFlavorRecommendation,
    TouchscreenFlavorSkuLink,
    TouchscreenFlavorStoreOverride,
    TouchscreenSquareVariationCache,
)
from app.security.csrf import verify_csrf
from app.security.touchscreen_devices import create_touchscreen_device, revoke_touchscreen_device
from app.services.digital_signage_storage import StorageUnavailable, configured_signage_storage
from app.services.touchscreen_catalog_service import StaleTouchscreenCatalog, catalog_for_store
from app.services.touchscreen_inventory_sync_service import sync_health, synchronize_touchscreen_cache
from app.services.touchscreen_management_service import save_flavor, set_flavor_published
from app.services.touchscreen_media_service import (
    remove_primary_flavor_image,
    set_primary_flavor_image,
    store_touchscreen_image,
    validate_image_upload,
)
from app.v2.feature_exposure import require_v2_feature
from app.v2.navigation import build_navigation


router = APIRouter(prefix='/v2/touchscreen', tags=['v2-touchscreen'])
feature_access = require_v2_feature('touchscreen_v2')
view_access = require_capability('touchscreen.view', Role.ADMIN, Role.MANAGER)
flavor_access = require_capability('touchscreen.manage_flavors', Role.ADMIN, Role.MANAGER)
category_access = require_capability('touchscreen.manage_categories', Role.ADMIN, Role.MANAGER)
media_access = require_capability('touchscreen.manage_media', Role.ADMIN, Role.MANAGER)
mapping_access = require_capability('touchscreen.manage_mappings', Role.ADMIN, Role.MANAGER)
recommendation_access = require_capability('touchscreen.manage_recommendations', Role.ADMIN, Role.MANAGER)
device_access = require_capability('touchscreen.manage_devices', Role.ADMIN, Role.MANAGER)
publish_access = require_capability('touchscreen.publish', Role.ADMIN, Role.MANAGER)
preview_access = require_capability('touchscreen.preview', Role.ADMIN, Role.MANAGER)


class Page:
    def __init__(self, label: str, description: str):
        self.slug = 'touchscreen'
        self.label = label
        self.description = description
        self.badge = 'Owner Preview'


def _context(request: Request, principal: Principal, label: str, description: str, **values):
    return {
        'request': request, 'principal': principal, 'page': Page(label, description),
        'navigation': build_navigation(request), 'stores': [], 'selected_store_ids': [],
        'all_stores_selected': True, 'store_scope_label': 'Organization-wide', 'scope_locked': True,
        **values,
    }


def _back(path: str, *, message: str = '', error: str = '') -> RedirectResponse:
    params = []
    if message: params.append(f'message={quote(message)}')
    if error: params.append(f'error={quote(error)}')
    return RedirectResponse(path + (('?' + '&'.join(params)) if params else ''), status_code=303)


@router.get('')
def root(_feature: Principal = Depends(feature_access), _principal: Principal = Depends(view_access)):
    return RedirectResponse('/v2/touchscreen/flavors', status_code=303)


@router.get('/flavors')
def flavors_page(request: Request, _feature: Principal = Depends(feature_access), principal: Principal = Depends(view_access), db: Session = Depends(get_db)):
    query = select(TouchscreenFlavor).where(TouchscreenFlavor.deleted_at.is_(None))
    status_filter = request.query_params.get('status', '')
    if status_filter == 'published': query = query.where(TouchscreenFlavor.is_published.is_(True))
    if status_filter == 'draft': query = query.where(TouchscreenFlavor.is_published.is_(False))
    flavors = db.execute(query.order_by(TouchscreenFlavor.display_order, TouchscreenFlavor.brand_name, TouchscreenFlavor.display_name)).scalars().all()
    ids = [row.id for row in flavors]
    mapping_counts = dict(db.execute(select(
        TouchscreenFlavorSkuLink.touchscreen_flavor_id, func.count(TouchscreenFlavorSkuLink.id)
    ).where(TouchscreenFlavorSkuLink.touchscreen_flavor_id.in_(ids) if ids else False).group_by(TouchscreenFlavorSkuLink.touchscreen_flavor_id)).all())
    formats: dict[int, set[str]] = {}
    for flavor_id, item_format in db.execute(select(TouchscreenFlavorSkuLink.touchscreen_flavor_id, TouchscreenFlavorSkuLink.format).where(
        TouchscreenFlavorSkuLink.touchscreen_flavor_id.in_(ids) if ids else False
    )).all(): formats.setdefault(flavor_id, set()).add(item_format)
    images = {row.touchscreen_flavor_id: row for row in db.execute(select(TouchscreenFlavorMedia).where(
        TouchscreenFlavorMedia.touchscreen_flavor_id.in_(ids) if ids else False, TouchscreenFlavorMedia.role == 'PRIMARY'
    )).scalars()}
    return request.app.state.templates.TemplateResponse('v2/touchscreen/flavors.html', _context(
        request, principal, 'Touchscreen Flavors', 'Manage customer-facing flavor profiles, mappings, images, and publishing.',
        flavors=flavors, mapping_counts=mapping_counts, formats=formats, images=images,
        message=request.query_params.get('message', ''), error=request.query_params.get('error', ''), status_filter=status_filter,
    ))


def _editor_context(request: Request, db: Session, principal: Principal, flavor: TouchscreenFlavor | None, error: str = ''):
    categories = db.execute(select(TouchscreenFlavorCategory).where(TouchscreenFlavorCategory.is_active.is_(True)).order_by(
        TouchscreenFlavorCategory.category_type, TouchscreenFlavorCategory.display_order, TouchscreenFlavorCategory.name
    )).scalars().all()
    variations = db.execute(select(TouchscreenSquareVariationCache).order_by(
        TouchscreenSquareVariationCache.item_name, TouchscreenSquareVariationCache.variation_name
    )).scalars().all()
    stores = db.execute(select(Store).where(Store.active.is_(True)).order_by(Store.name)).scalars().all()
    other_flavors = db.execute(select(TouchscreenFlavor).where(
        TouchscreenFlavor.deleted_at.is_(None), TouchscreenFlavor.id != flavor.id if flavor else True
    ).order_by(TouchscreenFlavor.brand_name, TouchscreenFlavor.display_name)).scalars().all()
    category_ids = set(); mappings = []; recommendation_ids = set(); overrides = {}; image = None
    if flavor:
        category_ids = set(db.execute(select(TouchscreenFlavorCategoryLink.category_id).where(TouchscreenFlavorCategoryLink.touchscreen_flavor_id == flavor.id)).scalars())
        mappings = db.execute(select(TouchscreenFlavorSkuLink).where(TouchscreenFlavorSkuLink.touchscreen_flavor_id == flavor.id).order_by(TouchscreenFlavorSkuLink.id)).scalars().all()
        recommendation_ids = set(db.execute(select(TouchscreenFlavorRecommendation.recommended_flavor_id).where(
            TouchscreenFlavorRecommendation.source_flavor_id == flavor.id, TouchscreenFlavorRecommendation.is_active.is_(True)
        )).scalars())
        overrides = {row.store_id: row for row in db.execute(select(TouchscreenFlavorStoreOverride).where(
            TouchscreenFlavorStoreOverride.touchscreen_flavor_id == flavor.id
        )).scalars()}
        image = db.execute(select(TouchscreenFlavorMedia, DigitalSignageMediaAsset).join(
            DigitalSignageMediaAsset, DigitalSignageMediaAsset.id == TouchscreenFlavorMedia.media_asset_id
        ).where(TouchscreenFlavorMedia.touchscreen_flavor_id == flavor.id, TouchscreenFlavorMedia.role == 'PRIMARY')).first()
    return _context(
        request, principal, 'Edit Flavor' if flavor else 'New Flavor',
        'Configure the customer profile and explicit Square variation classifications.',
        flavor=flavor, categories=categories, category_ids=category_ids, variations=variations, mappings=mappings,
        stores=stores, other_flavors=other_flavors, recommendation_ids=recommendation_ids, overrides=overrides,
        image=image, error=error, max_upload_bytes=settings.touchscreen_max_upload_bytes,
    )


@router.get('/flavors/new')
def new_flavor_page(request: Request, _feature: Principal = Depends(feature_access), principal: Principal = Depends(flavor_access), db: Session = Depends(get_db)):
    return request.app.state.templates.TemplateResponse('v2/touchscreen/flavor_editor.html', _editor_context(request, db, principal, None))


@router.get('/flavors/{flavor_id}')
def edit_flavor_page(flavor_id: int, request: Request, _feature: Principal = Depends(feature_access), principal: Principal = Depends(view_access), db: Session = Depends(get_db)):
    flavor = db.get(TouchscreenFlavor, flavor_id)
    if flavor is None or flavor.deleted_at is not None: raise HTTPException(status_code=404)
    return request.app.state.templates.TemplateResponse('v2/touchscreen/flavor_editor.html', _editor_context(request, db, principal, flavor, request.query_params.get('error', '')))


def _mapping_rows(form) -> list[dict]:
    rows = []
    for variation_id, item_format, cooling in zip(
        form.getlist('map_variation_id'), form.getlist('map_format'), form.getlist('map_cooling')
    ):
        if str(item_format).strip():
            rows.append({'square_variation_id': variation_id, 'format': item_format, 'cooling_type': cooling})
    return rows


@router.post('/flavors/save')
async def save_flavor_route(request: Request, _feature: Principal = Depends(feature_access), principal: Principal = Depends(flavor_access),
    _mapping: Principal = Depends(mapping_access), _recommendation: Principal = Depends(recommendation_access),
    _csrf: None = Depends(verify_csrf), db: Session = Depends(get_db)):
    form = await request.form()
    try:
        flavor_id = int(form.get('flavor_id')) if form.get('flavor_id') else None
        override_rows = []
        for store in db.execute(select(Store).where(Store.active.is_(True))).scalars():
            override_rows.append({'store_id': store.id, 'is_hidden': bool(form.get(f'hidden_store_{store.id}')),
                'inventory_display_threshold': form.get(f'threshold_store_{store.id}'), 'reason': form.get(f'reason_store_{store.id}')})
        flavor = save_flavor(db, flavor_id=flavor_id, principal=principal, ip=get_client_ip(request),
            brand_name=str(form.get('brand_name', '')), display_name=str(form.get('display_name', '')),
            short_description=str(form.get('short_description', '')), long_description=str(form.get('long_description', '')),
            display_order=int(str(form.get('display_order', '0')) or 0), is_active=bool(form.get('is_active')),
            is_touchscreen_visible=bool(form.get('is_touchscreen_visible')),
            category_ids={int(value) for value in form.getlist('category_id')}, mappings=_mapping_rows(form),
            recommendation_ids=[int(value) for value in form.getlist('recommendation_id')], store_overrides=override_rows)
        db.commit()
        return _back(f'/v2/touchscreen/flavors/{flavor.id}', message='Flavor saved.')
    except Exception as exc:
        db.rollback()
        return _back(f'/v2/touchscreen/flavors/{form.get("flavor_id")}' if form.get('flavor_id') else '/v2/touchscreen/flavors/new', error=str(exc))


@router.post('/flavors/{flavor_id}/publish')
async def publish_flavor_route(flavor_id: int, request: Request, _feature: Principal = Depends(feature_access), principal: Principal = Depends(publish_access), _csrf: None = Depends(verify_csrf), db: Session = Depends(get_db)):
    form = await request.form()
    try:
        set_flavor_published(db, flavor_id=flavor_id, published=str(form.get('published', 'false')).lower() == 'true', principal=principal, ip=get_client_ip(request)); db.commit()
        return _back(f'/v2/touchscreen/flavors/{flavor_id}', message='Publishing status updated.')
    except ValueError as exc:
        db.rollback(); return _back(f'/v2/touchscreen/flavors/{flavor_id}', error=str(exc))


@router.post('/flavors/{flavor_id}/image')
async def upload_flavor_image(flavor_id: int, request: Request, _feature: Principal = Depends(feature_access), principal: Principal = Depends(media_access), _csrf: None = Depends(verify_csrf), db: Session = Depends(get_db)):
    form = await request.form(); upload = form.get('media_file')
    if upload is None or not hasattr(upload, 'read'): return _back(f'/v2/touchscreen/flavors/{flavor_id}', error='Choose an image.')
    try:
        content = await upload.read(settings.touchscreen_max_upload_bytes + 1)
        if len(content) > settings.touchscreen_max_upload_bytes:
            raise ValueError(f'The image exceeds the {settings.touchscreen_max_upload_bytes // (1024 * 1024)} MB upload limit.')
        image = validate_image_upload(filename=upload.filename or '', browser_content_type=upload.content_type or '', content=content)
        asset = store_touchscreen_image(db, principal=principal, image=image, storage=configured_signage_storage(), ip=get_client_ip(request))
        set_primary_flavor_image(db, flavor_id=flavor_id, asset_id=asset.id, alt_text=str(form.get('alt_text', '')), principal=principal, ip=get_client_ip(request))
        db.commit(); return _back(f'/v2/touchscreen/flavors/{flavor_id}', message='Flavor image updated.')
    except Exception as exc:
        db.rollback(); return _back(f'/v2/touchscreen/flavors/{flavor_id}', error=str(exc))


@router.post('/flavors/{flavor_id}/image/remove')
def remove_flavor_image(flavor_id: int, request: Request, _feature: Principal = Depends(feature_access), principal: Principal = Depends(media_access), _csrf: None = Depends(verify_csrf), db: Session = Depends(get_db)):
    remove_primary_flavor_image(db, flavor_id=flavor_id, principal=principal, ip=get_client_ip(request)); db.commit()
    return _back(f'/v2/touchscreen/flavors/{flavor_id}', message='Flavor image removed.')


@router.get('/media/{public_token}/content')
def management_media(public_token: str, _feature: Principal = Depends(feature_access), _principal: Principal = Depends(view_access), db: Session = Depends(get_db)):
    asset = db.execute(select(DigitalSignageMediaAsset).where(DigitalSignageMediaAsset.public_token == public_token, DigitalSignageMediaAsset.archived_at.is_(None))).scalar_one_or_none()
    if asset is None: raise HTTPException(status_code=404)
    try: content = configured_signage_storage().get(asset.storage_key)
    except StorageUnavailable as exc: raise HTTPException(status_code=503, detail=str(exc)) from exc
    return Response(content=content, media_type=asset.content_type, headers={'Cache-Control': 'private, max-age=3600', 'X-Content-Type-Options': 'nosniff'})


@router.get('/categories')
def categories_page(request: Request, _feature: Principal = Depends(feature_access), principal: Principal = Depends(view_access), db: Session = Depends(get_db)):
    categories = db.execute(select(TouchscreenFlavorCategory).order_by(TouchscreenFlavorCategory.category_type, TouchscreenFlavorCategory.display_order, TouchscreenFlavorCategory.name)).scalars().all()
    return request.app.state.templates.TemplateResponse('v2/touchscreen/categories.html', _context(request, principal, 'Flavor Categories', 'Manage centrally controlled broad categories and fruit varieties.', categories=categories, message=request.query_params.get('message', ''), error=request.query_params.get('error', '')))


@router.post('/categories')
async def create_category_route(request: Request, _feature: Principal = Depends(feature_access), principal: Principal = Depends(category_access), _csrf: None = Depends(verify_csrf), db: Session = Depends(get_db)):
    from app.services.touchscreen_management_service import slugify
    form = await request.form(); name = str(form.get('name', '')).strip(); category_type = str(form.get('category_type', '')).upper()
    if not name or category_type not in {'BROAD', 'FRUIT', 'OTHER_NOTE'}: return _back('/v2/touchscreen/categories', error='Enter a name and valid category type.')
    db.add(TouchscreenFlavorCategory(name=name, slug=slugify(name), category_type=category_type, display_order=int(str(form.get('display_order', '0')) or 0), is_active=True)); db.commit()
    return _back('/v2/touchscreen/categories', message='Category created.')


@router.post('/categories/{category_id}/toggle')
def toggle_category(category_id: int, _feature: Principal = Depends(feature_access), _principal: Principal = Depends(category_access), _csrf: None = Depends(verify_csrf), db: Session = Depends(get_db)):
    category = db.get(TouchscreenFlavorCategory, category_id)
    if category is None: raise HTTPException(status_code=404)
    category.is_active = not category.is_active; category.updated_at = datetime.now(tz=timezone.utc); db.commit()
    return _back('/v2/touchscreen/categories', message='Category status updated.')


@router.get('/devices')
def devices_page(request: Request, _feature: Principal = Depends(feature_access), principal: Principal = Depends(view_access), db: Session = Depends(get_db)):
    devices = db.execute(select(TouchscreenDevice, Store).join(Store, Store.id == TouchscreenDevice.store_id).order_by(TouchscreenDevice.name)).all()
    stores = db.execute(select(Store).where(Store.active.is_(True)).order_by(Store.name)).scalars().all()
    return request.app.state.templates.TemplateResponse('v2/touchscreen/devices.html', _context(request, principal, 'Touchscreen Devices', 'Issue store-bound credentials and revoke devices.', devices=devices, stores=stores, revealed_token=request.query_params.get('token', ''), message=request.query_params.get('message', ''), error=request.query_params.get('error', '')))


@router.post('/devices')
async def create_device_route(request: Request, _feature: Principal = Depends(feature_access), principal: Principal = Depends(device_access), _csrf: None = Depends(verify_csrf), db: Session = Depends(get_db)):
    form = await request.form()
    try:
        _device, token = create_touchscreen_device(db, store_id=int(form.get('store_id')), name=str(form.get('name', '')), orientation=str(form.get('orientation', 'AUTO')), principal=principal, ip=get_client_ip(request)); db.commit()
        return _back('/v2/touchscreen/devices', message='Device created. Copy the one-time token now.') if not token else RedirectResponse(f'/v2/touchscreen/devices?message={quote("Device created. Copy the one-time token now.")}&token={quote(token)}', status_code=303)
    except Exception as exc:
        db.rollback(); return _back('/v2/touchscreen/devices', error=str(exc))


@router.post('/devices/{device_id}/revoke')
def revoke_device_route(device_id: int, request: Request, _feature: Principal = Depends(feature_access), principal: Principal = Depends(device_access), _csrf: None = Depends(verify_csrf), db: Session = Depends(get_db)):
    revoke_touchscreen_device(db, device_id=device_id, principal=principal, ip=get_client_ip(request)); db.commit()
    return _back('/v2/touchscreen/devices', message='Device revoked.')


@router.get('/preview')
def preview_page(request: Request, _feature: Principal = Depends(feature_access), principal: Principal = Depends(preview_access), db: Session = Depends(get_db)):
    stores = db.execute(select(Store).where(Store.active.is_(True)).order_by(Store.name)).scalars().all()
    store_id = int(request.query_params.get('store_id')) if request.query_params.get('store_id', '').isdigit() else (stores[0].id if stores else None)
    results = []; stale_error = ''
    if store_id:
        try: results = catalog_for_store(db, store_id=store_id)
        except StaleTouchscreenCatalog as exc: stale_error = str(exc)
    return request.app.state.templates.TemplateResponse('v2/touchscreen/preview.html', _context(request, principal, 'Touchscreen Preview', 'Preview the current stock-gated catalog by store.', stores=stores, selected_store_id=store_id, results=results, stale_error=stale_error))


@router.get('/sync')
def sync_status_page(request: Request, _feature: Principal = Depends(feature_access), principal: Principal = Depends(view_access), db: Session = Depends(get_db)):
    health = sync_health(db); now = datetime.now(tz=timezone.utc)
    age = None
    if health['last_success'] and health['last_success'].freshness_at:
        freshness = health['last_success'].freshness_at
        if freshness.tzinfo is None: freshness = freshness.replace(tzinfo=timezone.utc)
        age = now - freshness
    return request.app.state.templates.TemplateResponse('v2/touchscreen/sync.html', _context(request, principal, 'Square Cache Health', 'Monitor the local touchscreen catalog and inventory read model.', health=health, cache_age=age, max_age_minutes=settings.touchscreen_cache_max_age_minutes, message=request.query_params.get('message', ''), error=request.query_params.get('error', '')))


@router.post('/sync')
def run_sync_route(_feature: Principal = Depends(feature_access), principal: Principal = Depends(mapping_access), _csrf: None = Depends(verify_csrf), db: Session = Depends(get_db)):
    run = synchronize_touchscreen_cache(db, principal_id=principal.id)
    return _back('/v2/touchscreen/sync', message='Square cache synchronized.' if run.status == 'SUCCEEDED' else '', error=run.error_summary or '' if run.status != 'SUCCEEDED' else '')
