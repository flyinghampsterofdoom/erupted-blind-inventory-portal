import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.datastructures import QueryParams
from starlette.datastructures import FormData

from app.auth import Principal, Role
from app.config import settings
from app.routers.v2_ordering import FEATURE_KEY, feature_access, lifecycle_access, ordering_access
from app.security.csrf import verify_csrf
from app.services.v2_ordering_data_coordinator import OrderingDashboardData
from app.services.v2_ordering_lifecycle_repository import LifecycleProductRow, LifecycleWorkspacePage
from app.services.v2_ordering_lifecycle_service import LifecycleCommand, LifecycleTransitionResult
from app.v2.feature_exposure import FeatureExposure


def test_native_ordering_route_is_get_only_and_has_separate_feature_and_capability_dependencies():
    from app.main import app

    routes = [route for route in app.routes if getattr(route, 'path', '') == '/v2/ordering']
    assert len(routes) == 1
    route = routes[0]
    assert route.methods == {'GET'}
    dependency_calls = [dependency.call for dependency in route.dependant.dependencies]
    assert feature_access in dependency_calls
    assert ordering_access in dependency_calls

    lifecycle_routes = {
        (route.path, frozenset(route.methods), tuple(dependency.call for dependency in route.dependant.dependencies))
        for route in app.routes
        if getattr(route, 'path', '').startswith('/v2/ordering/products')
    }
    assert any(path == '/v2/ordering/products' and methods == {'GET'} for path, methods, _deps in lifecycle_routes)
    assert any(path == '/v2/ordering/products/archived' and methods == {'GET'} for path, methods, _deps in lifecycle_routes)
    mutation = next(item for item in lifecycle_routes if item[0] == '/v2/ordering/products/lifecycle')
    assert mutation[1] == {'POST'}
    assert feature_access in mutation[2]
    assert ordering_access in mutation[2]
    assert lifecycle_access in mutation[2]
    assert verify_csrf in mutation[2]


def test_native_feature_is_disabled_by_default_and_can_be_principal_scoped(monkeypatch):
    owner = Principal(id=4, username='owner', role=Role.ADMIN, store_id=None, active=True)
    other = Principal(id=5, username='other', role=Role.ADMIN, store_id=None, active=True)
    monkeypatch.setattr(settings, 'v2_enabled_features', '')
    monkeypatch.setattr(settings, 'v2_principal_features', '')
    with pytest.raises(HTTPException) as disabled:
        feature_access(owner)
    assert disabled.value.status_code == 404

    monkeypatch.setattr(settings, 'v2_principal_features', f'4:{FEATURE_KEY}')
    assert feature_access(owner) == owner
    with pytest.raises(HTTPException) as hidden:
        feature_access(other)
    assert hidden.value.status_code == 404
    assert FeatureExposure.from_settings().enabled(FEATURE_KEY, principal_id=4)
    assert not FeatureExposure.from_settings().enabled(FEATURE_KEY, principal_id=5)


def test_ordering_template_is_read_only_and_exposes_explanation_contract():
    root = Path(__file__).resolve().parents[1]
    template = (root / 'app/templates/v2/ordering/dashboard.html').read_text(encoding='utf-8')
    assert '<form' not in template
    assert 'No purchase order or inventory change can be made here.' in template
    assert '{{ row.freshness_label }}' in template
    for field in ('Applied policies', 'Debug inputs', 'Data quality', 'Sources', 'Why this result?'):
        assert field in template


def test_square_gateway_has_no_write_endpoint_or_method_surface():
    from app.services.v2_ordering_square_gateway import READ_ENDPOINTS, SquareOrderingReadGateway

    assert READ_ENDPOINTS == {
        '/v2/catalog/search-catalog-items',
        '/v2/inventory/batch-retrieve-counts',
        '/v2/orders/search',
        '/v2/inventory/changes/batch-retrieve',
    }
    names = set(dir(SquareOrderingReadGateway))
    assert not {'write', 'push', 'adjust', 'batch_change', 'physical_count'} & names


def test_route_renders_read_only_dashboard_context_without_mutation(monkeypatch):
    from app.routers import v2_ordering

    principal = Principal(id=4, username='owner', role=Role.ADMIN, store_id=None, active=True)

    class Rows:
        def all(self):
            return [SimpleNamespace(id=1, name='HWY99')]

    class Db:
        def execute(self, _query):
            return Rows()

    captured = {}

    class Templates:
        def TemplateResponse(self, name, context):
            captured['name'] = name
            captured['context'] = context
            return context

    request = SimpleNamespace(
        query_params=QueryParams('scope=all'),
        url=SimpleNamespace(path='/v2/ordering'),
        state=SimpleNamespace(permission_flags={'management.admin': True}, principal=principal),
        app=SimpleNamespace(state=SimpleNamespace(templates=Templates())),
    )
    monkeypatch.setattr(
        v2_ordering,
        'build_ordering_dashboard',
        lambda _db, *, store_ids: OrderingDashboardData(datetime(2026, 7, 25, tzinfo=timezone.utc), ()),
    )
    response = v2_ordering.ordering_dashboard_page(request, principal, principal, Db())
    assert response is captured['context']
    assert captured['name'] == 'v2/ordering/dashboard.html'
    assert captured['context']['dashboard'].rows == ()
    assert captured['context']['selected_store_ids'] == [1]
    assert captured['context']['all_stores_selected'] is True


def test_lifecycle_mutation_uses_only_local_service_and_commits_once(monkeypatch):
    from app.routers import v2_ordering

    principal = Principal(id=4, username='owner', role=Role.ADMIN, store_id=None, active=True)
    form = FormData([
        ('command', 'ARCHIVE'),
        ('selection', '0|VAR-1|0'),
        ('sku_0', 'SKU-1'),
        ('product_name_0', 'Product 1'),
        ('note', 'Owner archive'),
    ])

    class Request:
        headers = {}
        client = None

        async def form(self):
            return form

    class Db:
        committed = 0
        rolled_back = 0

        def commit(self):
            self.committed += 1

        def rollback(self):
            self.rolled_back += 1

    db = Db()
    monkeypatch.setattr(
        v2_ordering,
        'list_lifecycle_products',
        lambda _db, *, archived: (
            LifecycleProductRow('VAR-1', 'SKU-1', 'Product 1', 'Vendor', 'ACTIVE', 0, None),
        ),
    )
    calls = []
    monkeypatch.setattr(
        v2_ordering,
        'transition_lifecycle',
        lambda _db, **kwargs: calls.append(kwargs)
        or LifecycleTransitionResult(LifecycleCommand.ARCHIVE, 'batch', 1, (('VAR-1', 1),)),
    )
    response = asyncio.run(
        v2_ordering.mutate_product_lifecycle(Request(), principal, principal, principal, None, db)
    )
    assert response.status_code == 303
    assert calls[0]['command'] == LifecycleCommand.ARCHIVE
    assert calls[0]['selections'][0].square_variation_id == 'VAR-1'
    assert db.committed == 1 and db.rolled_back == 0


def test_lifecycle_workspace_context_preserves_filter_sort_and_page_size_in_links(monkeypatch):
    from app.routers import v2_ordering

    principal = Principal(id=4, username='owner', role=Role.ADMIN, store_id=None, active=True)
    request = SimpleNamespace(
        query_params=QueryParams(
            'product=Clickmate&vendor=7+Daze&inventory=UNKNOWN&sort=vendor&direction=desc&page=2&page_size=25'
        ),
        url=SimpleNamespace(path='/v2/ordering/products'),
        state=SimpleNamespace(permission_flags={'ordering.lifecycle.manage': True}, principal=principal),
    )
    captured = {}

    def workspace(_db, **kwargs):
        captured.update(kwargs)
        return LifecycleWorkspacePage(
            rows=(LifecycleProductRow('VAR-1', 'SKU-1', 'Clickmate', '7 Daze', 'ACTIVE', 0, None),),
            total_count=51,
            page_number=2,
            page_size=25,
            total_pages=3,
            range_start=26,
            range_end=50,
            status_counts={'ACTIVE': 50, 'NO_FUTURE_REORDER': 1, 'ARCHIVED': 2},
            vendor_options=('7 Daze',),
            store_options=((1, 'Andresen'),),
            query_count=6,
        )

    monkeypatch.setattr(v2_ordering, 'query_lifecycle_workspace', workspace)
    monkeypatch.setattr(v2_ordering, '_visible_navigation', lambda _request: ())
    context = v2_ordering._management_context(
        request, principal, object(), archived=False, page_number=2, page_size=25
    )
    assert captured['filters'].product_search == 'Clickmate'
    assert captured['filters'].vendor == '7 Daze'
    assert captured['sort'] == 'vendor' and captured['direction'] == 'desc'
    assert 'product=Clickmate' in context['next_url']
    assert 'vendor=7+Daze' in context['next_url']
    assert 'inventory=UNKNOWN' in context['next_url']
    assert 'page_size=25' in context['next_url']
    assert 'page=3' in context['next_url']
    assert context['previous_url'].endswith('page=1')
    assert context['range_start'] == 26 and context['range_end'] == 50


def test_lifecycle_workspace_rejects_unbounded_page_size():
    from app.routers import v2_ordering

    principal = Principal(id=4, username='owner', role=Role.ADMIN, store_id=None, active=True)
    request = SimpleNamespace(query_params=QueryParams(''), url=SimpleNamespace(path='/v2/ordering/products'))
    with pytest.raises(HTTPException) as exc:
        v2_ordering._management_context(
            request, principal, object(), archived=False, page_number=1, page_size=500
        )
    assert exc.value.status_code == 400


def test_lifecycle_management_route_has_no_square_gateway_dependency():
    root = Path(__file__).resolve().parents[1]
    route_source = (root / 'app/routers/v2_ordering.py').read_text(encoding='utf-8')
    repository_source = (root / 'app/services/v2_ordering_lifecycle_repository.py').read_text(encoding='utf-8')
    assert 'fetch_product_metadata' not in route_source
    assert 'SquareOrderingReadGateway' not in route_source
    assert 'SquareOrderingReadGateway' not in repository_source
