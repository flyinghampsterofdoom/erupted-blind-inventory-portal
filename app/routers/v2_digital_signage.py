from __future__ import annotations

from datetime import date, datetime, time, timezone
from urllib.parse import quote

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import Principal, Role, require_capability
from app.config import settings
from app.db import get_db
from app.dependencies import get_client_ip
from app.models import (
    DigitalSignageAdvertisementGroup,
    DigitalSignageDisplay,
    DigitalSignageGroupDisplay,
    DigitalSignageGroupItem,
    DigitalSignageMediaAsset,
)
from app.security.csrf import verify_csrf
from app.services.digital_signage_media_service import (
    MediaValidationError,
    archive_media,
    store_or_reuse_image,
    validate_image_upload,
)
from app.services.digital_signage_service import (
    GroupInput,
    SignageValidationError,
    add_group_item,
    archive_display,
    create_display,
    duplicate_group,
    reorder_group_items,
    reset_display_password,
    save_group,
    update_display,
    update_group_item,
)
from app.services.digital_signage_storage import StorageUnavailable, configured_signage_storage
from app.services.access_control_service import principal_has_permission
from app.v2.feature_exposure import require_v2_feature
from app.v2.navigation import build_navigation


router = APIRouter(prefix='/v2/digital-signage', tags=['v2-digital-signage'])
feature_access = require_v2_feature('digital_signage_v2')
view_access = require_capability('digital_signage.view', Role.ADMIN, Role.MANAGER)
display_manage_access = require_capability('digital_signage.manage_displays', Role.ADMIN, Role.MANAGER)
credential_access = require_capability('digital_signage.reset_display_credentials', Role.ADMIN, Role.MANAGER)
group_manage_access = require_capability('digital_signage.manage_groups', Role.ADMIN, Role.MANAGER)
media_manage_access = require_capability('digital_signage.manage_media', Role.ADMIN, Role.MANAGER)


class Page:
    def __init__(self, label: str, description: str):
        self.slug = 'digital-signage'
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
    query = []
    if message:
        query.append(f'message={quote(message)}')
    if error:
        query.append(f'error={quote(error)}')
    return RedirectResponse(path + (('?' + '&'.join(query)) if query else ''), status_code=303)


def _parse_date(value: object, *, required: bool = False) -> date | None:
    clean = str(value or '').strip()
    if not clean:
        if required:
            raise SignageValidationError('Start date is required.')
        return None
    try:
        return date.fromisoformat(clean)
    except ValueError as exc:
        raise SignageValidationError('Enter a valid date.') from exc


def _parse_time(value: object) -> time | None:
    clean = str(value or '').strip()
    if not clean:
        return None
    try:
        return time.fromisoformat(clean)
    except ValueError as exc:
        raise SignageValidationError('Enter a valid daily time.') from exc


def _group_input(form) -> GroupInput:
    display_ids = []
    for raw in form.getlist('display_id'):
        try:
            display_ids.append(int(raw))
        except ValueError:
            raise SignageValidationError('A selected TV display is invalid.')
    try:
        priority = int(str(form.get('priority', '0')))
    except ValueError as exc:
        raise SignageValidationError('Priority must be a whole number.') from exc
    return GroupInput(
        name=str(form.get('name', '')), start_date=_parse_date(form.get('start_date'), required=True),
        end_date=None if form.get('runs_forever') else _parse_date(form.get('end_date')),
        daily_start_time=_parse_time(form.get('daily_start_time')),
        daily_end_time=_parse_time(form.get('daily_end_time')),
        priority=priority, is_enabled=bool(form.get('is_enabled')),
        display_ids=tuple(sorted(set(display_ids))),
    )


@router.get('')
def signage_root(
    _feature: Principal = Depends(feature_access), principal: Principal = Depends(view_access),
):
    return RedirectResponse('/v2/digital-signage/groups', status_code=303)


@router.get('/displays')
def displays_page(
    request: Request, _feature: Principal = Depends(feature_access), principal: Principal = Depends(view_access),
    db: Session = Depends(get_db),
):
    return _render_displays(request, principal=principal, db=db)


def _render_displays(
    request: Request, *, principal: Principal, db: Session,
    message: str = '', error: str = '', revealed_password: str = '',
):
    displays = db.execute(select(DigitalSignageDisplay).where(
        DigitalSignageDisplay.archived_at.is_(None)
    ).order_by(DigitalSignageDisplay.name, DigitalSignageDisplay.id)).scalars().all()
    group_rows = db.execute(
        select(DigitalSignageGroupDisplay.display_id, DigitalSignageAdvertisementGroup.name)
        .join(DigitalSignageAdvertisementGroup, DigitalSignageAdvertisementGroup.id == DigitalSignageGroupDisplay.advertisement_group_id)
        .where(DigitalSignageAdvertisementGroup.archived_at.is_(None))
        .order_by(DigitalSignageAdvertisementGroup.name)
    ).all()
    groups_by_display: dict[int, list[str]] = {}
    for display_id, group_name in group_rows:
        groups_by_display.setdefault(display_id, []).append(str(group_name))
    response = request.app.state.templates.TemplateResponse('v2/digital_signage/displays.html', _context(
        request, principal, 'TV Displays', 'Create and manage independently authenticated television clients.',
        displays=displays, groups_by_display=groups_by_display,
        message=message or request.query_params.get('message', ''),
        error=error or request.query_params.get('error', ''), revealed_password=revealed_password,
    ))
    if revealed_password:
        response.headers['Cache-Control'] = 'no-store'
    return response


@router.post('/displays')
async def create_display_route(
    request: Request, _feature: Principal = Depends(feature_access), principal: Principal = Depends(display_manage_access),
    _csrf: None = Depends(verify_csrf), db: Session = Depends(get_db),
):
    form = await request.form()
    try:
        display, password = create_display(
            db, principal=principal, name=str(form.get('name', '')), username=str(form.get('username', '')),
            password=str(form.get('password', '')) or None, is_enabled=bool(form.get('is_enabled')),
            ip=get_client_ip(request),
        )
        db.commit()
    except (SignageValidationError, IntegrityError) as exc:
        db.rollback()
        message = str(exc) if isinstance(exc, SignageValidationError) else 'Display name, URL slug, and username must be unique.'
        return _back('/v2/digital-signage/displays', error=message)
    return _render_displays(
        request, principal=principal, db=db,
        message='TV display created. Copy the password now; it will not be shown again.',
        revealed_password=password,
    )


@router.post('/displays/{display_id}/edit')
async def edit_display_route(
    display_id: int, request: Request, _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(display_manage_access), _csrf: None = Depends(verify_csrf),
    db: Session = Depends(get_db),
):
    form = await request.form()
    try:
        update_display(
            db, display_id=display_id, principal=principal, name=str(form.get('name', '')),
            slug=str(form.get('slug', '')), username=str(form.get('username', '')),
            is_enabled=bool(form.get('is_enabled')), ip=get_client_ip(request),
        )
        db.commit()
    except (SignageValidationError, IntegrityError) as exc:
        db.rollback()
        message = str(exc) if isinstance(exc, SignageValidationError) else 'Display name, URL slug, and username must be unique.'
        return _back('/v2/digital-signage/displays', error=message)
    return _back('/v2/digital-signage/displays', message='TV display updated.')


@router.post('/displays/{display_id}/reset-password')
async def reset_password_route(
    display_id: int, request: Request, _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(credential_access), _csrf: None = Depends(verify_csrf),
    db: Session = Depends(get_db),
):
    form = await request.form()
    try:
        password = reset_display_password(
            db, display_id=display_id, principal=principal,
            password=str(form.get('password', '')) or None, ip=get_client_ip(request),
        )
        db.commit()
    except SignageValidationError as exc:
        db.rollback()
        return _back('/v2/digital-signage/displays', error=str(exc))
    return _render_displays(
        request, principal=principal, db=db,
        message='Password reset and active TV sessions revoked. Copy it now; it will not be shown again.',
        revealed_password=password,
    )


@router.post('/displays/{display_id}/archive')
def archive_display_route(
    display_id: int, request: Request, _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(display_manage_access), _csrf: None = Depends(verify_csrf),
    db: Session = Depends(get_db),
):
    try:
        archive_display(db, display_id=display_id, principal=principal, ip=get_client_ip(request))
        db.commit()
    except SignageValidationError as exc:
        db.rollback()
        return _back('/v2/digital-signage/displays', error=str(exc))
    return _back('/v2/digital-signage/displays', message='TV display archived and its sessions revoked.')


@router.get('/groups')
def groups_page(
    request: Request, _feature: Principal = Depends(feature_access), principal: Principal = Depends(view_access),
    db: Session = Depends(get_db),
):
    groups = db.execute(select(DigitalSignageAdvertisementGroup).where(
        DigitalSignageAdvertisementGroup.archived_at.is_(None)
    ).order_by(DigitalSignageAdvertisementGroup.priority.desc(), DigitalSignageAdvertisementGroup.name)).scalars().all()
    assignments = db.execute(
        select(DigitalSignageGroupDisplay.advertisement_group_id, DigitalSignageDisplay.name)
        .join(DigitalSignageDisplay, DigitalSignageDisplay.id == DigitalSignageGroupDisplay.display_id)
        .order_by(DigitalSignageDisplay.name)
    ).all()
    item_counts = dict(db.execute(select(
        DigitalSignageGroupItem.advertisement_group_id, func.count(DigitalSignageGroupItem.id)
    ).group_by(DigitalSignageGroupItem.advertisement_group_id)).all())
    permanent_groups = set(db.execute(select(DigitalSignageGroupItem.advertisement_group_id).where(
        DigitalSignageGroupItem.is_permanent.is_(True), DigitalSignageGroupItem.is_enabled.is_(True)
    )).scalars().all())
    names: dict[int, list[str]] = {}
    for group_id, display_name in assignments:
        names.setdefault(group_id, []).append(str(display_name))
    today = datetime.now(ZoneInfoSafe()).date()
    return request.app.state.templates.TemplateResponse('v2/digital_signage/groups.html', _context(
        request, principal, 'Advertisement Groups', 'Assign reusable scheduled media rotations to current active TVs.',
        groups=groups, assignments=names, item_counts=item_counts, permanent_groups=permanent_groups, today=today,
        message=request.query_params.get('message', ''), error=request.query_params.get('error', ''),
    ))


def ZoneInfoSafe():
    from zoneinfo import ZoneInfo
    from app.config import settings
    return ZoneInfo(settings.digital_signage_business_timezone)


@router.get('/groups/new')
@router.get('/groups/{group_id}/edit')
def group_editor(
    request: Request, group_id: int | None = None, _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(view_access), db: Session = Depends(get_db),
):
    group = db.get(DigitalSignageAdvertisementGroup, group_id) if group_id else None
    if group_id and (group is None or group.archived_at is not None):
        raise HTTPException(status_code=404)
    displays = db.execute(select(DigitalSignageDisplay).where(
        DigitalSignageDisplay.archived_at.is_(None)
    ).order_by(DigitalSignageDisplay.name)).scalars().all()
    selected_ids = set(db.execute(select(DigitalSignageGroupDisplay.display_id).where(
        DigitalSignageGroupDisplay.advertisement_group_id == group_id
    )).scalars()) if group_id else set()
    items = db.execute(
        select(DigitalSignageGroupItem, DigitalSignageMediaAsset)
        .join(DigitalSignageMediaAsset, DigitalSignageMediaAsset.id == DigitalSignageGroupItem.media_asset_id)
        .where(DigitalSignageGroupItem.advertisement_group_id == (group_id or -1))
        .order_by(DigitalSignageGroupItem.sort_order, DigitalSignageGroupItem.id)
    ).all()
    media = db.execute(select(DigitalSignageMediaAsset).where(
        DigitalSignageMediaAsset.archived_at.is_(None), DigitalSignageMediaAsset.media_type == 'IMAGE'
    ).order_by(DigitalSignageMediaAsset.created_at.desc())).scalars().all()
    return request.app.state.templates.TemplateResponse('v2/digital_signage/group_editor.html', _context(
        request, principal, 'Edit Advertisement Group' if group else 'Create Advertisement Group',
        'Choose current TVs first, then build and order the reusable media rotation.',
        group=group, displays=displays, selected_ids=selected_ids, items=items, media=media,
        today=datetime.now(ZoneInfoSafe()).date().isoformat(),
        max_upload_bytes=settings.digital_signage_max_upload_bytes,
        message=request.query_params.get('message', ''),
        error=request.query_params.get('error', ''),
    ))


@router.post('/groups/save')
@router.post('/groups/{group_id}/save')
async def save_group_route(
    request: Request, group_id: int | None = None, _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(group_manage_access), _csrf: None = Depends(verify_csrf),
    db: Session = Depends(get_db),
):
    try:
        form = await request.form()
        group = save_group(db, principal=principal, value=_group_input(form), ip=get_client_ip(request), group_id=group_id)
        db.commit()
    except (SignageValidationError, IntegrityError) as exc:
        db.rollback()
        message = str(exc) if isinstance(exc, SignageValidationError) else 'The group could not be saved because its values conflict.'
        return _back(f'/v2/digital-signage/groups/{group_id}/edit' if group_id else '/v2/digital-signage/groups/new', error=message)
    return _back(f'/v2/digital-signage/groups/{group.id}/edit', message='Advertisement group saved.')


@router.post('/groups/{group_id}/duplicate')
def duplicate_group_route(
    group_id: int, request: Request, _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(group_manage_access), _csrf: None = Depends(verify_csrf),
    db: Session = Depends(get_db),
):
    try:
        copied = duplicate_group(db, group_id=group_id, principal=principal, ip=get_client_ip(request))
        db.commit()
    except SignageValidationError as exc:
        db.rollback()
        return _back('/v2/digital-signage/groups', error=str(exc))
    return _back(f'/v2/digital-signage/groups/{copied.id}/edit', message='Group duplicated with reusable media references.')


@router.get('/groups/{group_id}/preview')
def group_preview(
    group_id: int, request: Request, _feature: Principal = Depends(feature_access),
    _principal: Principal = Depends(view_access), db: Session = Depends(get_db),
):
    group = db.get(DigitalSignageAdvertisementGroup, group_id)
    if group is None or group.archived_at is not None:
        raise HTTPException(status_code=404)
    rows = db.execute(
        select(DigitalSignageGroupItem, DigitalSignageMediaAsset)
        .join(DigitalSignageMediaAsset, DigitalSignageMediaAsset.id == DigitalSignageGroupItem.media_asset_id)
        .where(
            DigitalSignageGroupItem.advertisement_group_id == group_id,
            DigitalSignageGroupItem.is_enabled.is_(True),
            DigitalSignageMediaAsset.archived_at.is_(None),
        )
        .order_by(DigitalSignageGroupItem.sort_order, DigitalSignageGroupItem.created_at, DigitalSignageGroupItem.id)
    ).all()
    items = [{
        'url': f'/v2/digital-signage/media/{asset.public_token}/content',
        'duration_seconds': item.display_duration_seconds or 12,
        'permanent': item.is_permanent,
    } for item, asset in rows]
    response = request.app.state.templates.TemplateResponse(
        'v2/digital_signage/preview.html', {'request': request, 'group': group, 'items': items}
    )
    response.headers['Cache-Control'] = 'no-store'
    return response


@router.post('/groups/{group_id}/archive')
def archive_group_route(
    group_id: int, request: Request, _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(group_manage_access), _csrf: None = Depends(verify_csrf),
    db: Session = Depends(get_db),
):
    group = db.get(DigitalSignageAdvertisementGroup, group_id)
    if group is None or group.archived_at is not None:
        return _back('/v2/digital-signage/groups', error='Advertisement group was not found.')
    group.archived_at = datetime.now(tz=timezone.utc)
    group.archived_by_principal_id = principal.id
    group.is_enabled = False
    from app.v2.audit import V2AuditEvent, write_v2_audit_event
    write_v2_audit_event(db, event=V2AuditEvent(
        actor_principal_id=principal.id, action='GROUP_ARCHIVED', domain='DIGITAL_SIGNAGE',
        entity_type='advertisement_group', entity_id=group.id,
    ), ip=get_client_ip(request))
    db.commit()
    return _back('/v2/digital-signage/groups', message='Advertisement group archived.')


@router.post('/groups/{group_id}/items')
async def add_item_route(
    group_id: int, request: Request, _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(group_manage_access), _csrf: None = Depends(verify_csrf),
    db: Session = Depends(get_db),
):
    form = await request.form()
    try:
        media_id = int(str(form.get('media_asset_id', '')))
        permanent = bool(form.get('is_permanent'))
        duration = None if permanent else int(str(form.get('duration_seconds', '12')))
        add_group_item(
            db, group_id=group_id, media_asset_id=media_id, duration_seconds=duration,
            is_permanent=permanent, principal=principal, ip=get_client_ip(request),
        )
        db.commit()
    except (ValueError, SignageValidationError, IntegrityError) as exc:
        db.rollback()
        return _back(f'/v2/digital-signage/groups/{group_id}/edit', error=str(exc) or 'Invalid group item.')
    return _back(f'/v2/digital-signage/groups/{group_id}/edit', message='Media added to the rotation.')


@router.post('/groups/{group_id}/items/{item_id}/remove')
def remove_item_route(
    group_id: int, item_id: int, request: Request, _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(group_manage_access), _csrf: None = Depends(verify_csrf), db: Session = Depends(get_db),
):
    item = db.execute(select(DigitalSignageGroupItem).where(
        DigitalSignageGroupItem.id == item_id, DigitalSignageGroupItem.advertisement_group_id == group_id
    )).scalar_one_or_none()
    if item is None:
        return _back(f'/v2/digital-signage/groups/{group_id}/edit', error='Group item was not found.')
    db.delete(item)
    db.flush()
    remaining = db.execute(select(DigitalSignageGroupItem).where(
        DigitalSignageGroupItem.advertisement_group_id == group_id
    ).order_by(DigitalSignageGroupItem.sort_order, DigitalSignageGroupItem.id)).scalars().all()
    for index, row in enumerate(remaining):
        row.sort_order = -(index + 1)
    db.flush()
    for index, row in enumerate(remaining):
        row.sort_order = index
    from app.v2.audit import V2AuditEvent, write_v2_audit_event
    write_v2_audit_event(db, event=V2AuditEvent(
        actor_principal_id=principal.id, action='GROUP_ITEM_REMOVED', domain='DIGITAL_SIGNAGE',
        entity_type='group_item', entity_id=item_id, metadata={'group_id': group_id},
    ), ip=get_client_ip(request))
    db.commit()
    return _back(f'/v2/digital-signage/groups/{group_id}/edit', message='Item removed.')


@router.post('/groups/{group_id}/items/{item_id}/edit')
async def edit_item_route(
    group_id: int, item_id: int, request: Request, _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(group_manage_access), _csrf: None = Depends(verify_csrf), db: Session = Depends(get_db),
):
    form = await request.form()
    permanent = bool(form.get('is_permanent'))
    try:
        duration = None if permanent else int(str(form.get('duration_seconds', '')))
        update_group_item(
            db, group_id=group_id, item_id=item_id, duration_seconds=duration,
            is_permanent=permanent, is_enabled=bool(form.get('is_enabled')),
            principal=principal, ip=get_client_ip(request),
        )
        db.commit()
    except (ValueError, SignageValidationError, IntegrityError) as exc:
        db.rollback()
        return _back(f'/v2/digital-signage/groups/{group_id}/edit', error=str(exc) or 'Invalid group item.')
    return _back(f'/v2/digital-signage/groups/{group_id}/edit', message='Item playback settings updated.')


@router.post('/groups/{group_id}/items/reorder')
async def reorder_items_route(
    group_id: int, request: Request, _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(group_manage_access), _csrf: None = Depends(verify_csrf), db: Session = Depends(get_db),
):
    form = await request.form()
    try:
        ids = [int(value) for value in str(form.get('item_order', '')).split(',') if value.strip()]
        reorder_group_items(db, group_id=group_id, ordered_ids=ids, principal=principal, ip=get_client_ip(request))
        db.commit()
    except (ValueError, SignageValidationError) as exc:
        db.rollback()
        return _back(f'/v2/digital-signage/groups/{group_id}/edit', error=str(exc))
    return _back(f'/v2/digital-signage/groups/{group_id}/edit', message='Item order saved.')


@router.get('/media')
def media_page(
    request: Request, _feature: Principal = Depends(feature_access), principal: Principal = Depends(view_access),
    db: Session = Depends(get_db),
):
    rows = db.execute(select(DigitalSignageMediaAsset).where(
        DigitalSignageMediaAsset.archived_at.is_(None)
    ).order_by(DigitalSignageMediaAsset.created_at.desc())).scalars().all()
    references = dict(db.execute(select(
        DigitalSignageGroupItem.media_asset_id, func.count(DigitalSignageGroupItem.id)
    ).group_by(DigitalSignageGroupItem.media_asset_id)).all())
    reference_rows = db.execute(
        select(DigitalSignageGroupItem.media_asset_id, DigitalSignageAdvertisementGroup.name)
        .join(DigitalSignageAdvertisementGroup, DigitalSignageAdvertisementGroup.id == DigitalSignageGroupItem.advertisement_group_id)
        .where(DigitalSignageAdvertisementGroup.archived_at.is_(None))
        .order_by(DigitalSignageAdvertisementGroup.name)
    ).all()
    reference_names: dict[int, list[str]] = {}
    for media_asset_id, group_name in reference_rows:
        names = reference_names.setdefault(media_asset_id, [])
        if str(group_name) not in names:
            names.append(str(group_name))
    return request.app.state.templates.TemplateResponse('v2/digital_signage/media.html', _context(
        request, principal, 'Media Library', 'Upload each validated image once and reuse it across advertisement groups.',
        assets=rows, references=references, reference_names=reference_names,
        max_upload_bytes=settings.digital_signage_max_upload_bytes,
        message=request.query_params.get('message', ''), error=request.query_params.get('error', ''),
    ))


@router.post('/media/upload')
async def upload_media_route(
    request: Request, _feature: Principal = Depends(feature_access), principal: Principal = Depends(media_manage_access),
    _csrf: None = Depends(verify_csrf), db: Session = Depends(get_db),
):
    form = await request.form()
    upload = form.get('media_file')
    try:
        group_id = int(str(form.get('group_id', ''))) if form.get('group_id') else None
    except ValueError:
        group_id = None
    back_path = f'/v2/digital-signage/groups/{group_id}/edit' if group_id else '/v2/digital-signage/media'
    if upload is None or not hasattr(upload, 'read'):
        return _back(back_path, error='Choose an image to upload.')
    try:
        if group_id and not principal_has_permission(
            db, principal=principal, permission_key='digital_signage.manage_groups',
            fallback_allowed=principal.role in {Role.ADMIN, Role.MANAGER},
        ):
            raise HTTPException(status_code=403)
        from app.config import settings
        content = await upload.read(settings.digital_signage_max_upload_bytes + 1)
        image = validate_image_upload(
            filename=str(getattr(upload, 'filename', '')), browser_content_type=str(getattr(upload, 'content_type', '') or ''),
            content=content,
        )
        asset, reused = store_or_reuse_image(
            db, principal=principal, image=image, storage=configured_signage_storage(), ip=get_client_ip(request),
        )
        if group_id:
            permanent = bool(form.get('is_permanent'))
            try:
                duration = None if permanent else int(str(form.get('duration_seconds', '12')))
            except ValueError as exc:
                raise SignageValidationError('Display duration must be a whole number of seconds.') from exc
            add_group_item(
                db, group_id=group_id, media_asset_id=asset.id, duration_seconds=duration,
                is_permanent=permanent, principal=principal, ip=get_client_ip(request),
            )
        db.commit()
    except (MediaValidationError, SignageValidationError, StorageUnavailable) as exc:
        db.rollback()
        return _back(back_path, error=str(exc))
    except (BotoCoreError, ClientError):
        db.rollback()
        return _back(back_path, error='Private media storage failed. Nothing was saved; try again after storage is available.')
    except IntegrityError:
        db.rollback()
        return _back(back_path, error='The image is reusable, but it could not be linked because the requested group item conflicts.')
    note = 'Existing media reused; no duplicate was stored.' if reused else 'Image uploaded and validated.'
    if not image.approximately_widescreen:
        note += ' Warning: this image is not approximately 16:9 and will be fitted without cropping.'
    if group_id:
        note = ('Existing media reused and added to the rotation.' if reused else 'Image uploaded and added to the rotation.') + (
            ' Warning: this image is not approximately 16:9 and will be fitted without cropping.'
            if not image.approximately_widescreen else ''
        )
    return _back(back_path, message=note)


@router.post('/media/{asset_id}/archive')
def archive_media_route(
    asset_id: int, request: Request, _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(media_manage_access), _csrf: None = Depends(verify_csrf), db: Session = Depends(get_db),
):
    try:
        archive_media(db, asset_id=asset_id, principal=principal, ip=get_client_ip(request))
        db.commit()
    except MediaValidationError as exc:
        db.rollback()
        return _back('/v2/digital-signage/media', error=str(exc))
    return _back('/v2/digital-signage/media', message='Unused media archived. The private storage object was retained.')


@router.get('/media/{public_token}/content')
def admin_media_content(
    public_token: str, request: Request, _feature: Principal = Depends(feature_access),
    _principal: Principal = Depends(view_access), db: Session = Depends(get_db),
):
    asset = db.execute(select(DigitalSignageMediaAsset).where(
        DigitalSignageMediaAsset.public_token == public_token, DigitalSignageMediaAsset.archived_at.is_(None)
    )).scalar_one_or_none()
    if asset is None:
        raise HTTPException(status_code=404)
    etag = f'"{asset.content_hash}"'
    headers = {'ETag': etag, 'Cache-Control': 'private, max-age=31536000, immutable', 'X-Content-Type-Options': 'nosniff'}
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
