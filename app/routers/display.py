from __future__ import annotations

from datetime import datetime, timezone

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.dependencies import get_client_ip
from app.models import DigitalSignageDisplay, DigitalSignageDisplaySession, DigitalSignageMediaAsset
from app.security.csrf import verify_csrf
from app.security.display_sessions import (
    DISPLAY_SESSION_COOKIE,
    create_display_session,
    load_display_session,
)
from app.security.passwords import verify_password
from app.services.audit_service import log_audit, log_auth_event
from app.services.digital_signage_service import SignageValidationError, effective_playlist
from app.services.digital_signage_storage import StorageUnavailable, configured_signage_storage


router = APIRouter(prefix='/display', tags=['digital-signage-display'])
DISPLAY_CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


def _loaded_display(request: Request, db: Session):
    return load_display_session(db, request.cookies.get(DISPLAY_SESSION_COOKIE))


@router.get('/{slug}')
def display_page(slug: str, request: Request, db: Session = Depends(get_db)):
    display = db.execute(select(DigitalSignageDisplay).where(
        DigitalSignageDisplay.slug == slug, DigitalSignageDisplay.archived_at.is_(None)
    )).scalar_one_or_none()
    if display is None:
        raise HTTPException(status_code=404)
    loaded = _loaded_display(request, db)
    if loaded is not None and loaded[1].id == display.id:
        db.commit()
        response = request.app.state.templates.TemplateResponse(
            'display/player.html', {'request': request, 'display': display}
        )
        response.headers['Content-Security-Policy'] = DISPLAY_CSP
        response.headers['Cache-Control'] = 'no-store'
        return response
    if loaded is not None and loaded[1].id != display.id:
        db.commit()
        response = request.app.state.templates.TemplateResponse(
            'display/login.html',
            {'request': request, 'display': display, 'error': 'This browser is signed in to a different TV display. Sign out there before switching.'},
            status_code=403,
        )
        response.headers['Content-Security-Policy'] = DISPLAY_CSP
        response.headers['Cache-Control'] = 'no-store'
        return response
    db.commit()
    response = request.app.state.templates.TemplateResponse(
        'display/login.html', {'request': request, 'display': display, 'error': None}
    )
    response.headers['Content-Security-Policy'] = DISPLAY_CSP
    response.headers['Cache-Control'] = 'no-store'
    return response


@router.post('/{slug}/login')
async def display_login(slug: str, request: Request, _csrf: None = Depends(verify_csrf), db: Session = Depends(get_db)):
    form = await request.form()
    username = str(form.get('username', '')).strip()
    password = str(form.get('password', ''))
    ip = get_client_ip(request)
    user_agent = request.headers.get('user-agent')
    display = db.execute(select(DigitalSignageDisplay).where(
        DigitalSignageDisplay.slug == slug, DigitalSignageDisplay.archived_at.is_(None)
    )).scalar_one_or_none()
    valid = bool(
        display is not None and display.is_enabled and display.username.lower() == username.lower()
        and verify_password(password, display.password_hash)
    )
    log_auth_event(
        db, attempted_username=username, success=valid, ip=ip, user_agent=user_agent,
        principal_id=None, failure_reason=None if valid else 'DISPLAY_LOGIN_FAILED',
    )
    if not valid:
        db.commit()
        response = request.app.state.templates.TemplateResponse(
            'display/login.html',
            {'request': request, 'display': display or type('Missing', (), {'name': slug, 'slug': slug})(), 'error': 'Invalid username or password.'},
            status_code=401,
        )
        response.headers['Content-Security-Policy'] = DISPLAY_CSP
        response.headers['Cache-Control'] = 'no-store'
        return response
    token = create_display_session(db, display_id=display.id, ip=ip, user_agent=user_agent)
    log_audit(
        db, actor_principal_id=None, action='DISPLAY_AUTH_LOGIN', session_id=None, ip=ip,
        metadata={'display_id': display.id},
    )
    db.commit()
    response = RedirectResponse(f'/display/{display.slug}', status_code=303)
    response.set_cookie(
        key=DISPLAY_SESSION_COOKIE, value=token, httponly=True, secure=settings.session_cookie_secure,
        samesite='lax', path='/display', max_age=settings.digital_signage_display_session_ttl_days * 86400,
    )
    return response


@router.post('/logout')
def display_logout(request: Request, _csrf: None = Depends(verify_csrf), db: Session = Depends(get_db)):
    loaded = _loaded_display(request, db)
    slug = loaded[1].slug if loaded else ''
    if loaded:
        loaded[0].revoked_at = datetime.now(tz=timezone.utc)
    db.commit()
    response = RedirectResponse(f'/display/{slug}' if slug else '/login', status_code=303)
    response.delete_cookie(DISPLAY_SESSION_COOKIE, path='/display')
    return response


@router.get('/api/playlist')
def playlist(request: Request, db: Session = Depends(get_db)):
    loaded = _loaded_display(request, db)
    if loaded is None:
        return JSONResponse({'detail': 'Display authentication required.'}, status_code=401)
    try:
        payload = effective_playlist(db, display=loaded[1])
    except SignageValidationError as exc:
        return JSONResponse({'detail': str(exc)}, status_code=403)
    db.commit()
    etag = f'"{payload["playlist_version"]}"'
    headers = {'ETag': etag, 'Cache-Control': 'private, no-cache', 'X-Content-Type-Options': 'nosniff'}
    if request.headers.get('if-none-match') == etag:
        return Response(status_code=304, headers=headers)
    return JSONResponse(payload, headers=headers)


@router.get('/media/{public_token}')
def display_media(public_token: str, request: Request, db: Session = Depends(get_db)):
    loaded = _loaded_display(request, db)
    if loaded is None:
        raise HTTPException(status_code=401)
    # Media is only available when it is in this display's current effective playlist.
    playlist = effective_playlist(db, display=loaded[1])
    allowed_urls = {item['media_url'] for item in playlist['items']}
    if f'/display/media/{public_token}' not in allowed_urls:
        raise HTTPException(status_code=404)
    asset = db.execute(select(DigitalSignageMediaAsset).where(
        DigitalSignageMediaAsset.public_token == public_token,
        DigitalSignageMediaAsset.archived_at.is_(None),
    )).scalar_one_or_none()
    if asset is None:
        raise HTTPException(status_code=404)
    db.commit()
    etag = f'"{asset.content_hash}"'
    headers = {
        'ETag': etag, 'Cache-Control': 'private, max-age=31536000, immutable',
        'X-Content-Type-Options': 'nosniff',
    }
    if request.headers.get('if-none-match') == etag:
        return Response(status_code=304, headers=headers)
    try:
        content = configured_signage_storage().get(asset.storage_key)
    except StorageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (BotoCoreError, ClientError) as exc:
        raise HTTPException(status_code=503, detail='Private media storage is temporarily unavailable.') from exc
    headers['Content-Length'] = str(len(content))
    return Response(content=content, media_type=asset.content_type, headers=headers)
