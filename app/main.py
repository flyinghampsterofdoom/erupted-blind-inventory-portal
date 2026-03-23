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
_template_response_impl = app.state.templates.TemplateResponse


def _template_response_compat(*args, **kwargs):
    # Render currently runs a Starlette/Jinja version that expects request-first.
    # Keep backward compatibility with existing TemplateResponse(name, context, ...) calls.
    if args and isinstance(args[0], str):
        name = args[0]
        context = args[1] if len(args) > 1 else kwargs.get('context', {})
        status_code = args[2] if len(args) > 2 else kwargs.get('status_code', 200)
        headers = args[3] if len(args) > 3 else kwargs.get('headers')
        media_type = args[4] if len(args) > 4 else kwargs.get('media_type')
        background = args[5] if len(args) > 5 else kwargs.get('background')
        request = kwargs.get('request') or context.get('request')
        if request is None:
            raise ValueError('context must include a "request" key')
        return _template_response_impl(
            request,
            name,
            context,
            status_code=status_code,
            headers=headers,
            media_type=media_type,
            background=background,
        )
    return _template_response_impl(*args, **kwargs)


app.state.templates.TemplateResponse = _template_response_compat


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
