from pathlib import Path
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.datastructures import QueryParams

from app.auth import Principal, Role
from app.config import settings
from app.routers.v2_ordering import FEATURE_KEY, feature_access, ordering_access
from app.services.v2_ordering_data_coordinator import OrderingDashboardData
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
