from __future__ import annotations

from collections.abc import Mapping


class SquareWriteBoundaryViolation(RuntimeError):
    """Raised before a request can cross Erupted Admin's Square write boundary."""


_READ_ONLY_POST_ENDPOINTS = frozenset(
    {
        '/v2/catalog/search',
        '/v2/catalog/search-catalog-items',
        '/v2/inventory/batch-retrieve-counts',
        '/v2/inventory/changes/batch-retrieve',
        '/v2/orders/search',
        '/v2/payments/search',
        '/v2/team-members/search',
        '/v2/vendors/search',
    }
)
_INVENTORY_QUANTITY_ENDPOINT = '/v2/inventory/changes/batch-create'
_INVENTORY_CHANGE_TYPES = frozenset({'ADJUSTMENT', 'PHYSICAL_COUNT'})


def enforce_square_request_policy(
    method: str,
    path: str,
    payload: Mapping | None = None,
    *,
    inventory_quantity_write: bool = False,
) -> None:
    """Fail closed unless a Square request is read-only or an explicit inventory write.

    POST is used by several Square read/search APIs, so method alone cannot express
    authority. Inventory mutations require both the one approved endpoint and an
    explicit capability at the established inventory workflow call site.
    """
    clean_method = str(method or '').strip().upper()
    clean_path = '/' + str(path or '').strip().lstrip('/')

    if clean_method == 'GET':
        return
    if clean_method == 'POST' and clean_path in _READ_ONLY_POST_ENDPOINTS:
        return
    if clean_method == 'POST' and clean_path == _INVENTORY_QUANTITY_ENDPOINT:
        if not inventory_quantity_write:
            raise SquareWriteBoundaryViolation(
                'Square inventory writes require an explicit inventory-quantity capability.'
            )
        _validate_inventory_quantity_payload(payload)
        return
    raise SquareWriteBoundaryViolation(
        f'Square {clean_method or "UNKNOWN"} {clean_path} is prohibited: '
        'Erupted Admin may only read Square data or write inventory quantity through '
        'an approved inventory workflow.'
    )


def _validate_inventory_quantity_payload(payload: Mapping | None) -> None:
    if not isinstance(payload, Mapping):
        raise SquareWriteBoundaryViolation('Square inventory write payload must be an object.')
    changes = payload.get('changes')
    if not isinstance(changes, list) or not changes:
        raise SquareWriteBoundaryViolation('Square inventory write must contain inventory changes.')
    for change in changes:
        if not isinstance(change, Mapping):
            raise SquareWriteBoundaryViolation('Square inventory changes must be objects.')
        change_type = str(change.get('type') or '').strip().upper()
        if change_type not in _INVENTORY_CHANGE_TYPES:
            raise SquareWriteBoundaryViolation(
                f'Square inventory change type {change_type or "UNKNOWN"} is prohibited.'
            )
        body_key = change_type.lower()
        body = change.get(body_key)
        if not isinstance(body, Mapping):
            raise SquareWriteBoundaryViolation(
                f'Square {change_type} inventory change is missing {body_key} data.'
            )
        if not str(body.get('catalog_object_id') or '').strip():
            raise SquareWriteBoundaryViolation('Square inventory change is missing catalog_object_id.')
        if not str(body.get('location_id') or '').strip():
            raise SquareWriteBoundaryViolation('Square inventory change is missing location_id.')
        if body.get('quantity') is None:
            raise SquareWriteBoundaryViolation('Square inventory change is missing quantity.')
