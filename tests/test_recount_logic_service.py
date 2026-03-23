from __future__ import annotations

import unittest
from decimal import Decimal
from types import SimpleNamespace

from app.services.session_service import _evaluate_recount_rows


class RecountLogicServiceTests(unittest.TestCase):
    def test_new_non_zero_item_starts_recount_tracking(self) -> None:
        retained, closeout, removed, max_consecutive = _evaluate_recount_rows(
            existing_items={},
            non_zero_rows=[
                {
                    'variation_id': 'var-1',
                    'sku': 'SKU-1',
                    'item_name': 'Item',
                    'variation_name': 'Var',
                    'counted_qty': Decimal('8'),
                    'variance': Decimal('-2'),
                }
            ],
        )

        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0]['total_count_attempts'], 1)
        self.assertEqual(retained[0]['consecutive_match_count'], 1)
        self.assertEqual(closeout, [])
        self.assertEqual(removed, [])
        self.assertEqual(max_consecutive, 1)

    def test_zero_variance_items_are_removed_from_recount_queue(self) -> None:
        existing = {
            'var-1': SimpleNamespace(last_variance=Decimal('2'), consecutive_match_count=2, total_count_attempts=2),
            'var-2': SimpleNamespace(last_variance=Decimal('-1'), consecutive_match_count=1, total_count_attempts=1),
        }
        retained, closeout, removed, _ = _evaluate_recount_rows(
            existing_items=existing,
            non_zero_rows=[
                {
                    'variation_id': 'var-1',
                    'sku': 'SKU-1',
                    'item_name': 'Item 1',
                    'variation_name': 'Var 1',
                    'counted_qty': Decimal('10'),
                    'variance': Decimal('2'),
                }
            ],
        )

        self.assertEqual(len(retained), 0)
        self.assertEqual(len(closeout), 1)
        self.assertEqual(sorted(removed), ['var-2'])

    def test_three_matching_variances_become_closeout_candidates(self) -> None:
        existing = {
            'var-1': SimpleNamespace(last_variance=Decimal('3'), consecutive_match_count=2, total_count_attempts=2),
        }
        retained, closeout, removed, max_consecutive = _evaluate_recount_rows(
            existing_items=existing,
            non_zero_rows=[
                {
                    'variation_id': 'var-1',
                    'sku': 'SKU-1',
                    'item_name': 'Item',
                    'variation_name': 'Var',
                    'counted_qty': Decimal('12'),
                    'variance': Decimal('3'),
                }
            ],
        )

        self.assertEqual(retained, [])
        self.assertEqual(len(closeout), 1)
        self.assertEqual(closeout[0]['total_count_attempts'], 3)
        self.assertEqual(closeout[0]['consecutive_match_count'], 3)
        self.assertEqual(removed, [])
        self.assertEqual(max_consecutive, 3)

    def test_variance_change_resets_consecutive_but_keeps_attempt_count(self) -> None:
        existing = {
            'var-1': SimpleNamespace(last_variance=Decimal('3'), consecutive_match_count=2, total_count_attempts=2),
        }
        retained, closeout, removed, max_consecutive = _evaluate_recount_rows(
            existing_items=existing,
            non_zero_rows=[
                {
                    'variation_id': 'var-1',
                    'sku': 'SKU-1',
                    'item_name': 'Item',
                    'variation_name': 'Var',
                    'counted_qty': Decimal('11'),
                    'variance': Decimal('1'),
                }
            ],
        )

        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0]['total_count_attempts'], 3)
        self.assertEqual(retained[0]['consecutive_match_count'], 1)
        self.assertEqual(closeout, [])
        self.assertEqual(removed, [])
        self.assertEqual(max_consecutive, 1)


if __name__ == '__main__':
    unittest.main()
