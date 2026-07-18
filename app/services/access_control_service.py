from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    DashboardCategory,
    Principal as PrincipalModel,
    PrincipalPermissionOverride,
    PrincipalRole,
    RoleDashboardCategoryAccess,
    RolePermissionOverride,
    Store,
)


@dataclass(frozen=True)
class PermissionDef:
    key: str
    label: str
    description: str


CORE_PERMISSIONS: tuple[PermissionDef, ...] = (
    PermissionDef('management.access', 'Management Access', 'Can open the management dashboard and non-store tools.'),
    PermissionDef('management.admin', 'Admin Actions', 'Can run admin-only management actions.'),
    PermissionDef('management.groups', 'Manage Groups', 'Can open/manage count groups and store credentials.'),
    PermissionDef('management.users', 'Manage Users', 'Can manage users and access controls.'),
    PermissionDef('store.access', 'Store Access', 'Can access store workflows and forms.'),
)

NAVIGATION_PERMISSIONS: tuple[PermissionDef, ...] = (
    PermissionDef('nav.store_operations.all', 'All Store Operations Navigation', 'Shows all authorized Store Operations navigation items.'),
    PermissionDef('nav.store_operations.daily_chores', 'Daily Chore List Navigation', 'Shows Daily Chore List in Store Operations.'),
    PermissionDef('nav.store_operations.inventory_counts', 'Inventory Counts Navigation', 'Shows Inventory Counts in Store Operations.'),
    PermissionDef('nav.store_operations.non_sellable_counts', 'Non-Sellable Counts Navigation', 'Shows Non-Sellable Counts in Store Operations.'),
    PermissionDef('nav.store_operations.change_box_count', 'Change Box Count Navigation', 'Shows Change Box Count in Store Operations.'),
    PermissionDef('nav.store_operations.customer_requests', 'Customer Requests Navigation', 'Shows Customer Requests in Store Operations.'),
    PermissionDef('nav.store_operations.item_errors', 'Item Errors Navigation', 'Shows Item Errors in Store Operations.'),
    PermissionDef('nav.store_operations.customer_rewards_errors', 'Customer Rewards Errors Navigation', 'Shows Customer Rewards Errors in Store Operations.'),
    PermissionDef('nav.store_operations.repair_requests', 'Repair Requests Navigation', 'Shows Repair Requests in Store Operations.'),
    PermissionDef('nav.store_operations.exchange_forms', 'Exchange Forms Navigation', 'Shows Exchange Forms in Store Operations when exposed.'),
    PermissionDef('nav.inventory.all', 'All Inventory Navigation', 'Shows all authorized Inventory navigation items.'),
    PermissionDef('nav.inventory.ordering_tool', 'Ordering Tool Navigation', 'Shows Ordering Tool in Inventory.'),
    PermissionDef('nav.inventory.par_levels', 'Par / Level Manager Navigation', 'Shows Par / Level Manager in Inventory.'),
    PermissionDef('nav.inventory.vendor_skus', 'Vendor SKU Mappings Navigation', 'Shows Vendor SKU Mappings in Inventory.'),
    PermissionDef('nav.inventory.pdf_templates', 'PDF Templates Navigation', 'Shows PDF Templates in Inventory.'),
    PermissionDef('nav.inventory.current_orders', 'Current Orders Navigation', 'Shows Current Orders in Inventory.'),
    PermissionDef('nav.inventory.order_history', 'Order History Navigation', 'Shows Order History in Inventory.'),
    PermissionDef('nav.inventory.order_payments', 'Order Payments Navigation', 'Shows Order Payments in Inventory.'),
    PermissionDef('nav.reports.all', 'All Reports Navigation', 'Shows all authorized Reports navigation items.'),
    PermissionDef('nav.reports.cogs', 'COGS Report Navigation', 'Shows COGS Report in Reports.'),
    PermissionDef('nav.reports.stock_value', 'Stock Value Navigation', 'Shows Stock Value in Reports.'),
    PermissionDef('nav.reports.inventory_velocity', 'Inventory Velocity Navigation', 'Shows Inventory Velocity in Reports.'),
    PermissionDef('nav.reports.targeted_sku_demand', 'Targeted SKU Demand Navigation', 'Shows Targeted SKU Demand in Reports.'),
    PermissionDef('nav.reports.employee_recount_push', 'Employee Recount Push Navigation', 'Shows Employee Recount Push in Reports.'),
    PermissionDef('nav.reports.sales_transactions', 'Sales Transactions Navigation', 'Shows Sales Transactions in Reports.'),
    PermissionDef('nav.reports.gross_sales_store', 'Gross Sales by Store Navigation', 'Shows Gross Sales by Store in Reports.'),
    PermissionDef('nav.reports.sales_vendor', 'Sales by Vendor Navigation', 'Shows Sales by Vendor in Reports.'),
    PermissionDef('nav.reports.sales_employee', 'Sales by Employee Navigation', 'Shows Sales by Employee in Reports.'),
    PermissionDef('nav.reports.master_safe_change', 'Master Safe Change Usage Navigation', 'Shows Master Safe Change Usage in Reports.'),
    PermissionDef('nav.reports.customer_requests', 'Customer Requests Report Navigation', 'Shows Customer Requests in Reports.'),
    PermissionDef('nav.reports.exchange_forms', 'Exchange Forms Report Navigation', 'Shows Exchange Forms in Reports when exposed.'),
    PermissionDef('nav.scheduling.all', 'All Scheduling Navigation', 'Shows all authorized Scheduling navigation items.'),
    PermissionDef('nav.scheduling.board', 'Schedule Board Navigation', 'Shows Schedule Board in Scheduling.'),
    PermissionDef('nav.scheduling.shift_templates', 'Shift Templates Navigation', 'Shows Shift Templates in Scheduling.'),
    PermissionDef('nav.scheduling.availability', 'Employee Availability Navigation', 'Shows Employee Availability in Scheduling.'),
    PermissionDef('nav.scheduling.time_off', 'Time-Off Requests Navigation', 'Shows Time-Off Requests in Scheduling.'),
    PermissionDef('nav.scheduling.rules', 'Scheduling Rules Navigation', 'Shows Scheduling Rules in Scheduling.'),
    PermissionDef('nav.operation_settings.all', 'All Operation Settings Navigation', 'Shows all authorized Operation Settings navigation items.'),
    PermissionDef('nav.operation_settings.count_groups', 'Manage Count Groups Navigation', 'Shows Manage Count Groups in Operation Settings.'),
    PermissionDef('nav.operation_settings.employees', 'Employees Navigation', 'Shows Employees in Operation Settings.'),
    PermissionDef('nav.operation_settings.access_controls', 'Access Controls Navigation', 'Shows Access Controls in Operation Settings.'),
    PermissionDef('nav.operation_settings.daily_chore_editor', 'Daily Chore Editor Navigation', 'Shows Daily Chore Editor in Operation Settings.'),
    PermissionDef('nav.store_needs.all', 'All Store Needs Navigation', 'Shows all authorized Store Needs navigation items.'),
    PermissionDef('nav.store_needs.repair_requests', 'Store Needs Repair Requests Navigation', 'Shows Repair Requests in Store Needs.'),
    PermissionDef('nav.store_needs.change_unsellable', 'Store Change / Unsellable Needs Navigation', 'Shows Store Change / Unsellable Needs.'),
    PermissionDef('nav.store_needs.change_boxes', 'Change Boxes Navigation', 'Shows Change Boxes in Store Needs.'),
)

SCHEDULING_PERMISSIONS: tuple[PermissionDef, ...] = (
    PermissionDef('scheduling.view_own', 'View Own Schedule', 'Can view a schedule linked to the authenticated employee.'),
    PermissionDef('scheduling.time_off.submit_own', 'Submit Own Time Off', 'Can submit and cancel personal time-off requests when employee identity is linked.'),
    PermissionDef('scheduling.view_store', 'View Store Schedules', 'Can view schedules within an authorized store scope.'),
    PermissionDef('scheduling.view_all', 'View All Schedules', 'Can view schedules across all authorized stores.'),
    PermissionDef('scheduling.create_draft', 'Create Draft Schedules', 'Can create editable weekly schedule revisions.'),
    PermissionDef('scheduling.edit_draft_shifts', 'Edit Draft Shifts', 'Can create, move, reassign, and edit shifts in drafts.'),
    PermissionDef('scheduling.delete_draft_shifts', 'Delete Draft Shifts', 'Can delete shifts from draft schedules.'),
    PermissionDef('scheduling.copy', 'Copy Schedules', 'Can copy schedules and instantiate schedule templates.'),
    PermissionDef('scheduling.manage_shift_templates', 'Manage Shift Templates', 'Can create and maintain reusable shift templates.'),
    PermissionDef('scheduling.store_shifts.view', 'View Store Shifts', 'Can view reusable Store Shifts within authorized store scope.'),
    PermissionDef('scheduling.store_shifts.manage', 'Manage Store Shifts', 'Can create, edit, copy, activate, and reorder Store Shifts.'),
    PermissionDef('scheduling.store_shifts.place', 'Place Store Shifts', 'Can place reusable Store Shifts onto editable draft schedules.'),
    PermissionDef('scheduling.manage_schedule_templates', 'Manage Schedule Templates', 'Can create and maintain multiweek schedule templates.'),
    PermissionDef('scheduling.manage_preferences', 'Manage Scheduling Preferences', 'Can maintain employee scheduling profiles and store preferences.'),
    PermissionDef('scheduling.manage_availability', 'Manage Availability', 'Can maintain recurring employee availability windows.'),
    PermissionDef('scheduling.time_off.view', 'View Time Off', 'Can view management time-off records within authorized scope.'),
    PermissionDef('scheduling.time_off.review', 'Review Time Off', 'Can enter, approve, deny, and cancel management time-off records.'),
    PermissionDef('scheduling.manage_operating_hours', 'Manage Operating Hours', 'Can maintain ordinary store operating intervals.'),
    PermissionDef('scheduling.manage_special_hours', 'Manage Special Hours', 'Can maintain store holiday and special hours.'),
    PermissionDef('scheduling.manage_coverage', 'Manage Coverage Rules', 'Can maintain store staffing coverage requirements.'),
    PermissionDef('scheduling.view_labor_cost', 'View Labor Cost', 'Can view aggregate estimated scheduled labor cost.'),
    PermissionDef('scheduling.publish', 'Publish Schedules', 'Can publish a draft schedule with no serious warnings.'),
    PermissionDef('scheduling.modify_published', 'Modify Published Schedules', 'Can clone a published revision into a replacement draft.'),
    PermissionDef('scheduling.override_hard_unavailability', 'Override Hard Unavailability', 'Can explicitly schedule through hard unavailability.'),
    PermissionDef('scheduling.publish_with_warnings', 'Publish With Warnings', 'Can publish with serious warnings using confirmation and a reason.'),
)

PERMISSIONS = CORE_PERMISSIONS + NAVIGATION_PERMISSIONS + SCHEDULING_PERMISSIONS


def permission_defs() -> list[PermissionDef]:
    return list(PERMISSIONS)


FALLBACK_ROLE_SET_BY_PERMISSION: dict[str, set[PrincipalRole]] = {
    'management.access': {PrincipalRole.ADMIN, PrincipalRole.MANAGER, PrincipalRole.LEAD},
    'management.admin': {PrincipalRole.ADMIN, PrincipalRole.MANAGER},
    'management.groups': {PrincipalRole.ADMIN, PrincipalRole.MANAGER},
    'management.users': {PrincipalRole.ADMIN},
    'store.access': {PrincipalRole.STORE},
}

_ADMIN_MANAGER = {PrincipalRole.ADMIN, PrincipalRole.MANAGER}
_ALL_OPERATIONAL_ROLES = {
    PrincipalRole.ADMIN,
    PrincipalRole.MANAGER,
    PrincipalRole.LEAD,
    PrincipalRole.STORE,
}
for _permission in NAVIGATION_PERMISSIONS:
    if _permission.key.startswith('nav.store_operations.') or _permission.key.startswith('nav.store_needs.'):
        FALLBACK_ROLE_SET_BY_PERMISSION[_permission.key] = set(_ALL_OPERATIONAL_ROLES)
    elif _permission.key.startswith(('nav.inventory.', 'nav.reports.', 'nav.scheduling.')):
        FALLBACK_ROLE_SET_BY_PERMISSION[_permission.key] = set(_ADMIN_MANAGER)

for _permission in SCHEDULING_PERMISSIONS:
    # Self-service permissions are defined but intentionally default off until
    # employee/principal mapping and the self-service pages are operational.
    if _permission.key not in {'scheduling.view_own', 'scheduling.time_off.submit_own'}:
        FALLBACK_ROLE_SET_BY_PERMISSION[_permission.key] = set(_ADMIN_MANAGER)

FALLBACK_ROLE_SET_BY_PERMISSION.update(
    {
        'nav.operation_settings.all': {PrincipalRole.ADMIN},
        'nav.operation_settings.count_groups': set(_ADMIN_MANAGER),
        'nav.operation_settings.employees': {PrincipalRole.ADMIN},
        'nav.operation_settings.access_controls': {PrincipalRole.ADMIN},
        'nav.operation_settings.daily_chore_editor': set(_ADMIN_MANAGER),
    }
)


def _principal_role(role: PrincipalRole | str | Any) -> PrincipalRole:
    if isinstance(role, PrincipalRole):
        return role
    if hasattr(role, 'value'):
        return PrincipalRole(str(role.value).strip().upper())
    return PrincipalRole(str(role).strip().upper())


def _to_override_state(value: bool | None) -> str:
    if value is True:
        return 'ALLOW'
    if value is False:
        return 'DENY'
    return 'DEFAULT'


def fallback_allowed_for_role(*, role: PrincipalRole | str, permission_key: str) -> bool:
    clean_role = _principal_role(role)
    return clean_role in FALLBACK_ROLE_SET_BY_PERMISSION.get(str(permission_key), set())


def principal_has_permission(
    db: Session,
    *,
    principal: Any,
    permission_key: str,
    fallback_allowed: bool,
) -> bool:
    clean_key = str(permission_key or '').strip()
    if not clean_key:
        return fallback_allowed

    principal_override = db.execute(
        select(PrincipalPermissionOverride.allowed).where(
            PrincipalPermissionOverride.principal_id == principal.id,
            PrincipalPermissionOverride.permission_key == clean_key,
        )
    ).scalar_one_or_none()
    if principal_override is not None:
        return bool(principal_override)

    role_override = db.execute(
        select(RolePermissionOverride.allowed).where(
            RolePermissionOverride.role == _principal_role(principal.role),
            RolePermissionOverride.permission_key == clean_key,
        )
    ).scalar_one_or_none()
    if role_override is not None:
        return bool(role_override)

    return fallback_allowed


def effective_permission_flags(db: Session, *, principal: Any) -> dict[str, bool]:
    definitions = permission_defs()
    keys = [row.key for row in definitions]
    principal_rows = db.execute(
        select(
            PrincipalPermissionOverride.permission_key,
            PrincipalPermissionOverride.allowed,
        ).where(
            PrincipalPermissionOverride.principal_id == principal.id,
            PrincipalPermissionOverride.permission_key.in_(keys),
        )
    ).all()
    role_rows = db.execute(
        select(
            RolePermissionOverride.permission_key,
            RolePermissionOverride.allowed,
        ).where(
            RolePermissionOverride.role == _principal_role(principal.role),
            RolePermissionOverride.permission_key.in_(keys),
        )
    ).all()
    principal_map = {str(row.permission_key): bool(row.allowed) for row in principal_rows}
    role_map = {str(row.permission_key): bool(row.allowed) for row in role_rows}
    return {
        key: (
            principal_map[key]
            if key in principal_map
            else role_map[key]
            if key in role_map
            else fallback_allowed_for_role(role=principal.role, permission_key=key)
        )
        for key in keys
    }


def list_access_control_settings(db: Session) -> dict:
    defs = permission_defs()
    keys = [row.key for row in defs]
    roles = [PrincipalRole.ADMIN, PrincipalRole.MANAGER, PrincipalRole.LEAD, PrincipalRole.STORE]

    role_rows = db.execute(
        select(RolePermissionOverride).where(RolePermissionOverride.permission_key.in_(keys))
    ).scalars().all()
    role_override_map = {(row.role.value, row.permission_key): bool(row.allowed) for row in role_rows}

    principal_rows = db.execute(
        select(PrincipalModel, Store.name.label('store_name'))
        .outerjoin(Store, Store.id == PrincipalModel.store_id)
        .order_by(PrincipalModel.role.asc(), PrincipalModel.username.asc())
    ).all()

    principal_override_rows = db.execute(
        select(PrincipalPermissionOverride).where(PrincipalPermissionOverride.permission_key.in_(keys))
    ).scalars().all()
    principal_override_map = {
        (int(row.principal_id), row.permission_key): bool(row.allowed)
        for row in principal_override_rows
    }

    principals_out: list[dict] = []
    for principal, store_name in principal_rows:
        principal_id = int(principal.id)
        row = {
            'id': principal_id,
            'username': str(principal.username),
            'role': principal.role.value if hasattr(principal.role, 'value') else str(principal.role),
            'active': bool(principal.active),
            'store_name': str(store_name) if store_name else '',
            'custom_role_label': str(principal.custom_role_label or ''),
            'overrides': {},
        }
        for key in keys:
            row['overrides'][key] = _to_override_state(principal_override_map.get((principal_id, key)))
        principals_out.append(row)

    role_rows_out: list[dict] = []
    for role in roles:
        role_key = role.value
        role_row = {
            'role': role_key,
            'permissions': {},
        }
        for key in keys:
            role_row['permissions'][key] = bool(
                role_override_map.get((role_key, key), fallback_allowed_for_role(role=role, permission_key=key))
            )
        role_rows_out.append(role_row)

    return {
        'permission_defs': defs,
        'role_rows': role_rows_out,
        'principal_rows': principals_out,
    }


def save_role_permission_overrides(
    db: Session,
    *,
    actor_principal_id: int,
    allowed_map: dict[tuple[str, str], bool],
) -> None:
    defs = permission_defs()
    valid_keys = {row.key for row in defs}
    valid_roles = {role.value for role in [PrincipalRole.ADMIN, PrincipalRole.MANAGER, PrincipalRole.LEAD, PrincipalRole.STORE]}

    existing = db.execute(select(RolePermissionOverride)).scalars().all()
    existing_map = {(row.role.value, row.permission_key): row for row in existing}

    for (role_raw, permission_key), allowed in allowed_map.items():
        role_key = str(role_raw).strip().upper()
        if role_key not in valid_roles or permission_key not in valid_keys:
            continue
        row = existing_map.get((role_key, permission_key))
        if row is None:
            db.add(
                RolePermissionOverride(
                    role=PrincipalRole(role_key),
                    permission_key=permission_key,
                    allowed=bool(allowed),
                    updated_by_principal_id=actor_principal_id,
                )
            )
            continue
        row.allowed = bool(allowed)
        row.updated_by_principal_id = actor_principal_id
    db.flush()


def save_principal_permission_overrides(
    db: Session,
    *,
    actor_principal_id: int,
    override_map: dict[tuple[int, str], str],
    custom_role_labels: dict[int, str],
) -> None:
    defs = permission_defs()
    valid_keys = {row.key for row in defs}
    states = {'ALLOW', 'DENY', 'DEFAULT'}

    principals = db.execute(select(PrincipalModel)).scalars().all()
    principals_by_id = {int(row.id): row for row in principals}

    for principal_id, label in custom_role_labels.items():
        principal = principals_by_id.get(int(principal_id))
        if principal is None:
            continue
        clean = str(label or '').strip()
        principal.custom_role_label = clean or None

    existing = db.execute(select(PrincipalPermissionOverride)).scalars().all()
    existing_map = {(int(row.principal_id), row.permission_key): row for row in existing}

    for (principal_id_raw, permission_key), state in override_map.items():
        principal_id = int(principal_id_raw)
        if permission_key not in valid_keys or principal_id not in principals_by_id:
            continue
        state_clean = str(state or 'DEFAULT').strip().upper()
        if state_clean not in states:
            state_clean = 'DEFAULT'
        row = existing_map.get((principal_id, permission_key))
        if state_clean == 'DEFAULT':
            if row is not None:
                db.delete(row)
            continue
        allowed = state_clean == 'ALLOW'
        if row is None:
            db.add(
                PrincipalPermissionOverride(
                    principal_id=principal_id,
                    permission_key=permission_key,
                    allowed=allowed,
                    updated_by_principal_id=actor_principal_id,
                )
            )
            continue
        row.allowed = allowed
        row.updated_by_principal_id = actor_principal_id
    db.flush()


def list_role_dashboard_category_access(
    db: Session,
    *,
    role: str,
) -> dict:
    role_value = _principal_role(role)
    categories = db.execute(
        select(DashboardCategory)
        .where(DashboardCategory.active.is_(True))
        .order_by(DashboardCategory.position.asc(), DashboardCategory.name.asc(), DashboardCategory.id.asc())
    ).scalars().all()
    if not categories:
        return {'role': role_value.value, 'categories': []}

    category_ids = [int(row.id) for row in categories]
    rows = db.execute(
        select(RoleDashboardCategoryAccess).where(
            RoleDashboardCategoryAccess.role == role_value,
            RoleDashboardCategoryAccess.category_id.in_(category_ids),
        )
    ).scalars().all()
    by_category_id = {int(row.category_id): bool(row.allowed) for row in rows}

    out = []
    for category in categories:
        category_id = int(category.id)
        out.append(
            {
                'id': category_id,
                'name': str(category.name),
                'position': int(category.position),
                'allowed': bool(by_category_id.get(category_id, True)),
            }
        )
    return {'role': role_value.value, 'categories': out}


def save_role_dashboard_category_access(
    db: Session,
    *,
    role: str,
    actor_principal_id: int,
    allowed_by_category_id: dict[int, bool],
) -> None:
    role_value = _principal_role(role)
    categories = db.execute(
        select(DashboardCategory).where(DashboardCategory.active.is_(True))
    ).scalars().all()
    valid_ids = {int(row.id) for row in categories}

    existing = db.execute(
        select(RoleDashboardCategoryAccess).where(RoleDashboardCategoryAccess.role == role_value)
    ).scalars().all()
    existing_by_category = {int(row.category_id): row for row in existing}

    for category_id, allowed in allowed_by_category_id.items():
        clean_id = int(category_id)
        if clean_id not in valid_ids:
            continue
        row = existing_by_category.get(clean_id)
        if row is None:
            db.add(
                RoleDashboardCategoryAccess(
                    role=role_value,
                    category_id=clean_id,
                    allowed=bool(allowed),
                    updated_by_principal_id=actor_principal_id,
                )
            )
            continue
        row.allowed = bool(allowed)
        row.updated_by_principal_id = actor_principal_id
    db.flush()


def allowed_dashboard_category_ids_for_role(
    db: Session,
    *,
    role: PrincipalRole | str,
) -> set[int]:
    clean_role = _principal_role(role)
    categories = db.execute(
        select(DashboardCategory.id).where(DashboardCategory.active.is_(True))
    ).all()
    active_ids = {int(row.id) for row in categories}
    if not active_ids:
        return set()
    rows = db.execute(
        select(RoleDashboardCategoryAccess.category_id, RoleDashboardCategoryAccess.allowed)
        .where(
            RoleDashboardCategoryAccess.role == clean_role,
            RoleDashboardCategoryAccess.category_id.in_(sorted(active_ids)),
        )
    ).all()
    overrides = {int(row.category_id): bool(row.allowed) for row in rows}
    return {category_id for category_id in active_ids if overrides.get(category_id, True)}
