from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.services.purchase_order_admin_service import _select_next_receiving_store, _store_receive_priority_key


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


if __name__ == '__main__':
    unittest.main()
