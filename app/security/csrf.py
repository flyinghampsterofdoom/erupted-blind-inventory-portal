from __future__ import annotations

import secrets

from fastapi import HTTPException, Request, status

from app.config import settings


CSRF_COOKIE_NAME = 'csrf_token'
CSRF_FORM_FIELD = 'csrf_token'


def install_csrf_cookie_middleware(app) -> None:
    @app.middleware('http')
    async def csrf_cookie_middleware(request: Request, call_next):
        csrf_token = request.cookies.get(CSRF_COOKIE_NAME)
        if not csrf_token:
            csrf_token = secrets.token_urlsafe(24)
        request.state.csrf_token = csrf_token

        response = await call_next(request)
        if request.cookies.get(CSRF_COOKIE_NAME) != csrf_token:
            response.set_cookie(
                key=CSRF_COOKIE_NAME,
                value=csrf_token,
                httponly=False,
                secure=settings.session_cookie_secure,
                samesite='lax',
            )
        return response


async def verify_csrf(request: Request) -> None:
    if request.method in {'GET', 'HEAD', 'OPTIONS'}:
        return

    form = await request.form()
    form_token = form.get(CSRF_FORM_FIELD)
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    if not form_token or not cookie_token or form_token != cookie_token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Invalid CSRF token')
