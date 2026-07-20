from pathlib import Path
from types import SimpleNamespace

from starlette.datastructures import QueryParams

from app.auth import Principal, Role
from app.config import settings
from app.routers.v2 import V2_PAGES, _store_scope_context, _visible_navigation
from app.routers.management import admin_access
from app.services.access_control_service import permission_defs
from app.v2.navigation import NAVIGATION_REGISTRY


ORDERING_BRIDGE_FEATURE = 'ordering_v1_links_v2'
ORDERING_BRIDGE_DESTINATIONS = {
    'Ordering Tool': '/management/ordering-tool',
    'Par / Level Manager': '/management/ordering-tool/par-levels',
    'Vendor SKU Mappings': '/management/ordering-tool/mappings',
    'PDF Templates': '/management/ordering-tool/pdf-templates',
}
UNAVAILABLE_ORDERING_CHILDREN = {'Current Orders', 'Order History', 'Order Payments'}


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
        'Touchscreen',
        'Digital Signage',
        'Store Needs',
    ]
    store_operations = next(section for section in NAVIGATION_REGISTRY if section.key == 'store_operations')
    assert [child.label for child in store_operations.children] == [
        'Daily Store Log',
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
    daily_log = store_operations.children[0]
    assert daily_log.route_path == '/v2/store-operations/daily-logs'
    assert daily_log.feature_key == 'daily_store_logs_v2'
    assert daily_log.required_permissions == ('store.access',)


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
        assert all(
            permission in permission_keys
            for child in section.children
            for permission in child.required_permissions
        )
        assert all(
            permission in permission_keys
            for child in section.children
            for permission in child.any_permissions
        )


def test_ordering_navigation_bridge_is_disabled_by_default(monkeypatch):
    monkeypatch.setattr(settings, 'v2_enabled_features', '')
    monkeypatch.setattr(settings, 'v2_principal_features', '')
    request = _request(
        permissions={
            'management.admin': True,
            'nav.inventory.all': True,
        },
        principal=Principal(id=4, username='admin', role=Role.ADMIN, store_id=None, active=True),
    )
    inventory = next(section for section in _visible_navigation(request) if section.key == 'inventory')
    assert all(child.label not in ORDERING_BRIDGE_DESTINATIONS for child in inventory.children)
    unavailable = {child.label: child for child in inventory.children}
    assert set(unavailable) == UNAVAILABLE_ORDERING_CHILDREN
    assert all(not child.available and child.href is None for child in unavailable.values())


def test_ordering_navigation_bridge_exposes_exact_v1_destinations(monkeypatch):
    monkeypatch.setattr(settings, 'v2_enabled_features', ORDERING_BRIDGE_FEATURE)
    monkeypatch.setattr(settings, 'v2_principal_features', '')
    request = _request(
        permissions={
            'management.admin': True,
            'nav.inventory.all': True,
        },
        principal=Principal(id=4, username='admin', role=Role.ADMIN, store_id=None, active=True),
    )
    inventory = next(section for section in _visible_navigation(request) if section.key == 'inventory')
    children = {child.label: child for child in inventory.children}
    for label, href in ORDERING_BRIDGE_DESTINATIONS.items():
        assert children[label].available is True
        assert children[label].href == href
        assert children[label].context_label == 'Existing V1'
        assert children[label].helper_text == 'Opens current production tool'
    for label in UNAVAILABLE_ORDERING_CHILDREN:
        assert children[label].available is False
        assert children[label].href is None


def test_ordering_bridge_visibility_uses_effective_permissions(monkeypatch):
    monkeypatch.setattr(settings, 'v2_enabled_features', ORDERING_BRIDGE_FEATURE)
    principal = Principal(id=7, username='lead', role=Role.LEAD, store_id=None, active=True)
    one_child = _visible_navigation(
        _request(
            permissions={
                'management.admin': True,
                'nav.inventory.par_levels': True,
            },
            principal=principal,
        )
    )
    inventory = next(section for section in one_child if section.key == 'inventory')
    assert [(child.label, child.href) for child in inventory.children] == [
        ('Par / Level Manager', '/management/ordering-tool/par-levels')
    ]

    restricted_lead = _visible_navigation(
        _request(
            permissions={
                'management.access': True,
                'management.admin': False,
                'nav.inventory.all': True,
            },
            principal=principal,
        )
    )
    inventory = next(section for section in restricted_lead if section.key == 'inventory')
    assert all(child.label not in ORDERING_BRIDGE_DESTINATIONS for child in inventory.children)
    assert all(not child.available for child in inventory.children)


def test_ordering_bridge_unauthorized_users_and_current_store_do_not_gain_links(monkeypatch):
    monkeypatch.setattr(settings, 'v2_enabled_features', ORDERING_BRIDGE_FEATURE)
    principal = Principal(id=3, username='store', role=Role.STORE, store_id=10, active=True)
    permissions = {
        'store.access': True,
        'management.admin': False,
        'nav.inventory.ordering_tool': True,
    }
    without_store = _visible_navigation(
        _request(permissions=permissions, principal=principal, current_store_id=None)
    )
    with_store = _visible_navigation(
        _request(permissions=permissions, principal=principal, current_store_id=999)
    )
    assert without_store == with_store
    assert all(section.key != 'inventory' for section in with_store)


def test_ordering_bridge_is_static_navigation_without_v2_data_or_square_module():
    inventory = next(section for section in NAVIGATION_REGISTRY if section.key == 'inventory')
    bridge_children = [
        child for child in inventory.children if child.label in ORDERING_BRIDGE_DESTINATIONS
    ]
    assert {child.label: child.route_path for child in bridge_children} == ORDERING_BRIDGE_DESTINATIONS
    assert all(child.feature_key == ORDERING_BRIDGE_FEATURE for child in bridge_children)
    assert all(child.required_permissions == ('management.admin',) for child in bridge_children)
    assert all(not child.route_kind and not child.required_context for child in bridge_children)

    root = Path(__file__).resolve().parents[1]
    feature_owners = [
        path.relative_to(root).as_posix()
        for path in (root / 'app').rglob('*.py')
        if ORDERING_BRIDGE_FEATURE in path.read_text(encoding='utf-8')
    ]
    assert feature_owners == ['app/v2/navigation.py']
    assert not (root / 'app/routers/v2_ordering.py').exists()
    assert not list((root / 'app/services').glob('v2_ordering*.py'))


def test_daily_store_log_navigation_matches_feature_and_store_access(monkeypatch):
    monkeypatch.setattr(settings, 'v2_enabled_features', 'daily_store_logs_v2')
    monkeypatch.setattr(settings, 'v2_principal_features', '')
    store_principal = Principal(id=3, username='store', role=Role.STORE, store_id=10, active=True)
    store_sections = _visible_navigation(
        _request(
            permissions={'store.access': True, 'nav.store_operations.all': True},
            principal=store_principal,
            path='/v2/store-operations/daily-logs',
        )
    )
    section = next(row for row in store_sections if row.key == 'store_operations')
    child = next(row for row in section.children if row.label == 'Daily Store Log')
    assert child.href == '/v2/store-operations/daily-logs'
    assert child.available is True and child.active is True

    management_sections = _visible_navigation(
        _request(
            permissions={'management.access': True, 'nav.store_operations.all': True},
            principal=Principal(id=4, username='admin', role=Role.ADMIN, store_id=None, active=True),
        )
    )
    management_section = next(row for row in management_sections if row.key == 'store_operations')
    assert all(row.label != 'Daily Store Log' for row in management_section.children)


def test_mobile_navigation_and_preview_presentation_contracts_are_present():
    root = Path(__file__).resolve().parents[1]
    base = (root / 'app/templates/v2/base.html').read_text(encoding='utf-8')
    css = (root / 'app/static/v2/v2.css').read_text(encoding='utf-8')
    assert 'V2 Owner Preview' in base
    assert 'Coming Soon' in base
    assert 'section.key == \'store_operations\'' in base
    assert '@media (max-width: 760px)' in css
    assert '.is-drawer-open .v2-sidebar' in css
    assert '.v2-preview-banner { align-items: flex-start; flex-direction: column;' in css
    assert '.v2-access-actions { align-items: stretch; flex-direction: column;' in css


def test_v1_ordering_bridge_destinations_remain_direct_get_routes_with_admin_access():
    from app.main import app

    routes = {
        route.path: route
        for route in app.routes
        if getattr(route, 'path', '') in set(ORDERING_BRIDGE_DESTINATIONS.values())
    }
    assert set(routes) == set(ORDERING_BRIDGE_DESTINATIONS.values())
    for route in routes.values():
        assert 'GET' in route.methods
        dependency_calls = [dependency.call for dependency in route.dependant.dependencies]
        assert admin_access in dependency_calls


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
        'Shift Templates',
        'Employee Availability',
        'Time-Off Requests',
        'Scheduling Rules',
    ]
    assert all(section.key != 'inventory' for section in sections)


def test_schedule_board_navigation_requires_feature_and_board_capability(monkeypatch):
    monkeypatch.setattr(settings, 'v2_enabled_features', 'staff_scheduling_v2')
    principal = Principal(id=4, username='admin', role=Role.ADMIN, store_id=None, active=True)
    request = _request(
        permissions={
            'nav.scheduling.all': True,
            'scheduling.view_all': True,
        },
        principal=principal,
        path='/v2/scheduling/week',
    )
    scheduling = next(section for section in _visible_navigation(request) if section.key == 'scheduling')
    board = next(child for child in scheduling.children if child.label == 'Schedule Board')
    assert board.available is True
    assert board.href == '/v2/scheduling/week'
    assert board.active is True


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
