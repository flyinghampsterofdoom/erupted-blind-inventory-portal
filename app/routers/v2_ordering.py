from __future__ import annotations

import logging
from dataclasses import replace
from time import perf_counter
from urllib.parse import urlencode

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
from app.services.v2_ordering_lifecycle_service import (
    LifecycleCommand,
    LifecycleSelection,
    LifecycleTransitionError,
    transition_lifecycle,
)
from app.services.v2_ordering_square_gateway import SquareOrderingReadGateway
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
) -> dict:
    if page_number < 1 or page_size not in PAGE_SIZES:
        raise HTTPException(status_code=400, detail='Unsupported lifecycle page request')
    all_rows = list_lifecycle_products(db, archived=archived)
    if not archived and all_rows:
        try:
            products = SquareOrderingReadGateway().fetch_product_metadata(
                [row.square_variation_id for row in all_rows]
            )
        except Exception:
            logger.warning('v2_ordering_lifecycle_catalog_metadata_unavailable')
            products = {}
        all_rows = tuple(
            replace(
                row,
                product_name=(
                    ' — '.join(
                        value
                        for value in (
                            products[row.square_variation_id].item_name,
                            products[row.square_variation_id].variation_name,
                        )
                        if value
                    )
                    if row.square_variation_id in products
                    else row.product_name
                ),
            )
            for row in all_rows
        )
    start = (page_number - 1) * page_size
    rows = all_rows[start : start + page_size]
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
        'rows': rows,
        'archived': archived,
        'page_number': page_number,
        'page_size': page_size,
        'page_sizes': PAGE_SIZES,
        'has_previous': page_number > 1,
        'has_next': start + page_size < len(all_rows),
        'total_count': len(all_rows),
        'message': request.query_params.get('message', ''),
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
            selection.product_name_snapshot or snapshot_by_id[selection.square_variation_id].product_name,
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
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail='A selected product changed concurrently.') from exc

    destination = '/v2/ordering/products/archived' if command == LifecycleCommand.RESTORE else '/v2/ordering/products'
    query = urlencode({'message': f'{result.changed_count} product lifecycle record(s) updated.'})
    return RedirectResponse(f'{destination}?{query}', status_code=303)
