from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from app.services.inventory_velocity_report_service import (
    VelocityInventory,
    VelocitySale,
    calculate_inventory_health,
    calculate_transfer_opportunities,
    calculate_velocity_metrics,
    render_export_report,
)


class InventoryVelocityReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.end_date = date(2026, 6, 30)
        self.inventory = {
            'VAR-1': VelocityInventory('VAR-1', 'SKU-1', 'Alpha', 'Category', 'Vendor', Decimal('4'), False, {1: Decimal('10'), 2: Decimal('5')})
        }
        self.store_names = {1: 'Low', 2: 'High'}
        self.store_by_location = {'LOC-1': 1, 'LOC-2': 2}

    def test_calculates_margin_supply_reorder_and_previous_period_trend(self) -> None:
        sales = [
            VelocitySale(date(2026, 6, 29), 'VAR-1', 'LOC-1', Decimal('30'), Decimal('300')),
            VelocitySale(date(2026, 5, 30), 'VAR-1', 'LOC-1', Decimal('10'), Decimal('100')),
        ]
        row = calculate_velocity_metrics(sales, self.inventory, days=30, end_date=self.end_date, store_names=self.store_names, store_by_location=self.store_by_location)[0]
        self.assertEqual(row.units_sold, Decimal('30'))
        self.assertEqual(row.average_units_sold_per_day, Decimal('1'))
        self.assertEqual(row.days_of_supply_remaining, Decimal('15'))
        self.assertEqual(row.inventory_value_at_cost, Decimal('60'))
        self.assertEqual(row.gross_profit_dollars, Decimal('180'))
        self.assertEqual(row.gross_margin_percent, Decimal('0.6'))
        self.assertEqual(row.recommended_reorder_quantity, Decimal('15'))
        self.assertEqual(row.trend_label, '+200.0%')

    def test_handles_no_sales_missing_cost_and_negative_inventory(self) -> None:
        inventory = {
            'DEAD': VelocityInventory('DEAD', 'DEAD', 'Dead', 'Uncategorized', 'Unassigned', None, False, {1: Decimal('9')}),
            'NEG': VelocityInventory('NEG', 'NEG', 'Negative', 'Uncategorized', 'Vendor', Decimal('2'), False, {1: Decimal('-2')}),
        }
        rows = calculate_velocity_metrics([], inventory, days=15, end_date=self.end_date, store_names=self.store_names, store_by_location=self.store_by_location)
        by_sku = {row.sku: row for row in rows}
        self.assertEqual(by_sku['DEAD'].inventory_health_flag, 'Dead stock')
        self.assertIsNone(by_sku['DEAD'].inventory_value_at_cost)
        self.assertEqual(by_sku['NEG'].inventory_health_flag, 'Out of stock')
        self.assertEqual(calculate_inventory_health(Decimal('1'), Decimal('3'), Decimal('1')), 'Critical')

    def test_transfer_replenishes_14_days_without_dropping_source_below_21(self) -> None:
        inventory = {'VAR-1': VelocityInventory('VAR-1', 'SKU-1', 'Alpha', 'Category', 'Vendor', Decimal('4'), False, {1: Decimal('0'), 2: Decimal('40')})}
        sales = [
            VelocitySale(date(2026, 6, 29), 'VAR-1', 'LOC-1', Decimal('30'), Decimal('300')),
            VelocitySale(date(2026, 6, 29), 'VAR-1', 'LOC-2', Decimal('15'), Decimal('150')),
        ]
        transfers = calculate_transfer_opportunities([], sales, inventory, days=30, end_date=self.end_date, store_names=self.store_names, store_by_location=self.store_by_location)
        self.assertEqual(len(transfers), 1)
        self.assertEqual(transfers[0].suggested_transfer_quantity, Decimal('14'))

    def test_csv_contains_all_required_columns(self) -> None:
        row = calculate_velocity_metrics([], self.inventory, days=30, end_date=self.end_date, store_names=self.store_names, store_by_location=self.store_by_location)[0]
        output = render_export_report([row])
        self.assertEqual(len(output[0]), 18)
        self.assertEqual(output[1][1], 'SKU-1')


if __name__ == '__main__':
    unittest.main()
