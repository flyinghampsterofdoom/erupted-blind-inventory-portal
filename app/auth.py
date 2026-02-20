from dataclasses import dataclass
from enum import Enum

from fastapi import Depends, HTTPException, Request, status


class Role(str, Enum):
    MANAGER = "MANAGER"
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


def require_role(*allowed: Role):
    def _dep(principal: Principal = Depends(get_current_principal)) -> Principal:
        if principal.role not in allowed:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
        return principal

    return _dep


def assert_store_scope(principal: Principal, target_store_id: int) -> None:
    if principal.role == Role.MANAGER:
        return
    if principal.store_id != target_store_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
