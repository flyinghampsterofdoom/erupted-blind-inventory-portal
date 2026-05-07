from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from app.services.sales_transactions_report_service import (
    SalesReportLocation,
    build_employee_sales_report,
)


class _FakeSquareClient:
    def __init__(self, *, orders: list[dict], payments: list[dict], team_members: list[dict]):
        self.orders = orders
        self.payments = payments
        self.team_members = team_members
        self.order_payloads: list[dict] = []
        self.payment_queries: list[dict] = []

    def post(self, path: str, payload: dict) -> dict:
        if path == '/v2/orders/search':
            self.order_payloads.append(payload)
            return {'orders': self.orders}
        if path == '/v2/team-members/search':
            return {'team_members': self.team_members}
        raise AssertionError(f'unexpected Square POST path: {path}')

    def get(self, path: str, *, cursor: str | None = None, query: dict | None = None) -> dict:
        if path == '/v2/payments':
            self.payment_queries.append(query or {})
            return {'payments': self.payments}
        raise AssertionError(f'unexpected Square GET path: {path}')


class EmployeeSalesReportTests(unittest.TestCase):
    @patch('app.services.sales_transactions_report_service.list_square_locations_for_reports')
    @patch('app.services.sales_transactions_report_service._SquareClient')
    def test_employee_sales_uses_store_local_business_day(
        self,
        square_client_cls_mock,
        list_locations_mock,
    ) -> None:
        list_locations_mock.return_value = [
            SalesReportLocation(
                id='LOC-1',
                name='Sunset',
                timezone_name='America/Los_Angeles',
            )
        ]
        fake_client = _FakeSquareClient(
            orders=[
                {
                    'id': 'ORDER-1',
                    'location_id': 'LOC-1',
                    'closed_at': '2026-05-02T06:30:00Z',
                    'total_money': {'amount': 11000},
                    'total_tax_money': {'amount': 800},
                    'total_tip_money': {'amount': 200},
                    'total_discount_money': {'amount': 1000},
                    'net_amounts': {
                        'total_money': {'amount': 11000},
                        'tax_money': {'amount': 800},
                        'tip_money': {'amount': 200},
                        'discount_money': {'amount': 1000},
                    },
                }
            ],
            payments=[
                {
                    'id': 'PAYMENT-1',
                    'status': 'COMPLETED',
                    'order_id': 'ORDER-1',
                    'team_member_id': 'TEAM-1',
                    'total_money': {'amount': 11000},
                    'created_at': '2026-05-02T06:31:00Z',
                }
            ],
            team_members=[
                {
                    'id': 'TEAM-1',
                    'given_name': 'Ada',
                    'family_name': 'Lovelace',
                }
            ],
        )
        square_client_cls_mock.return_value = fake_client

        report = build_employee_sales_report(
            start_date=date(2026, 5, 1),
            end_date=date(2026, 5, 1),
        )

        closed_at_filter = fake_client.order_payloads[0]['query']['filter']['date_time_filter']['closed_at']
        self.assertEqual(closed_at_filter['start_at'], '2026-05-01T07:00:00Z')
        self.assertEqual(closed_at_filter['end_at'], '2026-05-02T07:00:00Z')
        self.assertEqual(report.total_transaction_count, 1)
        self.assertEqual(report.total_gross_sales, Decimal('110.00'))
        self.assertEqual(report.total_net_sales, Decimal('100.00'))
        self.assertEqual(report.average_gross_per_transaction, Decimal('110.00'))
        self.assertEqual(report.average_net_per_transaction, Decimal('100.00'))
        self.assertEqual(report.rows[0].employee_name, 'Ada Lovelace')
        self.assertEqual(report.rows[0].transaction_count, 1)

    @patch('app.services.sales_transactions_report_service.list_square_locations_for_reports')
    @patch('app.services.sales_transactions_report_service._SquareClient')
    def test_employee_sales_uses_largest_completed_payment_employee(
        self,
        square_client_cls_mock,
        list_locations_mock,
    ) -> None:
        list_locations_mock.return_value = [
            SalesReportLocation(id='LOC-1', name='Main', timezone_name='UTC')
        ]
        fake_client = _FakeSquareClient(
            orders=[
                {
                    'id': 'ORDER-1',
                    'location_id': 'LOC-1',
                    'closed_at': '2026-05-01T12:00:00Z',
                    'total_money': {'amount': 10000},
                    'total_tax_money': {'amount': 0},
                    'total_tip_money': {'amount': 0},
                    'total_discount_money': {'amount': 0},
                }
            ],
            payments=[
                {
                    'id': 'PAYMENT-1',
                    'status': 'COMPLETED',
                    'order_id': 'ORDER-1',
                    'team_member_id': 'TEAM-SMALL',
                    'total_money': {'amount': 1000},
                    'created_at': '2026-05-01T12:01:00Z',
                },
                {
                    'id': 'PAYMENT-2',
                    'status': 'COMPLETED',
                    'order_id': 'ORDER-1',
                    'team_member_id': 'TEAM-LARGE',
                    'total_money': {'amount': 9000},
                    'created_at': '2026-05-01T12:02:00Z',
                },
            ],
            team_members=[
                {'id': 'TEAM-SMALL', 'given_name': 'Small', 'family_name': 'Tender'},
                {'id': 'TEAM-LARGE', 'given_name': 'Large', 'family_name': 'Tender'},
            ],
        )
        square_client_cls_mock.return_value = fake_client

        report = build_employee_sales_report(
            start_date=date(2026, 5, 1),
            end_date=date(2026, 5, 1),
        )

        self.assertEqual(len(report.rows), 1)
        self.assertEqual(report.rows[0].team_member_id, 'TEAM-LARGE')
        self.assertEqual(report.rows[0].employee_name, 'Large Tender')
        self.assertEqual(report.total_transaction_count, 1)


if __name__ == '__main__':
    unittest.main()
