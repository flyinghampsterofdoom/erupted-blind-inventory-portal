from dataclasses import dataclass
from enum import Enum

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.services.access_control_service import principal_has_permission


class Role(str, Enum):
    ADMIN = "ADMIN"
    MANAGER = "MANAGER"
    LEAD = "LEAD"
    STORE = "STORE"


@dataclass
class Principal:
    id: int
    username: str
    role: Role
    store_id: int | None
    active: bool


def get_current_principal(request: Request) -> Principal:
    principal = getattr(request.state, "principal", None)
    if not principal:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    if not principal.active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    return principal


def is_admin_role(role: Role) -> bool:
    # Keep MANAGER as a supported legacy admin role.
    return role in {Role.ADMIN, Role.MANAGER}


def require_role(*allowed: Role):
    def _dep(principal: Principal = Depends(get_current_principal)) -> Principal:
        if principal.role not in allowed:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
        return principal

    return _dep


def require_capability(permission_key: str, *fallback_roles: Role):
    fallback_set = {role for role in fallback_roles}

    def _dep(
        principal: Principal = Depends(get_current_principal),
        db: Session = Depends(get_db),
    ) -> Principal:
        fallback_allowed = principal.role in fallback_set
        if not principal_has_permission(
            db,
            principal=principal,
            permission_key=permission_key,
            fallback_allowed=fallback_allowed,
        ):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
        return principal

    return _dep


def assert_store_scope(principal: Principal, target_store_id: int) -> None:
    if principal.role != Role.STORE:
        return
    if principal.store_id != target_store_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
