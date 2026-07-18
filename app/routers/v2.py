from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import Principal, Role, require_capability
from app.db import get_db
from app.models import Store
from app.security.csrf import verify_csrf
from app.services.v2_daily_store_log_service import portal_today
from app.services.v2_store_operations_completion_service import completion_statuses
from app.v2.current_store import (
    current_store_for_request,
    list_current_store_options,
    safe_return_target,
    set_current_store,
)
from app.v2.feature_exposure import require_v2_feature
from app.v2.navigation import NavigationSection, build_navigation


router = APIRouter(prefix='/v2', tags=['v2'])
v2_access = require_capability('management.access', Role.ADMIN, Role.MANAGER, Role.LEAD)
v2_admin_access = require_capability('management.admin', Role.ADMIN, Role.MANAGER)
store_operations_access = require_capability('store.access', Role.STORE)
daily_logs_feature_access = require_v2_feature('daily_store_logs_v2')


@dataclass(frozen=True)
class V2Page:
    slug: str
    label: str
    description: str
    permission: str = 'management.access'
    route_path: str | None = None
    badge: str = 'Owner Preview'
    active_prefix: str | None = None

    @property
    def href(self) -> str:
        return self.route_path or f'/v2/{self.slug}'


V2_PAGES: tuple[V2Page, ...] = (
    V2Page('overview', 'Overview', 'Owner preview for operational signals and cross-store priorities.'),
    V2Page('inventory', 'Inventory', 'Inventory workflows will be introduced in a later milestone.'),
    V2Page('ordering', 'Ordering', 'Ordering workflows will be introduced in a later milestone.'),
    V2Page('store-operations', 'Store Operations', 'Daily store workflows will be introduced in a later milestone.'),
    V2Page('audits', 'Audits', 'Audit workflows will be introduced in a later milestone.'),
    V2Page(
        'customer-forms',
        'Customer & Forms',
        'Customer and form workflows will be introduced in a later milestone.',
    ),
    V2Page('reports', 'Reports', 'Reporting workflows will be introduced in a later milestone.'),
    V2Page('admin', 'Admin', 'Administration tools will be introduced in a later milestone.', 'management.admin'),
)
PAGE_BY_SLUG = {page.slug: page for page in V2_PAGES}


def _natural_store_operations_date(value: date) -> str:
    return value.strftime('%A, %B %d, %Y').replace(' 0', ' ')


def _visible_navigation(request: Request) -> list[NavigationSection]:
    return build_navigation(request)


def _current_store_page_context(
    request: Request,
    principal: Principal,
    *,
    db: Session,
    return_to: str,
    error: str = '',
    selected_store_id: int | None = None,
) -> dict:
    current_store = current_store_for_request(request, db)
    return {
        'request': request,
        'principal': principal,
        'page': V2Page(
            'current-store',
            'Current Store',
            'Choose where you are working today.',
            permission='store.access',
            route_path='/v2/current-store',
            badge='Owner Preview',
        ),
        'navigation': _visible_navigation(request),
        'stores': [],
        'selected_store_ids': [],
        'all_stores_selected': False,
        'store_scope_label': current_store.name if current_store else 'Not selected',
        'scope_locked': True,
        'scope_caption': 'Current Store',
        'current_store': current_store,
        'show_current_store_context': True,
        'current_store_options': list_current_store_options(db),
        'return_to': return_to,
        'error': error,
        'selected_store_id': selected_store_id,
    }


def _store_scope_context(request: Request, db: Session, principal: Principal) -> dict:
    query = select(Store.id, Store.name).where(Store.active.is_(True))
    if principal.role == Role.STORE:
        query = query.where(Store.id == principal.store_id)
    store_rows = db.execute(query.order_by(Store.name.asc(), Store.id.asc())).all()
    stores = [{'id': int(row.id), 'name': str(row.name)} for row in store_rows]
    allowed_ids = {store['id'] for store in stores}

    raw_values = request.query_params.getlist('store_id')
    all_stores = not raw_values or 'all' in raw_values
    selected_ids: list[int] = []
    if not all_stores:
        for raw_value in raw_values:
            try:
                store_id = int(raw_value)
            except (TypeError, ValueError):
                continue
            if store_id in allowed_ids and store_id not in selected_ids:
                selected_ids.append(store_id)
        all_stores = not selected_ids

    selected_names = [store['name'] for store in stores if store['id'] in selected_ids]
    if all_stores:
        scope_label = 'All Stores'
    elif len(selected_names) == 1:
        scope_label = selected_names[0]
    else:
        scope_label = f'{len(selected_names)} stores'

    return {
        'stores': stores,
        'selected_store_ids': selected_ids,
        'all_stores_selected': all_stores,
        'store_scope_label': scope_label,
        'scope_locked': principal.role == Role.STORE,
    }


def _render_page(request: Request, db: Session, principal: Principal, slug: str):
    page = PAGE_BY_SLUG[slug]
    context = {
        'request': request,
        'principal': principal,
        'page': page,
        'navigation': _visible_navigation(request),
        **_store_scope_context(request, db, principal),
    }
    return request.app.state.templates.TemplateResponse('v2/page.html', context)


@router.get('')
@router.get('/')
def v2_root(_: Principal = Depends(v2_access)):
    return RedirectResponse('/v2/overview', status_code=303)


@router.get('/overview')
def overview(
    request: Request,
    principal: Principal = Depends(v2_access),
    db: Session = Depends(get_db),
):
    return _render_page(request, db, principal, 'overview')


@router.get('/inventory')
def inventory(
    request: Request,
    principal: Principal = Depends(v2_access),
    db: Session = Depends(get_db),
):
    return _render_page(request, db, principal, 'inventory')


@router.get('/ordering')
def ordering(
    request: Request,
    principal: Principal = Depends(v2_access),
    db: Session = Depends(get_db),
):
    return _render_page(request, db, principal, 'ordering')


@router.get('/store-operations')
def store_operations_dashboard(
    request: Request,
    _feature: Principal = Depends(daily_logs_feature_access),
    principal: Principal = Depends(store_operations_access),
    db: Session = Depends(get_db),
):
    current_store = current_store_for_request(request, db)
    if current_store is None:
        return RedirectResponse(
            f'/v2/current-store?return_to={quote("/v2/store-operations", safe="")}',
            status_code=303,
        )
    business_date = portal_today()
    permission_flags = getattr(request.state, 'permission_flags', {}) or {}
    context = {
        'request': request,
        'principal': principal,
        'page': V2Page(
            'store-operations',
            'Store Operations',
            'Today at your current store.',
            permission='store.access',
            route_path='/v2/store-operations',
            badge='Owner Preview',
            active_prefix='/v2/store-operations',
        ),
        'navigation': _visible_navigation(request),
        'stores': [],
        'selected_store_ids': [],
        'all_stores_selected': False,
        'store_scope_label': current_store.name,
        'scope_locked': True,
        'scope_caption': 'Current Store',
        'current_store': current_store,
        'show_current_store_context': True,
        'business_date': business_date,
        'business_date_label': _natural_store_operations_date(business_date),
        'completion_statuses': completion_statuses(
            db,
            store_id=current_store.id,
            business_date=business_date,
            permission_flags=permission_flags,
        ),
        'daily_log_href': '/v2/store-operations/daily-logs',
    }
    return request.app.state.templates.TemplateResponse(
        'v2/store_operations_dashboard.html',
        context,
    )


@router.get('/current-store')
def current_store_page(
    request: Request,
    _feature: Principal = Depends(daily_logs_feature_access),
    principal: Principal = Depends(store_operations_access),
    db: Session = Depends(get_db),
):
    return_to = safe_return_target(request.query_params.get('return_to'))
    return request.app.state.templates.TemplateResponse(
        'v2/current_store.html',
        _current_store_page_context(request, principal, db=db, return_to=return_to),
    )


@router.post('/current-store')
async def choose_current_store(
    request: Request,
    _feature: Principal = Depends(daily_logs_feature_access),
    principal: Principal = Depends(store_operations_access),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    form = await request.form()
    return_to = safe_return_target(str(form.get('return_to', '')))
    raw_store_id = str(form.get('store_id', '')).strip()
    try:
        store_id = int(raw_store_id)
        if store_id <= 0:
            raise ValueError
    except (TypeError, ValueError):
        store_id = None
    web_session_id = getattr(request.state, 'web_session_id', None)
    store = (
        set_current_store(db, web_session_id=web_session_id, store_id=store_id)
        if web_session_id is not None and store_id is not None
        else None
    )
    if store is None:
        return request.app.state.templates.TemplateResponse(
            'v2/current_store.html',
            _current_store_page_context(
                request,
                principal,
                db=db,
                return_to=return_to,
                error='Choose an active Erupted store.',
                selected_store_id=store_id,
            ),
            status_code=422,
        )
    db.commit()
    return RedirectResponse(return_to, status_code=303)


@router.get('/audits')
def audits(
    request: Request,
    principal: Principal = Depends(v2_access),
    db: Session = Depends(get_db),
):
    return _render_page(request, db, principal, 'audits')


@router.get('/customer-forms')
def customer_forms(
    request: Request,
    principal: Principal = Depends(v2_access),
    db: Session = Depends(get_db),
):
    return _render_page(request, db, principal, 'customer-forms')


@router.get('/reports')
def reports(
    request: Request,
    principal: Principal = Depends(v2_access),
    db: Session = Depends(get_db),
):
    return _render_page(request, db, principal, 'reports')


@router.get('/admin')
def admin(
    request: Request,
    principal: Principal = Depends(v2_admin_access),
    db: Session = Depends(get_db),
):
    return _render_page(request, db, principal, 'admin')
