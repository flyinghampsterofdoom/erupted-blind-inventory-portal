from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import Principal
from app.models import DigitalSignageGroupItem, DigitalSignageMediaAsset, TouchscreenFlavorMedia
from app.services.digital_signage_media_service import (
    MediaValidationError,
    ValidatedImage,
    validate_image_upload,
)
from app.services.digital_signage_storage import SignageObjectStorage
from app.v2.audit import V2AuditEvent, write_v2_audit_event


def store_touchscreen_image(
    db: Session, *, principal: Principal, image: ValidatedImage, storage: SignageObjectStorage, ip: str | None
) -> DigitalSignageMediaAsset:
    import secrets
    existing = db.execute(select(DigitalSignageMediaAsset).where(
        DigitalSignageMediaAsset.media_type == 'IMAGE',
        DigitalSignageMediaAsset.content_hash == image.content_hash,
    )).scalar_one_or_none()
    if existing is not None:
        existing.archived_at = None
        existing.archived_by_principal_id = None
        return existing
    storage_key = f'touchscreen/images/{image.content_hash}'
    storage.put(storage_key, image.content, content_type=image.content_type)
    asset = DigitalSignageMediaAsset(
        media_type='IMAGE', storage_key=storage_key, public_token=secrets.token_urlsafe(32),
        original_filename=image.original_filename, content_type=image.content_type,
        size_bytes=len(image.content), content_hash=image.content_hash, width=image.width, height=image.height,
        metadata_json={'source': 'touchscreen'}, created_by_principal_id=principal.id,
    )
    db.add(asset); db.flush()
    write_v2_audit_event(db, event=V2AuditEvent(
        actor_principal_id=principal.id, action='IMAGE_UPLOADED', domain='TOUCHSCREEN',
        entity_type='media_asset', entity_id=asset.id,
        after={'content_type': image.content_type, 'size_bytes': len(image.content), 'width': image.width, 'height': image.height},
    ), ip=ip)
    return asset


def set_primary_flavor_image(
    db: Session, *, flavor_id: int, asset_id: int, alt_text: str, principal: Principal, ip: str | None
) -> TouchscreenFlavorMedia:
    existing = db.execute(select(TouchscreenFlavorMedia).where(
        TouchscreenFlavorMedia.touchscreen_flavor_id == flavor_id,
        TouchscreenFlavorMedia.role == 'PRIMARY',
    )).scalars().first()
    before = {'media_asset_id': existing.media_asset_id} if existing else None
    if existing is None:
        existing = TouchscreenFlavorMedia(
            touchscreen_flavor_id=flavor_id, media_asset_id=asset_id, role='PRIMARY', sort_order=0, alt_text=alt_text,
        )
        db.add(existing)
    else:
        existing.media_asset_id = asset_id
        existing.alt_text = alt_text
    db.flush()
    write_v2_audit_event(db, event=V2AuditEvent(
        actor_principal_id=principal.id, action='IMAGE_REPLACED' if before else 'IMAGE_ATTACHED', domain='TOUCHSCREEN',
        entity_type='flavor', entity_id=flavor_id, before=before, after={'media_asset_id': asset_id},
    ), ip=ip)
    return existing


def remove_primary_flavor_image(db: Session, *, flavor_id: int, principal: Principal, ip: str | None) -> None:
    links = db.execute(select(TouchscreenFlavorMedia).where(
        TouchscreenFlavorMedia.touchscreen_flavor_id == flavor_id,
        TouchscreenFlavorMedia.role == 'PRIMARY',
    )).scalars().all()
    for link in links:
        db.delete(link)
    write_v2_audit_event(db, event=V2AuditEvent(
        actor_principal_id=principal.id, action='IMAGE_REMOVED', domain='TOUCHSCREEN',
        entity_type='flavor', entity_id=flavor_id,
    ), ip=ip)


def assert_media_unreferenced(db: Session, asset_id: int) -> None:
    signage = db.scalar(select(func.count(DigitalSignageGroupItem.id)).where(DigitalSignageGroupItem.media_asset_id == asset_id)) or 0
    touchscreen = db.scalar(select(func.count(TouchscreenFlavorMedia.id)).where(TouchscreenFlavorMedia.media_asset_id == asset_id)) or 0
    if signage or touchscreen:
        raise MediaValidationError('This media is still referenced by Digital Signage or Touchscreen and cannot be archived.')


__all__ = ['validate_image_upload', 'store_touchscreen_image', 'set_primary_flavor_image', 'remove_primary_flavor_image', 'assert_media_unreferenced']
