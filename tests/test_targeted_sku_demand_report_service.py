from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from app.services.square_ordering_data_service import CatalogVariationMeta
from app.services.targeted_sku_demand_report_service import (
    build_targeted_sku_demand_report,
    render_targeted_sku_demand_export,
    search_targeted_sku_options,
)


class _RowsResult:
    def __init__(self, rows: list):
        self._rows = rows

    def all(self) -> list:
        return self._rows


class _QueueDb:
    def __init__(self, results: list[_RowsResult]):
        self._results = results

    def execute(self, _query):
        return self._results.pop(0)


def catalog_meta(
    variation_id: str,
    sku: str,
    item_name: str,
    variation_name: str = 'Default',
    cost: Decimal | None = Decimal('2.00'),
) -> CatalogVariationMeta:
    return CatalogVariationMeta(
        variation_id=variation_id,
        sku=sku,
        gtin=None,
        item_name=item_name,
        variation_name=variation_name,
        unit_price=None,
        vendor_cost_by_square_vendor_id={},
        first_vendor_unit_cost=cost,
    )


class TargetedSkuDemandReportServiceTests(unittest.TestCase):
    @patch('app.services.targeted_sku_demand_report_service.fetch_catalog_variation_maps')
    def test_search_matches_all_query_terms_across_name_and_variation(self, catalog_mock) -> None:
        catalog = {
            'VAR-1': catalog_meta('VAR-1', 'SKU-1', 'GTI Screens', 'Mesh 20pk'),
            'VAR-2': catalog_meta('VAR-2', 'SKU-2', 'Unrelated Item', 'Default'),
        }
        catalog_mock.return_value = (catalog, {})
        db = _QueueDb([
            _RowsResult([SimpleNamespace(square_variation_id='VAR-1', unit_cost=Decimal('2.00'), name='Vendor A', id=10)])
        ])

        options = search_targeted_sku_options(db, query='gti mesh')

        self.assertEqual(len(options), 1)
        self.assertEqual(options[0].variation_id, 'VAR-1')
        self.assertEqual(options[0].vendor, 'Vendor A')

    @patch('app.services.targeted_sku_demand_report_service.fetch_on_hand_by_store_variation')
    @patch('app.services.targeted_sku_demand_report_service.fetch_sales_volume_by_variation')
    @patch('app.services.targeted_sku_demand_report_service.fetch_catalog_variation_maps')
    def test_report_calculates_purchase_need_from_sales_and_on_hand(self, catalog_mock, sales_mock, on_hand_mock) -> None:
        catalog = {'VAR-1': catalog_meta('VAR-1', 'SKU-1', 'GTI Screens', 'Mesh 20pk', Decimal('2.50'))}
        catalog_mock.return_value = (catalog, {})
        sales_mock.return_value = {'VAR-1': Decimal('30')}
        on_hand_mock.return_value = {(1, 'VAR-1'): Decimal('8'), (2, 'VAR-1'): Decimal('2')}
        db = _QueueDb(
            [
                _RowsResult([SimpleNamespace(id=1, name='Highway 99'), SimpleNamespace(id=2, name='Longview')]),
                _RowsResult([SimpleNamespace(square_variation_id='VAR-1', unit_cost=Decimal('2.50'), name='Vendor A', id=10)]),
            ]
        )

        report = build_targeted_sku_demand_report(
            db,
            variation_ids=['VAR-1'],
            lookback_days=30,
            target_days=60,
        )

        row = report.rows[0]
        self.assertEqual(row.units_sold, Decimal('30'))
        self.assertEqual(row.average_units_sold_per_day, Decimal('1'))
        self.assertEqual(row.target_inventory_quantity, Decimal('60'))
        self.assertEqual(row.current_inventory_quantity, Decimal('10'))
        self.assertEqual(row.recommended_purchase_quantity, Decimal('50'))
        self.assertEqual(row.estimated_purchase_cost, Decimal('125.00'))
        self.assertEqual(report.total_purchase_quantity, Decimal('50'))

    def test_export_contains_summary_and_selected_variation(self) -> None:
        # Keep CSV shape covered without hitting Square-backed helpers.
        from app.services.targeted_sku_demand_report_service import TargetedSkuDemandReport, TargetedSkuDemandRow

        csv_rows = render_targeted_sku_demand_export(
            TargetedSkuDemandReport(
                lookback_days=30,
                target_days=30,
                end_date=date(2026, 7, 9),
                rows=[
                    TargetedSkuDemandRow(
                        variation_id='VAR-1',
                        sku='SKU-1',
                        product_name='GTI Screens',
                        variation_name='Mesh 20pk',
                        vendor='Vendor A',
                        vendor_id=10,
                        units_sold=Decimal('30'),
                        average_units_sold_per_day=Decimal('1'),
                        target_days=30,
                        target_inventory_quantity=Decimal('30'),
                        current_inventory_quantity=Decimal('10'),
                        recommended_purchase_quantity=Decimal('20'),
                        estimated_purchase_cost=Decimal('50'),
                        days_of_supply_remaining=Decimal('10'),
                        store_location_breakdown='Highway 99: 10 on hand',
                    )
                ],
                stores=[(1, 'Highway 99')],
                total_purchase_quantity=Decimal('20'),
                total_estimated_purchase_cost=Decimal('50'),
                missing_cost_sku_count=0,
            )
        )

        self.assertEqual(csv_rows[0], ['Targeted SKU Demand Report'])
        self.assertIn('Recommended purchase quantity', csv_rows[7])
        self.assertEqual(csv_rows[8][0], 'SKU-1')


if __name__ == '__main__':
    unittest.main()
