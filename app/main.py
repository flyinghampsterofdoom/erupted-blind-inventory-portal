import os
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from types import SimpleNamespace

from fastapi import FastAPI, Request, Depends
from sqlalchemy.orm import Session
from app.db import get_db
from app.config import settings
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.auth import Role, get_current_principal
from app.v2.entry import landing_destination, portal_home_destination
from app.v2.workspaces import primary_enabled, DESTINATIONS
from app.v2.navigation import build_navigation
from app.v2.employee_schedule_presentation import employee_week_json
from app.schema_contract import assert_supported_schema
from app.routers import (
    auth,
    integrations,
    password_access,
    display,
    management,
    purchasing,
    forecast_validation,
    forecast_product,
    deployment_verification,
    store,
    touchscreen,
    v2,
    v2_daily_store_logs,
    v2_digital_signage,
    v2_exchanges_returns,
    v2_funding_reports,
    v2_ordering,
    v2_order_payments,
    v2_reporting,
    v2_scheduling,
    v2_scheduling_business_hours,
    v2_employees,
    v2_touchscreen,
)
from app.security.csrf import install_csrf_cookie_middleware
from app.security.headers import install_security_headers
from app.security.sessions import install_auth_session_middleware
from app.v2.statuses import status_context
from app.v2.feature_exposure import FeatureExposure

from app.performance import timed, PerformanceMiddleware

app = FastAPI(title='Blind Inventory Portal')



TEMPLATE_DIR = Path(__file__).resolve().parent / 'templates'
app.state.templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
PORTAL_TIMEZONE = ZoneInfo('America/Los_Angeles')
_template_response_impl = app.state.templates.TemplateResponse


@timed("template_ms")
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


@app.exception_handler(StarletteHTTPException)
async def v2_http_exception_handler(request: Request, exc: StarletteHTTPException):
    # A browser can restore a login form after its session cookie has changed.
    # Reject that submission, but return a fresh form instead of a JSON dead end.
    if (
        exc.status_code == 403
        and exc.detail == 'Invalid CSRF token'
        and request.method == 'POST'
        and request.url.path == '/login'
        and 'text/html' in request.headers.get('accept', '').lower()
        and request.headers.get('content-type', '').split(';', 1)[0].strip().lower()
            == 'application/x-www-form-urlencoded'
        and request.headers.get('X-CSRF-Token') is None
    ):
        form = await request.form()
        return_to = str(form.get('return_to', ''))[:2048]
        return app.state.templates.TemplateResponse(
            'login.html',
            {'request': request,
             'error': 'Your sign-in page expired or changed in another tab. Please sign in again.',
             'return_to': return_to},
            status_code=403,
            headers={'Cache-Control': 'no-store, private', 'Pragma': 'no-cache', 'Expires': '0'},
        )

    if (
        exc.status_code != 403
        or not request.url.path.startswith('/v2')
        or exc.detail == 'Invalid CSRF token'
    ):
        return await http_exception_handler(request, exc)

    principal = getattr(request.state, 'principal', None)
    flags = getattr(request.state, 'permission_flags', {}) or {}
    safe_actions: list[dict[str, str]] = []
    if primary_enabled(principal):
        safe_actions.append({'label': 'Open your workspace', 'href': landing_destination(request)})
    elif principal is not None and principal.active:
        if flags.get('management.access', False):
            safe_actions.append({'label': 'Open V2 Overview', 'href': '/v2/overview'})
        elif flags.get('store.access', False) and FeatureExposure.from_settings().enabled(
            'daily_store_logs_v2', principal_id=principal.id
        ):
            safe_actions.append({'label': 'Open Store Operations', 'href': '/v2/store-operations'})
        if flags.get('management.access', False):
            safe_actions.append({'label': 'Return to V1', 'href': '/management/home'})
        elif flags.get('store.access', False) or principal.role == Role.STORE:
            safe_actions.append({'label': 'Return to V1', 'href': '/store/home'})

    return app.state.templates.TemplateResponse(
        'v2/access_denied.html',
        {'request': request, 'safe_actions': safe_actions},
        status_code=403,
    )


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



def _compatibility_page(request):
    entry = next((d for d in sorted(DESTINATIONS, key=lambda d: len(d.path), reverse=True)
                  if request.url.path == d.path or request.url.path.startswith(d.path + '/')), None)
    return SimpleNamespace(label=entry.label if entry else 'Operations',
        description='Continue your work in this workspace.', badge='Operations')


app.state.templates.env.globals['purchasing_v3_enabled'] = lambda: settings.purchasing_v3_phase2_enabled
app.state.templates.env.globals['primary_navigation'] = build_navigation
app.state.templates.env.globals['portal_home'] = portal_home_destination
app.state.templates.env.filters['employee_week_json'] = employee_week_json
app.state.templates.env.globals['compatibility_page'] = _compatibility_page
app.state.templates.env.globals['csrf_token'] = _csrf_token
app.state.templates.env.globals['format_portal_datetime'] = _format_portal_datetime
app.state.templates.env.globals['v2_status'] = status_context
from app.v2.scheduling_presentation import board_status, continuity_tone
app.state.templates.env.globals['schedule_board_status'] = board_status
app.state.templates.env.globals['schedule_continuity_tone'] = continuity_tone
app.state.templates.env.globals['asset_version'] = (
    os.getenv('RENDER_GIT_COMMIT') or os.getenv('GIT_COMMIT') or 'dev'
)[:12]
app.state.templates.env.finalize = _jinja_finalize

V2_STATIC_DIR = Path(__file__).resolve().parent / 'static' / 'v2'
from app.routers.flavor_explorer import ProductStaticFiles
app.mount('/v2-assets', ProductStaticFiles(directory=str(V2_STATIC_DIR)), name='v2-assets')

install_security_headers(app)
install_csrf_cookie_middleware(app)
install_auth_session_middleware(app)
app.add_middleware(PerformanceMiddleware)

@app.middleware('http')
async def login_response_cache_control(request: Request, call_next):
    response = await call_next(request)
    protected_explorer = request.url.path.startswith(('/explorer/', '/flavor-explorer/media/'))
    if request.url.path == '/login' or (protected_explorer and response.status_code in (301, 302, 303, 307, 308, 401, 403)):
        response.headers['Cache-Control'] = 'no-store, private'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response


@app.middleware('http')
async def scheduling_maintenance_guard(request: Request, call_next):
    if (settings.scheduling_read_only and request.method not in ('GET', 'HEAD', 'OPTIONS')
            and (request.url.path == '/v2/scheduling' or request.url.path.startswith('/v2/scheduling/'))):
        return PlainTextResponse('Scheduling is temporarily read-only for maintenance.', status_code=503,
                                 headers={'Retry-After': '300', 'Cache-Control': 'no-store'})
    return await call_next(request)



app.include_router(auth.router)
app.include_router(integrations.router)
app.include_router(password_access.router)
app.include_router(store.router)
app.include_router(management.router)
app.include_router(purchasing.router)
app.include_router(forecast_validation.router)
app.include_router(forecast_product.router)
app.include_router(deployment_verification.router)
app.include_router(v2.router)
app.include_router(v2_ordering.router)
app.include_router(v2_order_payments.router)
app.include_router(v2_exchanges_returns.router)
app.include_router(v2_funding_reports.router)
app.include_router(v2_reporting.router)
app.include_router(v2_daily_store_logs.router)
app.include_router(v2_scheduling.router)
app.include_router(v2_scheduling_business_hours.router)
app.include_router(v2_employees.router)
app.include_router(v2_digital_signage.router)
app.include_router(display.router)
app.include_router(v2_touchscreen.router)
app.include_router(touchscreen.router)
from app.routers.flavor_explorer import router as flavor_explorer_router
from app.services.flavor_explorer.portal_api import admin_router as flavor_admin_router, employee_router as flavor_staff_router
from app.services.flavor_explorer.photo_media import router as flavor_photo_router
from app.services.flavor_explorer.compatibility.api import router as compatibility_router
from app.services.flavor_explorer.portal import install_portal
install_portal(app)
for _product_router in (flavor_explorer_router,flavor_admin_router,flavor_staff_router,flavor_photo_router,compatibility_router):
    app.include_router(_product_router)


@app.on_event('startup')
def _verify_schema_revision() -> None:
    assert_supported_schema()
    from app.services.runtime_identity_service import capture_identity
    app.state.runtime_identity = capture_identity()
    from app.db import engine
    from app.services.flavor_explorer.production_runtime import install_runtime
    install_runtime(app,engine)
    if settings.daily_evidence_collector_enabled:
        from app.services.daily_evidence_runtime import bind
        bind(app)


@app.on_event('shutdown')
def _stop_product_explorer():
    runtime=getattr(app.state,'product_explorer',None)
    if runtime is not None:runtime.catalog.stop()


@app.get('/')
def root(request: Request, db: Session = Depends(get_db)):
    get_current_principal(request)
    return RedirectResponse(landing_destination(request, return_to=request.query_params.get('return_to'), db=db), status_code=303)


@app.get('/robots.txt', response_class=PlainTextResponse)
def robots_txt() -> str:
    return 'User-agent: *\nDisallow: /\n'


# Explicitly gated owner inspection; migrations never run at web startup.
if settings.purchasing_v4_production_submission_enabled and not settings.purchasing_v4_owner_inspection_enabled:
    raise RuntimeError('Production submission requires the explicitly bound owner workspace')
if settings.purchasing_v4_owner_inspection_enabled:
    from sqlalchemy.engine import make_url
    from app.purchasing_v4.production_target import OwnerInspectionTarget
    from app.purchasing_v4.integration_app import create_app as _owner_inspection_app, Capabilities
    from app.purchasing_v4.production_submission import ProductionSubmissionTarget
    _v4_target=ProductionSubmissionTarget if settings.purchasing_v4_production_submission_enabled else OwnerInspectionTarget
    _v4_url=make_url(settings.database_url_normalized)
    _owner_inspection_app(settings.database_url_normalized,source_namespace='erupted-retained-square-source',
        owner_principal_id=settings.purchasing_owner_principal_id,capabilities=Capabilities(workspace=True,
            generation=settings.purchasing_v4_owner_calculation_enabled,
            configuration=settings.purchasing_v4_owner_calculation_enabled,
            authorization=settings.purchasing_v4_production_submission_enabled),
        supplier_resolution_enabled=settings.purchasing_v4_supplier_resolution_enabled,
        target=_v4_target(settings.purchasing_v4_database_name,
            settings.purchasing_v4_database_host,settings.purchasing_owner_principal_id,_v4_url.username))
