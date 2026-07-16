from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import Principal, Role, require_capability
from app.db import get_db
from app.models import Store
from app.v2.feature_exposure import FeatureExposure


router = APIRouter(prefix='/v2', tags=['v2'])
v2_access = require_capability('management.access', Role.ADMIN, Role.MANAGER, Role.LEAD)
v2_admin_access = require_capability('management.admin', Role.ADMIN, Role.MANAGER)


@dataclass(frozen=True)
class V2Page:
    slug: str
    label: str
    description: str
    permission: str = 'management.access'
    route_path: str | None = None
    badge: str = 'Milestone 1 · Shell'
    active_prefix: str | None = None

    @property
    def href(self) -> str:
        return self.route_path or f'/v2/{self.slug}'


V2_PAGES: tuple[V2Page, ...] = (
    V2Page('overview', 'Overview', 'A future home for operational signals and cross-store priorities.'),
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


def _visible_navigation(request: Request) -> list[V2Page]:
    permission_flags = getattr(request.state, 'permission_flags', {}) or {}
    navigation = [page for page in V2_PAGES if permission_flags.get(page.permission, False)]
    principal = getattr(request.state, 'principal', None)
    exposed = principal is not None and FeatureExposure.from_settings().enabled(
        'exchanges_returns_v2', principal_id=principal.id
    )
    management_authorized = permission_flags.get('management.access', False)
    store_submission_authorized = (
        permission_flags.get('store.access', False) and principal is not None and principal.store_id is not None
    )
    module_authorized = management_authorized or store_submission_authorized
    if exposed and module_authorized:
        module_path = (
            '/v2/customer-forms/exchanges-returns/history'
            if management_authorized
            else '/v2/customer-forms/exchanges-returns'
        )
        module_page = V2Page(
            'customer-forms/exchanges-returns',
            'Customer & Forms',
            'Submit and review exchange and return records.',
            route_path=module_path,
            badge='Milestone 4 · Local preview',
            active_prefix='/v2/customer-forms/exchanges-returns',
        )
        navigation = [module_page if page.slug == 'customer-forms' else page for page in navigation]
        if not any(page.slug == module_page.slug for page in navigation):
            navigation.append(module_page)
    return navigation


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
def store_operations(
    request: Request,
    principal: Principal = Depends(v2_access),
    db: Session = Depends(get_db),
):
    return _render_page(request, db, principal, 'store-operations')


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
