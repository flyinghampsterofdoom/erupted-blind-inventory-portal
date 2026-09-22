"""HTTP regression coverage for the production catalog-autocomplete route."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.auth import Principal, Role, get_current_principal
from app.db import get_db
from app.models import Base, PurchaseOrder, Vendor
from app.routers import management
from app.services import manual_ordering_service


@pytest.fixture
def route_client(monkeypatch):
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    names = ('vendors', 'purchase_orders', 'vendor_sku_configs',
             'principal_permission_overrides', 'role_permission_overrides')
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[name] for name in names])
    with Session(engine) as db:
        db.add(Vendor(id=1, name='Existing vendor', square_vendor_id='SQ1', active=True))
        db.flush()
        db.add(PurchaseOrder(id=1, vendor_id=1, created_by_principal_id=1))
        db.commit()

    # Fail on any attempted database write, even one later rolled back.
    @event.listens_for(engine, 'before_cursor_execute')
    def read_only(_connection, _cursor, statement, _parameters, _context, _many):
        assert statement.lstrip().upper().startswith('SELECT'), statement

    catalog = Mock(return_value={
        'V1': SimpleNamespace(item_name='Coastal Clouds Blue Raspberry', variation_name='6mg',
                              sku='SKU123', gtin='0123456789012'),
    })
    monkeypatch.setattr(manual_ordering_service, 'catalog_variations', catalog)
    app = FastAPI()
    app.include_router(management.router)
    principal = Principal(1, 'owner', Role.ADMIN, None, True)
    app.dependency_overrides[get_current_principal] = lambda: principal

    def database():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_db] = database
    # Keep the actual route, PurchaseOrder lookup, authorization dependency and search service.
    with TestClient(app) as client:
        yield client, app, catalog
    engine.dispose()


@pytest.mark.parametrize('query', ['coastal blue', '6mg', 'SKU123', '0123456789012'])
def test_existing_order_catalog_http_search_is_read_only(route_client, query):
    client, _, catalog = route_client
    response = client.get('/management/ordering-tool/orders/1/products', params={'q': query})
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    assert response.json() == {'products': [{
        'variation_id': 'V1', 'name': 'Coastal Clouds Blue Raspberry — 6mg',
        'sku': 'SKU123', 'gtin': '0123456789012', 'unit_cost': None,
    }]}
    catalog.assert_called_once_with()


def test_catalog_http_missing_order_does_not_read_catalog(route_client):
    client, _, catalog = route_client
    response = client.get('/management/ordering-tool/orders/999/products', params={'q': 'coastal'})
    assert response.status_code == 404
    assert response.json() == {'detail': 'Order not found'}
    catalog.assert_not_called()


def test_catalog_http_enforces_ordering_authorization(route_client):
    client, app, catalog = route_client
    app.dependency_overrides[get_current_principal] = lambda: Principal(2, 'counter', Role.STORE, 1, True)
    response = client.get('/management/ordering-tool/orders/1/products', params={'q': 'coastal'})
    assert response.status_code == 403
    catalog.assert_not_called()


def test_catalog_http_failure_remains_read_only(route_client):
    client, _, catalog = route_client
    catalog.side_effect = RuntimeError('Catalog unavailable')
    response = client.get('/management/ordering-tool/orders/1/products', params={'q': 'coastal'})
    assert response.status_code == 400
    assert response.json() == {'detail': 'Catalog unavailable'}
