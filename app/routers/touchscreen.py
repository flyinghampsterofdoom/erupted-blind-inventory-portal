from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import DigitalSignageMediaAsset, Store
from app.security.touchscreen_devices import load_touchscreen_device
from app.services.digital_signage_storage import StorageUnavailable, configured_signage_storage
from app.services.touchscreen_catalog_service import StaleTouchscreenCatalog, catalog_for_store, flavor_detail_for_store, require_fresh_cache


router = APIRouter(prefix='/touchscreen', tags=['touchscreen'])
DEVICE_COOKIE = 'erupted_touchscreen_device'
TOUCHSCREEN_CSP = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"


def _device_from_cookie(request: Request, db: Session):
    token = request.cookies.get(DEVICE_COOKIE)
    if not token: raise HTTPException(status_code=401, detail='Touchscreen device authentication required.')
    return load_touchscreen_device(db, token)


def _category_ids(request: Request, key: str) -> set[int]:
    values = request.query_params.getlist(key)
    return {int(value) for value in values if str(value).isdigit()}


@router.get('/api/session')
def session_api(request: Request, db: Session = Depends(get_db)):
    device = _device_from_cookie(request, db); store = db.get(Store, device.store_id); db.commit()
    return {'store': {'name': store.name if store else 'Assigned store'}, 'device': {'name': str(device.name)}, 'inactivity_minutes': settings.touchscreen_inactivity_minutes}


@router.get('/api/catalog')
def catalog_api(request: Request, db: Session = Depends(get_db)):
    device = _device_from_cookie(request, db)
    try:
        run = require_fresh_cache(db)
        results = catalog_for_store(db, store_id=device.store_id, format_filter=request.query_params.get('format', 'both'), broad_category_ids=_category_ids(request, 'broad_category_id'), fruit_category_ids=_category_ids(request, 'fruit_category_id'))
    except StaleTouchscreenCatalog as exc:
        db.commit(); return JSONResponse({'detail': str(exc), 'code': 'CATALOG_STALE'}, status_code=503)
    freshness = run.freshness_at
    if freshness.tzinfo is None:
        freshness = freshness.replace(tzinfo=timezone.utc)
    valid_until = freshness + timedelta(minutes=settings.touchscreen_cache_max_age_minutes)
    ttl_seconds = max(0, int((valid_until - datetime.now(tz=timezone.utc)).total_seconds()))
    db.commit()
    return JSONResponse(
        {'results': results, 'count': len(results)},
        headers={'X-Touchscreen-Catalog-Ttl': str(ttl_seconds), 'Cache-Control': 'private, no-cache'},
    )


@router.get('/api/flavors/{flavor_id}')
def flavor_api(flavor_id: int, request: Request, db: Session = Depends(get_db)):
    device = _device_from_cookie(request, db)
    try: detail = flavor_detail_for_store(db, store_id=device.store_id, flavor_id=flavor_id, format_filter=request.query_params.get('format', 'both'))
    except StaleTouchscreenCatalog as exc:
        db.commit(); return JSONResponse({'detail': str(exc), 'code': 'CATALOG_STALE'}, status_code=503)
    db.commit()
    if detail is None: raise HTTPException(status_code=404)
    return detail


@router.get('/media/{public_token}')
def media(public_token: str, request: Request, db: Session = Depends(get_db)):
    device = _device_from_cookie(request, db)
    try: catalog = catalog_for_store(db, store_id=device.store_id)
    except StaleTouchscreenCatalog: raise HTTPException(status_code=503, detail='Catalog unavailable.')
    allowed_urls = {row['image']['url'] for row in catalog if row.get('image')}
    if f'/touchscreen/media/{public_token}' not in allowed_urls: raise HTTPException(status_code=404)
    asset = db.execute(select(DigitalSignageMediaAsset).where(DigitalSignageMediaAsset.public_token == public_token, DigitalSignageMediaAsset.archived_at.is_(None))).scalar_one_or_none()
    db.commit()
    if asset is None: raise HTTPException(status_code=404)
    try: content = configured_signage_storage().get(asset.storage_key)
    except StorageUnavailable as exc: raise HTTPException(status_code=503, detail='Image temporarily unavailable.') from exc
    return Response(content=content, media_type=asset.content_type, headers={'Cache-Control': 'private, max-age=86400', 'ETag': f'"{asset.content_hash}"', 'X-Content-Type-Options': 'nosniff'})


@router.get('/service-worker.js')
def service_worker():
    return FileResponse('app/static/v2/touchscreen-sw.js', media_type='application/javascript', headers={'Service-Worker-Allowed': '/touchscreen/'})


@router.get('/{device_token}')
def touchscreen_page(device_token: str, request: Request, db: Session = Depends(get_db)):
    device = load_touchscreen_device(db, device_token); store = db.get(Store, device.store_id); db.commit()
    response = request.app.state.templates.TemplateResponse('touchscreen/app.html', {'request': request, 'store': store, 'device': device})
    response.set_cookie(DEVICE_COOKIE, device_token, httponly=True, secure=settings.session_cookie_secure, samesite='strict', path='/touchscreen')
    response.headers['Content-Security-Policy'] = TOUCHSCREEN_CSP
    response.headers['Cache-Control'] = 'no-store'
    return response
