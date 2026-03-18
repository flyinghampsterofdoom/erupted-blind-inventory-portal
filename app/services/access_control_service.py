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


PERMISSIONS: tuple[PermissionDef, ...] = (
    PermissionDef('management.access', 'Management Access', 'Can open the management dashboard and non-store tools.'),
    PermissionDef('management.admin', 'Admin Actions', 'Can run admin-only management actions.'),
    PermissionDef('management.groups', 'Manage Groups', 'Can open/manage count groups and store credentials.'),
    PermissionDef('management.users', 'Manage Users', 'Can manage users and access controls.'),
    PermissionDef('store.access', 'Store Access', 'Can access store workflows and forms.'),
)


def permission_defs() -> list[PermissionDef]:
    return list(PERMISSIONS)


FALLBACK_ROLE_SET_BY_PERMISSION: dict[str, set[PrincipalRole]] = {
    'management.access': {PrincipalRole.ADMIN, PrincipalRole.MANAGER, PrincipalRole.LEAD},
    'management.admin': {PrincipalRole.ADMIN, PrincipalRole.MANAGER},
    'management.groups': {PrincipalRole.ADMIN, PrincipalRole.MANAGER},
    'management.users': {PrincipalRole.ADMIN},
    'store.access': {PrincipalRole.STORE},
}


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
