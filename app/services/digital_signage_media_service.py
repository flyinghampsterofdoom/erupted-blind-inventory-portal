from __future__ import annotations

import hashlib
import io
import secrets
import warnings
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.auth import Principal
from app.config import settings
from app.models import DigitalSignageGroupItem, DigitalSignageMediaAsset
from app.services.digital_signage_storage import SignageObjectStorage
from app.v2.audit import V2AuditEvent, write_v2_audit_event


SUPPORTED_FORMATS = {
    'JPEG': ('image/jpeg', {'.jpg', '.jpeg'}),
    'PNG': ('image/png', {'.png'}),
    'WEBP': ('image/webp', {'.webp'}),
}
MAX_IMAGE_DIMENSION = 8192
MAX_IMAGE_PIXELS = 40_000_000


class MediaValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ValidatedImage:
    content: bytes
    content_hash: str
    content_type: str
    width: int
    height: int
    original_filename: str

    @property
    def approximately_widescreen(self) -> bool:
        return abs((self.width / self.height) - (16 / 9)) <= 0.08


def validate_image_upload(*, filename: str, browser_content_type: str, content: bytes) -> ValidatedImage:
    clean_name = str(filename or '').replace('\\', '/').split('/')[-1].strip()
    suffix = f'.{clean_name.rsplit(".", 1)[1].lower()}' if '.' in clean_name else ''
    if not clean_name or clean_name in {'.', '..'} or '\x00' in clean_name:
        raise MediaValidationError('Choose a file with a valid filename.')
    if suffix in {'.zip', '.html', '.htm'} or browser_content_type in {'application/zip', 'text/html'}:
        raise MediaValidationError('HTML animation packages are not enabled yet.')
    if len(content) > settings.digital_signage_max_upload_bytes:
        raise MediaValidationError(
            f'The image exceeds the {settings.digital_signage_max_upload_bytes // (1024 * 1024)} MB upload limit.'
        )
    if not content:
        raise MediaValidationError('The uploaded file is empty.')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as image:
                image.verify()
            with Image.open(io.BytesIO(content)) as image:
                image_format = str(image.format or '').upper()
                width, height = image.size
                if width * height > MAX_IMAGE_PIXELS:
                    raise MediaValidationError(f'Images may contain at most {MAX_IMAGE_PIXELS:,} pixels.')
                image.load()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        if isinstance(exc, MediaValidationError):
            raise
        raise MediaValidationError('The uploaded file is not a valid supported image.') from exc
    if image_format not in SUPPORTED_FORMATS:
        raise MediaValidationError('Only JPEG, PNG, and WebP images are supported.')
    content_type, extensions = SUPPORTED_FORMATS[image_format]
    if suffix not in extensions:
        raise MediaValidationError('The filename extension does not match the image content.')
    if browser_content_type and browser_content_type != content_type:
        raise MediaValidationError('The reported file type does not match the image content.')
    if width <= 0 or height <= 0 or width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION:
        raise MediaValidationError(f'Image dimensions must be between 1 and {MAX_IMAGE_DIMENSION} pixels.')
    return ValidatedImage(
        content=content,
        content_hash=hashlib.sha256(content).hexdigest(),
        content_type=content_type,
        width=width,
        height=height,
        original_filename=clean_name[:255],
    )


def store_or_reuse_image(
    db: Session,
    *,
    principal: Principal,
    image: ValidatedImage,
    storage: SignageObjectStorage,
    ip: str | None,
) -> tuple[DigitalSignageMediaAsset, bool]:
    # Serialize uploads for the same content hash on PostgreSQL. Without this,
    # concurrent requests can both miss the initial lookup, upload the same
    # object, and then race on the database uniqueness constraint.
    if db.get_bind().dialect.name == 'postgresql':
        lock_key = int.from_bytes(bytes.fromhex(image.content_hash[:16]), byteorder='big', signed=True)
        db.execute(text('SELECT pg_advisory_xact_lock(:lock_key)'), {'lock_key': lock_key})
    existing = db.execute(
        select(DigitalSignageMediaAsset).where(
            DigitalSignageMediaAsset.media_type == 'IMAGE',
            DigitalSignageMediaAsset.content_hash == image.content_hash,
        )
    ).scalar_one_or_none()
    if existing is not None:
        restored = existing.archived_at is not None
        if restored:
            existing.archived_at = None
            existing.archived_by_principal_id = None
        write_v2_audit_event(
            db,
            event=V2AuditEvent(
                actor_principal_id=principal.id,
                action='MEDIA_RESTORED' if restored else 'MEDIA_REUSED',
                domain='DIGITAL_SIGNAGE',
                entity_type='media_asset',
                entity_id=existing.id,
                metadata={'content_hash': image.content_hash, 'original_filename': image.original_filename},
            ),
            ip=ip,
        )
        return existing, True

    storage_key = f'digital-signage/images/{image.content_hash}'
    storage.put(storage_key, image.content, content_type=image.content_type)
    asset = DigitalSignageMediaAsset(
        media_type='IMAGE',
        storage_key=storage_key,
        public_token=secrets.token_urlsafe(32),
        original_filename=image.original_filename,
        content_type=image.content_type,
        size_bytes=len(image.content),
        content_hash=image.content_hash,
        width=image.width,
        height=image.height,
        metadata_json={'approximately_16_9': image.approximately_widescreen},
        created_by_principal_id=principal.id,
    )
    db.add(asset)
    db.flush()
    write_v2_audit_event(
        db,
        event=V2AuditEvent(
            actor_principal_id=principal.id,
            action='MEDIA_UPLOADED',
            domain='DIGITAL_SIGNAGE',
            entity_type='media_asset',
            entity_id=asset.id,
            after={
                'media_type': 'IMAGE', 'content_hash': image.content_hash,
                'content_type': image.content_type, 'size_bytes': len(image.content),
                'width': image.width, 'height': image.height,
            },
        ),
        ip=ip,
    )
    return asset, False


def archive_media(db: Session, *, asset_id: int, principal: Principal, ip: str | None) -> DigitalSignageMediaAsset:
    asset = db.get(DigitalSignageMediaAsset, asset_id)
    if asset is None or asset.archived_at is not None:
        raise MediaValidationError('Media asset was not found.')
    reference_count = db.scalar(
        select(func.count(DigitalSignageGroupItem.id)).where(DigitalSignageGroupItem.media_asset_id == asset.id)
    ) or 0
    if reference_count:
        raise MediaValidationError('This media is still referenced by an advertisement group and cannot be archived.')
    from datetime import datetime, timezone
    asset.archived_at = datetime.now(tz=timezone.utc)
    asset.archived_by_principal_id = principal.id
    write_v2_audit_event(
        db,
        event=V2AuditEvent(
            actor_principal_id=principal.id, action='MEDIA_ARCHIVED', domain='DIGITAL_SIGNAGE',
            entity_type='media_asset', entity_id=asset.id,
        ),
        ip=ip,
    )
    return asset
