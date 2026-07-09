from __future__ import annotations

import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from app.models import PurchaseOrder, PurchaseOrderLine, PurchaseOrderStoreAllocation, Store, Vendor, VendorSkuConfig
from app.services.inventory_velocity_report_service import StockCoveragePurchaseRow, StoreDemandSplit
from app.services.purchase_order_admin_service import (
    create_purchase_order_from_stock_coverage_rows,
    _line_matches_barcode,
    _normalize_scan_key,
    _select_next_receiving_store,
    _should_remove_saved_order_line,
    _square_receive_quantity_from_singles,
    _line_receive_scan_increment,
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


class _ScalarAllResult:
    def __init__(self, rows: list):
        self._rows = rows

    def all(self) -> list:
        return self._rows


class _ExecuteAllResult:
    def __init__(self, rows: list):
        self._rows = rows

    def scalars(self) -> _ScalarAllResult:
        return _ScalarAllResult(self._rows)


class _PackConfigDb:
    def __init__(self, rows: list):
        self._rows = rows

    def execute(self, _query) -> _ExecuteAllResult:
        return _ExecuteAllResult(self._rows)


class _ScalarOneResult:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _RowsResult:
    def __init__(self, rows: list):
        self._rows = rows

    def all(self) -> list:
        return self._rows

    def scalars(self) -> _ScalarAllResult:
        return _ScalarAllResult(self._rows)


class _PurchaseOrderCreateDb:
    def __init__(self):
        self.added: list = []
        self._results = [
            _ScalarOneResult(Vendor(id=20, square_vendor_id='VENDOR-20', name='Vendor A', active=True)),
            _RowsResult([
                VendorSkuConfig(
                    id=30,
                    vendor_id=20,
                    sku='SKU-1',
                    square_variation_id='VAR-1',
                    unit_cost=Decimal('4.00'),
                    active=True,
                    is_default_vendor=True,
                )
            ]),
            _RowsResult([Store(id=1, name='Highway 99', active=True), Store(id=2, name='Longview', active=True)]),
        ]

    def execute(self, _query):
        return self._results.pop(0)

    def add(self, row) -> None:
        self.added.append(row)

    def flush(self) -> None:
        next_po_id = 100
        next_line_id = 200
        for row in self.added:
            if isinstance(row, PurchaseOrder) and row.id is None:
                row.id = next_po_id
                next_po_id += 1
            if isinstance(row, PurchaseOrderLine) and row.id is None:
                row.id = next_line_id
                next_line_id += 1


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

    def test_scan_increment_uses_vendor_mapping_gtin_pack_size(self) -> None:
        db = _PackConfigDb(
            [
                SimpleNamespace(
                    sku='COIL-SKU',
                    square_variation_id='SQUARE-VAR-1',
                    gtin='00123456789012',
                    pack_size=5,
                )
            ]
        )
        po = SimpleNamespace(vendor_id=20)
        line = SimpleNamespace(sku='COIL-SKU', variation_id='SQUARE-VAR-1')

        self.assertEqual(
            _line_receive_scan_increment(db, po=po, line=line, barcode_key=_normalize_scan_key('123456789012')),
            5,
        )

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

    def test_save_changes_keeps_previously_removed_line_removed(self) -> None:
        self.assertTrue(
            _should_remove_saved_order_line(
                line_id=10,
                ordered_qty=0,
                received_qty_total=5,
                removed_line_ids=set(),
                currently_removed=True,
            )
        )

    def test_save_changes_removes_line_when_explicitly_checked(self) -> None:
        self.assertTrue(
            _should_remove_saved_order_line(
                line_id=10,
                ordered_qty=0,
                received_qty_total=5,
                removed_line_ids={10},
            )
        )

    @patch('app.services.purchase_order_admin_service.fetch_catalog_by_sku')
    def test_create_purchase_order_from_stock_coverage_rows_creates_editable_draft(self, catalog_mock) -> None:
        catalog_mock.return_value = {}
        db = _PurchaseOrderCreateDb()
        row = StockCoveragePurchaseRow(
            rank=1,
            sku='SKU-1',
            product_name='Alpha',
            category='Category',
            vendor='Vendor A',
            units_sold=Decimal('30'),
            average_units_sold_per_day=Decimal('1'),
            target_months=Decimal('2'),
            target_days=Decimal('60'),
            target_inventory_quantity=Decimal('60'),
            current_inventory_quantity=Decimal('10'),
            recommended_purchase_quantity=Decimal('50'),
            estimated_purchase_cost=Decimal('200'),
            days_of_supply_remaining=Decimal('10'),
            store_location_breakdown='Highway 99: 30 sold / 10 on hand',
            vendor_id=20,
            store_splits=[
                StoreDemandSplit(1, 'Highway 99', Decimal('20'), Decimal('0.667'), Decimal('40'), Decimal('10'), Decimal('30'), Decimal('15')),
                StoreDemandSplit(2, 'Longview', Decimal('10'), Decimal('0.333'), Decimal('20'), Decimal('0'), Decimal('20'), Decimal('0')),
            ],
        )

        po = create_purchase_order_from_stock_coverage_rows(
            db,
            vendor_id=20,
            rows=[row],
            created_by_principal_id=5,
            history_lookback_days=30,
            target_months=Decimal('2'),
        )

        lines = [item for item in db.added if isinstance(item, PurchaseOrderLine)]
        allocations = [item for item in db.added if isinstance(item, PurchaseOrderStoreAllocation)]
        self.assertEqual(po.id, 100)
        self.assertEqual(po.vendor_id, 20)
        self.assertEqual(lines[0].ordered_qty, 50)
        self.assertEqual(lines[0].unit_cost, Decimal('4.00'))
        self.assertEqual(lines[0].suggested_par_level, 60)
        self.assertEqual([allocation.allocated_qty for allocation in allocations], [30, 20])


if __name__ == '__main__':
    unittest.main()
