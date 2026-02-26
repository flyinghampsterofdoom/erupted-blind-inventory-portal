from __future__ import annotations

import unittest
from decimal import Decimal

from app.models import ParLevelSource, PurchaseOrderConfidenceState
from app.services.purchase_order_math_service import (
    LineMathInput,
    MathOverrides,
    OrderingMathParams,
    compute_line_recommendation,
    resolve_math_params,
)


class PurchaseOrderMathServiceTests(unittest.TestCase):
    def test_resolve_math_params_uses_defaults(self) -> None:
        defaults = OrderingMathParams(reorder_weeks=5, stock_up_weeks=10, history_lookback_days=120)
        resolved = resolve_math_params(defaults, None)
        self.assertEqual(resolved.reorder_weeks, 5)
        self.assertEqual(resolved.stock_up_weeks, 10)
        self.assertEqual(resolved.history_lookback_days, 120)

    def test_resolve_math_params_applies_overrides(self) -> None:
        defaults = OrderingMathParams(reorder_weeks=5, stock_up_weeks=10, history_lookback_days=120)
        overrides = MathOverrides(reorder_weeks=4, stock_up_weeks=9, history_lookback_days=60)
        resolved = resolve_math_params(defaults, overrides)
        self.assertEqual(resolved.reorder_weeks, 4)
        self.assertEqual(resolved.stock_up_weeks, 9)
        self.assertEqual(resolved.history_lookback_days, 60)

    def test_manual_par_takes_precedence_as_reorder_floor(self) -> None:
        params = OrderingMathParams(reorder_weeks=5, stock_up_weeks=10, history_lookback_days=120)
        line = LineMathInput(
            sku='SKU-1',
            current_on_hand=Decimal('0'),
            in_transit_qty=0,
            history_daily_units=[Decimal('0.5')] * 120,  # avg weekly 3.5
            unit_pack_size=1,
            min_order_qty=0,
            manual_level=50,
            manual_par=60,
            par_source=ParLevelSource.MANUAL,
        )
        result = compute_line_recommendation(line, params)
        self.assertEqual(result.suggested_reorder_level, 18)
        self.assertEqual(result.effective_reorder_level, 50)
        self.assertEqual(result.effective_stock_up_level, 60)
        self.assertEqual(result.rounded_recommended_qty, 60)

    def test_in_transit_is_subtracted_before_rounding(self) -> None:
        params = OrderingMathParams(reorder_weeks=5, stock_up_weeks=10, history_lookback_days=120)
        line = LineMathInput(
            sku='SKU-2',
            current_on_hand=Decimal('10'),
            in_transit_qty=20,
            history_daily_units=[Decimal('1')] * 120,  # avg weekly 7, stock-up target 70
            unit_pack_size=5,
            min_order_qty=0,
        )
        result = compute_line_recommendation(line, params)
        self.assertEqual(result.suggested_stock_up_level, 70)
        self.assertEqual(result.raw_recommended_qty, 40)  # 70 - (10 + 20)
        self.assertEqual(result.rounded_recommended_qty, 40)

    def test_pack_rounding_and_minimum_order_qty(self) -> None:
        params = OrderingMathParams(reorder_weeks=5, stock_up_weeks=10, history_lookback_days=120)
        line = LineMathInput(
            sku='SKU-3',
            current_on_hand=Decimal('60'),
            in_transit_qty=0,
            history_daily_units=[Decimal('1')] * 120,  # stock-up target 70
            unit_pack_size=6,
            min_order_qty=11,
            manual_level=65,
            manual_par=70,
            par_source=ParLevelSource.MANUAL,
        )
        result = compute_line_recommendation(line, params)
        self.assertEqual(result.raw_recommended_qty, 11)
        self.assertEqual(result.rounded_recommended_qty, 12)

    def test_above_reorder_level_does_not_order(self) -> None:
        params = OrderingMathParams(reorder_weeks=5, stock_up_weeks=10, history_lookback_days=120)
        line = LineMathInput(
            sku='SKU-6',
            current_on_hand=Decimal('4'),
            in_transit_qty=0,
            history_daily_units=[Decimal('0.1')] * 120,  # reorder ~4, stock-up ~7
            unit_pack_size=1,
            min_order_qty=0,
            manual_level=3,
            manual_par=5,
            par_source=ParLevelSource.MANUAL,
        )
        result = compute_line_recommendation(line, params)
        self.assertEqual(result.effective_reorder_level, 3)
        self.assertEqual(result.effective_stock_up_level, 5)
        self.assertEqual(result.raw_recommended_qty, 0)
        self.assertEqual(result.rounded_recommended_qty, 0)

    def test_low_signal_history_yields_low_confidence(self) -> None:
        params = OrderingMathParams(reorder_weeks=5, stock_up_weeks=10, history_lookback_days=120)
        line = LineMathInput(
            sku='SKU-4',
            current_on_hand=Decimal('0'),
            in_transit_qty=0,
            history_daily_units=[Decimal('0')] * 10,
            unit_pack_size=1,
            min_order_qty=0,
        )
        result = compute_line_recommendation(line, params)
        self.assertLess(result.confidence_score, Decimal('0.80'))
        self.assertEqual(result.confidence_state, PurchaseOrderConfidenceState.LOW)

    def test_average_uses_full_lookback_window(self) -> None:
        params = OrderingMathParams(reorder_weeks=5, stock_up_weeks=10, history_lookback_days=10)
        # 2 sales days, 8 zero days => avg should include all 10 days.
        line = LineMathInput(
            sku='SKU-5',
            current_on_hand=Decimal('0'),
            in_transit_qty=0,
            history_daily_units=[
                Decimal('0'),
                Decimal('0'),
                Decimal('3'),
                Decimal('0'),
                Decimal('0'),
                Decimal('0'),
                Decimal('0'),
                Decimal('4'),
                Decimal('0'),
                Decimal('0'),
            ],
            unit_pack_size=1,
            min_order_qty=0,
        )
        result = compute_line_recommendation(line, params)
        # avg_daily=(3+4)/10=0.7 => avg_weekly=4.9
        self.assertEqual(result.avg_weekly_units, Decimal('4.9000'))


if __name__ == '__main__':
    unittest.main()
