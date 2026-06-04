from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.services.purchase_order_admin_service import (
    _line_matches_barcode,
    _normalize_scan_key,
    _select_next_receiving_store,
    _should_remove_saved_order_line,
    _square_receive_quantity_from_singles,
    _store_receive_priority_key,
)


def store(store_id: int, name: str) -> SimpleNamespace:
    return SimpleNamespace(id=store_id, name=name)


def allocation(store_id: int, allocated_qty: int, received_qty: int) -> SimpleNamespace:
    return SimpleNamespace(
        store_id=store_id,
        allocated_qty=allocated_qty,
        store_received_qty=received_qty,
    )


class PurchaseOrderAdminReceivingServiceTests(unittest.TestCase):
    def test_store_receive_priority_orders_requested_stores(self) -> None:
        stores = [
            store(1, 'Andresen'),
            store(2, 'Highway 99'),
            store(3, 'SR503'),
            store(4, 'Longview'),
        ]

        sorted_stores = sorted(stores, key=lambda row: _store_receive_priority_key(row.name))

        self.assertEqual([row.name for row in sorted_stores], ['Highway 99', 'Longview', 'Andresen', 'SR503'])

    def test_select_next_receiving_store_uses_first_store_with_open_need(self) -> None:
        stores = [
            store(1, 'HWY 99'),
            store(2, 'Longview'),
            store(3, 'Andresen'),
            store(4, 'SR 503'),
        ]
        allocation_by_store_id = {
            1: allocation(1, allocated_qty=2, received_qty=2),
            2: allocation(2, allocated_qty=3, received_qty=1),
            3: allocation(3, allocated_qty=2, received_qty=0),
            4: allocation(4, allocated_qty=2, received_qty=0),
        }

        selected_store, overage = _select_next_receiving_store(stores, allocation_by_store_id)

        self.assertEqual(selected_store.name, 'Longview')
        self.assertFalse(overage)

    def test_select_next_receiving_store_favors_hwy99_for_overage(self) -> None:
        stores = [
            store(1, 'HWY 99'),
            store(2, 'Longview'),
            store(3, 'Andresen'),
            store(4, 'SR 503'),
        ]
        allocation_by_store_id = {
            1: allocation(1, allocated_qty=2, received_qty=2),
            2: allocation(2, allocated_qty=3, received_qty=3),
            3: allocation(3, allocated_qty=2, received_qty=2),
            4: allocation(4, allocated_qty=2, received_qty=2),
        }

        selected_store, overage = _select_next_receiving_store(stores, allocation_by_store_id)

        self.assertEqual(selected_store.name, 'HWY 99')
        self.assertTrue(overage)

    def test_line_matches_gtin_barcode_with_zero_padding(self) -> None:
        line = SimpleNamespace(sku='ABC-123', variation_id='SQUARE-VAR-1', gtin='00123456789012')

        self.assertTrue(_line_matches_barcode(line, _normalize_scan_key('123456789012')))

    def test_square_receive_quantity_converts_singles_to_packs(self) -> None:
        self.assertEqual(_square_receive_quantity_from_singles(10, 5), 2)

    def test_square_receive_quantity_rejects_partial_pack(self) -> None:
        with self.assertRaisesRegex(ValueError, 'does not align to pack size 5'):
            _square_receive_quantity_from_singles(11, 5)

    def test_save_changes_keeps_zero_order_line_with_received_qty(self) -> None:
        self.assertFalse(
            _should_remove_saved_order_line(
                line_id=10,
                ordered_qty=0,
                received_qty_total=1,
                removed_line_ids=set(),
            )
        )

    def test_save_changes_removes_unreceived_zero_order_line(self) -> None:
        self.assertTrue(
            _should_remove_saved_order_line(
                line_id=10,
                ordered_qty=0,
                received_qty_total=0,
                removed_line_ids=set(),
            )
        )


if __name__ == '__main__':
    unittest.main()
