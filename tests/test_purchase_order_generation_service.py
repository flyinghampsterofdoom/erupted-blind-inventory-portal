from __future__ import annotations

import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from app.services.purchase_order_generation_service import generate_vendor_scoped_recommendations
from app.services.purchase_order_math_service import OrderingMathParams


class PurchaseOrderGenerationServiceTests(unittest.TestCase):
    @patch('app.services.purchase_order_generation_service.resolve_effective_math_params')
    @patch('app.services.purchase_order_generation_service._par_levels_by_vendor_sku')
    @patch('app.services.purchase_order_generation_service._open_in_transit_by_vendor_store_sku')
    @patch('app.services.purchase_order_generation_service.list_selected_vendor_skus')
    @patch('app.services.purchase_order_generation_service._active_store_ids')
    def test_zero_on_hand_forces_minimum_one(
        self,
        active_store_ids_mock,
        selected_vendor_skus_mock,
        in_transit_mock,
        par_levels_mock,
        resolve_params_mock,
    ) -> None:
        active_store_ids_mock.return_value = [1]
        selected_vendor_skus_mock.return_value = {
            10: [SimpleNamespace(sku='SKU-1', pack_size=6, min_order_qty=0)],
        }
        in_transit_mock.return_value = {}
        par_levels_mock.return_value = {}
        resolve_params_mock.return_value = OrderingMathParams(reorder_weeks=5, stock_up_weeks=10, history_lookback_days=30)

        result = generate_vendor_scoped_recommendations(
            db=SimpleNamespace(),
            vendor_ids=[10],
            history_loader=lambda _vendor_id, _store_id, _sku, _lookback_days: [Decimal('0')] * 30,
            on_hand_loader=lambda _store_id, _sku: Decimal('0'),
            overrides=None,
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].result.rounded_recommended_qty, 1)
        self.assertEqual(result[0].result.raw_recommended_qty, 1)

    @patch('app.services.purchase_order_generation_service.resolve_effective_math_params')
    @patch('app.services.purchase_order_generation_service._par_levels_by_vendor_sku')
    @patch('app.services.purchase_order_generation_service._open_in_transit_by_vendor_store_sku')
    @patch('app.services.purchase_order_generation_service.list_selected_vendor_skus')
    @patch('app.services.purchase_order_generation_service._active_store_ids')
    def test_non_zero_on_hand_does_not_force_minimum_one(
        self,
        active_store_ids_mock,
        selected_vendor_skus_mock,
        in_transit_mock,
        par_levels_mock,
        resolve_params_mock,
    ) -> None:
        active_store_ids_mock.return_value = [1]
        selected_vendor_skus_mock.return_value = {
            10: [SimpleNamespace(sku='SKU-1', pack_size=1, min_order_qty=0)],
        }
        in_transit_mock.return_value = {}
        par_levels_mock.return_value = {}
        resolve_params_mock.return_value = OrderingMathParams(reorder_weeks=5, stock_up_weeks=10, history_lookback_days=30)

        result = generate_vendor_scoped_recommendations(
            db=SimpleNamespace(),
            vendor_ids=[10],
            history_loader=lambda _vendor_id, _store_id, _sku, _lookback_days: [Decimal('0')] * 30,
            on_hand_loader=lambda _store_id, _sku: Decimal('2'),
            overrides=None,
        )

        self.assertEqual(result, [])


if __name__ == '__main__':
    unittest.main()
