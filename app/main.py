from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth import Role, get_current_principal
from app.routers import auth, management, store
from app.security.csrf import install_csrf_cookie_middleware
from app.security.headers import install_security_headers
from app.security.sessions import install_auth_session_middleware

app = FastAPI(title='Blind Inventory Portal')

TEMPLATE_DIR = Path(__file__).resolve().parent / 'templates'
app.state.templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
PORTAL_TIMEZONE = ZoneInfo('America/Los_Angeles')


def _format_portal_datetime(value: datetime) -> str:
    dt = value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(PORTAL_TIMEZONE).replace(microsecond=0)
    return dt.strftime('%Y-%m-%d %I:%M:%S %p %Z')


def _jinja_finalize(value):
    if isinstance(value, datetime):
        return _format_portal_datetime(value)
    return value


def _csrf_token(request: Request) -> str:
    return getattr(request.state, 'csrf_token', '')


app.state.templates.env.globals['csrf_token'] = _csrf_token
app.state.templates.env.globals['format_portal_datetime'] = _format_portal_datetime
app.state.templates.env.finalize = _jinja_finalize

install_security_headers(app)
install_csrf_cookie_middleware(app)
install_auth_session_middleware(app)

app.include_router(auth.router)
app.include_router(store.router)
app.include_router(management.router)


@app.get('/')
def root(request: Request):
    principal = get_current_principal(request)
    permission_flags = getattr(request.state, 'permission_flags', {}) or {}
    if permission_flags.get('management.access'):
        return RedirectResponse('/management/home', status_code=303)
    if permission_flags.get('store.access') or principal.role == Role.STORE:
        return RedirectResponse('/store/home', status_code=303)
    return RedirectResponse('/login', status_code=303)


@app.get('/robots.txt', response_class=PlainTextResponse)
def robots_txt() -> str:
    return 'User-agent: *\nDisallow: /\n'
