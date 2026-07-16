from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import Principal, Role
from app.models import Store


class ScopeMode(str, Enum):
    ASSIGNED = 'assigned'
    SINGLE = 'single'
    MULTIPLE = 'multiple'
    ALL = 'all'


@dataclass(frozen=True)
class ScopedStore:
    id: int
    name: str


@dataclass(frozen=True)
class ResolvedStoreScope:
    stores: tuple[ScopedStore, ...]
    mode: ScopeMode
    locked: bool
    write_compatible: bool

    @property
    def store_ids(self) -> tuple[int, ...]:
        return tuple(store.id for store in self.stores)

    @property
    def store_names(self) -> tuple[str, ...]:
        return tuple(store.name for store in self.stores)


def _parse_requested_store_ids(request: Request) -> tuple[int, ...]:
    values = request.query_params.getlist('store_id')
    parsed: list[int] = []
    for value in values:
        try:
            store_id = int(value)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='Invalid store_id') from exc
        if store_id not in parsed:
            parsed.append(store_id)
    return tuple(parsed)


def resolve_store_scope(
    *,
    principal: Principal,
    authorized_stores: list[ScopedStore] | tuple[ScopedStore, ...],
    requested_store_ids: tuple[int, ...] = (),
    request_all: bool = False,
    for_write: bool = False,
) -> ResolvedStoreScope:
    authorized = tuple(authorized_stores)
    authorized_by_id = {store.id: store for store in authorized}
    unauthorized = [store_id for store_id in requested_store_ids if store_id not in authorized_by_id]
    if unauthorized:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Requested store scope is not authorized',
        )

    if principal.role == Role.STORE:
        if principal.store_id is None or principal.store_id not in authorized_by_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Assigned store is unavailable')
        if requested_store_ids and requested_store_ids != (principal.store_id,):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Assigned store scope is locked')
        selected = (authorized_by_id[principal.store_id],)
        return ResolvedStoreScope(selected, ScopeMode.ASSIGNED, True, True)

    if requested_store_ids and request_all:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='Use either scope=all or store_id values, not both',
        )
    if request_all or not requested_store_ids:
        selected = authorized
        mode = ScopeMode.ALL
    else:
        selected = tuple(authorized_by_id[store_id] for store_id in requested_store_ids)
        mode = ScopeMode.SINGLE if len(selected) == 1 else ScopeMode.MULTIPLE
    if not selected:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='No authorized stores are available')
    if for_write and len(selected) != 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='This write requires exactly one resolved store',
        )
    return ResolvedStoreScope(selected, mode, False, len(selected) == 1)


def resolve_request_store_scope(
    request: Request,
    db: Session,
    principal: Principal,
    *,
    for_write: bool = False,
) -> ResolvedStoreScope:
    authorized_stores = list_authorized_stores(db, principal)
    scope_value = request.query_params.get('scope')
    if scope_value not in {None, '', 'all'}:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='Invalid scope')
    return resolve_store_scope(
        principal=principal,
        authorized_stores=authorized_stores,
        requested_store_ids=_parse_requested_store_ids(request),
        request_all=scope_value == 'all',
        for_write=for_write,
    )


def list_authorized_stores(db: Session, principal: Principal) -> list[ScopedStore]:
    query = select(Store.id, Store.name).where(Store.active.is_(True))
    if principal.role == Role.STORE:
        query = query.where(Store.id == principal.store_id)
    rows = db.execute(query.order_by(Store.name.asc(), Store.id.asc())).all()
    return [ScopedStore(id=int(row.id), name=str(row.name)) for row in rows]
