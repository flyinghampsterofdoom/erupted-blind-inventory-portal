from __future__ import annotations

import logging
from time import perf_counter
from urllib.parse import parse_qsl, urlencode, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import Principal, Role, require_capability
from app.db import get_db
from app.dependencies import get_client_ip
from app.routers.v2 import V2Page, _visible_navigation
from app.security.csrf import verify_csrf
from app.services.v2_ordering_data_coordinator import build_ordering_dashboard
from app.services.v2_ordering_lifecycle_repository import list_lifecycle_products
from app.services.v2_ordering_lifecycle_repository import (
    ACTIVE,
    ARCHIVED,
    NO_FUTURE_REORDER,
    LifecycleWorkspaceFilters,
    query_lifecycle_workspace,
)
from app.services.v2_ordering_catalog_service import refresh_ordering_catalog_identity
from app.services.v2_ordering_lifecycle_service import (
    LifecycleCommand,
    LifecycleSelection,
    LifecycleTransitionError,
    transition_lifecycle,
)
from app.services.v2_ordering_view_model_service import dashboard_view
from app.v2.feature_exposure import require_v2_feature
from app.v2.store_scope import ScopeMode, list_authorized_stores, resolve_request_store_scope


FEATURE_KEY = 'ordering_intelligence_v2'
router = APIRouter(prefix='/v2/ordering', tags=['v2-ordering'])
feature_access = require_v2_feature(FEATURE_KEY)
ordering_access = require_capability('management.admin', Role.ADMIN, Role.MANAGER)
lifecycle_access = require_capability('ordering.lifecycle.manage')
logger = logging.getLogger(__name__)
PAGE_SIZES = (25, 50, 100, 250)
LIFECYCLE_SORTS = ('product', 'sku', 'vendor', 'lifecycle', 'changed_at', 'changed_by')
LIFECYCLE_PATHS = ('/v2/ordering/products', '/v2/ordering/products/archived')


def _scope_context(scope, authorized_stores) -> dict:
    if scope.mode == ScopeMode.ALL:
        label = 'All Stores'
    elif len(scope.store_names) == 1:
        label = scope.store_names[0]
    else:
        label = f'{len(scope.store_names)} stores'
    return {
        'stores': [{'id': row.id, 'name': row.name} for row in authorized_stores],
        'selected_store_ids': list(scope.store_ids),
        'all_stores_selected': scope.mode == ScopeMode.ALL,
        'store_scope_label': label,
        'scope_locked': scope.locked,
        'scope_caption': 'Stores',
    }


@router.get('')
def ordering_dashboard_page(
    request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(ordering_access),
    db: Session = Depends(get_db),
):
    started = perf_counter()
    scope = resolve_request_store_scope(request, db, principal)
    authorized = list_authorized_stores(db, principal)
    data = build_ordering_dashboard(db, store_ids=scope.store_ids)
    context = {
        'request': request,
        'principal': principal,
        'page': V2Page(
            'ordering',
            'Ordering Intelligence',
            'Read-only, explainable store-level replenishment recommendations.',
            permission='management.admin',
            route_path='/v2/ordering',
            badge='Owner Preview · Read only',
            active_prefix='/v2/ordering',
        ),
        'navigation': _visible_navigation(request),
        'dashboard': dashboard_view(data),
        **_scope_context(scope, authorized),
    }
    permission_flags = getattr(request.state, 'permission_flags', {})
    context['can_manage_lifecycle'] = bool(permission_flags.get('ordering.lifecycle.manage', False))
    render_started = perf_counter()
    response = request.app.state.templates.TemplateResponse('v2/ordering/dashboard.html', context)
    logger.info(
        'v2_ordering_request_metrics',
        extra={
            'ordering_metrics': {
                **data.metrics.__dict__,
                'store_count': len(scope.store_ids),
                'template_render_seconds': perf_counter() - render_started,
                'request_seconds_after_dependencies': perf_counter() - started,
                'response_bytes': len(getattr(response, 'body', b'')),
            }
        },
    )
    return response


def _management_context(
    request: Request,
    principal: Principal,
    db: Session,
    *,
    archived: bool,
    page_number: int,
    page_size: int,
    query_params=None,
    error: str = '',
    form_note: str = '',
    form_command: str = '',
    selected_ids: set[str] | None = None,
) -> dict:
    if page_number < 1 or page_size not in PAGE_SIZES:
        raise HTTPException(status_code=400, detail='Unsupported lifecycle page request')
    params = query_params if query_params is not None else request.query_params
    filters = LifecycleWorkspaceFilters(
        product_search=str(params.get('product') or '').strip(),
        sku_search=str(params.get('sku') or '').strip(),
        vendor=str(params.get('vendor') or '').strip(),
        lifecycle=str(params.get('lifecycle') or '').strip(),
        mapping=str(params.get('mapping') or 'ANY').strip(),
        name_state=str(params.get('name_state') or 'ANY').strip(),
    )
    sort = str(params.get('sort') or 'product').strip()
    direction = str(params.get('direction') or 'asc').strip().lower()
    try:
        workspace = query_lifecycle_workspace(
            db,
            archived=archived,
            filters=filters,
            sort=sort,
            direction=direction,
            page_number=page_number,
            page_size=page_size,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    base_path = '/v2/ordering/products/archived' if archived else '/v2/ordering/products'
    query_state = {
        'product': filters.product_search,
        'sku': filters.sku_search,
        'vendor': filters.vendor,
        'lifecycle': filters.lifecycle,
        'mapping': filters.mapping if filters.mapping != 'ANY' else '',
        'name_state': filters.name_state if filters.name_state != 'ANY' else '',
        'sort': sort if sort != 'product' else '',
        'direction': direction if direction != 'asc' else '',
        'page_size': str(page_size) if page_size != 50 else '',
    }

    def url_with(**changes) -> str:
        values = {**query_state, **changes}
        query = urlencode({key: value for key, value in values.items() if value not in ('', None)})
        return f'{base_path}?{query}' if query else base_path

    active_filters = []
    filter_labels = {
        'product': ('Product', filters.product_search),
        'sku': ('SKU', filters.sku_search),
        'vendor': ('Vendor', filters.vendor),
        'lifecycle': ('Lifecycle', filters.lifecycle.replace('_', ' ').title()),
        'mapping': ('Mapping', filters.mapping.title()),
        'name_state': ('Product name', filters.name_state.title()),
    }
    for key, (label, value) in filter_labels.items():
        if value and not (key in {'mapping', 'name_state'} and str(value).upper() == 'ANY'):
            active_filters.append(
                {'key': key, 'label': label, 'value': value, 'remove_url': url_with(**{key: '', 'page': 1})}
            )

    sort_urls = {}
    for field in LIFECYCLE_SORTS:
        next_direction = 'desc' if sort == field and direction == 'asc' else 'asc'
        sort_urls[field] = url_with(sort=field, direction=next_direction, page=1)
    current_query = urlencode({key: value for key, value in query_state.items() if value not in ('', None)})
    return_to = f'{base_path}?{current_query}' if current_query else base_path
    return {
        'request': request,
        'principal': principal,
        'page': V2Page(
            'ordering-lifecycle',
            'Archived Products' if archived else 'Product Lifecycle',
            'Explicit owner-managed lifecycle controls for Ordering Intelligence.',
            permission='ordering.lifecycle.manage',
            route_path='/v2/ordering/products/archived' if archived else '/v2/ordering/products',
            badge='Owner only',
            active_prefix='/v2/ordering',
        ),
        'navigation': _visible_navigation(request),
        'rows': workspace.rows,
        'archived': archived,
        'page_number': workspace.page_number,
        'page_size': page_size,
        'page_sizes': PAGE_SIZES,
        'has_previous': workspace.page_number > 1,
        'has_next': workspace.page_number < workspace.total_pages,
        'previous_url': url_with(page=workspace.page_number - 1),
        'next_url': url_with(page=workspace.page_number + 1),
        'total_count': workspace.total_count,
        'unfiltered_count': workspace.unfiltered_count,
        'total_pages': workspace.total_pages,
        'range_start': workspace.range_start,
        'range_end': workspace.range_end,
        'status_counts': workspace.status_counts,
        'vendor_options': workspace.vendor_options,
        'coverage': workspace.coverage,
        'filters': filters,
        'sort': sort,
        'direction': direction,
        'sort_urls': sort_urls,
        'active_filters': active_filters,
        'clear_filters_url': url_with(
            product='', sku='', vendor='', lifecycle='', mapping='', name_state='', page=1
        ),
        'page_size_url': base_path,
        'return_to': return_to,
        'query_state': query_state,
        'query_count': workspace.query_count,
        'message': request.query_params.get('message', ''),
        'error': error,
        'form_note': form_note,
        'form_command': form_command,
        'selected_ids': selected_ids or set(),
        'lifecycle_options': (ACTIVE, NO_FUTURE_REORDER) if not archived else (ARCHIVED,),
        'mapping_options': ('ANY', 'MAPPED', 'UNMAPPED'),
        'name_options': ('ANY', 'KNOWN', 'UNKNOWN'),
    }


@router.get('/products')
def lifecycle_products_page(
    request: Request,
    page: int = 1,
    page_size: int = 50,
    _feature: Principal = Depends(feature_access),
    _ordering: Principal = Depends(ordering_access),
    principal: Principal = Depends(lifecycle_access),
    db: Session = Depends(get_db),
):
    return request.app.state.templates.TemplateResponse(
        'v2/ordering/lifecycle_products.html',
        _management_context(request, principal, db, archived=False, page_number=page, page_size=page_size),
    )


@router.get('/products/archived')
def archived_products_page(
    request: Request,
    page: int = 1,
    page_size: int = 50,
    _feature: Principal = Depends(feature_access),
    _ordering: Principal = Depends(ordering_access),
    principal: Principal = Depends(lifecycle_access),
    db: Session = Depends(get_db),
):
    return request.app.state.templates.TemplateResponse(
        'v2/ordering/lifecycle_products.html',
        _management_context(request, principal, db, archived=True, page_number=page, page_size=page_size),
    )


@router.post('/products/catalog/refresh')
def refresh_product_catalog(
    request: Request,
    _feature: Principal = Depends(feature_access),
    _ordering: Principal = Depends(ordering_access),
    principal: Principal = Depends(lifecycle_access),
    _csrf: None = Depends(verify_csrf),
    db: Session = Depends(get_db),
):
    result = refresh_ordering_catalog_identity(
        db,
        actor_principal_id=principal.id,
        ip=get_client_ip(request),
    )
    db.commit()
    if result.outcome == 'COMPLETE':
        message = (
            f'Catalog metadata refreshed for {result.covered_mapped_count} mapped products '
            f'in {result.square_page_count} Square page(s).'
        )
    elif result.outcome == 'PARTIAL':
        message = (
            f'Catalog refresh was partial. {result.missing_mapped_count} mapped product(s) '
            'still have unknown names; prior metadata was preserved.'
        )
    else:
        message = 'Catalog refresh failed. Existing catalog metadata was preserved.'
    return RedirectResponse(
        f'/v2/ordering/products?{urlencode({"message": message})}',
        status_code=303,
    )


@router.post('/products/lifecycle')
async def mutate_product_lifecycle(
    request: Request,
    _feature: Principal = Depends(feature_access),
    _ordering: Principal = Depends(ordering_access),
    principal: Principal = Depends(lifecycle_access),
    _csrf: None = Depends(verify_csrf),
    db: Session = Depends(get_db),
):
    form = await request.form()
    raw_return_to = str(form.get('return_to') or '')

    def safe_return_to(default_path: str) -> tuple[str, dict[str, str]]:
        parsed = urlsplit(raw_return_to)
        if parsed.scheme or parsed.netloc or parsed.path not in LIFECYCLE_PATHS:
            return default_path, {}
        return parsed.path, {key: value for key, value in parse_qsl(parsed.query, keep_blank_values=False)}

    def safe_page_state(params: dict[str, str]) -> tuple[int, int]:
        try:
            page_number = max(1, int(params.pop('page', '1')))
        except (TypeError, ValueError):
            page_number = 1
        try:
            resolved_page_size = int(params.get('page_size', '50'))
        except (TypeError, ValueError):
            resolved_page_size = 50
        if resolved_page_size not in PAGE_SIZES:
            resolved_page_size = 50
        return page_number, resolved_page_size
    try:
        command = LifecycleCommand(str(form.get('command') or ''))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail='Unsupported lifecycle command') from exc
    selections: list[LifecycleSelection] = []
    for packed in form.getlist('selection'):
        try:
            raw_index, variation_id, raw_version = str(packed).split('|', 2)
            index = int(raw_index)
            expected_version = int(raw_version)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail='Invalid lifecycle selection') from exc
        selections.append(
            LifecycleSelection(
                variation_id,
                expected_version,
                str(form.get(f'sku_{index}') or ''),
                str(form.get(f'product_name_{index}') or ''),
            )
        )

    snapshot_rows = list_lifecycle_products(db, archived=command == LifecycleCommand.RESTORE)
    snapshot_by_id = {row.square_variation_id: row for row in snapshot_rows}
    if any(selection.square_variation_id not in snapshot_by_id for selection in selections):
        raise HTTPException(status_code=400, detail='A selected product is not available in this lifecycle view.')
    enriched = tuple(
        LifecycleSelection(
            selection.square_variation_id,
            selection.expected_version,
            selection.sku_snapshot or snapshot_by_id[selection.square_variation_id].sku,
            selection.product_name_snapshot or (
                snapshot_by_id[selection.square_variation_id].product_name
                if snapshot_by_id[selection.square_variation_id].product_name_available
                else ''
            ),
        )
        for selection in selections
    )
    try:
        result = transition_lifecycle(
            db,
            command=command,
            selections=enriched,
            actor_principal_id=principal.id,
            note=str(form.get('note') or ''),
            ip=get_client_ip(request),
        )
        db.commit()
    except LifecycleTransitionError as exc:
        db.rollback()
        status_code = 409 if exc.code in {'STALE_VERSION', 'INVALID_TRANSITION'} else 400
        default_path = '/v2/ordering/products/archived' if command == LifecycleCommand.RESTORE else '/v2/ordering/products'
        return_path, return_params = safe_return_to(default_path)
        page_number, resolved_page_size = safe_page_state(return_params)
        context = _management_context(
            request,
            principal,
            db,
            archived=return_path.endswith('/archived'),
            page_number=page_number,
            page_size=resolved_page_size,
            query_params=return_params,
            error=str(exc),
            form_note=str(form.get('note') or ''),
            form_command=command.value,
            selected_ids={selection.square_variation_id for selection in selections},
        )
        return request.app.state.templates.TemplateResponse(
            'v2/ordering/lifecycle_products.html', context, status_code=status_code
        )
    except IntegrityError as exc:
        db.rollback()
        default_path = '/v2/ordering/products/archived' if command == LifecycleCommand.RESTORE else '/v2/ordering/products'
        return_path, return_params = safe_return_to(default_path)
        page_number, resolved_page_size = safe_page_state(return_params)
        context = _management_context(
            request,
            principal,
            db,
            archived=return_path.endswith('/archived'),
            page_number=page_number,
            page_size=resolved_page_size,
            query_params=return_params,
            error='A selected product changed concurrently. Review the refreshed rows and try again.',
            form_note=str(form.get('note') or ''),
            form_command=command.value,
            selected_ids={selection.square_variation_id for selection in selections},
        )
        return request.app.state.templates.TemplateResponse(
            'v2/ordering/lifecycle_products.html', context, status_code=409
        )

    default_path = '/v2/ordering/products/archived' if command == LifecycleCommand.RESTORE else '/v2/ordering/products'
    destination, destination_params = safe_return_to(default_path)
    destination_params['message'] = f'{result.changed_count} product lifecycle record(s) updated.'
    return RedirectResponse(f'{destination}?{urlencode(destination_params)}', status_code=303)
