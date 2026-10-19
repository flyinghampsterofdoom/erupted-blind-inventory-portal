from datetime import date, datetime, timezone
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
    if path in {'/v2/inventory/batch-retrieve-counts', '/v2/inventory/counts/batch-retrieve'}:
        return {
            'counts': [
                {
                    'location_id': 'LOC-1',
                    'catalog_object_id': 'VAR-1',
                    'quantity': '7',
                    'state': 'IN_STOCK',
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
    assert freshness.status == DataFreshness.FRESH
    assert freshness.oldest_age_hours == 0
    assert data.source_calculated_at == datetime(2026, 7, 23, 12, tzinfo=timezone.utc)
    assert result.metrics.request_count == 4
    assert result.metrics.inventory_count_variation_ids_submitted == 1
    assert result.metrics.inventory_change_variation_ids_submitted == 1
    assert result.metrics.inventory_change_page_count == 1
    assert result.metrics.inventory_changes_returned == 1
    assert dict(result.metrics.endpoint_request_counts) == {
        '/v2/catalog/search-catalog-items': 1,
        '/v2/inventory/counts/batch-retrieve': 1,
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


def test_current_inventory_read_is_chunked_paginated_and_never_synthesizes_missing_zero():
    calls = []

    def inventory_post(path, payload):
        calls.append((path, dict(payload)))
        assert path == '/v2/inventory/counts/batch-retrieve'
        if len(calls) == 1:
            return {
                'counts': [{
                    'location_id': 'LOC-1', 'catalog_object_id': 'VAR-0000',
                    'state': 'IN_STOCK', 'quantity': '0', 'calculated_at': '2026-07-25T10:00:00Z',
                }],
                'cursor': 'NEXT',
            }
        if len(calls) == 2:
            return {
                'counts': [{
                    'location_id': 'LOC-1', 'catalog_object_id': 'VAR-0000',
                    'state': 'IN_STOCK', 'quantity': '2', 'calculated_at': '2026-07-25T11:00:00Z',
                }]
            }
        return {
            'counts': [{
                'location_id': 'LOC-2', 'catalog_object_id': 'VAR-1000',
                'state': 'IN_STOCK', 'quantity': '-1.5', 'calculated_at': '2026-07-25T11:30:00Z',
            }]
        }

    variations = [f'VAR-{index:04d}' for index in range(1001)]
    result = SquareOrderingReadGateway(inventory_post).fetch_current_inventory_counts(
        location_ids=['LOC-1', 'LOC-2'], variation_ids=variations,
    )

    assert len(calls) == 3
    assert len(calls[0][1]['catalog_object_ids']) == 1000
    assert calls[1][1]['cursor'] == 'NEXT'
    assert len(calls[2][1]['catalog_object_ids']) == 1
    assert result.metrics.request_count == 3
    assert result.counts[('LOC-1', 'VAR-0000')].quantity == Decimal('2')
    assert result.counts[('LOC-2', 'VAR-1000')].quantity == Decimal('-1.5')
    assert ('LOC-2', 'VAR-0001') not in result.counts


@pytest.mark.parametrize('fraction,micros',[('7',700000),('79',790000),('790',790000),
    ('7901',790100),('79012',790120),('790123',790123),('790123456',790123)])
def test_square_rfc3339_fractional_precision_is_not_missing_history(fraction,micros):
    from app.services.v2_ordering_square_gateway import _parse_datetime
    assert _parse_datetime(f'2026-08-05T20:42:07.{fraction}Z')==datetime(2026,8,5,20,42,7,micros,tzinfo=timezone.utc)
    assert _parse_datetime(f'2026-08-05T13:42:07.{fraction}-07:00')==datetime(2026,8,5,20,42,7,micros,tzinfo=timezone.utc)
    assert _parse_datetime('2026-99-05T20:42:07.79Z') is None
    assert _parse_datetime('2026') is None
    assert _parse_datetime('2026-08') is None
    assert _parse_datetime('20260805') is None


def test_real_history_timestamp_shape_retains_adjustment_and_physical_count_uncertainty():
    def read(path,payload):
        assert path=='/v2/inventory/changes/batch-retrieve'
        return {'changes':[
            {'type':'ADJUSTMENT','adjustment':{'id':'historical-adjustment','location_id':'LOC',
                'catalog_object_id':'VAR','quantity':'3','from_state':'NONE','to_state':'IN_STOCK',
                'occurred_at':'2026-08-05T20:42:07.79Z'}},
            {'type':'PHYSICAL_COUNT','physical_count':{'location_id':'LOC','catalog_object_id':'OTHER'}}]}
    gateway=SquareOrderingReadGateway(read)
    result=gateway._inventory_deltas(['LOC'],['VAR','OTHER'],as_of=datetime(2026,10,18,tzinfo=timezone.utc),history_days=84,include_today=True)
    assert result[('LOC','VAR',date(2026,8,5))]==3
    assert gateway._uncertain_history_pairs=={('LOC','OTHER')}
