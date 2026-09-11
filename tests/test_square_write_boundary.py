from __future__ import annotations

from pathlib import Path

import pytest

from app.services.square_request_policy import (
    SquareWriteBoundaryViolation,
    enforce_square_request_policy,
)


@pytest.mark.parametrize(
    ('method', 'path'),
    [
        ('POST', '/v2/catalog/batch-upsert'),
        ('POST', '/v2/catalog/object'),
        ('DELETE', '/v2/catalog/object/ABC'),
        ('PUT', '/v2/vendors/ABC'),
        ('PATCH', '/v2/catalog/object/ABC'),
    ],
)
def test_catalog_product_vendor_mutations_fail_closed(method, path):
    with pytest.raises(SquareWriteBoundaryViolation):
        enforce_square_request_policy(method, path, {})


def test_inventory_endpoint_requires_explicit_quantity_capability():
    payload = {
        'changes': [{
            'type': 'PHYSICAL_COUNT',
            'physical_count': {
                'catalog_object_id': 'VAR-1',
                'location_id': 'LOC-1',
                'quantity': '3.000',
            },
        }]
    }
    with pytest.raises(SquareWriteBoundaryViolation):
        enforce_square_request_policy('POST', '/v2/inventory/changes/batch-create', payload)

    enforce_square_request_policy(
        'POST',
        '/v2/inventory/changes/batch-create',
        payload,
        inventory_quantity_write=True,
    )


def test_purchase_order_receiving_adjustment_remains_allowed():
    enforce_square_request_policy(
        'POST',
        '/v2/inventory/changes/batch-create',
        {
            'changes': [{
                'type': 'ADJUSTMENT',
                'adjustment': {
                    'catalog_object_id': 'VAR-1',
                    'location_id': 'LOC-1',
                    'quantity': '6.000',
                },
            }]
        },
        inventory_quantity_write=True,
    )


def test_inventory_capability_cannot_authorize_non_inventory_or_unknown_change():
    with pytest.raises(SquareWriteBoundaryViolation):
        enforce_square_request_policy(
            'POST',
            '/v2/catalog/batch-upsert',
            {'objects': []},
            inventory_quantity_write=True,
        )
    with pytest.raises(SquareWriteBoundaryViolation):
        enforce_square_request_policy(
            'POST',
            '/v2/inventory/changes/batch-create',
            {'changes': [{'type': 'CATALOG_UPDATE', 'catalog_update': {}}]},
            inventory_quantity_write=True,
        )


@pytest.mark.parametrize(
    'path',
    [
        '/v2/catalog/search',
        '/v2/catalog/search-catalog-items',
        '/v2/inventory/batch-retrieve-counts',
        '/v2/inventory/changes/batch-retrieve',
        '/v2/orders/search',
        '/v2/payments/search',
        '/v2/team-members/search',
        '/v2/vendors/search',
    ],
)
def test_known_post_based_square_reads_remain_allowed(path):
    enforce_square_request_policy('POST', path, {})


def test_every_raw_square_http_transport_invokes_central_policy():
    unguarded = []
    for path in sorted(Path('app').rglob('*.py')):
        source = path.read_text(encoding='utf-8')
        if 'urlopen(' not in source or 'square' not in source.lower():
            continue
        if 'enforce_square_request_policy' not in source:
            unguarded.append(str(path))
    assert unguarded == []


@pytest.mark.parametrize(
    'path',
    [
        'app/services/count_square_sync_service.py',
        'app/services/admin_store_count_service.py',
        'app/services/purchase_order_admin_service.py',
        'app/services/ordering_emergency_service.py',
    ],
)
def test_approved_inventory_workflows_request_explicit_quantity_capability(path):
    source = Path(path).read_text(encoding='utf-8')
    assert "'/v2/inventory/changes/batch-create'" in source
    assert 'inventory_quantity_write=True' in source


def test_physical_recount_and_auto_closeout_share_guarded_inventory_writer():
    source = Path('app/services/count_square_sync_service.py').read_text(encoding='utf-8')
    assert 'def push_session_variance_to_square' in source
    assert 'def push_session_recount_variance_to_square' in source
    assert 'def push_recount_closeout_rows_to_square' in source
    # Definition plus the shared session path and automatic closeout path.
    assert source.count('_push_rows_to_square(') == 3
