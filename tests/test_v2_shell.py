from types import SimpleNamespace

from starlette.datastructures import QueryParams

from app.auth import Principal, Role
from app.config import settings
from app.routers.v2 import V2_PAGES, _store_scope_context, _visible_navigation
from app.services.access_control_service import permission_defs
from app.v2.navigation import NAVIGATION_REGISTRY


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


def _request(query='', permissions=None, principal=None, path='/v2/overview', current_store_id=None):
    return SimpleNamespace(
        query_params=QueryParams(query),
        url=SimpleNamespace(path=path),
        state=SimpleNamespace(
            permission_flags=permissions or {},
            principal=principal,
            current_store_id=current_store_id,
        ),
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


def test_navigation_registry_has_expected_section_order_and_store_operations_children():
    assert [section.label for section in NAVIGATION_REGISTRY] == [
        'Overview',
        'Store Operations',
        'Inventory',
        'Reports',
        'Scheduling',
        'Operation Settings',
        'Store Needs',
    ]
    store_operations = next(section for section in NAVIGATION_REGISTRY if section.key == 'store_operations')
    assert [child.label for child in store_operations.children] == [
        'Daily Chore List',
        'Inventory Counts',
        'Non-Sellable Counts',
        'Change Box Count',
        'Customer Requests',
        'Item Errors',
        'Customer Rewards Errors',
        'Repair Requests',
        'Exchange Forms',
    ]
    assert all(child.label != 'Daily Store Log' for child in store_operations.children)


def test_navigation_registry_keys_permissions_and_order_are_centralized():
    permission_keys = {row.key for row in permission_defs()}
    section_keys = [section.key for section in NAVIGATION_REGISTRY]
    child_keys = [child.key for section in NAVIGATION_REGISTRY for child in section.children]
    assert len(section_keys) == len(set(section_keys))
    assert len(child_keys) == len(set(child_keys))
    assert [section.order for section in NAVIGATION_REGISTRY] == sorted(
        section.order for section in NAVIGATION_REGISTRY
    )
    for section in NAVIGATION_REGISTRY:
        assert [child.order for child in section.children] == sorted(
            child.order for child in section.children
        )
        if section.all_children_permission:
            assert section.all_children_permission in permission_keys
        assert all(child.permission in permission_keys for child in section.children)


def test_partial_reports_and_operation_settings_visibility():
    request = _request(
        permissions={
            'nav.reports.cogs': True,
            'nav.reports.inventory_velocity': True,
            'nav.reports.sales_employee': True,
            'nav.operation_settings.daily_chore_editor': True,
        }
    )
    sections = _visible_navigation(request)
    assert [section.label for section in sections] == ['Reports', 'Operation Settings']
    reports = sections[0]
    assert [child.label for child in reports.children] == [
        'COGS Report',
        'Inventory Velocity',
        'Sales by Employee',
    ]
    assert [child.label for child in sections[1].children] == ['Daily Chore Editor']


def test_broad_permission_scheduling_visibility_empty_sections_and_active_state(monkeypatch):
    monkeypatch.setattr(settings, 'v2_enabled_features', 'exchanges_returns_v2')
    principal = Principal(id=4, username='admin', role=Role.ADMIN, store_id=None, active=True)
    request = _request(
        permissions={
            'management.access': True,
            'nav.reports.all': True,
            'nav.scheduling.all': True,
        },
        principal=principal,
        path='/v2/customer-forms/exchanges-returns/history',
    )
    sections = _visible_navigation(request)
    assert [section.label for section in sections] == ['Overview', 'Reports', 'Scheduling']
    reports = next(section for section in sections if section.key == 'reports')
    assert len(reports.children) == 12
    assert reports.active is True and reports.expanded is True
    scheduling = next(section for section in sections if section.key == 'scheduling')
    assert [child.label for child in scheduling.children] == [
        'Schedule Board',
        'Shift Templates',
        'Employee Availability',
        'Time-Off Requests',
        'Scheduling Rules',
    ]
    assert all(section.key != 'inventory' for section in sections)


def test_exchange_navigation_uses_effective_permissions_feature_and_context(monkeypatch):
    monkeypatch.setattr(settings, 'v2_enabled_features', 'exchanges_returns_v2')
    store_principal = Principal(id=3, username='store', role=Role.STORE, store_id=10, active=True)
    management_only = _request(
        permissions={
            'store.access': False,
            'management.access': True,
            'nav.store_operations.exchange_forms': True,
        },
        principal=store_principal,
    )
    section = next(row for row in _visible_navigation(management_only) if row.key == 'store_operations')
    module = next(child for child in section.children if child.label == 'Exchange Forms')
    assert module.href == '/v2/customer-forms/exchanges-returns/history?nav=store-operations'

    admin_without_store = Principal(id=4, username='admin', role=Role.ADMIN, store_id=None, active=True)
    store_only_without_scope = _request(
        permissions={
            'store.access': True,
            'management.access': False,
            'nav.store_operations.exchange_forms': True,
        },
        principal=admin_without_store,
    )
    sections = _visible_navigation(store_only_without_scope)
    assert all(
        child.label != 'Exchange Forms'
        for section in sections
        for child in section.children
    )


def test_current_store_context_does_not_expand_navigation_permissions():
    permissions = {'nav.reports.cogs': True}
    without_store = _visible_navigation(_request(permissions=permissions, current_store_id=None))
    with_store = _visible_navigation(_request(permissions=permissions, current_store_id=999))
    assert without_store == with_store


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
