from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from app.services.square_ordering_data_service import (
    fetch_catalog_by_sku,
    fetch_sales_volume_by_variation,
    sync_vendor_sku_configs_from_square,
)


class _ScalarResult:
    def __init__(self, rows: list):
        self._rows = rows

    def all(self) -> list:
        return self._rows


class _ExecuteResult:
    def __init__(self, *, scalar_rows: list | None = None, rows: list | None = None):
        self._scalar_rows = scalar_rows or []
        self._rows = rows or []

    def scalars(self) -> _ScalarResult:
        return _ScalarResult(self._scalar_rows)

    def all(self) -> list:
        return self._rows


class _FakeDb:
    def __init__(self):
        self.execute_count = 0
        self.added = []
        self.flush_count = 0

    def execute(self, _query) -> _ExecuteResult:
        self.execute_count += 1
        if self.execute_count == 1:
            return _ExecuteResult(scalar_rows=[])
        if self.execute_count == 2:
            return _ExecuteResult(scalar_rows=[])
        raise AssertionError(f'unexpected execute call #{self.execute_count}')

    def add(self, row) -> None:
        self.added.append(row)

    def flush(self) -> None:
        self.flush_count += 1


class _ExistingMappingDb(_FakeDb):
    def __init__(self, existing):
        super().__init__()
        self.existing = existing

    def execute(self, _query) -> _ExecuteResult:
        self.execute_count += 1
        if self.execute_count == 1:
            return _ExecuteResult(scalar_rows=[self.existing])
        if self.execute_count == 2:
            return _ExecuteResult(scalar_rows=[])
        raise AssertionError(f'unexpected execute call #{self.execute_count}')


class SquareOrderingDataServiceTests(unittest.TestCase):
    @patch('app.services.square_ordering_data_service.fetch_catalog_variation_maps')
    def test_catalog_only_sku_metadata_never_promotes_square_cost_to_local_cost(
        self,
        catalog_maps_mock,
    ) -> None:
        catalog_maps_mock.return_value = ({}, {
            'SKU-1': SimpleNamespace(
                variation_id='VAR-1',
                gtin=None,
                item_name='Item',
                variation_name='Default',
                first_vendor_unit_cost=Decimal('15.00'),
                unit_price=Decimal('20.00'),
            )
        })

        result = fetch_catalog_by_sku()

        self.assertIsNone(result['SKU-1'].unit_cost)

    @patch(
        'app.services.square_ordering_data_service._active_vendor_square_map',
        return_value={'JUICEHEAD-SQUARE-ID': 7},
    )
    @patch('app.services.square_ordering_data_service._square_post')
    def test_square_cost_never_changes_existing_local_unit_cost(
        self,
        square_post_mock,
        _active_vendor_square_map_mock,
    ) -> None:
        cost_variants = {
            'missing vendor cost': {
                'item_variation_vendor_infos': [
                    {'item_variation_vendor_info_data': {'vendor_id': 'JUICEHEAD-SQUARE-ID'}}
                ]
            },
            'vendor block absent': {
                'item_variation_vendor_info_ids': ['unresolved-info-id'],
            },
            'zero': {
                'item_variation_vendor_infos': [
                    {'item_variation_vendor_info_data': {
                        'vendor_id': 'JUICEHEAD-SQUARE-ID', 'price_money': {'amount': 0}
                    }}
                ]
            },
            'lower': {
                'item_variation_vendor_infos': [
                    {'item_variation_vendor_info_data': {
                        'vendor_id': 'JUICEHEAD-SQUARE-ID', 'price_money': {'amount': 1250}
                    }}
                ]
            },
            'equal': {
                'item_variation_vendor_infos': [
                    {'item_variation_vendor_info_data': {
                        'vendor_id': 'JUICEHEAD-SQUARE-ID', 'price_money': {'amount': 1308}
                    }}
                ]
            },
            'higher': {
                'item_variation_vendor_infos': [
                    {'item_variation_vendor_info_data': {
                        'vendor_id': 'JUICEHEAD-SQUARE-ID', 'price_money': {'amount': 1500}
                    }}
                ]
            },
            'malformed': {
                'item_variation_vendor_infos': [
                    {'item_variation_vendor_info_data': {
                        'vendor_id': 'JUICEHEAD-SQUARE-ID', 'price_money': {'amount': 'not-money'}
                    }}
                ]
            },
        }
        for label, vendor_fields in cost_variants.items():
            with self.subTest(label=label):
                existing = SimpleNamespace(
                    id=1,
                    vendor_id=7,
                    sku='JUICEHEAD-SKU',
                    square_variation_id='VAR-JUICEHEAD',
                    gtin='GTIN-JUICEHEAD',
                    unit_cost=Decimal('13.08'),
                    pack_size=1,
                    min_order_qty=0,
                    active=True,
                    is_default_vendor=True,
                    updated_at=None,
                )
                vdata = {
                    'sku': existing.sku,
                    'upc': existing.gtin,
                    **vendor_fields,
                }
                # Preserve an explicit assignment for the block-absent case.
                if label == 'vendor block absent':
                    vdata['item_variation_vendor_infos'] = [
                        {'id': 'unresolved-info-id', 'item_variation_vendor_info_data': {
                            'vendor_id': 'JUICEHEAD-SQUARE-ID'
                        }}
                    ]
                square_post_mock.return_value = {
                    'items': [{'item_data': {'variations': [{
                        'id': existing.square_variation_id,
                        'item_variation_data': vdata,
                    }]}}]
                }
                db = _ExistingMappingDb(existing)

                result = sync_vendor_sku_configs_from_square(db, vendor_ids=[7])

                self.assertEqual(existing.unit_cost, Decimal('13.08'))
                self.assertEqual(result['updated'], 0)
                self.assertEqual(db.flush_count, 0)

    @patch(
        'app.services.square_ordering_data_service._active_vendor_square_map',
        return_value={'JUICEHEAD-SQUARE-ID': 7},
    )
    @patch('app.services.square_ordering_data_service._square_post')
    def test_new_square_mapping_keeps_unknown_cost_as_none(
        self,
        square_post_mock,
        _active_vendor_square_map_mock,
    ) -> None:
        square_post_mock.return_value = {
            'items': [{'item_data': {'variations': [{
                'id': 'NEW-VAR',
                'item_variation_data': {
                    'sku': 'NEW-SKU',
                    'item_variation_vendor_infos': [
                        {'item_variation_vendor_info_data': {
                            'vendor_id': 'JUICEHEAD-SQUARE-ID',
                            'price_money': {'amount': 1500},
                        }}
                    ],
                },
            }]}}]
        }
        db = _FakeDb()

        result = sync_vendor_sku_configs_from_square(db, vendor_ids=[7])

        self.assertEqual(result['created'], 1)
        self.assertIsNone(db.added[0].unit_cost)
    @patch(
        'app.services.square_ordering_data_service._active_vendor_square_map',
        return_value={'EIGHTCIG-SQUARE-ID': 2},
    )
    @patch('app.services.square_ordering_data_service._square_post')
    def test_vendor_scoped_sync_creates_mapping_when_no_local_mapping_exists(
        self,
        square_post_mock,
        _active_vendor_square_map_mock,
    ) -> None:
        square_post_mock.return_value = {
            'items': [
                {
                    'item_data': {
                        'variations': [
                            {
                                'id': 'SQUARE-VARIATION-1',
                                'item_variation_data': {
                                    'sku': 'DUP-SKU',
                                    'item_variation_vendor_infos': [
                                        {
                                            'item_variation_vendor_info_data': {
                                                'vendor_id': 'EIGHTCIG-SQUARE-ID',
                                                'ordinal': 1,
                                                'price_money': {'amount': 1234},
                                            }
                                        }
                                    ],
                                },
                            }
                        ],
                    }
                }
            ]
        }
        db = _FakeDb()

        result = sync_vendor_sku_configs_from_square(db, vendor_ids=[2])

        self.assertEqual(result['created'], 1)
        self.assertEqual(result['vendor_reassigned'], 0)
        self.assertEqual(len(db.added), 1)
        self.assertEqual(db.flush_count, 2)

    @patch(
        'app.services.square_ordering_data_service._active_store_location_map',
        return_value={1: 'LOC-1', 2: 'LOC-2', 3: 'LOC-3'},
    )
    @patch('app.services.square_ordering_data_service._fetch_daily_sales')
    def test_fetch_sales_volume_by_variation_sums_selected_variations_across_stores(
        self,
        fetch_daily_sales_mock,
        _active_store_location_map_mock,
    ) -> None:
        fetch_daily_sales_mock.return_value = {
            ('LOC-1', 'VAR-1', date(2026, 5, 1)): Decimal('2'),
            ('LOC-2', 'VAR-1', date(2026, 5, 1)): Decimal('3.5'),
            ('LOC-3', 'VAR-1', date(2026, 5, 1)): Decimal('99'),
            ('LOC-1', 'VAR-2', date(2026, 5, 1)): Decimal('4'),
            ('LOC-1', 'IGNORED', date(2026, 5, 1)): Decimal('10'),
        }

        result = fetch_sales_volume_by_variation(
            SimpleNamespace(),
            variation_ids=['VAR-1', 'VAR-2'],
            lookback_days=30,
            store_ids=[1, 2],
        )

        self.assertEqual(result, {'VAR-1': Decimal('5.5'), 'VAR-2': Decimal('4')})
        args = fetch_daily_sales_mock.call_args.args
        self.assertEqual(args[0], ['LOC-1', 'LOC-2'])


if __name__ == '__main__':
    unittest.main()
