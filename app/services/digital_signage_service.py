from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from app.auth import Principal
from app.config import settings
from app.models import (
    DigitalSignageAdvertisementGroup,
    DigitalSignageDisplay,
    DigitalSignageDisplaySession,
    DigitalSignageGroupDisplay,
    DigitalSignageGroupItem,
    DigitalSignageMediaAsset,
)
from app.security.passwords import hash_password
from app.v2.audit import V2AuditEvent, write_v2_audit_event


DISPLAY_LOCK_KEY = 0x45525550544544
MIN_DURATION_SECONDS = 5
MAX_DURATION_SECONDS = 300
SLUG_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9-]{0,63}$')


class SignageValidationError(ValueError):
    pass


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def generated_display_password() -> str:
    return secrets.token_urlsafe(18)


def slug_from_name(name: str) -> str:
    slug = re.sub(r'[^A-Za-z0-9]+', '-', str(name or '').strip()).strip('-').upper()
    if not SLUG_PATTERN.fullmatch(slug):
        raise SignageValidationError('The display name cannot produce a valid URL slug.')
    return slug


def _lock_display_capacity(db: Session) -> None:
    if db.bind is not None and db.bind.dialect.name == 'postgresql':
        db.execute(text('SELECT pg_advisory_xact_lock(:lock_key)'), {'lock_key': DISPLAY_LOCK_KEY})


def _active_display_count(db: Session) -> int:
    return int(db.scalar(select(func.count(DigitalSignageDisplay.id)).where(
        DigitalSignageDisplay.is_enabled.is_(True),
        DigitalSignageDisplay.archived_at.is_(None),
    )) or 0)


def _validate_capacity(db: Session) -> None:
    if _active_display_count(db) >= settings.digital_signage_max_active_displays:
        raise SignageValidationError(
            f'Only {settings.digital_signage_max_active_displays} active TV displays are allowed.'
        )


def create_display(
    db: Session,
    *,
    principal: Principal,
    name: str,
    username: str,
    password: str | None,
    is_enabled: bool,
    ip: str | None,
) -> tuple[DigitalSignageDisplay, str]:
    clean_name = str(name or '').strip()
    clean_username = str(username or '').strip() or clean_name
    if not clean_name or len(clean_name) > 100:
        raise SignageValidationError('Display name is required and must be 100 characters or fewer.')
    if not clean_username or len(clean_username) > 100:
        raise SignageValidationError('Display username is required and must be 100 characters or fewer.')
    slug = slug_from_name(clean_name)
    raw_password = str(password or '') or generated_display_password()
    if len(raw_password) < 12:
        raise SignageValidationError('Display passwords must be at least 12 characters.')
    _lock_display_capacity(db)
    if is_enabled:
        _validate_capacity(db)
    display = DigitalSignageDisplay(
        name=clean_name,
        slug=slug,
        username=clean_username,
        password_hash=hash_password(raw_password),
        is_enabled=is_enabled,
        created_by_principal_id=principal.id,
        updated_by_principal_id=principal.id,
    )
    db.add(display)
    db.flush()
    write_v2_audit_event(
        db,
        event=V2AuditEvent(
            actor_principal_id=principal.id, action='DISPLAY_CREATED', domain='DIGITAL_SIGNAGE',
            entity_type='display', entity_id=display.id,
            after={'name': clean_name, 'slug': slug, 'username': clean_username, 'is_enabled': is_enabled},
        ),
        ip=ip,
    )
    return display, raw_password


def update_display(
    db: Session,
    *,
    display_id: int,
    principal: Principal,
    name: str,
    slug: str,
    username: str,
    is_enabled: bool,
    ip: str | None,
) -> DigitalSignageDisplay:
    display = db.execute(select(DigitalSignageDisplay).where(DigitalSignageDisplay.id == display_id).with_for_update()).scalar_one_or_none()
    if display is None or display.archived_at is not None:
        raise SignageValidationError('TV display was not found.')
    clean_name, clean_slug, clean_username = str(name).strip(), str(slug).strip(), str(username).strip()
    if not clean_name or not clean_username or not SLUG_PATTERN.fullmatch(clean_slug):
        raise SignageValidationError('Name, username, and a valid URL slug are required.')
    before = {'name': display.name, 'slug': display.slug, 'username': display.username, 'is_enabled': display.is_enabled}
    if is_enabled and not display.is_enabled:
        _lock_display_capacity(db)
        _validate_capacity(db)
    display.name = clean_name
    display.slug = clean_slug
    display.username = clean_username
    display.is_enabled = is_enabled
    display.updated_by_principal_id = principal.id
    display.updated_at = _now()
    if not is_enabled:
        revoke_display_sessions(db, display.id)
    write_v2_audit_event(
        db,
        event=V2AuditEvent(
            actor_principal_id=principal.id, action='DISPLAY_UPDATED', domain='DIGITAL_SIGNAGE',
            entity_type='display', entity_id=display.id, before=before,
            after={'name': clean_name, 'slug': clean_slug, 'username': clean_username, 'is_enabled': is_enabled},
        ),
        ip=ip,
    )
    return display


def revoke_display_sessions(db: Session, display_id: int) -> None:
    now = _now()
    sessions = db.execute(select(DigitalSignageDisplaySession).where(
        DigitalSignageDisplaySession.display_id == display_id,
        DigitalSignageDisplaySession.revoked_at.is_(None),
    )).scalars().all()
    for session in sessions:
        session.revoked_at = now


def reset_display_password(
    db: Session, *, display_id: int, principal: Principal, password: str | None, ip: str | None
) -> str:
    display = db.execute(select(DigitalSignageDisplay).where(DigitalSignageDisplay.id == display_id).with_for_update()).scalar_one_or_none()
    if display is None or display.archived_at is not None:
        raise SignageValidationError('TV display was not found.')
    raw_password = str(password or '') or generated_display_password()
    if len(raw_password) < 12:
        raise SignageValidationError('Display passwords must be at least 12 characters.')
    display.password_hash = hash_password(raw_password)
    display.password_rotated_at = _now()
    display.updated_at = _now()
    display.updated_by_principal_id = principal.id
    revoke_display_sessions(db, display.id)
    write_v2_audit_event(
        db,
        event=V2AuditEvent(
            actor_principal_id=principal.id, action='DISPLAY_PASSWORD_RESET', domain='DIGITAL_SIGNAGE',
            entity_type='display', entity_id=display.id, metadata={'sessions_revoked': True},
        ),
        ip=ip,
    )
    return raw_password


def archive_display(
    db: Session, *, display_id: int, principal: Principal, ip: str | None
) -> DigitalSignageDisplay:
    display = db.execute(select(DigitalSignageDisplay).where(
        DigitalSignageDisplay.id == display_id
    ).with_for_update()).scalar_one_or_none()
    if display is None or display.archived_at is not None:
        raise SignageValidationError('TV display was not found.')
    display.is_enabled = False
    display.archived_at = _now()
    display.archived_by_principal_id = principal.id
    display.updated_at = _now()
    display.updated_by_principal_id = principal.id
    revoke_display_sessions(db, display.id)
    write_v2_audit_event(
        db,
        event=V2AuditEvent(
            actor_principal_id=principal.id, action='DISPLAY_ARCHIVED', domain='DIGITAL_SIGNAGE',
            entity_type='display', entity_id=display.id, after={'is_enabled': False, 'archived': True},
        ), ip=ip,
    )
    return display


@dataclass(frozen=True)
class GroupInput:
    name: str
    start_date: date
    end_date: date | None
    daily_start_time: time | None
    daily_end_time: time | None
    priority: int
    is_enabled: bool
    display_ids: tuple[int, ...]


def validate_group_input(value: GroupInput) -> None:
    if not value.name.strip() or len(value.name.strip()) > 150:
        raise SignageValidationError('Group name is required and must be 150 characters or fewer.')
    if value.end_date is not None and value.end_date < value.start_date:
        raise SignageValidationError('End date cannot be before start date.')
    if (value.daily_start_time is None) != (value.daily_end_time is None):
        raise SignageValidationError('Daily start and end times must both be supplied or both omitted.')
    if value.daily_start_time is not None and value.daily_end_time <= value.daily_start_time:
        raise SignageValidationError(
            'Daily end time must be later than start time. Overnight windows are not supported yet; use date scheduling instead.'
        )


def save_group(
    db: Session,
    *,
    principal: Principal,
    value: GroupInput,
    ip: str | None,
    group_id: int | None = None,
) -> DigitalSignageAdvertisementGroup:
    validate_group_input(value)
    valid_ids = set(db.execute(select(DigitalSignageDisplay.id).where(
        DigitalSignageDisplay.id.in_(value.display_ids or (-1,)),
        DigitalSignageDisplay.archived_at.is_(None),
    )).scalars())
    if valid_ids != set(value.display_ids):
        raise SignageValidationError('One or more selected TV displays are unavailable.')
    group = db.execute(select(DigitalSignageAdvertisementGroup).where(
        DigitalSignageAdvertisementGroup.id == group_id
    ).with_for_update()).scalar_one_or_none() if group_id else None
    if group_id and (group is None or group.archived_at is not None):
        raise SignageValidationError('Advertisement group was not found.')
    action = 'GROUP_UPDATED' if group else 'GROUP_CREATED'
    before = None
    if group is None:
        group = DigitalSignageAdvertisementGroup(
            created_by_principal_id=principal.id, updated_by_principal_id=principal.id,
            name='', start_date=value.start_date,
        )
        db.add(group)
        db.flush()
    else:
        previous_display_ids = sorted(db.execute(select(DigitalSignageGroupDisplay.display_id).where(
            DigitalSignageGroupDisplay.advertisement_group_id == group.id
        )).scalars().all())
        before = {
            'name': group.name, 'is_enabled': group.is_enabled, 'start_date': str(group.start_date),
            'end_date': str(group.end_date) if group.end_date else None,
            'daily_start_time': str(group.daily_start_time) if group.daily_start_time else None,
            'daily_end_time': str(group.daily_end_time) if group.daily_end_time else None,
            'priority': group.priority, 'display_ids': previous_display_ids,
        }
    group.name = value.name.strip()
    group.start_date = value.start_date
    group.end_date = value.end_date
    group.daily_start_time = value.daily_start_time
    group.daily_end_time = value.daily_end_time
    group.priority = value.priority
    group.is_enabled = value.is_enabled
    group.updated_by_principal_id = principal.id
    group.updated_at = _now()
    db.execute(delete(DigitalSignageGroupDisplay).where(DigitalSignageGroupDisplay.advertisement_group_id == group.id))
    for display_id in sorted(valid_ids):
        db.add(DigitalSignageGroupDisplay(advertisement_group_id=group.id, display_id=display_id))
    write_v2_audit_event(
        db,
        event=V2AuditEvent(
            actor_principal_id=principal.id, action=action, domain='DIGITAL_SIGNAGE',
            entity_type='advertisement_group', entity_id=group.id, before=before,
            after={
                'name': group.name, 'is_enabled': group.is_enabled, 'start_date': str(group.start_date),
                'end_date': str(group.end_date) if group.end_date else None,
                'daily_start_time': str(group.daily_start_time) if group.daily_start_time else None,
                'daily_end_time': str(group.daily_end_time) if group.daily_end_time else None,
                'priority': group.priority,
                'display_ids': sorted(valid_ids),
            },
        ), ip=ip,
    )
    return group


def add_group_item(
    db: Session, *, group_id: int, media_asset_id: int, duration_seconds: int | None,
    is_permanent: bool, principal: Principal, ip: str | None,
) -> DigitalSignageGroupItem:
    group = db.get(DigitalSignageAdvertisementGroup, group_id)
    asset = db.get(DigitalSignageMediaAsset, media_asset_id)
    if group is None or group.archived_at is not None or asset is None or asset.archived_at is not None:
        raise SignageValidationError('The group or media asset is unavailable.')
    if asset.media_type != 'IMAGE':
        raise SignageValidationError('HTML animation packages are not enabled yet.')
    if is_permanent:
        existing = db.scalar(select(func.count(DigitalSignageGroupItem.id)).where(
            DigitalSignageGroupItem.advertisement_group_id == group_id,
            DigitalSignageGroupItem.is_permanent.is_(True),
            DigitalSignageGroupItem.is_enabled.is_(True),
        )) or 0
        if existing:
            raise SignageValidationError('Only one active permanent item is allowed in an advertisement group.')
        duration_seconds = None
    elif duration_seconds is None or not MIN_DURATION_SECONDS <= duration_seconds <= MAX_DURATION_SECONDS:
        raise SignageValidationError(
            f'Display duration must be between {MIN_DURATION_SECONDS} and {MAX_DURATION_SECONDS} seconds.'
        )
    max_order = db.scalar(select(func.max(DigitalSignageGroupItem.sort_order)).where(
        DigitalSignageGroupItem.advertisement_group_id == group_id
    ))
    next_order = int(max_order) + 1 if max_order is not None else 0
    item = DigitalSignageGroupItem(
        advertisement_group_id=group_id, media_asset_id=media_asset_id,
        display_duration_seconds=duration_seconds, is_permanent=is_permanent,
        sort_order=next_order, is_enabled=True,
    )
    db.add(item)
    db.flush()
    write_v2_audit_event(
        db,
        event=V2AuditEvent(
            actor_principal_id=principal.id, action='GROUP_ITEM_ADDED', domain='DIGITAL_SIGNAGE',
            entity_type='group_item', entity_id=item.id,
            after={'group_id': group_id, 'media_asset_id': media_asset_id, 'duration_seconds': duration_seconds, 'permanent': is_permanent},
        ), ip=ip,
    )
    return item


def update_group_item(
    db: Session, *, group_id: int, item_id: int, duration_seconds: int | None,
    is_permanent: bool, is_enabled: bool, principal: Principal, ip: str | None,
) -> DigitalSignageGroupItem:
    item = db.execute(select(DigitalSignageGroupItem).where(
        DigitalSignageGroupItem.id == item_id,
        DigitalSignageGroupItem.advertisement_group_id == group_id,
    ).with_for_update()).scalar_one_or_none()
    if item is None:
        raise SignageValidationError('Group item was not found.')
    if is_permanent and is_enabled:
        another = db.scalar(select(func.count(DigitalSignageGroupItem.id)).where(
            DigitalSignageGroupItem.advertisement_group_id == group_id,
            DigitalSignageGroupItem.id != item_id,
            DigitalSignageGroupItem.is_permanent.is_(True),
            DigitalSignageGroupItem.is_enabled.is_(True),
        )) or 0
        if another:
            raise SignageValidationError('Only one active permanent item is allowed in an advertisement group.')
        duration_seconds = None
    elif not is_permanent and (duration_seconds is None or not MIN_DURATION_SECONDS <= duration_seconds <= MAX_DURATION_SECONDS):
        raise SignageValidationError(
            f'Display duration must be between {MIN_DURATION_SECONDS} and {MAX_DURATION_SECONDS} seconds.'
        )
    elif is_permanent:
        duration_seconds = None
    before = {'duration_seconds': item.display_duration_seconds, 'permanent': item.is_permanent, 'is_enabled': item.is_enabled}
    item.display_duration_seconds = duration_seconds
    item.is_permanent = is_permanent
    item.is_enabled = is_enabled
    item.updated_at = _now()
    write_v2_audit_event(
        db,
        event=V2AuditEvent(
            actor_principal_id=principal.id, action='GROUP_ITEM_UPDATED', domain='DIGITAL_SIGNAGE',
            entity_type='group_item', entity_id=item.id, before=before,
            after={'duration_seconds': duration_seconds, 'permanent': is_permanent, 'is_enabled': is_enabled},
        ), ip=ip,
    )
    return item


def reorder_group_items(
    db: Session, *, group_id: int, ordered_ids: list[int], principal: Principal, ip: str | None
) -> None:
    rows = db.execute(select(DigitalSignageGroupItem).where(
        DigitalSignageGroupItem.advertisement_group_id == group_id
    ).with_for_update()).scalars().all()
    if set(ordered_ids) != {row.id for row in rows} or len(ordered_ids) != len(rows):
        raise SignageValidationError('Item order must include every group item exactly once.')
    # Temporary negative positions avoid the group/order unique constraint while swapping.
    for index, row in enumerate(rows):
        row.sort_order = -(index + 1)
    db.flush()
    by_id = {row.id: row for row in rows}
    for index, item_id in enumerate(ordered_ids):
        by_id[item_id].sort_order = index
        by_id[item_id].updated_at = _now()
    write_v2_audit_event(
        db,
        event=V2AuditEvent(
            actor_principal_id=principal.id, action='GROUP_ITEMS_REORDERED', domain='DIGITAL_SIGNAGE',
            entity_type='advertisement_group', entity_id=group_id, after={'item_ids': ordered_ids},
        ), ip=ip,
    )


def duplicate_group(db: Session, *, group_id: int, principal: Principal, ip: str | None) -> DigitalSignageAdvertisementGroup:
    source = db.get(DigitalSignageAdvertisementGroup, group_id)
    if source is None or source.archived_at is not None:
        raise SignageValidationError('Advertisement group was not found.')
    assignments = db.execute(select(DigitalSignageGroupDisplay.display_id).where(
        DigitalSignageGroupDisplay.advertisement_group_id == group_id
    )).scalars().all()
    copied = save_group(
        db, principal=principal, ip=ip,
        value=GroupInput(
            name=f'{source.name} Copy', start_date=source.start_date, end_date=source.end_date,
            daily_start_time=source.daily_start_time, daily_end_time=source.daily_end_time,
            priority=source.priority, is_enabled=False, display_ids=tuple(assignments),
        ),
    )
    items = db.execute(select(DigitalSignageGroupItem).where(
        DigitalSignageGroupItem.advertisement_group_id == group_id
    ).order_by(DigitalSignageGroupItem.sort_order, DigitalSignageGroupItem.id)).scalars().all()
    for item in items:
        db.add(DigitalSignageGroupItem(
            advertisement_group_id=copied.id, media_asset_id=item.media_asset_id,
            display_duration_seconds=item.display_duration_seconds, is_permanent=item.is_permanent,
            sort_order=item.sort_order, is_enabled=item.is_enabled,
        ))
    write_v2_audit_event(
        db,
        event=V2AuditEvent(
            actor_principal_id=principal.id, action='GROUP_DUPLICATED', domain='DIGITAL_SIGNAGE',
            entity_type='advertisement_group', entity_id=copied.id, metadata={'source_group_id': group_id},
        ), ip=ip,
    )
    return copied


def effective_playlist(db: Session, *, display: DigitalSignageDisplay, now: datetime | None = None) -> dict:
    if not display.is_enabled or display.archived_at is not None:
        raise SignageValidationError('This TV display is disabled.')
    business_now = (now or _now()).astimezone(ZoneInfo(settings.digital_signage_business_timezone))
    query = (
        select(DigitalSignageAdvertisementGroup, DigitalSignageGroupItem, DigitalSignageMediaAsset)
        .join(DigitalSignageGroupDisplay, DigitalSignageGroupDisplay.advertisement_group_id == DigitalSignageAdvertisementGroup.id)
        .join(DigitalSignageGroupItem, DigitalSignageGroupItem.advertisement_group_id == DigitalSignageAdvertisementGroup.id)
        .join(DigitalSignageMediaAsset, DigitalSignageMediaAsset.id == DigitalSignageGroupItem.media_asset_id)
        .where(
            DigitalSignageGroupDisplay.display_id == display.id,
            DigitalSignageAdvertisementGroup.is_enabled.is_(True),
            DigitalSignageAdvertisementGroup.archived_at.is_(None),
            DigitalSignageAdvertisementGroup.start_date <= business_now.date(),
            (DigitalSignageAdvertisementGroup.end_date.is_(None) | (DigitalSignageAdvertisementGroup.end_date >= business_now.date())),
            DigitalSignageGroupItem.is_enabled.is_(True),
            DigitalSignageMediaAsset.archived_at.is_(None),
        )
        .order_by(
            DigitalSignageAdvertisementGroup.priority.desc(),
            DigitalSignageAdvertisementGroup.created_at.asc(),
            DigitalSignageAdvertisementGroup.id.asc(),
            DigitalSignageGroupItem.sort_order.asc(),
            DigitalSignageGroupItem.created_at.asc(),
            DigitalSignageGroupItem.id.asc(),
        )
    )
    eligible = []
    for group, item, asset in db.execute(query).all():
        if group.daily_start_time is not None and not (
            group.daily_start_time <= business_now.time().replace(tzinfo=None) < group.daily_end_time
        ):
            continue
        eligible.append((group, item, asset))
    permanent = next((row for row in eligible if row[1].is_permanent), None)
    selected = [permanent] if permanent else eligible
    items = [
        {
            'item_id': item.id,
            'media_type': asset.media_type,
            'media_url': f'/display/media/{asset.public_token}',
            'duration_seconds': item.display_duration_seconds,
            'permanent': bool(item.is_permanent),
            'width': asset.width,
            'height': asset.height,
        }
        for group, item, asset in selected
    ]
    version_source = json.dumps(items, sort_keys=True, separators=(',', ':')).encode()
    return {
        'display': {'name': str(display.name), 'slug': str(display.slug)},
        'generated_at': business_now.isoformat(),
        'playlist_version': hashlib.sha256(version_source).hexdigest(),
        'refresh_after_seconds': 300,
        'mode': 'PERMANENT' if permanent else 'ROTATION',
        'items': items,
    }
