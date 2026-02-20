from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.dependencies import get_client_ip, get_templates
from app.models import Principal as PrincipalModel
from app.security.csrf import verify_csrf
from app.security.passwords import verify_password
from app.security.sessions import create_web_session, revoke_web_session
from app.services.audit_service import log_audit, log_auth_event

router = APIRouter(tags=['auth'])


@router.get('/login')
def login_page(request: Request, templates: Jinja2Templates = Depends(get_templates)):
    return templates.TemplateResponse('login.html', {'request': request, 'error': None})


@router.post('/login')
async def login_submit(
    request: Request,
    db: Session = Depends(get_db),
    _: None = Depends(verify_csrf),
):
    form = await request.form()
    username = str(form.get('username', '')).strip()
    password = str(form.get('password', ''))
    ip = get_client_ip(request)
    user_agent = request.headers.get('user-agent')

    principal = db.execute(select(PrincipalModel).where(PrincipalModel.username == username)).scalar_one_or_none()
    if not principal:
        log_auth_event(
            db,
            attempted_username=username,
            success=False,
            failure_reason='UNKNOWN_USERNAME',
            principal_id=None,
            ip=ip,
            user_agent=user_agent,
        )
        db.commit()
        return request.app.state.templates.TemplateResponse(
            'login.html',
            {'request': request, 'error': 'Invalid username or password'},
            status_code=401,
        )

    if not principal.active:
        log_auth_event(
            db,
            attempted_username=username,
            success=False,
            failure_reason='INACTIVE_PRINCIPAL',
            principal_id=principal.id,
            ip=ip,
            user_agent=user_agent,
        )
        db.commit()
        return request.app.state.templates.TemplateResponse(
            'login.html',
            {'request': request, 'error': 'Invalid username or password'},
            status_code=401,
        )

    if not verify_password(password, principal.password_hash):
        log_auth_event(
            db,
            attempted_username=username,
            success=False,
            failure_reason='BAD_PASSWORD',
            principal_id=principal.id,
            ip=ip,
            user_agent=user_agent,
        )
        db.commit()
        return request.app.state.templates.TemplateResponse(
            'login.html',
            {'request': request, 'error': 'Invalid username or password'},
            status_code=401,
        )

    token = create_web_session(db, principal.id, ip=ip, user_agent=user_agent)
    log_auth_event(
        db,
        attempted_username=username,
        success=True,
        failure_reason=None,
        principal_id=principal.id,
        ip=ip,
        user_agent=user_agent,
    )
    log_audit(
        db,
        actor_principal_id=principal.id,
        action='AUTH_LOGIN',
        session_id=None,
        ip=ip,
        metadata={'username': username},
    )
    db.commit()

    response = RedirectResponse('/', status_code=303)
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_samesite,
        max_age=settings.session_ttl_minutes * 60,
    )
    return response


@router.post('/logout')
def logout(request: Request, db: Session = Depends(get_db), _: None = Depends(verify_csrf)):
    principal = getattr(request.state, 'principal', None)
    token = request.cookies.get(settings.session_cookie_name)
    if token:
        revoke_web_session(db, token)

    ip = get_client_ip(request)
    log_audit(
        db,
        actor_principal_id=principal.id if principal else None,
        action='AUTH_LOGOUT',
        session_id=None,
        ip=ip,
        metadata={},
    )
    db.commit()

    response = RedirectResponse('/login', status_code=303)
    response.delete_cookie(settings.session_cookie_name)
    return response
