from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from app.services.inventory_velocity_report_service import (
    InventoryVelocityReport,
    VelocityInventory,
    VelocityRow,
    VelocitySale,
    calculate_inventory_health,
    calculate_transfer_opportunities,
    calculate_velocity_metrics,
    build_stock_coverage_purchase_report,
    render_export_report,
    render_stock_coverage_purchase_export,
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

    def test_stock_coverage_purchase_uses_velocity_rank_and_target_months(self) -> None:
        velocity_row = VelocityRow(
            rank=1,
            variation_id='VAR-1',
            sku='SKU-1',
            product_name='Alpha',
            category='Category',
            vendor='Vendor',
            units_sold=Decimal('30'),
            sales_revenue=Decimal('300'),
            gross_profit_dollars=Decimal('180'),
            gross_margin_percent=Decimal('0.6'),
            average_units_sold_per_day=Decimal('1'),
            current_inventory_quantity=Decimal('15'),
            inventory_value_at_cost=Decimal('60'),
            days_of_supply_remaining=Decimal('15'),
            last_sold_date=self.end_date,
            store_location_breakdown='Low: 30 sold / 15 on hand',
            inventory_health_flag='Watch',
            recommended_reorder_quantity=Decimal('15'),
            trend_percent=None,
            trend_label='New',
            previous_units_sold=Decimal('0'),
            discontinued=False,
        )
        velocity_report = InventoryVelocityReport(30, self.end_date, [velocity_row], [], {'top': [velocity_row]}, [(1, 'Low')], ['Category'], ['Vendor'])
        with patch('app.services.inventory_velocity_report_service.build_inventory_velocity_report', return_value=velocity_report):
            report = build_stock_coverage_purchase_report(None, days=30, target_months=Decimal('3'), top_n=10)
        self.assertEqual(report.rows[0].rank, 1)
        self.assertEqual(report.rows[0].target_inventory_quantity, Decimal('90'))
        self.assertEqual(report.rows[0].recommended_purchase_quantity, Decimal('75'))
        self.assertEqual(report.rows[0].estimated_purchase_cost, Decimal('300'))
        self.assertEqual(report.vendor_summaries[0].vendor, 'Vendor')
        self.assertEqual(report.vendor_summaries[0].estimated_purchase_cost, Decimal('300'))
        self.assertEqual(report.total_estimated_purchase_cost, Decimal('300'))
        self.assertEqual(report.total_purchase_quantity, Decimal('75'))

    def test_stock_coverage_purchase_csv_contains_purchase_columns(self) -> None:
        velocity_row = calculate_velocity_metrics(
            [VelocitySale(date(2026, 6, 29), 'VAR-1', 'LOC-1', Decimal('30'), Decimal('300'))],
            self.inventory,
            days=30,
            end_date=self.end_date,
            store_names=self.store_names,
            store_by_location=self.store_by_location,
        )[0]
        velocity_report = InventoryVelocityReport(30, self.end_date, [velocity_row], [], {'top': [velocity_row]}, [(1, 'Low')], ['Category'], ['Vendor'])
        with patch('app.services.inventory_velocity_report_service.build_inventory_velocity_report', return_value=velocity_report):
            report = build_stock_coverage_purchase_report(None, days=30, target_months=Decimal('2'), top_n=1)
        output = render_stock_coverage_purchase_export(report)
        self.assertEqual(output[0], ['Vendor Purchase Summary'])
        self.assertEqual(output[2][0], 'Vendor')
        self.assertEqual(output[3][0], 'Total')
        self.assertIn('Recommended purchase quantity', output[5])
        self.assertEqual(output[6][1], 'SKU-1')
        self.assertEqual(output[6][7], '2')


if __name__ == '__main__':
    unittest.main()
