from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.services.v2_ordering_policy_service import DataFreshness, assess_freshness
from app.services.v2_ordering_square_gateway import READ_ENDPOINTS, SquareOrderingReadGateway


NOW = datetime(2026, 7, 25, 12, tzinfo=timezone.utc)


def _post(path, _payload):
    assert path in READ_ENDPOINTS
    if path == '/v2/catalog/search-catalog-items':
        return {
            'items': [
                {
                    'created_at': '2026-01-01T00:00:00Z',
                    'item_data': {
                        'name': 'Item',
                        'variations': [
                            {
                                'id': 'VAR-1',
                                'item_variation_data': {'sku': 'SKU-1', 'name': 'Variation'},
                            }
                        ],
                    },
                }
            ]
        }
    if path == '/v2/inventory/batch-retrieve-counts':
        return {
            'counts': [
                {
                    'location_id': 'LOC-1',
                    'catalog_object_id': 'VAR-1',
                    'quantity': '7',
                    'calculated_at': '2026-07-23T12:00:00Z',
                }
            ]
        }
    if path == '/v2/orders/search':
        return {
            'orders': [
                {
                    'location_id': 'LOC-1',
                    'closed_at': '2026-07-24T10:00:00Z',
                    'line_items': [{'catalog_object_id': 'VAR-1', 'quantity': '2'}],
                }
            ]
        }
    if path == '/v2/inventory/changes/batch-retrieve':
        return {
            'changes': [
                {
                    'type': 'ADJUSTMENT',
                    'adjustment': {
                        'location_id': 'LOC-1',
                        'catalog_object_id': 'VAR-1',
                        'quantity': '3',
                        'from_state': 'IN_STOCK',
                        'to_state': 'SOLD',
                        'occurred_at': '2026-07-24T09:00:00Z',
                    },
                }
            ]
        }
    raise AssertionError(path)


def test_gateway_uses_only_allowlisted_reads_and_preserves_timestamps():
    result = SquareOrderingReadGateway(_post).fetch(
        location_by_store={1: 'LOC-1'},
        variation_ids=['VAR-1'],
        as_of=NOW,
    )
    product = result.products['VAR-1']
    data = result.by_store_variation[(1, 'VAR-1')]
    assert product.item_name == 'Item'
    assert data.current_on_hand == Decimal('7')
    assert sum(row.quantity for row in data.daily_sales) == Decimal('2')
    assert sum(row.quantity for row in data.daily_inventory_deltas) == Decimal('-3')
    freshness = assess_freshness(data.required_sources, as_of=NOW)
    assert freshness.status == DataFreshness.STALE
    assert freshness.oldest_age_hours == 48
    assert result.metrics.request_count == 4
    assert result.metrics.inventory_count_variation_ids_submitted == 1
    assert result.metrics.inventory_change_variation_ids_submitted == 1
    assert result.metrics.inventory_change_page_count == 1
    assert result.metrics.inventory_changes_returned == 1
    assert dict(result.metrics.endpoint_request_counts) == {
        '/v2/catalog/search-catalog-items': 1,
        '/v2/inventory/batch-retrieve-counts': 1,
        '/v2/inventory/changes/batch-retrieve': 1,
        '/v2/orders/search': 1,
    }


def test_gateway_rejects_any_non_read_endpoint():
    gateway = SquareOrderingReadGateway(_post)
    try:
        gateway._read_post('/v2/inventory/batch-change', {})
    except ValueError as exc:
        assert 'not read-only' in str(exc)
    else:
        raise AssertionError('write endpoint was accepted')


def test_gateway_normalizes_partial_failure_without_hiding_sku():
    def failing_post(path, payload):
        if path == '/v2/orders/search':
            raise RuntimeError('sales unavailable')
        return _post(path, payload)

    result = SquareOrderingReadGateway(failing_post).fetch(
        location_by_store={1: 'LOC-1'},
        variation_ids=['VAR-1'],
        as_of=NOW,
    )
    data = result.by_store_variation[(1, 'VAR-1')]
    sales = next(source for source in data.required_sources if source.source == 'sales')
    assert sales.available is False
    assert sales.observed_at is None
    assert assess_freshness(data.required_sources, as_of=NOW).status == DataFreshness.CRITICAL


def test_catalog_identity_refresh_is_bulk_paginated_and_catalog_only():
    calls = []

    def paginated_post(path, payload):
        calls.append((path, dict(payload)))
        assert path == '/v2/catalog/search-catalog-items'
        index = len(calls)
        return {
            'items': [{
                'id': f'ITEM-{index}',
                'updated_at': '2026-07-25T10:00:00Z',
                'item_data': {
                    'name': f'Product {index}',
                    'variations': [{
                        'id': f'VAR-{index}',
                        'updated_at': '2026-07-25T11:00:00Z',
                        'item_variation_data': {'sku': f'SKU-{index}', 'name': 'Default'},
                    }],
                },
            }],
            **({'cursor': 'NEXT'} if index == 1 else {}),
        }

    result = SquareOrderingReadGateway(paginated_post).fetch_catalog_identity(['VAR-1', 'VAR-2'])

    assert set(result.products) == {'VAR-1', 'VAR-2'}
    assert result.products['VAR-1'].item_id == 'ITEM-1'
    assert result.products['VAR-1'].updated_at.isoformat() == '2026-07-25T11:00:00+00:00'
    assert result.metrics.request_count == 2
    assert dict(result.metrics.endpoint_request_counts) == {'/v2/catalog/search-catalog-items': 2}
    assert calls[0][1] == {'limit': 100}
    assert calls[1][1] == {'limit': 100, 'cursor': 'NEXT'}


def test_failed_catalog_identity_read_retains_attempt_metrics():
    def failed_post(path, _payload):
        assert path == '/v2/catalog/search-catalog-items'
        raise RuntimeError('unavailable')

    gateway = SquareOrderingReadGateway(failed_post)
    with pytest.raises(RuntimeError):
        gateway.fetch_catalog_identity(['VAR-1'])
    assert gateway.current_metrics().request_count == 1
    assert dict(gateway.current_metrics().endpoint_request_counts) == {
        '/v2/catalog/search-catalog-items': 1
    }
