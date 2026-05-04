from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from app.services.square_ordering_data_service import (
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
            return _ExecuteResult(rows=[SimpleNamespace(sku='DUP-SKU', vendor_id=1)])
        raise AssertionError(f'unexpected execute call #{self.execute_count}')

    def add(self, row) -> None:
        self.added.append(row)

    def flush(self) -> None:
        self.flush_count += 1


class SquareOrderingDataServiceTests(unittest.TestCase):
    @patch(
        'app.services.square_ordering_data_service._active_vendor_square_map',
        return_value={'EIGHTCIG-SQUARE-ID': 2},
    )
    @patch('app.services.square_ordering_data_service._square_post')
    def test_vendor_scoped_sync_skips_sku_defaulted_to_other_vendor(
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

        self.assertEqual(result['created'], 0)
        self.assertEqual(result['skipped_conflict_default_vendor'], 1)
        self.assertEqual(db.added, [])
        self.assertEqual(db.flush_count, 0)

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
