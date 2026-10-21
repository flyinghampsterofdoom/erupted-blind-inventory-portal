from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.auth as auth_module
import app.routers.v2 as v2_router
from app.auth import Principal, Role, get_current_principal
from app.db import get_db
from app.security.csrf import install_csrf_cookie_middleware
from app.services.v2_square_data_service import SquareDataRefreshResult


@pytest.fixture
def square_action_site(monkeypatch):
    principal = SimpleNamespace(
        value=Principal(
            id=6,
            username='owner',
            role=Role.ADMIN,
            store_id=None,
            active=True,
        )
    )
    refresh = Mock(
        return_value=SquareDataRefreshResult(
            started=True,
            state='current',
            message='Square data updated.',
        )
    )
    status = SimpleNamespace(
        value={
            'state': 'current',
            'label': 'Current',
            'last_successful_at': '2026-08-11T18:00:00+00:00',
        }
    )

    app = FastAPI()
    install_csrf_cookie_middleware(app)
    app.include_router(v2_router.router)

    def current_principal():
        return principal.value

    def db_override():
        yield object()

    app.dependency_overrides[get_current_principal] = current_principal
    app.dependency_overrides[get_db] = db_override
    monkeypatch.setattr(
        auth_module,
        'principal_has_permission',
        lambda _db, *, principal, permission_key, fallback_allowed: fallback_allowed,
    )
    monkeypatch.setattr(v2_router, 'refresh_square_sales_data', refresh)
    monkeypatch.setattr(
        v2_router,
        'serializable_square_data_status',
        lambda _db: status.value,
    )

    with TestClient(app, follow_redirects=False) as client:
        client.get('/v2/square-data/status')
        yield SimpleNamespace(
            client=client,
            principal=principal,
            refresh=refresh,
            status=status,
        )


def _post_refresh(site, *, return_to='/v2/overview', csrf_token=None):
    token = csrf_token if csrf_token is not None else site.client.cookies.get('csrf_token')
    return site.client.post(
        '/v2/square-data/refresh',
        data={'csrf_token': token, 'return_to': return_to},
    )


@pytest.mark.parametrize('role', [Role.ADMIN, Role.MANAGER])
def test_owner_and_manager_can_trigger_shared_square_refresh_once(square_action_site, role):
    square_action_site.principal.value = Principal(
        id=6,
        username=role.value.lower(),
        role=role,
        store_id=None,
        active=True,
    )

    response = _post_refresh(square_action_site)

    assert response.status_code == 303
    assert response.headers['location'] == '/v2/overview?message=Square%20data%20updated.'
    square_action_site.refresh.assert_called_once_with(actor_id=6, force=True)


@pytest.mark.parametrize('role', [Role.LEAD, Role.STORE])
def test_principals_without_admin_capability_remain_blocked(square_action_site, role):
    square_action_site.principal.value = Principal(
        id=9,
        username=role.value.lower(),
        role=role,
        store_id=1 if role == Role.STORE else None,
        active=True,
    )

    response = _post_refresh(square_action_site)

    assert response.status_code == 403
    square_action_site.refresh.assert_not_called()


def test_square_refresh_action_enforces_csrf(square_action_site):
    response = _post_refresh(square_action_site, csrf_token='invalid')

    assert response.status_code == 403
    assert response.json()['detail'] == 'Invalid CSRF token'
    square_action_site.refresh.assert_not_called()


@pytest.mark.parametrize(
    ('return_to', 'expected'),
    [
        ('/v2/overview', '/v2/overview'),
        ('/v2/admin', '/v2/admin'),
        ('/v2/reports?store_id=all', '/v2/reports?store_id=all'),
    ],
)
def test_refresh_returns_to_multiple_shared_v2_pages(square_action_site, return_to, expected):
    response = _post_refresh(square_action_site, return_to=return_to)

    separator = '&' if '?' in expected else '?'
    assert response.status_code == 303
    assert response.headers['location'] == (
        f'{expected}{separator}message=Square%20data%20updated.'
    )
    square_action_site.refresh.assert_called_once_with(actor_id=6, force=True)


@pytest.mark.parametrize(
    'unsafe_return_to',
    [
        '/v2/store-operations/daily-logs\\outside',
        '/management/home',
        'https://example.com/v2/overview',
        '/v2/square-data/refresh',
    ],
)
def test_refresh_rejects_unsafe_return_targets(square_action_site, unsafe_return_to):
    response = _post_refresh(square_action_site, return_to=unsafe_return_to)

    assert response.status_code == 303
    assert response.headers['location'].startswith('/v2/overview?message=')


def test_failed_refresh_returns_to_v2_and_exposes_failed_status(square_action_site):
    square_action_site.refresh.return_value = SquareDataRefreshResult(
        started=True,
        state='failed',
        message='Square data update failed: upstream unavailable',
    )
    square_action_site.status.value = {
        'state': 'failed',
        'label': 'Update failed',
        'last_successful_at': '2026-08-10T12:00:00+00:00',
        'last_error': 'upstream unavailable',
    }

    response = _post_refresh(square_action_site, return_to='/v2/admin')
    visible_status = square_action_site.client.get('/v2/square-data/status')

    assert response.status_code == 303
    assert response.headers['location'].startswith('/v2/admin?error=')
    assert visible_status.status_code == 200
    assert visible_status.json()['state'] == 'failed'
    assert visible_status.json()['label'] == 'Update failed'


def test_refresh_route_is_shared_v2_admin_action_without_workspace_feature_guard():
    route = next(
        route
        for route in v2_router.router.routes
        if route.path == '/v2/square-data/refresh'
    )

    assert route.name == 'square_data_refresh_action'
    assert route.methods == {'POST'}
    assert [dependency.call for dependency in route.dependant.dependencies] == [
        v2_router.v2_admin_access,
        v2_router.verify_csrf,
    ]
