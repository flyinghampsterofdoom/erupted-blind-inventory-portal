from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.auth import Principal, Role, require_capability
from app.db import get_db
from app.routers.v2 import V2Page, _visible_navigation
from app.services.v2_ordering_data_coordinator import build_ordering_dashboard
from app.services.v2_ordering_view_model_service import dashboard_view
from app.v2.feature_exposure import require_v2_feature
from app.v2.store_scope import ScopeMode, list_authorized_stores, resolve_request_store_scope


FEATURE_KEY = 'ordering_intelligence_v2'
router = APIRouter(prefix='/v2/ordering', tags=['v2-ordering'])
feature_access = require_v2_feature(FEATURE_KEY)
ordering_access = require_capability('management.admin', Role.ADMIN, Role.MANAGER)


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
    return request.app.state.templates.TemplateResponse('v2/ordering/dashboard.html', context)
