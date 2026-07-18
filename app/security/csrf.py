from __future__ import annotations

import hmac
import secrets

from fastapi import HTTPException, Request, status

from app.config import settings


CSRF_COOKIE_NAME = 'csrf_token'
CSRF_FORM_FIELD = 'csrf_token'
CSRF_HEADER_NAME = 'X-CSRF-Token'


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

    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    request_token = request.headers.get(CSRF_HEADER_NAME)
    if request_token is None:
        form = await request.form()
        request_token = form.get(CSRF_FORM_FIELD)
    if (
        not request_token
        or not cookie_token
        or not hmac.compare_digest(str(request_token), str(cookie_token))
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Invalid CSRF token')
