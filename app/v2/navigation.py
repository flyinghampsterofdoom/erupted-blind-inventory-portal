from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from app.v2.feature_exposure import FeatureExposure


COMING_LATER = 'coming_later'


@dataclass(frozen=True)
class NavigationChildDef:
    key: str
    label: str
    order: int
    permission: str
    route_kind: str = ''
    route_path: str | None = None
    active_prefix: str | None = None
    feature_key: str | None = None
    required_permissions: tuple[str, ...] = ()
    any_permissions: tuple[str, ...] = ()
    required_context: str = ''
    placeholder_mode: str = COMING_LATER
    context_label: str = ''
    helper_text: str = ''


@dataclass(frozen=True)
class NavigationSectionDef:
    key: str
    label: str
    order: int
    all_children_permission: str | None
    children: tuple[NavigationChildDef, ...]
    landing_route_kind: str = ''
    landing_feature_key: str | None = None
    landing_permissions: tuple[str, ...] = ()
    active_prefixes: tuple[str, ...] = ()


@dataclass(frozen=True)
class NavigationChild:
    key: str
    label: str
    href: str | None
    active: bool
    available: bool
    context_label: str = ''
    helper_text: str = ''


@dataclass(frozen=True)
class NavigationSection:
    key: str
    label: str
    landing_href: str | None
    children: tuple[NavigationChild, ...]
    active: bool
    expanded: bool


def _child(
    key: str,
    label: str,
    order: int,
    permission: str,
    *,
    route_kind: str = '',
    route_path: str | None = None,
    active_prefix: str | None = None,
    feature_key: str | None = None,
    required_permissions: tuple[str, ...] = (),
    any_permissions: tuple[str, ...] = (),
    required_context: str = '',
    context_label: str = '',
    helper_text: str = '',
) -> NavigationChildDef:
    return NavigationChildDef(
        key=key,
        label=label,
        order=order,
        permission=permission,
        route_kind=route_kind,
        route_path=route_path,
        active_prefix=active_prefix,
        feature_key=feature_key,
        required_permissions=required_permissions,
        any_permissions=any_permissions,
        required_context=required_context,
        placeholder_mode='' if route_kind or route_path else COMING_LATER,
        context_label=context_label,
        helper_text=helper_text,
    )


NAVIGATION_REGISTRY: tuple[NavigationSectionDef, ...] = (
    NavigationSectionDef(
        key='overview',
        label='Overview',
        order=10,
        all_children_permission=None,
        children=(
            _child(
                'overview.operations',
                'Operations Overview',
                10,
                'management.access',
                route_path='/v2/overview',
                active_prefix='/v2/overview',
            ),
        ),
        active_prefixes=('/v2/overview',),
    ),
    NavigationSectionDef(
        key='store_operations',
        label='Store Operations',
        order=20,
        all_children_permission='nav.store_operations.all',
        landing_route_kind='store_operations_landing',
        landing_feature_key='daily_store_logs_v2',
        landing_permissions=('store.access', 'management.access'),
        active_prefixes=('/v2/store-operations', '/v2/current-store'),
        children=(
            _child(
                'store_operations.daily_store_log',
                'Daily Store Log',
                5,
                'store.access',
                route_path='/v2/store-operations/daily-logs',
                active_prefix='/v2/store-operations/daily-logs',
                feature_key='daily_store_logs_v2',
                required_permissions=('store.access',),
            ),
            _child('store_operations.daily_chores', 'Daily Chore List', 10, 'nav.store_operations.daily_chores'),
            _child('store_operations.inventory_counts', 'Inventory Counts', 20, 'nav.store_operations.inventory_counts'),
            _child('store_operations.non_sellable_counts', 'Non-Sellable Counts', 30, 'nav.store_operations.non_sellable_counts'),
            _child('store_operations.change_box_count', 'Change Box Count', 40, 'nav.store_operations.change_box_count'),
            _child('store_operations.customer_requests', 'Customer Requests', 50, 'nav.store_operations.customer_requests'),
            _child('store_operations.item_errors', 'Item Errors', 60, 'nav.store_operations.item_errors'),
            _child(
                'store_operations.customer_rewards_errors',
                'Customer Rewards Errors',
                70,
                'nav.store_operations.customer_rewards_errors',
            ),
            _child('store_operations.repair_requests', 'Repair Requests', 80, 'nav.store_operations.repair_requests'),
            _child(
                'store_operations.exchange_forms',
                'Exchange Forms',
                90,
                'nav.store_operations.exchange_forms',
                route_kind='exchange_forms',
                active_prefix='/v2/customer-forms/exchanges-returns',
                feature_key='exchanges_returns_v2',
                required_context='assigned_store_for_employee',
            ),
        ),
    ),
    NavigationSectionDef(
        key='inventory',
        label='Inventory',
        order=30,
        all_children_permission='nav.inventory.all',
        children=(
            _child(
                'inventory.ordering_tool',
                'Ordering Tool',
                10,
                'nav.inventory.ordering_tool',
                route_path='/management/ordering-tool',
                active_prefix='/management/ordering-tool',
                feature_key='ordering_v1_links_v2',
                required_permissions=('management.admin',),
                context_label='Existing V1',
                helper_text='Opens current production tool',
            ),
            _child(
                'inventory.par_levels',
                'Par / Level Manager',
                20,
                'nav.inventory.par_levels',
                route_path='/management/ordering-tool/par-levels',
                active_prefix='/management/ordering-tool/par-levels',
                feature_key='ordering_v1_links_v2',
                required_permissions=('management.admin',),
                context_label='Existing V1',
                helper_text='Opens current production tool',
            ),
            _child(
                'inventory.vendor_skus',
                'Vendor SKU Mappings',
                30,
                'nav.inventory.vendor_skus',
                route_path='/management/ordering-tool/mappings',
                active_prefix='/management/ordering-tool/mappings',
                feature_key='ordering_v1_links_v2',
                required_permissions=('management.admin',),
                context_label='Existing V1',
                helper_text='Opens current production tool',
            ),
            _child(
                'inventory.pdf_templates',
                'PDF Templates',
                40,
                'nav.inventory.pdf_templates',
                route_path='/management/ordering-tool/pdf-templates',
                active_prefix='/management/ordering-tool/pdf-templates',
                feature_key='ordering_v1_links_v2',
                required_permissions=('management.admin',),
                context_label='Existing V1',
                helper_text='Opens current production tool',
            ),
            _child('inventory.current_orders', 'Current Orders', 50, 'nav.inventory.current_orders'),
            _child('inventory.order_history', 'Order History', 60, 'nav.inventory.order_history'),
            _child('inventory.order_payments', 'Order Payments', 70, 'nav.inventory.order_payments'),
        ),
        active_prefixes=('/v2/inventory', '/v2/ordering', '/management/ordering-tool'),
    ),
    NavigationSectionDef(
        key='reports',
        label='Reports',
        order=40,
        all_children_permission='nav.reports.all',
        children=(
            _child('reports.cogs', 'COGS Report', 10, 'nav.reports.cogs'),
            _child('reports.stock_value', 'Stock Value', 20, 'nav.reports.stock_value'),
            _child('reports.inventory_velocity', 'Inventory Velocity', 30, 'nav.reports.inventory_velocity'),
            _child('reports.targeted_sku_demand', 'Targeted SKU Demand', 40, 'nav.reports.targeted_sku_demand'),
            _child('reports.employee_recount_push', 'Employee Recount Push', 50, 'nav.reports.employee_recount_push'),
            _child('reports.sales_transactions', 'Sales Transactions', 60, 'nav.reports.sales_transactions'),
            _child('reports.gross_sales_store', 'Gross Sales by Store', 70, 'nav.reports.gross_sales_store'),
            _child('reports.sales_vendor', 'Sales by Vendor', 80, 'nav.reports.sales_vendor'),
            _child('reports.sales_employee', 'Sales by Employee', 90, 'nav.reports.sales_employee'),
            _child('reports.master_safe_change', 'Master Safe Change Usage', 100, 'nav.reports.master_safe_change'),
            _child('reports.customer_requests', 'Customer Requests', 110, 'nav.reports.customer_requests'),
            _child(
                'reports.exchange_forms',
                'Exchange Forms',
                120,
                'nav.reports.exchange_forms',
                route_kind='exchange_forms_management',
                active_prefix='/v2/customer-forms/exchanges-returns',
                feature_key='exchanges_returns_v2',
            ),
        ),
        active_prefixes=('/v2/reports',),
    ),
    NavigationSectionDef(
        key='scheduling',
        label='Scheduling',
        order=50,
        all_children_permission='nav.scheduling.all',
        children=(
            _child(
                'scheduling.board',
                'Schedule Board',
                10,
                'nav.scheduling.board',
                route_path='/v2/scheduling/week',
                active_prefix='/v2/scheduling/week',
                feature_key='staff_scheduling_v2',
                any_permissions=('scheduling.view_all', 'scheduling.view_store'),
            ),
            _child('scheduling.shift_templates', 'Shift Templates', 20, 'nav.scheduling.shift_templates'),
            _child('scheduling.availability', 'Employee Availability', 30, 'nav.scheduling.availability'),
            _child('scheduling.time_off', 'Time-Off Requests', 40, 'nav.scheduling.time_off'),
            _child('scheduling.rules', 'Scheduling Rules', 50, 'nav.scheduling.rules'),
        ),
    ),
    NavigationSectionDef(
        key='operation_settings',
        label='Operation Settings',
        order=60,
        all_children_permission='nav.operation_settings.all',
        children=(
            _child('operation_settings.count_groups', 'Manage Count Groups', 10, 'nav.operation_settings.count_groups'),
            _child('operation_settings.employees', 'Employees', 20, 'nav.operation_settings.employees'),
            _child('operation_settings.access_controls', 'Access Controls', 30, 'nav.operation_settings.access_controls'),
            _child(
                'operation_settings.daily_chore_editor',
                'Daily Chore Editor',
                40,
                'nav.operation_settings.daily_chore_editor',
            ),
        ),
    ),
    NavigationSectionDef(
        key='touchscreen',
        label='Touchscreen',
        order=64,
        all_children_permission='nav.touchscreen.all',
        active_prefixes=('/v2/touchscreen',),
        children=(
            _child('touchscreen.flavors', 'Flavors', 10, 'touchscreen.view', route_path='/v2/touchscreen/flavors', active_prefix='/v2/touchscreen/flavors', feature_key='touchscreen_v2', required_permissions=('touchscreen.view',)),
            _child('touchscreen.categories', 'Categories', 20, 'touchscreen.view', route_path='/v2/touchscreen/categories', active_prefix='/v2/touchscreen/categories', feature_key='touchscreen_v2', required_permissions=('touchscreen.view',)),
            _child('touchscreen.devices', 'Devices', 30, 'touchscreen.view', route_path='/v2/touchscreen/devices', active_prefix='/v2/touchscreen/devices', feature_key='touchscreen_v2', required_permissions=('touchscreen.view',)),
            _child('touchscreen.preview', 'Store Preview', 40, 'touchscreen.preview', route_path='/v2/touchscreen/preview', active_prefix='/v2/touchscreen/preview', feature_key='touchscreen_v2', required_permissions=('touchscreen.preview',)),
            _child('touchscreen.sync', 'Square Cache', 50, 'touchscreen.view', route_path='/v2/touchscreen/sync', active_prefix='/v2/touchscreen/sync', feature_key='touchscreen_v2', required_permissions=('touchscreen.view',)),
        ),
    ),
    NavigationSectionDef(
        key='digital_signage',
        label='Digital Signage',
        order=65,
        all_children_permission='nav.digital_signage.all',
        active_prefixes=('/v2/digital-signage',),
        children=(
            _child(
                'digital_signage.groups',
                'Advertisement Groups',
                10,
                'digital_signage.view',
                route_path='/v2/digital-signage/groups',
                active_prefix='/v2/digital-signage/groups',
                feature_key='digital_signage_v2',
                required_permissions=('digital_signage.view',),
            ),
            _child(
                'digital_signage.media',
                'Media Library',
                20,
                'digital_signage.view',
                route_path='/v2/digital-signage/media',
                active_prefix='/v2/digital-signage/media',
                feature_key='digital_signage_v2',
                required_permissions=('digital_signage.view',),
            ),
            _child(
                'digital_signage.displays',
                'TV Displays',
                30,
                'digital_signage.view',
                route_path='/v2/digital-signage/displays',
                active_prefix='/v2/digital-signage/displays',
                feature_key='digital_signage_v2',
                required_permissions=('digital_signage.view',),
            ),
        ),
    ),
    NavigationSectionDef(
        key='store_needs',
        label='Store Needs',
        order=70,
        all_children_permission='nav.store_needs.all',
        children=(
            _child('store_needs.repair_requests', 'Repair Requests', 10, 'nav.store_needs.repair_requests'),
            _child(
                'store_needs.change_unsellable',
                'Store Change / Unsellable Needs',
                20,
                'nav.store_needs.change_unsellable',
            ),
            _child('store_needs.change_boxes', 'Change Boxes', 30, 'nav.store_needs.change_boxes'),
        ),
    ),
)


def _feature_enabled(exposure: FeatureExposure, feature_key: str | None, principal_id: int | None) -> bool:
    return feature_key is None or exposure.enabled(feature_key, principal_id=principal_id)


def _route_for_kind(kind: str, flags: dict[str, bool]) -> str | None:
    management = flags.get('management.access', False)
    store = flags.get('store.access', False)
    if kind == 'store_operations_landing':
        if management:
            return '/v2/store-operations/daily-logs/history'
        if store:
            return '/v2/store-operations'
    if kind == 'exchange_forms':
        if management:
            return '/v2/customer-forms/exchanges-returns/history?nav=store-operations'
        if store:
            return '/v2/customer-forms/exchanges-returns'
    if kind == 'exchange_forms_management' and management:
        return '/v2/customer-forms/exchanges-returns/history?nav=reports'
    return None


def _context_allows(
    required_context: str,
    *,
    flags: dict[str, bool],
    principal,
) -> bool:
    if required_context == 'assigned_store_for_employee':
        return bool(
            flags.get('management.access', False)
            or (
                flags.get('store.access', False)
                and principal is not None
                and principal.store_id is not None
            )
        )
    return True


def build_navigation(request: Request) -> list[NavigationSection]:
    flags = getattr(request.state, 'permission_flags', {}) or {}
    principal = getattr(request.state, 'principal', None)
    principal_id = principal.id if principal is not None else None
    exposure = FeatureExposure.from_settings()
    path = getattr(getattr(request, 'url', None), 'path', '')
    nav_context = getattr(request, 'query_params', {}).get('nav', '')
    sections: list[NavigationSection] = []

    for section_def in sorted(NAVIGATION_REGISTRY, key=lambda row: row.order):
        broad_allowed = bool(
            section_def.all_children_permission
            and flags.get(section_def.all_children_permission, False)
        )
        children: list[NavigationChild] = []
        for child_def in sorted(section_def.children, key=lambda row: row.order):
            if not (broad_allowed or flags.get(child_def.permission, False)):
                continue
            if not _feature_enabled(exposure, child_def.feature_key, principal_id):
                continue
            if not all(flags.get(key, False) for key in child_def.required_permissions):
                continue
            if child_def.any_permissions and not any(
                flags.get(key, False) for key in child_def.any_permissions
            ):
                continue
            if not _context_allows(
                child_def.required_context,
                flags=flags,
                principal=principal,
            ):
                continue
            href = child_def.route_path or _route_for_kind(child_def.route_kind, flags)
            available = bool(href)
            if not available and child_def.placeholder_mode != COMING_LATER:
                continue
            active_prefix = child_def.active_prefix or href or ''
            active = bool(active_prefix and (path == active_prefix or path.startswith(f'{active_prefix}/')))
            if child_def.key == 'store_operations.exchange_forms' and flags.get('management.access', False):
                active = active and nav_context == 'store-operations'
            elif child_def.key == 'reports.exchange_forms':
                active = active and nav_context in {'', 'reports'}
            children.append(
                NavigationChild(
                    key=child_def.key,
                    label=child_def.label,
                    href=href,
                    active=active,
                    available=available,
                    context_label=child_def.context_label,
                    helper_text=child_def.helper_text,
                )
            )

        landing_allowed = any(flags.get(key, False) for key in section_def.landing_permissions)
        landing_href = None
        if (
            landing_allowed
            and _feature_enabled(exposure, section_def.landing_feature_key, principal_id)
        ):
            landing_href = _route_for_kind(section_def.landing_route_kind, flags)

        if not children and not landing_href:
            continue
        section_active = any(child.active for child in children) or any(
            path == prefix or path.startswith(f'{prefix}/')
            for prefix in section_def.active_prefixes
        )
        sections.append(
            NavigationSection(
                key=section_def.key,
                label=section_def.label,
                landing_href=landing_href,
                children=tuple(children),
                active=section_active,
                expanded=section_active,
            )
        )
    return sections
