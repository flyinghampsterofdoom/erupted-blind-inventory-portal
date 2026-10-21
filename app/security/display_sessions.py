from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import DigitalSignageDisplay, DigitalSignageDisplaySession


DISPLAY_SESSION_COOKIE = 'erupted_display_session'


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_display_session(
    db: Session, *, display_id: int, ip: str | None, user_agent: str | None
) -> str:
    token = secrets.token_urlsafe(48)
    db.add(DigitalSignageDisplaySession(
        display_id=display_id, token_hash=_hash_token(token), ip=ip, user_agent=user_agent,
        expires_at=_now() + timedelta(days=settings.digital_signage_display_session_ttl_days),
    ))
    db.flush()
    return token


def load_display_session(db: Session, token: str | None) -> tuple[DigitalSignageDisplaySession, DigitalSignageDisplay] | None:
    if not token:
        return None
    row = db.execute(
        select(DigitalSignageDisplaySession, DigitalSignageDisplay)
        .join(DigitalSignageDisplay, DigitalSignageDisplay.id == DigitalSignageDisplaySession.display_id)
        .where(DigitalSignageDisplaySession.token_hash == _hash_token(token))
    ).one_or_none()
    if row is None:
        return None
    session, display = row
    if session.revoked_at is not None or session.expires_at <= _now() or not display.is_enabled or display.archived_at is not None:
        return None
    session.last_seen_at = _now()
    display.last_seen_at = _now()
    return session, display


def require_display(request: Request, db: Session) -> DigitalSignageDisplay:
    loaded = load_display_session(db, request.cookies.get(DISPLAY_SESSION_COOKIE))
    if loaded is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return loaded[1]
