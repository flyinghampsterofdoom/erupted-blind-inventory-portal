from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from app.services.inventory_velocity_report_service import (
    InventoryVelocityReport,
    InventoryStockEvent,
    VelocityInventory,
    VelocityRow,
    VelocitySale,
    calculate_stockout_adjustments,
    calculate_inventory_health,
    calculate_transfer_opportunities,
    calculate_velocity_metrics,
    build_stock_coverage_purchase_report,
    fetch_inventory_stock_events,
    render_export_report,
    render_stock_coverage_purchase_export,
    summarize_stock_coverage_purchase_rows,
)


class InventoryVelocityReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.end_date = date(2026, 6, 30)
        self.inventory = {
            'VAR-1': VelocityInventory('VAR-1', 'SKU-1', 'Alpha', 'Category', 'Vendor', Decimal('4'), False, {1: Decimal('10'), 2: Decimal('5')})
        }
        self.store_names = {1: 'Low', 2: 'High'}
        self.store_by_location = {'LOC-1': 1, 'LOC-2': 2}

    @patch('app.services.inventory_velocity_report_service._SquareClient')
    def test_fetch_inventory_stock_events_requests_only_square_supported_change_types(self, square_client_mock) -> None:
        client = square_client_mock.return_value
        client.post.return_value = {'changes': []}
        db = SimpleNamespace(
            execute=lambda _query: SimpleNamespace(
                all=lambda: [SimpleNamespace(square_location_id='LOC-1')]
            )
        )

        fetch_inventory_stock_events(
            db,
            variation_ids=['VAR-1'],
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 30),
        )

        payload = client.post.call_args.args[1]
        self.assertEqual(payload['types'], ['ADJUSTMENT'])
        self.assertNotIn('TRANSFER', payload['types'])

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
        self.assertEqual(len(output[0]), 21)
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
            vendor_id=10,
        )
        velocity_report = InventoryVelocityReport(30, self.end_date, [velocity_row], [], {'top': [velocity_row]}, [(1, 'Low')], ['Category'], ['Vendor'])
        with patch('app.services.inventory_velocity_report_service.build_inventory_velocity_report', return_value=velocity_report):
            report = build_stock_coverage_purchase_report(None, days=30, target_months=Decimal('3'), top_n=10)
        self.assertEqual(report.rows[0].rank, 1)
        self.assertEqual(report.rows[0].target_inventory_quantity, Decimal('90'))
        self.assertEqual(report.rows[0].recommended_purchase_quantity, Decimal('75'))
        self.assertEqual(report.rows[0].estimated_purchase_cost, Decimal('300'))
        self.assertEqual(report.vendor_summaries[0].vendor, 'Vendor')
        self.assertEqual(report.vendor_summaries[0].vendor_id, 10)
        self.assertEqual(report.vendor_summaries[0].estimated_purchase_cost, Decimal('300'))
        self.assertEqual(report.total_estimated_purchase_cost, Decimal('300'))
        self.assertEqual(report.total_purchase_quantity, Decimal('75'))

    def test_stock_coverage_purchase_uses_store_specific_need(self) -> None:
        inventory = {
            'VAR-1': VelocityInventory(
                'VAR-1',
                'SKU-1',
                'Alpha',
                'Category',
                'Vendor',
                Decimal('4'),
                False,
                {1: Decimal('0'), 2: Decimal('40')},
                10,
            )
        }
        velocity_row = calculate_velocity_metrics(
            [VelocitySale(date(2026, 6, 29), 'VAR-1', 'LOC-1', Decimal('30'), Decimal('300'))],
            inventory,
            days=30,
            end_date=self.end_date,
            store_names={1: 'HWY99', 2: 'Longview'},
            store_by_location=self.store_by_location,
        )[0]
        velocity_report = InventoryVelocityReport(30, self.end_date, [velocity_row], [], {'top': [velocity_row]}, [(1, 'HWY99'), (2, 'Longview')], ['Category'], ['Vendor'])
        with patch('app.services.inventory_velocity_report_service.build_inventory_velocity_report', return_value=velocity_report):
            report = build_stock_coverage_purchase_report(None, days=30, target_months=Decimal('1'), top_n=1)

        row = report.rows[0]
        self.assertEqual(row.target_inventory_quantity, Decimal('30'))
        self.assertEqual(row.current_inventory_quantity, Decimal('40'))
        self.assertEqual(row.recommended_purchase_quantity, Decimal('30'))
        self.assertTrue(row.store_specific_need_masked)
        self.assertIn('HWY99: 30 sold / 0 on hand / 30 need', row.store_location_breakdown)

    def test_stock_coverage_purchase_applies_target_months_to_store_specific_need(self) -> None:
        inventory = {
            'VAR-1': VelocityInventory(
                'VAR-1',
                'SKU-1',
                'Alpha',
                'Category',
                'Vendor',
                Decimal('4'),
                False,
                {1: Decimal('0')},
                10,
            )
        }
        sales = [VelocitySale(date(2026, 6, 29), 'VAR-1', 'LOC-1', Decimal('30'), Decimal('300'))]
        with (
            patch('app.services.inventory_velocity_report_service.fetch_current_inventory', return_value=(inventory, [(1, 'HWY99')], {'LOC-1': 1})),
            patch('app.services.inventory_velocity_report_service.fetch_sales_data', return_value=sales),
            patch('app.services.inventory_velocity_report_service.fetch_inventory_stock_events', return_value=[]),
        ):
            report = build_stock_coverage_purchase_report(None, days=30, target_months=Decimal('3'), top_n=1, end_date=self.end_date)

        row = report.rows[0]
        self.assertEqual(row.target_days, Decimal('90'))
        self.assertEqual(row.target_inventory_quantity, Decimal('90'))
        self.assertEqual(row.recommended_purchase_quantity, Decimal('90'))
        self.assertIn('HWY99: 30 sold / 0 on hand / 90 need', row.store_location_breakdown)

    def test_stock_coverage_purchase_adjusts_demand_for_zero_stock_days(self) -> None:
        inventory = {
            'VAR-1': VelocityInventory(
                'VAR-1',
                'SKU-1',
                'Alpha',
                'Category',
                'Vendor',
                Decimal('4'),
                False,
                {1: Decimal('0')},
                10,
            )
        }
        sales = [VelocitySale(date(2026, 6, 15), 'VAR-1', 'LOC-1', Decimal('15'), Decimal('150'))]
        events = [InventoryStockEvent(date(2026, 6, 16), 'VAR-1', 'LOC-1', Decimal('-15'))]
        adjustments = calculate_stockout_adjustments(
            sales,
            inventory,
            events,
            days=30,
            end_date=self.end_date,
            store_by_location={'LOC-1': 1},
        )
        velocity_row = calculate_velocity_metrics(
            sales,
            inventory,
            days=30,
            end_date=self.end_date,
            store_names={1: 'HWY99'},
            store_by_location={'LOC-1': 1},
            target_days=Decimal('30'),
            stockout_adjustments=adjustments,
        )[0]
        velocity_report = InventoryVelocityReport(30, self.end_date, [velocity_row], [], {'top': [velocity_row]}, [(1, 'HWY99')], ['Category'], ['Vendor'])
        with patch('app.services.inventory_velocity_report_service.build_inventory_velocity_report', return_value=velocity_report):
            report = build_stock_coverage_purchase_report(None, days=30, target_months=Decimal('1'), top_n=1)

        row = report.rows[0]
        self.assertEqual(row.units_sold, Decimal('15'))
        self.assertEqual(row.adjusted_units_sold, Decimal('30.000'))
        self.assertEqual(row.estimated_lost_units, Decimal('15.000'))
        self.assertEqual(row.zero_stock_days, 15)
        self.assertEqual(row.recommended_purchase_quantity, Decimal('30'))
        self.assertIn('15 zero days / 15 est. lost', row.store_location_breakdown)

    def test_stock_coverage_summary_can_filter_by_vendor_id(self) -> None:
        rows = [
            calculate_velocity_metrics(
                [VelocitySale(date(2026, 6, 29), 'VAR-1', 'LOC-1', Decimal('30'), Decimal('300'))],
                {'VAR-1': VelocityInventory('VAR-1', 'SKU-1', 'Alpha', 'Category', 'Vendor A', Decimal('4'), False, {1: Decimal('0')}, 10)},
                days=30,
                end_date=self.end_date,
                store_names=self.store_names,
                store_by_location=self.store_by_location,
            )[0],
            calculate_velocity_metrics(
                [VelocitySale(date(2026, 6, 29), 'VAR-2', 'LOC-1', Decimal('30'), Decimal('300'))],
                {'VAR-2': VelocityInventory('VAR-2', 'SKU-2', 'Beta', 'Category', 'Vendor B', Decimal('2'), False, {1: Decimal('10')}, 20)},
                days=30,
                end_date=self.end_date,
                store_names=self.store_names,
                store_by_location=self.store_by_location,
            )[0],
        ]
        stock_rows = []
        for row in rows:
            velocity_report = InventoryVelocityReport(30, self.end_date, [row], [], {'top': [row]}, [(1, 'Low')], ['Category'], [row.vendor])
            with patch('app.services.inventory_velocity_report_service.build_inventory_velocity_report', return_value=velocity_report):
                stock_rows.extend(build_stock_coverage_purchase_report(None, days=30, target_months=Decimal('1'), top_n=1).rows)

        filtered_rows = [row for row in stock_rows if row.vendor_id == 20]
        summaries, total_quantity, total_cost, missing_cost_count = summarize_stock_coverage_purchase_rows(filtered_rows)

        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0].vendor, 'Vendor B')
        self.assertEqual(summaries[0].vendor_id, 20)
        self.assertEqual(total_quantity, Decimal('20'))
        self.assertEqual(total_cost, Decimal('40'))
        self.assertEqual(missing_cost_count, 0)

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
        self.assertEqual(output[6][10], '2')


if __name__ == '__main__':
    unittest.main()
