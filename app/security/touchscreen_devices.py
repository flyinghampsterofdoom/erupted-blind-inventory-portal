from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import Principal
from app.models import TouchscreenDevice
from app.v2.audit import V2AuditEvent, write_v2_audit_event


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def hash_device_token(token: str) -> str:
    return hashlib.sha256(str(token).encode('utf-8')).hexdigest()


def create_touchscreen_device(
    db: Session, *, store_id: int, name: str, orientation: str, principal: Principal, ip: str | None
) -> tuple[TouchscreenDevice, str]:
    clean_name = str(name or '').strip()
    clean_orientation = str(orientation or 'AUTO').strip().upper()
    if not clean_name:
        raise ValueError('Device name is required.')
    if clean_orientation not in {'AUTO', 'LANDSCAPE', 'PORTRAIT'}:
        raise ValueError('Choose a valid orientation.')
    token = secrets.token_urlsafe(48)
    device = TouchscreenDevice(
        store_id=store_id,
        name=clean_name,
        token_hash=hash_device_token(token),
        status='ACTIVE',
        orientation=clean_orientation,
        created_by_principal_id=principal.id,
    )
    db.add(device)
    db.flush()
    write_v2_audit_event(db, event=V2AuditEvent(
        actor_principal_id=principal.id, action='DEVICE_CREATED', domain='TOUCHSCREEN',
        entity_type='device', entity_id=device.id, store_ids=(store_id,),
        after={'name': clean_name, 'orientation': clean_orientation, 'status': 'ACTIVE'},
    ), ip=ip)
    return device, token


def revoke_touchscreen_device(db: Session, *, device_id: int, principal: Principal, ip: str | None) -> None:
    device = db.get(TouchscreenDevice, device_id)
    if device is None:
        raise ValueError('Touchscreen device was not found.')
    if device.status == 'REVOKED':
        return
    device.status = 'REVOKED'
    device.revoked_at = _now()
    device.updated_at = _now()
    write_v2_audit_event(db, event=V2AuditEvent(
        actor_principal_id=principal.id, action='DEVICE_REVOKED', domain='TOUCHSCREEN',
        entity_type='device', entity_id=device.id, store_ids=(device.store_id,),
        before={'status': 'ACTIVE'}, after={'status': 'REVOKED'},
    ), ip=ip)


def load_touchscreen_device(db: Session, token: str) -> TouchscreenDevice:
    device = db.execute(select(TouchscreenDevice).where(
        TouchscreenDevice.token_hash == hash_device_token(token)
    )).scalar_one_or_none()
    if device is None or device.status != 'ACTIVE' or device.revoked_at is not None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Invalid touchscreen device.')
    device.last_seen_at = _now()
    return device
