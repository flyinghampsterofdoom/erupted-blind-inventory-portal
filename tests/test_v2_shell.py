from types import SimpleNamespace

from starlette.datastructures import QueryParams

from app.auth import Principal, Role
from app.config import settings
from app.routers.v2 import V2_PAGES, _store_scope_context, _visible_navigation


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Db:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, _query):
        return _Rows(self.rows)


def _request(query='', permissions=None, principal=None):
    return SimpleNamespace(
        query_params=QueryParams(query),
        state=SimpleNamespace(permission_flags=permissions or {}, principal=principal),
    )


def test_v2_page_set_matches_milestone_shell():
    assert [page.label for page in V2_PAGES] == [
        'Overview',
        'Inventory',
        'Ordering',
        'Store Operations',
        'Audits',
        'Customer & Forms',
        'Reports',
        'Admin',
    ]


def test_navigation_uses_existing_permission_flags():
    request = _request(permissions={'management.access': True, 'management.admin': False})
    assert [page.slug for page in _visible_navigation(request)] == [
        'overview',
        'inventory',
        'ordering',
        'store-operations',
        'audits',
        'customer-forms',
        'reports',
    ]


def test_exchange_navigation_uses_effective_permissions_not_literal_role(monkeypatch):
    monkeypatch.setattr(settings, 'v2_enabled_features', 'exchanges_returns_v2')
    store_principal = Principal(id=3, username='store', role=Role.STORE, store_id=10, active=True)
    management_only = _request(
        permissions={'store.access': False, 'management.access': True},
        principal=store_principal,
    )
    module = next(page for page in _visible_navigation(management_only) if page.label == 'Customer & Forms')
    assert module.href == '/v2/customer-forms/exchanges-returns/history'

    admin_without_store = Principal(id=4, username='admin', role=Role.ADMIN, store_id=None, active=True)
    store_only_without_scope = _request(
        permissions={'store.access': True, 'management.access': False},
        principal=admin_without_store,
    )
    assert all(page.slug != 'customer-forms/exchanges-returns' for page in _visible_navigation(store_only_without_scope))


def test_store_scope_supports_all_and_multiple_valid_stores():
    principal = Principal(id=1, username='admin', role=Role.ADMIN, store_id=None, active=True)
    rows = [SimpleNamespace(id=10, name='North'), SimpleNamespace(id=20, name='South')]

    all_context = _store_scope_context(_request(), _Db(rows), principal)
    assert all_context['all_stores_selected'] is True
    assert all_context['store_scope_label'] == 'All Stores'

    multi_context = _store_scope_context(_request('store_id=10&store_id=20&store_id=999'), _Db(rows), principal)
    assert multi_context['selected_store_ids'] == [10, 20]
    assert multi_context['store_scope_label'] == '2 stores'


def test_invalid_store_scope_falls_back_to_all_stores():
    principal = Principal(id=1, username='lead', role=Role.LEAD, store_id=None, active=True)
    rows = [SimpleNamespace(id=10, name='North')]
    context = _store_scope_context(_request('store_id=not-an-id&store_id=999'), _Db(rows), principal)
    assert context['all_stores_selected'] is True
    assert context['selected_store_ids'] == []
