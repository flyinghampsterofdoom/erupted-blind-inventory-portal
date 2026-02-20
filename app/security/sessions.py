from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request
from sqlalchemy import select

from app.auth import Principal, Role
from app.config import settings
from app.db import SessionLocal
from app.models import Principal as PrincipalModel
from app.models import WebSession


AUTH_EXEMPT_PATHS = {'/login', '/robots.txt'}


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _session_expiry() -> datetime:
    return _now() + timedelta(minutes=settings.session_ttl_minutes)


def create_web_session(db, principal_id: int, ip: str | None, user_agent: str | None) -> str:
    token = secrets.token_urlsafe(48)
    web_session = WebSession(
        session_token=token,
        principal_id=principal_id,
        ip=ip,
        user_agent=user_agent,
        expires_at=_session_expiry(),
    )
    db.add(web_session)
    db.flush()
    return token


def revoke_web_session(db, token: str) -> None:
    session = db.execute(select(WebSession).where(WebSession.session_token == token)).scalar_one_or_none()
    if not session or session.revoked_at is not None:
        return
    session.revoked_at = _now()


def load_principal_from_token(db, token: str | None) -> Principal | None:
    if not token:
        return None

    row = db.execute(
        select(WebSession, PrincipalModel)
        .join(PrincipalModel, PrincipalModel.id == WebSession.principal_id)
        .where(WebSession.session_token == token)
    ).one_or_none()
    if not row:
        return None

    web_session, principal = row
    now = _now()
    if web_session.revoked_at is not None or web_session.expires_at <= now:
        return None

    web_session.last_seen_at = now
    web_session.expires_at = _session_expiry()
    role = Role(principal.role.value if hasattr(principal.role, 'value') else principal.role)
    return Principal(
        id=principal.id,
        username=principal.username,
        role=role,
        store_id=principal.store_id,
        active=principal.active,
    )


def install_auth_session_middleware(app: FastAPI) -> None:
    @app.middleware('http')
    async def auth_session_middleware(request: Request, call_next):
        token = request.cookies.get(settings.session_cookie_name)
        with SessionLocal() as db:
            principal = load_principal_from_token(db, token)
            request.state.principal = principal
            db.commit()

        if request.url.path not in AUTH_EXEMPT_PATHS and request.state.principal is None:
            from fastapi.responses import RedirectResponse

            return RedirectResponse('/login', status_code=303)

        response = await call_next(request)
        return response
