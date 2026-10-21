from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Store, WebSession


DEFAULT_RETURN_TO = '/v2/store-operations/daily-logs'
RETURN_PREFIX = '/v2/store-operations'


@dataclass(frozen=True)
class CurrentStore:
    id: int
    name: str


def safe_return_target(value: str | None) -> str:
    raw = str(value or '').strip()
    parsed = urlsplit(raw)
    if (
        not raw
        or parsed.scheme
        or parsed.netloc
        or not (parsed.path == RETURN_PREFIX or parsed.path.startswith(f'{RETURN_PREFIX}/'))
        or parsed.path.startswith('/v2/current-store')
    ):
        return DEFAULT_RETURN_TO
    return raw


def list_current_store_options(db: Session) -> list[CurrentStore]:
    rows = db.execute(
        select(Store.id, Store.name)
        .where(Store.active.is_(True))
        .order_by(Store.name.asc(), Store.id.asc())
    ).all()
    return [CurrentStore(id=int(row.id), name=str(row.name)) for row in rows]


def active_store(db: Session, store_id: int | None) -> CurrentStore | None:
    if not store_id:
        return None
    row = db.execute(
        select(Store.id, Store.name).where(Store.id == store_id, Store.active.is_(True))
    ).one_or_none()
    return CurrentStore(id=int(row.id), name=str(row.name)) if row else None


def current_store_for_request(request: Request, db: Session) -> CurrentStore | None:
    if getattr(request.state, 'current_store_checked_at', None) is None:
        return None
    return active_store(db, getattr(request.state, 'current_store_id', None))


def set_current_store(
    db: Session,
    *,
    web_session_id: int,
    store_id: int,
    now: datetime | None = None,
) -> CurrentStore | None:
    store = active_store(db, store_id)
    if store is None:
        return None
    session = db.get(WebSession, web_session_id)
    if session is None or session.revoked_at is not None:
        return None
    checked_at = now or datetime.now(tz=timezone.utc)
    session.current_store_id = store.id
    session.current_store_checked_at = checked_at
    return store
