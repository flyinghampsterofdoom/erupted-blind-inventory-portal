from __future__ import annotations

import secrets
from urllib.parse import quote
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request
from starlette.concurrency import run_in_threadpool
from pathlib import Path
import os
import re
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select

from app.auth import Principal, Role
from app.config import settings
from app.performance import timed
from app.db import SessionLocal
from app.models import Principal as PrincipalModel
from app.models import WebSession, Employee
from app.services.access_control_service import (
    effective_permission_flags,
)
from app.services.v2_square_data_service import square_data_status


AUTH_EXEMPT_PATHS = {'/login', '/robots.txt', '/session-status', '/forgot-password', '/password'}


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


def load_session_from_token(db, token: str | None, *, renew: bool = True) -> tuple[WebSession, Principal] | None:
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

    employee = db.execute(select(Employee).where(Employee.principal_id == principal.id)).scalar_one_or_none()
    db.info['authenticated_employee_id'] = employee.id if employee and employee.active else None
    account_active = principal.active and (employee is None or employee.active)

    if renew and account_active:
        web_session.last_seen_at = now
        web_session.expires_at = _session_expiry()
    role = Role(principal.role.value if hasattr(principal.role, 'value') else principal.role)
    return web_session, Principal(
        id=principal.id,
        username=principal.username,
        role=role,
        store_id=principal.store_id,
        active=account_active,
    )


def load_principal_from_token(db, token: str | None) -> Principal | None:
    loaded = load_session_from_token(db, token)
    return loaded[1] if loaded else None


PUBLIC_ASSETS = frozenset(path.name for path in (Path(__file__).resolve().parents[1] / 'static' / 'v2').iterdir()
                          if path.is_file() and path.suffix in {'.js', '.css'})


def is_public_asset(request):
    prefix = '/v2-assets/'
    return (request.method in {'GET', 'HEAD'} and request.url.path.startswith(prefix)
            and request.url.path[len(prefix):] in PUBLIC_ASSETS)


def install_auth_session_middleware(app: FastAPI) -> None:
    @app.middleware('http')
    async def auth_session_middleware(request: Request, call_next):
        # This exact route authenticates its own fixed read-only observation.
        # Ignore browser cookies for every method; unsupported methods must never
        # renew a session either. This does not establish a business principal.
        if request.url.path == '/management/deployment/observation':
            return await call_next(request)
        # This one GET has its own pinned public-key verifier. Do not resolve or
        # renew a browser session, open a business connection, or grant a principal.
        if request.method == 'GET' and request.url.path == '/management/forecast-validation/runtime':
            return await call_next(request)
        if is_public_asset(request):
            response = await call_next(request)
            version = (os.getenv('RENDER_GIT_COMMIT') or os.getenv('GIT_COMMIT') or '')[:12]
            if (response.status_code in {200,304} and re.fullmatch(r'[0-9a-f]{12}', version)
                    and request.query_params.get('v') == version):
                response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
            return response
        is_autosave = request.headers.get('x-requested-with') == 'autosave'
        is_display_route = (
            request.url.path == '/display'
            or request.url.path.startswith('/display/')
            or request.url.path in {'/v2-assets/display.css', '/v2-assets/display.js'}
        )
        is_touchscreen_route = (
            request.url.path == '/touchscreen'
            or request.url.path.startswith('/touchscreen/')
            or request.url.path in {'/v2-assets/touchscreen.css', '/v2-assets/touchscreen.js'}
        )
        token = request.cookies.get(settings.session_cookie_name)
        @timed("auth_ms")
        def resolve():
            with SessionLocal() as db:
                loaded = load_session_from_token(db, token, renew=request.url.path != '/session-status')
                web_session, principal = loaded if loaded else (None, None)
                request.state.principal = principal
                request.state.web_session_id = web_session.id if web_session else None
                request.state.current_store_id = web_session.current_store_id if web_session else None
                request.state.current_store_checked_at = web_session.current_store_checked_at if web_session else None
                request.state.login_at = web_session.created_at if web_session else None
                request.state.session_expires_at = web_session.expires_at if web_session else None
                permission_flags = (
                    effective_permission_flags(db, principal=principal)
                    if principal is not None
                    else {}
                )
                request.state.permission_flags = permission_flags
                request.state.square_data_status = (
                    square_data_status(db)
                    if principal is not None and request.url.path.startswith('/v2')
                    else None
                )
                # Link context is presentation-only; load_session retains the existing
                # account/employee activation and session renewal rules.
                request.state.employee_id = db.info.get('authenticated_employee_id') if principal else None
                from app.v2.workspaces import primary_enabled
                request.state.v2_primary = primary_enabled(principal)
                request.state.permission_override_maps = db.info.get('permission_override_maps')
                db.commit()
            return web_session, principal

        web_session, principal = await run_in_threadpool(resolve)
        from app.security.manual_scheduling_session import acknowledged, set_acknowledgement
        request.state.manual_scheduling_acknowledged = acknowledged(request)

        exempt=AUTH_EXEMPT_PATHS | getattr(request.app.state,'additional_auth_exempt_paths',frozenset())
        if request.url.path not in exempt and not is_display_route and not is_touchscreen_route and request.state.principal is None:
            if is_autosave:
                return Response(status_code=401)
            target = request.url.path + ('?' + request.url.query if request.url.query else '')
            from app.v2.workspaces import local_get_target, primary_rollout_configured
            # Carry GET intent only. Validation and exposure are checked again
            # after authentication; never capture a body or an unsafe method.
            login = '/login'
            if request.method == 'GET' and primary_rollout_configured() and local_get_target(target):
                login += '?return_to=' + quote(target, safe='')
            return RedirectResponse(login, status_code=303)

        response = await call_next(request)
        if web_session is not None and request.state.principal is not None and token and request.url.path not in {'/login', '/logout', '/session-status'}:
            if request.state.manual_scheduling_acknowledged:
                set_acknowledgement(response, request)
            response.set_cookie(
                key=settings.session_cookie_name,
                value=token,
                httponly=True,
                secure=settings.session_cookie_secure,
                samesite=settings.session_cookie_samesite,
                max_age=settings.session_ttl_minutes * 60,
            )
        return response
