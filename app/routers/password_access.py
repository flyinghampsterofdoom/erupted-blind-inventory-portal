from fastapi import APIRouter, BackgroundTasks, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError
import logging
from app.db import get_db
from app.models import Principal
from app.security.csrf import verify_csrf
from app.services.password_reset_service import request_password_email, redeem, throttle
from fastapi.responses import RedirectResponse

router = APIRouter(tags=['employee-password'])
GENERIC = 'If an eligible account exists for that email address, password instructions have been sent.'
HEADERS = {'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'}


def process_request(engine, email):
    try:
        with Session(engine) as db:
            account = db.scalar(select(Principal).where(Principal.username == email))
            if account:
                request_password_email(db, principal_id=account.id)
    except (SQLAlchemyError, ValueError):
        # No exception detail: provider errors, SQL parameters, email addresses
        # and token material must not enter application logs.
        logging.getLogger(__name__).warning('Password email background request failed.')


@router.get('/forgot-password')
def forgot(request: Request):
    return request.app.state.templates.TemplateResponse('password_access.html', dict(request=request, mode='forgot'), headers=HEADERS)


@router.post('/forgot-password')
async def forgot_submit(request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db), _=Depends(verify_csrf)):
    form = await request.form()
    email = str(form.get('email', '')).strip().lower()[:254]
    # Ignore attacker-supplied forwarded headers. Direct peer limits can be
    # conservative behind a shared proxy; address + global limits remain useful.
    peer = request.client.host if request.client else 'unknown'
    allowed = throttle(db, 'forgot:global', limit=120, seconds=3600)
    if allowed:
        allowed = throttle(db, 'forgot:peer:' + peer, limit=30, seconds=3600)
    if allowed:
        allowed = throttle(db, 'forgot:address:' + email, limit=3, seconds=3600)
    db.commit()
    if allowed:
        background_tasks.add_task(process_request, db.get_bind(), email)
    return request.app.state.templates.TemplateResponse('password_access.html', dict(request=request, mode='forgot', message=GENERIC), headers=HEADERS)


@router.get('/password')
def password_page(request: Request):
    return request.app.state.templates.TemplateResponse('password_access.html', dict(request=request, mode='password'), headers=HEADERS)


@router.post('/password')
async def password_submit(request: Request, db: Session = Depends(get_db), _=Depends(verify_csrf)):
    form = await request.form()
    raw = str(form.get('token', ''))
    peer = request.client.host if request.client else 'unknown'
    allowed = throttle(db, 'redeem:peer:' + peer, limit=60, seconds=3600)
    db.commit()
    try:
        if not allowed:
            raise ValueError('Too many attempts. Please try again later.')
        redeem(db, raw, str(form.get('password', '')), str(form.get('confirmation', '')))
        db.commit()
        return RedirectResponse('/login?password_saved=1', status_code=303, headers=HEADERS)
    except ValueError as exc:
        db.rollback()
        return request.app.state.templates.TemplateResponse('password_access.html',
            dict(request=request, mode='password', error=str(exc), token=raw if len(raw) <= 128 else ''), status_code=400, headers=HEADERS)
